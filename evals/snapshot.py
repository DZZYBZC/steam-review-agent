"""
evals/snapshot.py — Versioned eval snapshots.

A snapshot is a small JSON file that captures the aggregate metrics from one
eval run, plus the git SHA of the code that produced them. Two snapshots
taken at different commits can be diffed to answer "did this change move
the numbers, and in which direction?"

Files land in evals/snapshots/snapshot_<timestamp>.json.

Public surface:
  - write_snapshot(scored, gating, judge, judge_action, pairwise, records, run_file, filters) -> Path
  - load_latest_snapshot(exclude=None) -> dict | None
  - diff_snapshots(prev, curr) -> list[str]   # one line per changed metric
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"

# Bump when the snapshot aggregates dict shape changes in a non-additive way
# (field renamed, removed, or semantics changed). Visible in the snapshot
# payload and in the diff print path so transitions are interpretable.
# See git log for the per-version history.
SNAPSHOT_SCHEMA_VERSION = 8

# Metrics surfaced in the snapshot summary AND used for the one-line diff.
# Order is preserved in the diff output. Each entry is a dotted path into
# the snapshot["aggregates"] dict (resolved by _get_path).
DIFF_METRICS: list[tuple[str, str, str]] = [
    # (label, dotted_path, format_spec)
    ("action_correct_rate",          "action.correct_rate",                ".3f"),
    ("action_macro_f1",              "action.macro_f1",                    ".3f"),
    ("action_freeze_correct_rate",   "action.freeze_correct_rate",         ".3f"),
    ("action_freeze_n",              "action.freeze_n_total",              "d"),
    ("recall_source_mean",           "retrieval.recall_source_mean",       ".3f"),
    ("recall_relevant_mean",         "retrieval.recall_relevant_mean",     ".3f"),
    # concept_recall generalizes retrieval_recall over named concepts.
    # Values diverge from slot recall when manual annotation adds equivalents.
    ("concept_recall_source_mean",    "retrieval.concept_recall_source_mean",     ".3f"),
    ("concept_recall_postfilter_mean","retrieval.concept_recall_postfilter_mean", ".3f"),
    ("n_concept_unannotated_eligible","retrieval.n_concept_unannotated_eligible", "d"),
    # sufficiency_postfilter_rate is the conditional headline
    # (promoted when sufficient_sets coverage ≥ 80%).
    ("sufficiency_at_source_rate",    "retrieval.sufficiency_at_source_rate",     ".3f"),
    ("sufficiency_postfilter_rate",   "retrieval.sufficiency_postfilter_rate",    ".3f"),
    ("sufficient_sets_coverage_pct",  "retrieval.sufficient_sets_coverage_pct",   ".1f"),
    ("relevant_concept_precision_mean","retrieval.relevant_concept_precision_mean",".3f"),
    ("citation_concept_precision_mean","retrieval.citation_concept_precision_mean",".3f"),
    ("citation_concept_recall_mean",  "retrieval.citation_concept_recall_mean",  ".3f"),
    ("ndcg_at_k_mean",               "retrieval.ndcg_at_k_mean",               ".3f"),
    ("concept_hit_source_rate",      "retrieval.concept_hit_source_rate",  ".3f"),
    ("concept_hit_relevant_rate",    "retrieval.concept_hit_relevant_rate",".3f"),
    ("n_lost_to_filter",             "retrieval.n_lost_to_filter",         "d"),
    ("citation_subset_ok_rate",      "citation.subset_ok_rate",            ".3f"),
    # Schema v2: grounding.compliant_rate (a rate that mixed two different
    # things) replaced by an explicit count of HARD violations only, plus a
    # parallel diagnostic count for the low_conf_with_cite flag.
    ("grounding_band_hard_violations","grounding.n_hard_violations",       "d"),
    ("low_conf_with_cite_flag_count","diagnostics.low_conf_with_cite_flag_count", "d"),
    # Schema v3: LLM judge rulings on the low_conf_with_cite flagged cases.
    ("judge_lc_honest_hedge",        "judge.low_conf_with_cite.n_honest_hedge",         "d"),
    ("judge_lc_misleading_fix_claim","judge.low_conf_with_cite.n_misleading_fix_claim", "d"),
    ("judge_lc_unclear",             "judge.low_conf_with_cite.n_unclear",              "d"),
    # Schema v4: LLM judge rulings on the wrong_action_severity flagged cases.
    # judge_error is in the diff so any future regression in judge infra
    # (model deprecation, schema drift, prompt-driven parse failure) shows up
    # as movement in this row rather than hiding inside a substantive bucket.
    ("judge_act_over_escalation",       "judge.action.n_over_escalation",       "d"),
    ("judge_act_missed_escalation",     "judge.action.n_missed_escalation",     "d"),
    ("judge_act_category_drift",        "judge.action.n_category_drift",        "d"),
    ("judge_act_tolerable_disagreement","judge.action.n_tolerable_disagreement","d"),
    ("judge_act_judge_error",           "judge.action.n_judge_error",           "d"),
    # Schema v5: pairwise revision-improvement judge rulings. judge_pw_judge_error
    # belongs in DIFF_METRICS for the same reason as the action equivalent —
    # infrastructure misfires must be visible immediately. judge_pw_n_deterministic
    # belongs because a jump/drop in the shortcut count is itself a signal:
    # a drop means the responder is changing more drafts; a spike means it's
    # becoming a no-op revision-loop tax.
    ("judge_pw_revision_improved",   "judge.pairwise.n_revision_improved",   "d"),
    ("judge_pw_revision_neutral",    "judge.pairwise.n_revision_neutral",    "d"),
    ("judge_pw_revision_regressed",  "judge.pairwise.n_revision_regressed",  "d"),
    ("judge_pw_judge_error",         "judge.pairwise.n_judge_error",         "d"),
    ("judge_pw_n_judged",            "judge.pairwise.n_judged",              "d"),
    ("judge_pw_n_deterministic",     "judge.pairwise.n_deterministic",       "d"),
    # Schema v7: split retrieval judges. Two parallel sub-blocks. Predicate
    # counts AND judge_error counts both sit in DIFF_METRICS so future drift
    # in either is visible in snapshot diffs (not just terminal output).
    # Disagreement bucket counts likewise live in the diff so the
    # over-claim/under-use ratio is tracked across runs.
    ("judge_evd_gold_supports",          "judge.pool_sufficiency.n_supports",            "d"),
    ("judge_evd_gold_partially_supports","judge.pool_sufficiency.n_partially_supports",  "d"),
    ("judge_evd_gold_does_not_support",  "judge.pool_sufficiency.n_does_not_support",    "d"),
    ("judge_evd_gold_judge_error",       "judge.pool_sufficiency.n_judge_error",         "d"),
    ("judge_evd_gold_predicate_count",   "judge.pool_sufficiency.n_predicate_eligible",  "d"),
    ("judge_evd_gold_n_judged",          "judge.pool_sufficiency.n_judged",              "d"),
    ("judge_evd_draft_supports",         "judge.draft_grounding.n_supports",           "d"),
    ("judge_evd_draft_partially_supports","judge.draft_grounding.n_partially_supports","d"),
    ("judge_evd_draft_does_not_support", "judge.draft_grounding.n_does_not_support",   "d"),
    ("judge_evd_draft_judge_error",      "judge.draft_grounding.n_judge_error",        "d"),
    ("judge_evd_draft_predicate_count",  "judge.draft_grounding.n_predicate_eligible", "d"),
    ("judge_evd_draft_n_judged",         "judge.draft_grounding.n_judged",             "d"),
    ("judge_evd_n_with_both_judges",                "judge.evidence_disagreement.n_with_both_judges",                "d"),
    ("judge_evd_n_agreement",                       "judge.evidence_disagreement.n_agreement",                       "d"),
    ("judge_evd_n_gold_supports_draft_no_support",  "judge.evidence_disagreement.n_gold_supports_draft_no_support",  "d"),
    ("judge_evd_n_gold_no_support_draft_supports",  "judge.evidence_disagreement.n_gold_no_support_draft_supports",  "d"),
    ("judge_evd_n_other_disagreement",              "judge.evidence_disagreement.n_other_disagreement",              "d"),
    ("critic_approval_overall",      "critic_health.approval_rate_overall",".3f"),
    ("critic_iter0_approval",        "critic_health.iter0_approval",       ".3f"),
    ("action_override_runs",         "action_freeze.n_runs_with_action_override", "d"),
    ("effective_iter0_rate",         "action_freeze.effective_iter0_rate",  ".3f"),
    ("gating_accuracy",              "gating.accuracy",                    ".3f"),
    ("gating_false_skip_rate",       "gating.false_skip_rate",             ".3f"),
    ("gating_false_retrieve_rate",   "gating.false_retrieve_rate",         ".3f"),
    ("total_tokens",                 "cost.total_tokens",                  ",d"),
    # Schema v8: multipart-review stratum metrics for bounded multi-aspect
    # investigation experiment.
    ("mp_concept_recall_post_mean",  "multipart.concept_recall_postfilter_mean",      ".3f"),
    ("mp_sufficiency_post_rate",     "multipart.sufficiency_postfilter_rate",          ".3f"),
    ("mp_relevant_prec_mean",        "multipart.relevant_concept_precision_mean",      ".3f"),
    ("mp_citation_prec_mean",        "multipart.citation_concept_precision_mean",      ".3f"),
    ("mp_secondary_probe_rate",      "multipart.secondary_probe_rate",                ".3f"),
    ("mp_n_cases",                   "multipart.n_cases",                              "d"),
    ("role_coerced_count",           "multipart.role_coerced_count",                   "d"),
]


# ---------- Git helpers ----------------------------------------------------

def _git_sha() -> str:
    """Return current HEAD sha. 'unstaged' if working tree is dirty."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no_git"

    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return sha

    return f"{sha[:12]}+unstaged" if dirty else sha[:12]


# ---------- Judge aggregates (schema v3 + v4) -----------------------------

def _build_judge_grounding_block(judge: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `low_conf_with_cite` sub-block of the judge aggregate. If the
    grounding judge was not run (judge is None), return zero counts so the
    schema shape is stable across runs — downstream diff readers prefer
    "0/0/0" over missing keys.
    """
    if judge is None:
        rulings: dict[str, int] = {}
        n_flagged = 0
    else:
        rulings = judge.get("rulings", {}) or {}
        n_flagged = judge.get("n_flagged", 0)
    return {
        "n_flagged":              n_flagged,
        "n_honest_hedge":         rulings.get("honest_hedge", 0),
        "n_misleading_fix_claim": rulings.get("misleading_fix_claim", 0),
        "n_unclear":              rulings.get("unclear", 0),
    }


def _build_judge_action_block(judge_action: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `action` sub-block of the judge aggregate (schema v4). Mirrors
    _build_judge_grounding_block. n_judge_error is reported alongside the
    four semantic buckets so judge infrastructure misfires are visible
    immediately and never get silently absorbed into a substantive ruling.
    """
    if judge_action is None:
        rulings: dict[str, int] = {}
        n_flagged = 0
    else:
        rulings = judge_action.get("rulings", {}) or {}
        n_flagged = judge_action.get("n_flagged", 0)
    return {
        "n_flagged":                n_flagged,
        "n_over_escalation":        rulings.get("over_escalation", 0),
        "n_missed_escalation":      rulings.get("missed_escalation", 0),
        "n_category_drift":         rulings.get("category_drift", 0),
        "n_tolerable_disagreement": rulings.get("tolerable_disagreement", 0),
        "n_judge_error":            rulings.get("judge_error", 0),
    }


def _build_evidence_judge_block(judge_retrieval: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build a single retrieval-judge sub-block (used for both gold and draft
    flavors). Mirrors the other judge block builders. Predicate count is
    surfaced separately so the reporter and snapshot diffs can show the
    drop-from-eligible-to-judged dropout chain.
    """
    if judge_retrieval is None:
        rulings: dict[str, int] = {}
        n_judged = 0
        n_predicate_eligible = 0
    else:
        rulings = judge_retrieval.get("rulings", {}) or {}
        n_judged = judge_retrieval.get("n_judged", 0)
        n_predicate_eligible = judge_retrieval.get("n_predicate_eligible", 0)
    return {
        "n_predicate_eligible": n_predicate_eligible,
        "n_judged":             n_judged,
        "n_supports":           rulings.get("supports", 0),
        "n_partially_supports": rulings.get("partially_supports", 0),
        "n_does_not_support":   rulings.get("does_not_support", 0),
        "n_judge_error":        rulings.get("judge_error", 0),
    }


def _build_evidence_disagreement_block(
    judge_gold: dict[str, Any] | None,
    judge_draft: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Build the disagreement bucket counts. Strict bucket definitions per the
    plan: a case enters the disagreement tally only if BOTH judges actually
    ran on it AND neither ruling is judge_error.

    Buckets partition the both-ran population:
      - n_gold_supports_draft_no_support : gold=supports AND draft=does_not_support
      - n_gold_no_support_draft_supports : gold=does_not_support AND draft=supports
      - n_other_disagreement             : any other disagreement (incl. partially_supports)
      - n_agreement                      : exact ruling match
    """
    g_per_case = (judge_gold or {}).get("per_case", {}) or {}
    d_per_case = (judge_draft or {}).get("per_case", {}) or {}

    both_ids = [
        cid for cid in g_per_case
        if cid in d_per_case
        and g_per_case[cid].get("ruling") != "judge_error"
        and d_per_case[cid].get("ruling") != "judge_error"
    ]
    n_with_both = len(both_ids)
    n_gold_sup_draft_no = 0
    n_gold_no_draft_sup = 0
    n_other = 0
    n_agree = 0
    for cid in both_ids:
        g = g_per_case[cid]["ruling"]
        d = d_per_case[cid]["ruling"]
        if g == d:
            n_agree += 1
        elif g == "supports" and d == "does_not_support":
            n_gold_sup_draft_no += 1
        elif g == "does_not_support" and d == "supports":
            n_gold_no_draft_sup += 1
        else:
            n_other += 1

    # Invariant: buckets must partition the both-ran population.
    assert (
        n_gold_sup_draft_no + n_gold_no_draft_sup + n_other + n_agree == n_with_both
    ), "evidence-judge disagreement buckets do not partition n_with_both_judges"

    return {
        "n_with_both_judges":              n_with_both,
        "n_agreement":                     n_agree,
        "n_gold_supports_draft_no_support": n_gold_sup_draft_no,
        "n_gold_no_support_draft_supports": n_gold_no_draft_sup,
        "n_other_disagreement":            n_other,
    }


def _build_pairwise_block(pairwise: dict[str, Any] | None) -> dict[str, Any]:
    """
    Build the `pairwise` sub-block of the judge aggregate (schema v5). Mirrors
    the other two judge blocks. n_judge_error stays isolated. n_deterministic
    is the count of revision_neutral rulings that came from the deterministic
    normalize-equal shortcut (a subset of n_revision_neutral, NOT a separate
    bucket — surfaced so the snapshot diff can track shortcut firing rate
    over time).
    """
    if pairwise is None:
        rulings: dict[str, int] = {}
        n_judged = 0
        n_deterministic = 0
    else:
        rulings = pairwise.get("rulings", {}) or {}
        n_judged = pairwise.get("n_judged", 0)
        n_deterministic = pairwise.get("n_deterministic", 0)
    return {
        "n_judged":             n_judged,
        "n_revision_improved":  rulings.get("revision_improved", 0),
        "n_revision_neutral":   rulings.get("revision_neutral", 0),
        "n_revision_regressed": rulings.get("revision_regressed", 0),
        "n_judge_error":        rulings.get("judge_error", 0),
        "n_deterministic":      n_deterministic,
    }


# ---------- Aggregate builder ---------------------------------------------

def _build_aggregates(
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None,
    judge_action: dict[str, Any] | None,
    pairwise: dict[str, Any] | None,
    judge_evd_gold: dict[str, Any] | None,
    judge_evd_draft: dict[str, Any] | None,
    records: list[dict],
) -> dict[str, Any]:
    """
    Roll the scorer outputs into a flat dict of comparable numbers.
    These are the values diffed across snapshots.
    """
    per_case = scored["per_case"]
    n_scored = len(per_case)

    # Action — exclude cases that died on infrastructure errors (llm_error,
    # parse_error). They're missing decisions, not wrong ones, and shouldn't
    # poison the snapshot diff.
    action_evaluable = [
        s["action_correctness"] for s in per_case.values()
        if s["action_correctness"].get("applicable", True)
    ]
    n_action_evaluable = len(action_evaluable)
    n_action_excluded = n_scored - n_action_evaluable
    n_correct = sum(1 for s in action_evaluable if s["correct"])
    action_correct_rate = (n_correct / n_action_evaluable) if n_action_evaluable else None

    # Macro F1: unweighted average of per-class F1 across the four action labels.
    # Penalizes poor performance on rare classes that accuracy would hide.
    from config import PROPOSED_ACTIONS
    _f1s: list[float] = []
    for cls in PROPOSED_ACTIONS:
        tp = sum(1 for s in action_evaluable if s["ideal"] == cls and s["predicted"] == cls)
        fp = sum(1 for s in action_evaluable if s["ideal"] != cls and s["predicted"] == cls)
        fn = sum(1 for s in action_evaluable if s["ideal"] == cls and s["predicted"] != cls)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        _f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
    action_macro_f1 = round(sum(_f1s) / len(_f1s), 3) if _f1s else None

    # Split exclusions for snapshot diffs.
    excluded_action = [
        s["action_correctness"] for s in per_case.values()
        if not s["action_correctness"].get("applicable", True)
    ]
    n_excluded_infra = sum(
        1 for s in excluded_action
        if s.get("stop_reason") in {"llm_error", "parse_error"}
    )
    n_excluded_no_response = sum(
        1 for s in excluded_action
        if s.get("stop_reason") == "no_response_needed"
    )
    n_excluded_action_freeze = sum(
        1 for s in excluded_action
        if s.get("action_freeze")
    )

    # Action-freeze accuracy: tracked separately since these are contested
    # actions deferred to human review (auto-approved in evals).
    action_freeze_cases = [
        s["action_correctness"] for s in per_case.values()
        if s["action_correctness"].get("action_freeze")
    ]
    n_freeze_correct = sum(1 for s in action_freeze_cases if s["correct"])
    n_freeze_total = len(action_freeze_cases)
    freeze_correct_rate = round(n_freeze_correct / n_freeze_total, 3) if n_freeze_total else None

    # Retrieval (schema v6): slot-based recall with source vs relevant pools.
    # Cases with empty must_include are not_applicable and excluded entirely.
    # Cases where the gate skipped retrieval (source_ids empty) are surfaced
    # as a separate count and excluded from the recall/concept-hit means —
    # they're a gating failure mode, not a retrieval failure mode.
    retrieval_scored = [
        s["retrieval_recall"] for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
    ]
    n_gate_false_skip = sum(1 for s in retrieval_scored if s.get("gate_false_skip"))
    eligible = [s for s in retrieval_scored if not s.get("gate_false_skip")]

    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 3) if vals else None

    recall_source_mean = _mean([s["recall_source"] for s in eligible])
    recall_relevant_mean = _mean([s["recall_relevant"] for s in eligible])
    concept_hit_source_rate = _mean([1.0 if s["concept_hit_source"] else 0.0 for s in eligible])
    concept_hit_relevant_rate = _mean([1.0 if s["concept_hit_relevant"] else 0.0 for s in eligible])
    # Chunks the retriever surfaced but the investigator's Self-RAG filter dropped.
    n_lost_to_filter = sum(len(s.get("dropped_by_filter") or []) for s in eligible)
    n_cases_with_drops = sum(1 for s in eligible if s.get("dropped_by_filter"))

    # Concept recall. Same eligibility rules as retrieval_recall:
    # not_applicable cases are excluded entirely; gate_false_skip cases are
    # excluded from the mean.
    concept_scored = [
        s["concept_recall"] for s in per_case.values()
        if not s["concept_recall"].get("not_applicable")
    ]
    concept_eligible = [s for s in concept_scored if not s.get("gate_false_skip")]
    concept_recall_source_mean = _mean([s["recall_source"] for s in concept_eligible])
    concept_recall_postfilter_mean = _mean([s["recall_postfilter"] for s in concept_eligible])
    # Annotation-coverage gauge: cases that are retrieval-eligible (have
    # must_include_chunk_ids) but lack required_concepts. Should be 0; if it
    # climbs, a newly-added case wasn't migrated and concept recall silently shrinks.
    n_concept_unannotated_eligible = sum(
        1 for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
        and s["concept_recall"].get("not_applicable")
    )

    # Evidence sufficiency. A case is sufficient at a pool iff any declared
    # sufficient_set is fully covered there. Cases without sufficient_sets
    # are not_applicable and excluded from the rate.
    suff_scored = [
        s["evidence_sufficiency"] for s in per_case.values()
        if not s["evidence_sufficiency"].get("not_applicable")
    ]
    def _rate(vals: list[bool]) -> float | None:
        return round(sum(1 for v in vals if v) / len(vals), 3) if vals else None
    sufficiency_at_source_rate = _rate([s["sufficient_at_source"] for s in suff_scored])
    sufficiency_postfilter_rate = _rate([s["sufficient_postfilter"] for s in suff_scored])
    # Annotation-coverage gauge for sufficient_sets. Retrieval-eligible
    # denominator (same as retrieval_recall's n_with_must_include).
    n_retrieval_eligible = len(retrieval_scored)
    n_sufficient_sets_annotated = len(suff_scored)
    sufficient_sets_coverage_pct = (
        round(100.0 * n_sufficient_sets_annotated / n_retrieval_eligible, 1)
        if n_retrieval_eligible else None
    )

    # Relevant-concept precision. Diagnostic for filter noise relative to
    # the annotator-listed concept pool.
    precision_scored = [
        s["relevant_concept_precision"] for s in per_case.values()
        if not s["relevant_concept_precision"].get("not_applicable")
    ]
    relevant_concept_precision_mean = _mean([s["precision"] for s in precision_scored])

    # Citation-concept precision (same as above but scoped to cited chunks).
    cite_precision_scored = [
        s["citation_concept_precision"] for s in per_case.values()
        if not s["citation_concept_precision"].get("not_applicable")
    ]
    citation_concept_precision_mean = _mean([s["precision"] for s in cite_precision_scored])

    # Citation-concept recall: of retrieved gold concepts, how many did the responder cite?
    cite_recall_scored = [
        s["citation_concept_recall"] for s in per_case.values()
        if not s["citation_concept_recall"].get("not_applicable")
    ]
    citation_concept_recall_mean = _mean([s["recall"] for s in cite_recall_scored])

    # NDCG@K: rank-aware retrieval quality.
    ndcg_scored = [
        s["ndcg_at_k"] for s in per_case.values()
        if not s["ndcg_at_k"].get("not_applicable")
    ]
    ndcg_mean = _mean([s["ndcg"] for s in ndcg_scored])

    # Citation subset
    n_cite_ok = sum(1 for s in per_case.values() if s["citation_audit"]["subset_ok"])
    cite_rate = (n_cite_ok / n_scored) if n_scored else None

    # Grounding band — schema v2: count hard violations explicitly, and
    # count low_conf_with_cite as a separate diagnostic flag (not a failure).
    n_hard_violations = sum(
        1 for s in per_case.values()
        if s["grounding_band_compliance"].get("hard_violation")
    )
    n_low_conf_with_cite_flag = sum(
        1 for s in per_case.values()
        if s["grounding_band_compliance"].get("flag") == "low_conf_with_cite"
    )

    # Token cost
    total_tokens = sum(s["token_cost"]["total"] for s in per_case.values())

    # Critic health: pull a couple of headline numbers up to top level for diff
    ch = scored["batch"]["critic_health"]
    iter0 = ch["approval_rate_by_iteration"].get(0)

    # Stop reasons
    by_stop: dict[str, int] = defaultdict(int)
    for r in records:
        if r.get("ok"):
            by_stop[(r["result"] or {}).get("stop_reason") or "(none)"] += 1
        else:
            by_stop["(harness_error)"] += 1

    # Failure-mode tally (deterministic). Schema v2: low_conf_with_cite is a
    # diagnostic flag, not a failure — counted in the diagnostics dict below.
    failure_modes: dict[str, int] = defaultdict(int)
    for s in per_case.values():
        cite = s["citation_audit"]
        if cite["out_of_set_ids"]:
            failure_modes["cited_irrelevant_patch"] += 1
        hv = s["grounding_band_compliance"].get("hard_violation")
        if hv:
            failure_modes[hv] += 1
        act = s["action_correctness"]
        # Skip infra-error cases — no predicted action to be wrong about.
        if act.get("applicable", True) and not act["correct"] and act["ideal"] and act["predicted"]:
            failure_modes["wrong_action_severity"] += 1

    # Action-freeze metrics (Iter7): computed from serialized result records.
    n_runs_with_action_override = sum(
        1 for r in records
        if r.get("ok") and (r.get("result") or {}).get("action_override_count", 0) > 0
    )
    # effective_iter0_rate: fraction of eligible runs that reached human_approval
    # after their first critic evaluation (by normal approval OR coordinator override).
    # Denominator: non-error runs that reached the critic at least once.
    _eligible_for_eff = [
        r for r in records
        if r.get("ok")
        and (r.get("result") or {}).get("stop_reason") not in {"llm_error", "parse_error", "no_response_needed"}
    ]
    _n_eff_denom = len(_eligible_for_eff)
    # Numerator: runs where either (a) critic approved at iter0 (iteration_count==1
    # means only one responder pass happened) or (b) coordinator overrode at iter0
    # (first_override_at_iteration==1, set once and never overwritten).
    _n_eff_numer = sum(
        1 for r in _eligible_for_eff
        if (
            (r.get("result") or {}).get("first_override_at_iteration", -1) == 1
            or (r.get("result") or {}).get("iteration_count", 0) == 1
        )
    )
    effective_iter0_rate = round(_n_eff_numer / _n_eff_denom, 3) if _n_eff_denom else None

    # Multipart stratum: metrics restricted to cases tagged multipart=true.
    # _build_aggregates doesn't receive the cases list directly, so we use
    # the secondary_probe_check scorer which carries is_multipart from the
    # golden case at scoring time.
    mp_case_ids = set(
        cid for cid, s in per_case.items()
        if s.get("secondary_probe_check", {}).get("is_multipart")
    )
    mp_n = len(mp_case_ids)

    # Concept recall (multipart stratum)
    mp_concept = [
        per_case[cid]["concept_recall"] for cid in mp_case_ids
        if not per_case[cid]["concept_recall"].get("not_applicable")
        and not per_case[cid]["concept_recall"].get("gate_false_skip")
    ]
    mp_concept_recall_postfilter_mean = _mean([s["recall_postfilter"] for s in mp_concept])

    # Sufficiency (multipart stratum)
    mp_suff = [
        per_case[cid]["evidence_sufficiency"] for cid in mp_case_ids
        if not per_case[cid]["evidence_sufficiency"].get("not_applicable")
    ]
    mp_sufficiency_postfilter_rate = _rate([s["sufficient_postfilter"] for s in mp_suff])

    # Precision (multipart stratum)
    mp_prec = [
        per_case[cid]["relevant_concept_precision"] for cid in mp_case_ids
        if not per_case[cid]["relevant_concept_precision"].get("not_applicable")
    ]
    mp_relevant_concept_precision_mean = _mean([s["precision"] for s in mp_prec])

    mp_cite_prec = [
        per_case[cid]["citation_concept_precision"] for cid in mp_case_ids
        if not per_case[cid]["citation_concept_precision"].get("not_applicable")
    ]
    mp_citation_concept_precision_mean = _mean([s["precision"] for s in mp_cite_prec])

    # Secondary probe rate (multipart stratum)
    mp_probe = [
        per_case[cid]["secondary_probe_check"] for cid in mp_case_ids
        if per_case[cid]["secondary_probe_check"].get("applicable")
    ]
    mp_probed = sum(1 for s in mp_probe if s["probed"])
    mp_secondary_probe_rate = round(mp_probed / len(mp_probe), 3) if mp_probe else None

    # Role coerced count (whole set)
    role_coerced_count = sum(
        1 for s in per_case.values()
        if s.get("secondary_probe_check", {}).get("role_coerced")
    )

    return {
        "n_records": len(records),
        "n_scored": n_scored,
        "stop_reasons": dict(by_stop),
        "action": {
            "n_correct": n_correct,
            "n_evaluable": n_action_evaluable,
            "n_excluded_total": n_action_excluded,
            "n_excluded_infra_error": n_excluded_infra,
            "n_excluded_no_response": n_excluded_no_response,
            "n_excluded_action_freeze": n_excluded_action_freeze,
            "correct_rate": action_correct_rate,
            "macro_f1": action_macro_f1,
            "freeze_n_correct": n_freeze_correct,
            "freeze_n_total": n_freeze_total,
            "freeze_correct_rate": freeze_correct_rate,
        },
        "retrieval": {
            "recall_source_mean": recall_source_mean,
            "recall_relevant_mean": recall_relevant_mean,
            "concept_hit_source_rate": concept_hit_source_rate,
            "concept_hit_relevant_rate": concept_hit_relevant_rate,
            "n_with_must_include": len(retrieval_scored),
            "n_eligible_for_recall": len(eligible),
            "n_gate_false_skip_in_recall_pool": n_gate_false_skip,
            "n_lost_to_filter": n_lost_to_filter,
            "n_cases_with_filter_drops": n_cases_with_drops,
            "concept_recall_source_mean": concept_recall_source_mean,
            "concept_recall_postfilter_mean": concept_recall_postfilter_mean,
            "n_concept_eligible_for_recall": len(concept_eligible),
            "n_concept_unannotated_eligible": n_concept_unannotated_eligible,
            "sufficiency_at_source_rate": sufficiency_at_source_rate,
            "sufficiency_postfilter_rate": sufficiency_postfilter_rate,
            "sufficient_sets_coverage_pct": sufficient_sets_coverage_pct,
            "n_sufficient_sets_annotated": n_sufficient_sets_annotated,
            "n_retrieval_eligible": n_retrieval_eligible,
            "relevant_concept_precision_mean": relevant_concept_precision_mean,
            "n_relevant_concept_precision_scored": len(precision_scored),
            "citation_concept_precision_mean": citation_concept_precision_mean,
            "n_citation_concept_precision_scored": len(cite_precision_scored),
            "citation_concept_recall_mean": citation_concept_recall_mean,
            "n_citation_concept_recall_scored": len(cite_recall_scored),
            "ndcg_at_k_mean": ndcg_mean,
            "n_ndcg_scored": len(ndcg_scored),
        },
        "citation": {
            "subset_ok_rate": cite_rate,
        },
        "grounding": {
            "n_hard_violations": n_hard_violations,
        },
        "diagnostics": {
            "low_conf_with_cite_flag_count": n_low_conf_with_cite_flag,
        },
        "judge": {
            "low_conf_with_cite":   _build_judge_grounding_block(judge),
            "action":               _build_judge_action_block(judge_action),
            "pairwise":             _build_pairwise_block(pairwise),
            "pool_sufficiency":     _build_evidence_judge_block(judge_evd_gold),
            "draft_grounding":    _build_evidence_judge_block(judge_evd_draft),
            "evidence_disagreement": _build_evidence_disagreement_block(judge_evd_gold, judge_evd_draft),
        },
        "critic_health": {
            "approval_rate_overall": ch["approval_rate_overall"],
            "iter0_approval": iter0,
            "mean_iters_to_approval": ch["mean_iterations_to_approval"],
            "rejections_by_reason_type": ch["rejections_by_reason_type"],
        },
        "action_freeze": {
            "n_runs_with_action_override": n_runs_with_action_override,
            "effective_iter0_rate": effective_iter0_rate,
        },
        "gating": {
            "accuracy": gating["accuracy"],
            "false_skip_rate": gating["false_skip_rate"],
            "false_retrieve_rate": gating["false_retrieve_rate"],
            "confusion": gating["confusion"],
        },
        "cost": {
            "total_tokens": total_tokens,
        },
        "failure_modes": dict(failure_modes),
        "multipart": {
            "n_cases": mp_n,
            "concept_recall_postfilter_mean": mp_concept_recall_postfilter_mean,
            "sufficiency_postfilter_rate": mp_sufficiency_postfilter_rate,
            "relevant_concept_precision_mean": mp_relevant_concept_precision_mean,
            "citation_concept_precision_mean": mp_citation_concept_precision_mean,
            "secondary_probe_rate": mp_secondary_probe_rate,
            "role_coerced_count": role_coerced_count,
        },
    }


# ---------- Read / write --------------------------------------------------

def write_snapshot(
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None,
    judge_action: dict[str, Any] | None,
    pairwise: dict[str, Any] | None,
    judge_evd_gold: dict[str, Any] | None,
    judge_evd_draft: dict[str, Any] | None,
    records: list[dict],
    run_file: Path | str,
    filters: dict,
) -> Path:
    """Write a snapshot file and return its path."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "timestamp": timestamp,
        "git_sha": _git_sha(),
        "run_file": str(run_file),
        "filters": filters,
        "aggregates": _build_aggregates(
            scored, gating, judge, judge_action, pairwise,
            judge_evd_gold, judge_evd_draft, records,
        ),
    }
    out = SNAPSHOTS_DIR / f"snapshot_{timestamp}.json"
    with out.open("w") as f:
        json.dump(payload, f, indent=2)
    return out


def load_latest_snapshot(exclude: Path | None = None) -> dict | None:
    """
    Return the most recent snapshot dict, or None if no snapshots exist.
    `exclude` lets the caller skip the file just written so we compare
    against the previous one rather than against ourselves.
    """
    if not SNAPSHOTS_DIR.exists():
        return None
    candidates = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"))
    if exclude is not None:
        candidates = [p for p in candidates if p != exclude]
    if not candidates:
        return None
    with candidates[-1].open() as f:
        return json.load(f)


def _get_path(d: dict, dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _fmt(v: Any, spec: str) -> str:
    if v is None:
        return "n/a"
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def diff_snapshots(prev: dict, curr: dict) -> list[str]:
    """
    Return one-line-per-metric diff strings between two snapshots.
    Skips metrics that are unchanged or both None.
    """
    prev_agg = prev.get("aggregates", {})
    curr_agg = curr.get("aggregates", {})
    lines: list[str] = []

    # Schema version transition annotation. Missing field means v1 (pre-v2
    # snapshots had no schema_version key). Surface a one-line note at the
    # top of the diff so the field renames/replacements that follow are
    # interpretable rather than mysterious.
    prev_v = prev.get("schema_version", 1)
    curr_v = curr.get("schema_version", 1)
    if prev_v != curr_v:
        # Per-transition annotations. New entries get added as the schema
        # bumps; older transitions remain interpretable forever.
        annotations = {
            (1, 2): "grounding metric split: low_conf_with_cite is now a flag, not a violation",
            (2, 3): "judge layer added: low_conf_with_cite cases now ruled by LLM",
            (1, 3): "grounding metric split (v1→v2) AND judge layer added (v2→v3)",
            (3, 4): "judge_action layer added: wrong_action_severity cases now ruled by LLM",
            (2, 4): "judge_grounding (v2→v3) AND judge_action (v3→v4) layers added",
            (1, 4): "grounding metric split (v1→v2), judge_grounding (v2→v3), AND judge_action (v3→v4) layers added",
            (4, 5): "pairwise revision-improvement judge added: multi-iteration approved cases now ruled by LLM (with deterministic normalize-equal shortcut)",
            (3, 5): "judge_action (v3→v4) AND pairwise revision-improvement (v4→v5) layers added",
            (2, 5): "judge_grounding (v2→v3), judge_action (v3→v4), AND pairwise (v4→v5) layers added",
            (1, 5): "grounding metric split (v1→v2), judge_grounding (v2→v3), judge_action (v3→v4), AND pairwise (v4→v5) layers added",
            (5, 6): "retrieval block split into source vs relevant recall, concept hit-rate companion added, false-skip cases separated, lost-to-filter diagnostic added, recall_at_k_mean removed (was a conflation)",
            (4, 6): "pairwise (v4→v5) AND retrieval recalibration (v5→v6)",
            (3, 6): "judge_action (v3→v4), pairwise (v4→v5), AND retrieval recalibration (v5→v6)",
            (2, 6): "judge_grounding (v2→v3), judge_action (v3→v4), pairwise (v4→v5), AND retrieval recalibration (v5→v6)",
            (1, 6): "full schema evolution (v1→v6)",
            (6, 7): "split retrieval judges added: judge_pool_sufficiency (pure retrieval) and judge_draft_grounding (joint retrieval+drafting), with disagreement bucket counts",
            (5, 7): "retrieval recalibration (v5→v6) AND split retrieval judges (v6→v7)",
            (4, 7): "pairwise (v4→v5), retrieval recalibration (v5→v6), AND split retrieval judges (v6→v7)",
            (1, 7): "full schema evolution (v1→v7)",
            (7, 8): "multipart stratum added: concept recall, sufficiency, precision, secondary probe rate, role coerced count",
            (6, 8): "split retrieval judges (v6→v7) AND multipart stratum (v7→v8)",
            (1, 8): "full schema evolution (v1→v8)",
        }
        note = annotations.get((prev_v, curr_v), "schema shape changed")
        lines.append(
            f"  ⚠ schema_version changed: {prev_v} → {curr_v} ({note})"
        )

    for label, path, spec in DIFF_METRICS:
        p = _get_path(prev_agg, path)
        c = _get_path(curr_agg, path)
        if p is None and c is None:
            continue
        if p == c:
            continue
        # Compute delta if both numeric
        delta_str = ""
        if isinstance(p, (int, float)) and isinstance(c, (int, float)):
            delta = c - p
            sign = "+" if delta >= 0 else ""
            try:
                delta_str = f"  ({sign}{format(delta, spec)})"
            except (TypeError, ValueError):
                delta_str = f"  ({sign}{delta})"
        lines.append(f"  {label:<32} {_fmt(p, spec):>12} -> {_fmt(c, spec):>12}{delta_str}")
    return lines
