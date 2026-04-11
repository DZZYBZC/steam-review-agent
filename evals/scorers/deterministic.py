"""
evals/scorers/deterministic.py — Pure-function scorers (M5 Step 5).

Each per-case scorer takes (case, result) and returns a small dict.
Batch-level scorers take (all_records) and return aggregate dicts.
No LLM calls. No I/O except critic_health, which reads audit_log_iterations.

Expected shapes:
  case   = a golden.json case dict
  result = the inner dict written by run_evals._serialize_result()
           (i.e., records[i]["result"], not the outer record wrapper)
  records = list of run_evals output records:
            [{"case_id", "ok", "elapsed_seconds", "result", ...}, ...]
"""

from __future__ import annotations

import json
from typing import Any

from pipeline.storage import get_connection


# Stop reasons that mean the agent never produced a real action — the run
# died on infrastructure (Anthropic 529, JSON parse fail, etc.). These cases
# should NOT count toward action_correct_rate or wrong_action_severity, since
# they're not the agent making a wrong decision; they're a missing decision.
INFRASTRUCTURE_ERROR_STOP_REASONS = {"llm_error", "parse_error"}

# Stop reasons where the agent intentionally produced no action (skip path).
# Like infra errors, these cases have no predicted action and shouldn't be
# counted in action_correct_rate or wrong_action_severity. They also write
# zero rows to audit_log_iterations (the critic never runs), so they must
# be filtered out of critic_health's run_id pool too.
NO_RESPONSE_STOP_REASONS = {"no_response_needed"}


# ---------- Per-case scorers ----------------------------------------------

def _slot_label(slot: Any, idx: int) -> str:
    """
    Human-readable label for a requirement slot, used in diagnostic lists
    (missing_from_source, dropped_by_filter).
    """
    if isinstance(slot, str):
        return slot
    if isinstance(slot, dict) and "any_of" in slot:
        any_of = slot["any_of"]
        if isinstance(any_of, list) and any_of:
            return f"any_of[{'|'.join(str(x) for x in any_of)}]"
    return f"slot_{idx}"


def _slot_hit(slot: Any, pool: set[str], idx: int) -> bool:
    """
    A requirement slot is hit iff:
      - string slot "X": X ∈ pool
      - {"any_of": [...]}: at least one element ∈ pool
    Raises ValueError on any other shape (including a bare list, which is
    ambiguous — force callers to the explicit any_of object form).
    """
    if isinstance(slot, str):
        return slot in pool
    if isinstance(slot, dict):
        if "any_of" in slot:
            any_of = slot["any_of"]
            if not isinstance(any_of, list) or not all(isinstance(x, str) for x in any_of):
                raise ValueError(
                    f"Slot {idx}: 'any_of' must be a list of chunk_id strings, got {type(any_of).__name__}"
                )
            return any(x in pool for x in any_of)
        raise ValueError(
            f"Slot {idx}: dict slot has no recognized key (expected 'any_of'), got keys={list(slot.keys())}"
        )
    raise ValueError(
        f"Slot {idx}: must_include_chunk_ids entry must be a string or {{'any_of': [...]}} object, got {type(slot).__name__}"
    )


def retrieval_recall(case: dict, result: dict) -> dict:
    """
    Slot-based retrieval recall with both source-level and post-filter pools.

    Each entry in must_include_chunk_ids is one requirement SLOT:
      - flat string "chunkA" — one required chunk (legacy; scores identically
        to the previous recall_at_k for cases using only flat strings)
      - {"any_of": ["chunkA_v1", "chunkA_v2"]} — one required concept expressed
        across equivalent chunks; the slot is hit if any element is retrieved

    The recall denominator is the number of slots, not the total chunk count.
    Equivalence groups never inflate the denominator.

    Two pools are scored independently:
      - source_ids:   raw retriever top-5 (pre-investigator filter)
      - relevant_ids: post-Self-RAG filter (what the investigator kept)

    Concept hit-rate is a companion metric: "did at least one required slot
    land in the pool" — recall asks how much, concept_hit asks if we got into
    the right neighborhood at all.

    Empty must_include → not_applicable=True.
    gate_false_skip=True when must_include is non-empty but source_ids is empty
    (the runtime gate skipped retrieval — already counted as false_skip in
    gating_accuracy; excluded from the recall mean in the aggregate).
    """
    must = list(case.get("must_include_chunk_ids", []) or [])
    if not must:
        return {
            "recall_source": None,
            "recall_relevant": None,
            "hits_source": 0,
            "hits_relevant": 0,
            "concept_hit_source": None,
            "concept_hit_relevant": None,
            "n_required_slots": 0,
            "missing_from_source": [],
            "dropped_by_filter": [],
            "gate_false_skip": False,
            "not_applicable": True,
        }

    ep = result.get("evidence_package") or {}
    source_pool = set(ep.get("source_ids", []) or [])
    relevant_pool = set(ep.get("relevant_ids", []) or [])

    n_slots = len(must)
    hits_source = 0
    hits_relevant = 0
    missing_from_source: list[str] = []
    dropped_by_filter: list[str] = []

    for idx, slot in enumerate(must):
        in_source = _slot_hit(slot, source_pool, idx)
        in_relevant = _slot_hit(slot, relevant_pool, idx)
        label = _slot_label(slot, idx)
        if in_source:
            hits_source += 1
        else:
            missing_from_source.append(label)
        if in_relevant:
            hits_relevant += 1
        elif in_source:
            # Retriever surfaced it, investigator filter dropped it.
            dropped_by_filter.append(label)

    gate_false_skip = (ep.get("retrieval_decision") == "skipped") or not source_pool

    return {
        "recall_source": round(hits_source / n_slots, 3),
        "recall_relevant": round(hits_relevant / n_slots, 3),
        "hits_source": hits_source,
        "hits_relevant": hits_relevant,
        "concept_hit_source": hits_source >= 1,
        "concept_hit_relevant": hits_relevant >= 1,
        "n_required_slots": n_slots,
        "missing_from_source": missing_from_source,
        "dropped_by_filter": dropped_by_filter,
        "gate_false_skip": gate_false_skip,
        "not_applicable": False,
    }


def concept_recall(case: dict, result: dict) -> dict:
    """
    Concept-based retrieval recall (Phase A1 of the layered retrieval plan).

    Generalizes the slot/`any_of` shape of `retrieval_recall` into named
    concepts with stable concept_ids. A concept is hit iff any chunk_id in
    its `any_of` lands in the pool. Per-case score = hit_concepts / total
    (all concepts implicitly weight 1.0 in v1).

    Output shape mirrors `retrieval_recall` exactly so reporter/snapshot
    wiring is symmetric, with two intentional name changes:
      - `recall_postfilter` (clearer than legacy `recall_relevant`)
      - diagnostic lists carry `concept_id` (machine-stable across renames)

    Empty `required_concepts` → not_applicable=True.
    `gate_false_skip` mirrors `retrieval_recall`'s definition exactly so the
    A1 identity check can compare cases byte-for-byte after the mechanical
    1:1 migration.
    """
    concepts = case.get("required_concepts") or []
    if not concepts:
        return {
            "recall_source": None,
            "recall_postfilter": None,
            "hits_source": 0,
            "hits_postfilter": 0,
            "concept_hit_source": None,
            "concept_hit_postfilter": None,
            "n_concepts": 0,
            "concepts_missing_from_source": [],
            "concepts_dropped_by_filter": [],
            "gate_false_skip": False,
            "not_applicable": True,
        }

    # Schema validation — fail loud, not silent.
    seen_ids: set[str] = set()
    for idx, c in enumerate(concepts):
        if not isinstance(c, dict):
            raise ValueError(
                f"required_concepts[{idx}] must be a dict, got {type(c).__name__}"
            )
        cid = c.get("concept_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(
                f"required_concepts[{idx}] missing/empty concept_id"
            )
        if cid in seen_ids:
            raise ValueError(
                f"required_concepts has duplicate concept_id={cid!r}"
            )
        seen_ids.add(cid)
        any_of = c.get("any_of")
        if not isinstance(any_of, list) or not any_of or not all(isinstance(x, str) for x in any_of):
            raise ValueError(
                f"required_concepts[{idx}] (concept_id={cid!r}): "
                f"'any_of' must be a non-empty list of chunk_id strings"
            )

    ep = result.get("evidence_package") or {}
    source_pool = set(ep.get("source_ids", []) or [])
    relevant_pool = set(ep.get("relevant_ids", []) or [])

    n = len(concepts)
    hits_source = 0
    hits_postfilter = 0
    missing_from_source: list[str] = []
    dropped_by_filter: list[str] = []

    for c in concepts:
        cid = c["concept_id"]
        any_of = c["any_of"]
        in_source = any(x in source_pool for x in any_of)
        in_relevant = any(x in relevant_pool for x in any_of)
        if in_source:
            hits_source += 1
        else:
            missing_from_source.append(cid)
        if in_relevant:
            hits_postfilter += 1
        elif in_source:
            dropped_by_filter.append(cid)

    # Subset invariant: post-filter pool ⊆ source pool ⇒ hits_postfilter ≤ hits_source.
    # Violation = scorer or pool-construction bug; assert at scorer time.
    assert hits_postfilter <= hits_source, (
        f"concept_recall invariant violated for case {case.get('case_id')}: "
        f"hits_postfilter={hits_postfilter} > hits_source={hits_source}"
    )

    gate_false_skip = (ep.get("retrieval_decision") == "skipped") or not source_pool

    return {
        "recall_source": round(hits_source / n, 3),
        "recall_postfilter": round(hits_postfilter / n, 3),
        "hits_source": hits_source,
        "hits_postfilter": hits_postfilter,
        "concept_hit_source": hits_source >= 1,
        "concept_hit_postfilter": hits_postfilter >= 1,
        "n_concepts": n,
        "concepts_missing_from_source": missing_from_source,
        "concepts_dropped_by_filter": dropped_by_filter,
        "gate_false_skip": gate_false_skip,
        "not_applicable": False,
    }


def action_correctness(case: dict, result: dict) -> dict:
    """
    Exact match between proposed_action and ideal_action. Also surfaces the
    confusion-matrix cell (ideal, predicted) for stratified reporting.

    Cases that died on infrastructure errors (llm_error, parse_error) return
    applicable=False — they're excluded from the action_correct_rate
    denominator AND from the wrong_action_severity failure-mode tally, since
    the agent never produced an action to be right or wrong about.
    """
    ideal = case.get("ideal_action", "") or ""
    predicted = result.get("proposed_action", "") or ""
    stop_reason = result.get("stop_reason", "") or ""

    if stop_reason in INFRASTRUCTURE_ERROR_STOP_REASONS or stop_reason in NO_RESPONSE_STOP_REASONS:
        return {
            "correct": None,
            "ideal": ideal,
            "predicted": predicted,
            "cell": (ideal, predicted),
            "applicable": False,
            "stop_reason": stop_reason,
        }

    return {
        "correct": ideal == predicted and ideal != "",
        "ideal": ideal,
        "predicted": predicted,
        "cell": (ideal, predicted),
        "applicable": True,
        "stop_reason": stop_reason,
    }


def citation_audit(case: dict, result: dict) -> dict:
    """
    Verify source_ids_cited ⊆ relevant_ids (Critic invariant: no fabricated
    chunk ids).
    """
    cited = set(result.get("source_ids_cited", []) or [])
    relevant = set((result.get("evidence_package") or {}).get("relevant_ids", []) or [])

    out_of_set = sorted(cited - relevant)
    return {
        "subset_ok": not out_of_set,
        "out_of_set_ids": out_of_set,
        "n_cited": len(cited),
    }


def evidence_utilization(case: dict, result: dict) -> dict:
    """
    Fraction of retrieved relevant_ids that were actually cited by the
    Responder. Low utilization = Investigator dug up evidence the Responder
    ignored.

    Empty relevant_ids → not_applicable (gate skipped or zero hits).
    """
    cited = set(result.get("source_ids_cited", []) or [])
    relevant = set((result.get("evidence_package") or {}).get("relevant_ids", []) or [])

    if not relevant:
        return {
            "utilization": None,
            "cited": len(cited),
            "available": 0,
            "not_applicable": True,
        }

    used = cited & relevant
    return {
        "utilization": round(len(used) / len(relevant), 3),
        "cited": len(used),
        "available": len(relevant),
        "not_applicable": False,
    }


def token_cost(case: dict, result: dict) -> dict:
    """
    Per-node and total token usage. Pulled directly from result.token_usage.
    """
    usage = result.get("token_usage", {}) or {}
    by_node = {}
    total_in = 0
    total_out = 0
    for node, counts in usage.items():
        inp = int(counts.get("input", 0) or 0)
        out = int(counts.get("output", 0) or 0)
        by_node[node] = {"input": inp, "output": out}
        total_in += inp
        total_out += out
    return {
        "by_node": by_node,
        "total_input": total_in,
        "total_output": total_out,
        "total": total_in + total_out,
    }


def grounding_band_compliance(case: dict, result: dict) -> dict:
    """
    Confidence-band rule check (per skills/draft-response/SKILL.md):
      - confidence < 0.4 → source_ids_cited MUST be empty
      - 0.4 ≤ confidence < 0.7 → judge territory, no deterministic check
      - confidence ≥ 0.7 → if relevant_ids non-empty, source_ids_cited MUST be non-empty

    Returns compliant=True for any case where the gate skipped retrieval
    (no confidence to band against).

    Schema v2 (post-skip-gate iteration): the "low confidence + citation"
    pattern is no longer treated as a hard violation. Inspection of all 13
    surviving cases after the skip-gate iteration showed the Responder uses
    citations as referential anchors for hedged denials ("we found patch X
    but it does not address your specific complaint"), not as fix claims.
    The deterministic scorer cannot tell honest hedging from misleading
    fix claims — that is V1.5 LLM-judge territory. So:

      - hard_violation: high_confidence_no_citation only — unambiguous
        under-citation, the Responder is sitting on retrieved evidence.
      - flag: low_conf_with_cite — surfaced as a diagnostic count for the
        V1.5 judge to rule on, NOT counted as a failure.

    See evals/POST_V1_GROUNDING_FLAG_PLAN.md for the full rationale.
    NOTE: this creates a temporary prompt–scorer mismatch by design, pending
    V1.5 judge validation. The skill prompt still says "no citations below
    conf 0.4" — do not "fix" the prompt on the assumption the rule is dead;
    the scorer change is intentional.
    """
    ep = result.get("evidence_package") or {}
    if ep.get("retrieval_decision") == "skipped" or "confidence" not in ep:
        return {
            "compliant": True,
            "hard_violation": None,
            "flag": None,
            "confidence": None,
            "not_applicable": True,
        }

    confidence = float(ep.get("confidence") or 0.0)
    cited = result.get("source_ids_cited", []) or []
    relevant = ep.get("relevant_ids", []) or []

    hard_violation: str | None = None
    flag: str | None = None
    if confidence < 0.4 and cited:
        flag = "low_conf_with_cite"
    elif confidence >= 0.7 and relevant and not cited:
        hard_violation = "high_confidence_no_citation"

    return {
        "compliant": hard_violation is None,
        "hard_violation": hard_violation,
        "flag": flag,
        "confidence": round(confidence, 3),
        "not_applicable": False,
    }


# ---------- Batch-level scorers -------------------------------------------

def critic_health(records: list[dict]) -> dict:
    """
    Batch-level Critic behavior. Reads audit_log_iterations rows for every
    run_id present in records and computes:
      - approval rate by iteration index (0, 1, 2, ...)
      - rejection breakdown by reason_type
      - rejection rate overall
      - mean iterations to approval

    Records are matched to DB rows by run_id (set on the Coordinator's first
    pass). Records with no run_id (e.g., the run errored before coordinator
    minted one) are skipped from the DB join, but counted in totals.
    """
    # Skip-path runs (stop_reason == "no_response_needed") DO mint a run_id
    # but write zero rows to audit_log_iterations because the critic never
    # runs. Including them would inflate n_runs_with_audit and depress
    # n_runs_reaching_approval / n_runs_with_audit.
    run_ids = [
        (r.get("result") or {}).get("run_id")
        for r in records
        if r.get("ok")
        and (r.get("result") or {}).get("run_id")
        and (r.get("result") or {}).get("stop_reason") not in NO_RESPONSE_STOP_REASONS
    ]

    by_iteration: dict[int, dict[str, int]] = {}
    by_reason_type: dict[str, int] = {}
    total_iterations = 0
    total_approvals = 0
    total_rejections = 0
    iters_to_approval: list[int] = []

    if run_ids:
        conn = get_connection()
        try:
            placeholders = ",".join("?" * len(run_ids))
            cur = conn.execute(
                f"""
                SELECT run_id, iteration, approved, reason_type
                FROM audit_log_iterations
                WHERE run_id IN ({placeholders})
                ORDER BY run_id, iteration
                """,
                run_ids,
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        per_run: dict[str, list[tuple[int, int, str]]] = {}
        for run_id, iteration, approved, reason_type in rows:
            per_run.setdefault(run_id, []).append((iteration, approved, reason_type or ""))

        for run_id, iters in per_run.items():
            for iteration, approved, reason_type in iters:
                total_iterations += 1
                cell = by_iteration.setdefault(iteration, {"approved": 0, "rejected": 0})
                if approved:
                    cell["approved"] += 1
                    total_approvals += 1
                else:
                    cell["rejected"] += 1
                    total_rejections += 1
                    if reason_type:
                        by_reason_type[reason_type] = by_reason_type.get(reason_type, 0) + 1

            # iters_to_approval: index of first approval, if any
            for iteration, approved, _ in iters:
                if approved:
                    iters_to_approval.append(iteration)
                    break

    approval_rate_by_iter = {
        i: round(cell["approved"] / (cell["approved"] + cell["rejected"]), 3)
        for i, cell in by_iteration.items()
        if (cell["approved"] + cell["rejected"]) > 0
    }
    overall_approval_rate = (
        round(total_approvals / total_iterations, 3) if total_iterations else None
    )
    mean_iters_to_approval = (
        round(sum(iters_to_approval) / len(iters_to_approval), 3)
        if iters_to_approval
        else None
    )

    return {
        "n_runs_with_audit": len(run_ids),
        "total_iterations": total_iterations,
        "total_approvals": total_approvals,
        "total_rejections": total_rejections,
        "approval_rate_overall": overall_approval_rate,
        "approval_rate_by_iteration": approval_rate_by_iter,
        "rejections_by_reason_type": by_reason_type,
        "mean_iterations_to_approval": mean_iters_to_approval,
        "n_runs_reaching_approval": len(iters_to_approval),
    }


# ---------- Convenience: run all per-case scorers -------------------------

PER_CASE_SCORERS = {
    "retrieval_recall": retrieval_recall,
    "concept_recall": concept_recall,
    "action_correctness": action_correctness,
    "citation_audit": citation_audit,
    "evidence_utilization": evidence_utilization,
    "token_cost": token_cost,
    "grounding_band_compliance": grounding_band_compliance,
}


def score_case(case: dict, result: dict) -> dict[str, Any]:
    """Run every per-case scorer against one (case, result) pair."""
    return {name: fn(case, result) for name, fn in PER_CASE_SCORERS.items()}


def score_records(cases: list[dict], records: list[dict]) -> dict[str, Any]:
    """
    Run all scorers over a full batch. Joins cases ↔ records by case_id.

    Returns:
      {
        "per_case": {case_id: {scorer_name: result_dict, ...}, ...},
        "batch": {"critic_health": {...}}
      }
    """
    case_by_id = {c["case_id"]: c for c in cases}
    per_case: dict[str, dict] = {}
    for record in records:
        if not record.get("ok"):
            continue
        case_id = record["case_id"]
        case = case_by_id.get(case_id)
        if case is None:
            continue
        per_case[case_id] = score_case(case, record["result"])

    return {
        "per_case": per_case,
        "batch": {"critic_health": critic_health(records)},
    }
