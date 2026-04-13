"""
evals/reporter.py — Stratified terminal report.

Consumes the output of evals.scorers.deterministic.score_records() and
evals.scorers.gating_accuracy.gating_accuracy_batch() and prints a
human-readable summary to stdout. No file I/O, no LLM calls.

Sections:
  1. Overall summary
  2. Per-category breakdown (action correctness + recall@k)
  3. Action confusion matrix
  4. Deterministic failure-mode tally (derived from scorer outputs)
  5. Diagnostic flags (low_conf_with_cite, etc.)
  6. LLM judge — low_conf_with_cite rulings (V1.5)
  7. LLM judge — wrong_action_severity rulings (V1.5)
  8. Critic health
  9. Gating accuracy (confusion matrix + load-bearing case lists)

Usage:
    from evals.reporter import print_report
    print_report(cases, records, scored, gating, judge, judge_action)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from config import PROPOSED_ACTIONS


# ---------- Helpers --------------------------------------------------------

def _hr(char: str = "=", width: int = 70) -> str:
    return char * width


def _pct(num: int, denom: int) -> str:
    if not denom:
        return "n/a"
    return f"{num}/{denom} ({100 * num / denom:.0f}%)"


def _fmt_float(v: float | None, places: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{places}f}"


# ---------- Section builders ----------------------------------------------

def _overall_section(records: list[dict], scored: dict) -> list[str]:
    lines = [_hr(), "OVERALL", _hr()]
    n_total = len(records)
    n_ok = sum(1 for r in records if r.get("ok"))
    n_err = n_total - n_ok

    # Stop-reason breakdown
    by_stop: dict[str, int] = defaultdict(int)
    for r in records:
        if r.get("ok"):
            by_stop[(r["result"] or {}).get("stop_reason") or "(none)"] += 1
        else:
            by_stop["(harness_error)"] += 1

    # Token totals
    total_tokens = 0
    for case_id, scores in scored["per_case"].items():
        total_tokens += scores["token_cost"]["total"]

    # Action correctness rollup. Excludes cases with no agent-produced action:
    # infrastructure errors (llm_error, parse_error) and skip-path cases
    # (no_response_needed). Both are missing decisions, not wrong ones.
    action_evaluable = [
        s["action_correctness"] for s in scored["per_case"].values()
        if s["action_correctness"].get("applicable", True)
    ]
    n_correct = sum(1 for s in action_evaluable if s["correct"])
    n_action_scored = len(action_evaluable)

    excluded = [
        s["action_correctness"] for s in scored["per_case"].values()
        if not s["action_correctness"].get("applicable", True)
    ]
    n_infra = sum(
        1 for s in excluded
        if s.get("stop_reason") in {"llm_error", "parse_error"}
    )
    n_no_response = sum(
        1 for s in excluded
        if s.get("stop_reason") == "no_response_needed"
    )
    n_action_excluded = len(excluded)

    lines.append(f"  Cases:            {n_total}")
    lines.append(f"  Succeeded:        {n_ok}")
    lines.append(f"  Errored:          {n_err}")
    if n_action_excluded:
        lines.append(
            f"  Action correct:   {_pct(n_correct, n_action_scored)}  "
            f"({n_action_excluded} excluded — infra: {n_infra}, no-response: {n_no_response})"
        )
    else:
        lines.append(f"  Action correct:   {_pct(n_correct, n_action_scored)}")
    lines.append(f"  Total tokens:     {total_tokens:,}")
    lines.append(f"  Stop reasons:     " + ", ".join(f"{k}={v}" for k, v in sorted(by_stop.items())))
    return lines


def _per_category_section(cases: list[dict], scored: dict) -> list[str]:
    lines = ["", _hr(), "PER CATEGORY (annotated_category)", _hr()]
    case_by_id = {c["case_id"]: c for c in cases}

    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for case_id, scores in scored["per_case"].items():
        cat = case_by_id.get(case_id, {}).get("annotated_category", "?")
        grouped[cat].append((case_id, scores))

    header = f"  {'category':<22} {'n':>3}  {'action✓':>10}  {'recall_src':>10}  {'recall_rel':>10}  {'cite⊆rel':>10}  {'bandH✓':>8}  {'lc⚑':>4}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_drops = 0
    total_cases_with_drops = 0
    for cat in sorted(grouped):
        rows = grouped[cat]
        n = len(rows)
        # Exclude infrastructure-error cases from action denominator only.
        action_rows = [s for _, s in rows if s["action_correctness"].get("applicable", True)]
        n_action_evaluable = len(action_rows)
        n_act = sum(1 for s in action_rows if s["action_correctness"]["correct"])
        # Retrieval: schema v6 slot-based recall. Exclude not_applicable AND
        # gate_false_skip cases from the per-category means (they're surfaced
        # in the aggregate retrieval block instead).
        retrieval_eligible = [
            s["retrieval_recall"] for _, s in rows
            if not s["retrieval_recall"].get("not_applicable")
            and not s["retrieval_recall"].get("gate_false_skip")
        ]
        src_vals = [r["recall_source"] for r in retrieval_eligible]
        rel_vals = [r["recall_relevant"] for r in retrieval_eligible]
        recall_src_avg = sum(src_vals) / len(src_vals) if src_vals else None
        recall_rel_avg = sum(rel_vals) / len(rel_vals) if rel_vals else None
        n_cite_ok = sum(1 for _, s in rows if s["citation_audit"]["subset_ok"])
        # bandH✓ counts only HARD band violations (compliant=True under v2 schema).
        n_band_ok = sum(1 for _, s in rows if s["grounding_band_compliance"]["compliant"])
        # lc⚑ is the diagnostic flag count (low_conf_with_cite); not a failure.
        n_lc_flag = sum(
            1 for _, s in rows
            if s["grounding_band_compliance"].get("flag") == "low_conf_with_cite"
        )
        # Filter drops rolled across the whole table.
        for r in retrieval_eligible:
            drops = r.get("dropped_by_filter") or []
            total_drops += len(drops)
            if drops:
                total_cases_with_drops += 1
        lines.append(
            f"  {cat:<22} {n:>3}  {_pct(n_act, n_action_evaluable):>10}  {_fmt_float(recall_src_avg, 2):>10}  {_fmt_float(recall_rel_avg, 2):>10}  {_pct(n_cite_ok, n):>10}  {_pct(n_band_ok, n):>8}  {n_lc_flag:>4}"
        )
    lines.append(
        f"  source→relevant drops: {total_drops} chunks lost to investigator filter "
        f"across {total_cases_with_drops} case(s)"
    )
    return lines


def _layered_retrieval_section(scored: dict) -> list[str]:
    """
    Layered retrieval display. Rows, cheapest → most semantic:
      - gold slot recall (diagnostic; legacy retrieval_recall)
      - concept recall
      - relevant-concept precision
      - sufficiency rate (with provisional tag when coverage < 80%)
      - HEADLINE line (headline rule: concept_recall_postfilter until
        sufficient_sets coverage ≥ 80%, then sufficiency_postfilter_rate)

    Every row prints [n_scored=K, eligible=M] so per-row denominator
    differences are visible.
    """
    per_case = scored["per_case"]

    # M = retrieval-eligible (canonical): cases with non-empty must_include_chunk_ids
    retrieval_eligible = [
        s["retrieval_recall"] for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
    ]
    M = len(retrieval_eligible)

    # gold slot recall: K = M minus gate_false_skip
    gold_pool = [s for s in retrieval_eligible if not s.get("gate_false_skip")]
    gold_src = _mean([s["recall_source"] for s in gold_pool])
    gold_post = _mean([s["recall_relevant"] for s in gold_pool])

    # concept recall
    concept_pool = [
        s["concept_recall"] for s in per_case.values()
        if not s["concept_recall"].get("not_applicable")
        and not s["concept_recall"].get("gate_false_skip")
    ]
    concept_src = _mean([s["recall_source"] for s in concept_pool])
    concept_post = _mean([s["recall_postfilter"] for s in concept_pool])

    # concept annotation coverage (should be 100% post A1)
    n_concept_unannotated = sum(
        1 for s in per_case.values()
        if not s["retrieval_recall"].get("not_applicable")
        and s["concept_recall"].get("not_applicable")
    )
    n_concept_annotated = M - n_concept_unannotated

    # relevant-concept precision
    precision_pool = [
        s["relevant_concept_precision"] for s in per_case.values()
        if not s["relevant_concept_precision"].get("not_applicable")
    ]
    precision_mean = _mean([s["precision"] for s in precision_pool])

    # citation-concept precision
    cite_prec_pool = [
        s["citation_concept_precision"] for s in per_case.values()
        if not s["citation_concept_precision"].get("not_applicable")
    ]
    cite_prec_mean = _mean([s["precision"] for s in cite_prec_pool])

    # sufficiency
    suff_pool = [
        s["evidence_sufficiency"] for s in per_case.values()
        if not s["evidence_sufficiency"].get("not_applicable")
    ]
    def _rate(vals: list[bool]) -> float | None:
        return (sum(1 for v in vals if v) / len(vals)) if vals else None
    suff_src = _rate([s["sufficient_at_source"] for s in suff_pool])
    suff_post = _rate([s["sufficient_postfilter"] for s in suff_pool])
    n_suff_annotated = len(suff_pool)
    suff_coverage_pct = (100.0 * n_suff_annotated / M) if M else 0.0
    provisional = suff_coverage_pct < 80.0

    lines = [
        "",
        _hr(),
        f"RETRIEVAL (LAYERED)   [eligible = {M} retrieval-eligible cases]",
        _hr(),
        f"  annotation coverage:    concept-annotated {n_concept_annotated}/{M} ({100.0 * n_concept_annotated / M:.0f}%)   "
        f"sufficient_sets {n_suff_annotated}/{M} ({suff_coverage_pct:.0f}%)",
        f"                          unannotated retrieval-eligible: {n_concept_unannotated}",
        "",
        f"  gold slot recall (diag): src {_fmt_float(gold_src, 3)}   post {_fmt_float(gold_post, 3)}"
        f"    [n_scored={len(gold_pool)}, eligible={M}]",
        f"  concept recall         : src {_fmt_float(concept_src, 3)}   post {_fmt_float(concept_post, 3)}"
        f"    [n_scored={len(concept_pool)}, eligible={M}]",
        f"  relevant-concept prec. : {_fmt_float(precision_mean, 3)}"
        f"    [n_scored={len(precision_pool)}, eligible={M}]",
        f"  citation-concept prec. : {_fmt_float(cite_prec_mean, 3)}"
        f"    [n_scored={len(cite_prec_pool)}, eligible={M}]",
    ]
    suff_line = (
        f"  sufficiency rate       : src {_fmt_float(suff_src, 3)}    post {_fmt_float(suff_post, 3)}"
    )
    if provisional:
        suff_line += f"   (provisional — coverage {n_suff_annotated}/{M} < 80%)"
    suff_line += f"    [n_scored={n_suff_annotated}, eligible={M}]"
    lines.append(suff_line)
    lines.append("")

    if provisional:
        headline_val = _fmt_float(concept_post, 3)
        lines.append(
            f"  HEADLINE: concept_recall_postfilter_mean = {headline_val}   "
            f"(provisional; sufficient_sets coverage {suff_coverage_pct:.0f}% < 80%)"
        )
    else:
        headline_val = _fmt_float(suff_post, 3)
        lines.append(
            f"  HEADLINE: sufficiency_postfilter_rate = {headline_val}   "
            f"(promoted; sufficient_sets coverage {suff_coverage_pct:.0f}% ≥ 80%)"
        )
    return lines


def _mean(vals: list[float | None]) -> float | None:
    clean = [v for v in vals if v is not None]
    return sum(clean) / len(clean) if clean else None


def _multipart_stratum_section(
    cases: list[dict],
    scored: dict,
    judge_evd_draft: dict | None = None,
) -> list[str]:
    """
    Multipart review stratum. Shows key metrics restricted to cases
    tagged multipart=true vs non-multipart, plus secondary probe rate.
    """
    case_by_id = {c["case_id"]: c for c in cases}
    per_case = scored["per_case"]

    mp_ids = [
        cid for cid in per_case
        if case_by_id.get(cid, {}).get("multipart")
    ]
    non_mp_ids = [
        cid for cid in per_case
        if not case_by_id.get(cid, {}).get("multipart")
    ]

    if not mp_ids:
        return ["", _hr(), "MULTIPART STRATUM", _hr(), "  (no multipart cases in golden set)"]

    lines = ["", _hr(), f"MULTIPART STRATUM   [multipart={len(mp_ids)}, non-multipart={len(non_mp_ids)}]", _hr()]

    def _stratum_metrics(ids: list[str], label: str) -> list[str]:
        out = [f"  {label} (n={len(ids)}):"]

        # Concept recall
        cr = [
            per_case[cid]["concept_recall"] for cid in ids
            if not per_case[cid]["concept_recall"].get("not_applicable")
            and not per_case[cid]["concept_recall"].get("gate_false_skip")
        ]
        cr_post = _mean([s["recall_postfilter"] for s in cr])
        out.append(f"    concept_recall_postfilter : {_fmt_float(cr_post)}  [n={len(cr)}]")

        # Sufficiency
        sf = [
            per_case[cid]["evidence_sufficiency"] for cid in ids
            if not per_case[cid]["evidence_sufficiency"].get("not_applicable")
        ]
        sf_rate = None
        if sf:
            sf_ok = sum(1 for s in sf if s["sufficient_postfilter"])
            sf_rate = round(sf_ok / len(sf), 3)
        out.append(f"    sufficiency_postfilter   : {_fmt_float(sf_rate)}  [n={len(sf)}]")

        # Action correctness
        ac = [
            per_case[cid]["action_correctness"] for cid in ids
            if per_case[cid]["action_correctness"].get("applicable", True)
        ]
        ac_ok = sum(1 for s in ac if s["correct"])
        out.append(f"    action_correct           : {_pct(ac_ok, len(ac))}")

        # Precision
        rp = [
            per_case[cid]["relevant_concept_precision"] for cid in ids
            if not per_case[cid]["relevant_concept_precision"].get("not_applicable")
        ]
        rp_mean = _mean([s["precision"] for s in rp])
        out.append(f"    relevant_concept_prec.   : {_fmt_float(rp_mean)}  [n={len(rp)}]")

        cp = [
            per_case[cid]["citation_concept_precision"] for cid in ids
            if not per_case[cid]["citation_concept_precision"].get("not_applicable")
        ]
        cp_mean = _mean([s["precision"] for s in cp])
        out.append(f"    citation_concept_prec.   : {_fmt_float(cp_mean)}  [n={len(cp)}]")

        return out

    lines.extend(_stratum_metrics(mp_ids, "multipart"))
    lines.append("")
    lines.extend(_stratum_metrics(non_mp_ids, "non-multipart"))

    # Secondary probe rate (multipart only)
    mp_probe = [
        per_case[cid]["secondary_probe_check"] for cid in mp_ids
        if per_case[cid].get("secondary_probe_check", {}).get("applicable")
    ]
    mp_probed = sum(1 for s in mp_probe if s["probed"])
    probe_rate = _pct(mp_probed, len(mp_probe)) if mp_probe else "n/a"
    lines.append("")
    lines.append(f"  secondary_probe_rate (multipart): {probe_rate}")

    # Role coerced count (whole set)
    n_coerced = sum(
        1 for s in per_case.values()
        if s.get("secondary_probe_check", {}).get("role_coerced")
    )
    lines.append(f"  role_coerced_count (whole set)  : {n_coerced}")

    # Draft grounding (multipart stratum) — from judge if available
    if judge_evd_draft and judge_evd_draft.get("per_case"):
        d_per_case = judge_evd_draft["per_case"]
        mp_judged = {cid: d_per_case[cid] for cid in mp_ids if cid in d_per_case}
        if mp_judged:
            rulings: dict[str, int] = defaultdict(int)
            for r in mp_judged.values():
                ruling = r.get("ruling", "judge_error")
                rulings[ruling] += 1
            lines.append("")
            lines.append(f"  draft_grounding (multipart, n={len(mp_judged)}):")
            for ruling in ("supports", "partially_supports", "does_not_support", "judge_error"):
                lines.append(f"    {ruling:<24} {rulings.get(ruling, 0):>3}")

    return lines


def _confusion_matrix_section(scored: dict) -> list[str]:
    lines = ["", _hr(), "ACTION CONFUSION MATRIX (rows=ideal, cols=predicted)", _hr()]
    actions = list(PROPOSED_ACTIONS)
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    other_seen: set[str] = set()
    for s in scored["per_case"].values():
        if not s["action_correctness"].get("applicable", True):
            continue  # infra-error cases have no predicted action
        ideal, pred = s["action_correctness"]["cell"]
        matrix[(ideal, pred)] += 1
        if pred not in actions and pred:
            other_seen.add(pred)
        if ideal not in actions and ideal:
            other_seen.add(ideal)

    cols = actions + sorted(other_seen)
    header = "  " + " " * 14 + "".join(f"{c[:12]:>13}" for c in cols)
    lines.append(header)
    for ideal in actions + sorted(o for o in other_seen if o not in actions):
        row = f"  {ideal:<14}"
        for pred in cols:
            row += f"{matrix.get((ideal, pred), 0):>13}"
        lines.append(row)
    return lines


def _failure_mode_section(scored: dict) -> list[str]:
    """
    Derive deterministic failure-mode counts from the per-case scorer outputs.
    Only the modes a deterministic scorer can detect — judge-only modes are
    not tallied here.

    Schema v2: low_conf_with_cite is surfaced in DIAGNOSTIC FLAGS, not here.
    """
    lines = [
        "",
        _hr(),
        "DETERMINISTIC FAILURE-MODE TALLY  (schema v2: low_conf_with_cite is a flag, see DIAGNOSTIC FLAGS section)",
        _hr(),
    ]
    counts: dict[str, list[str]] = defaultdict(list)

    for case_id, s in scored["per_case"].items():
        # cited_irrelevant_patch: any cited chunk_id outside relevant_ids
        cite = s["citation_audit"]
        if cite["out_of_set_ids"]:
            counts["cited_irrelevant_patch"].append(case_id)

        # grounding hard violations only — low_conf_with_cite is now a flag.
        band = s["grounding_band_compliance"]
        if band.get("hard_violation") == "high_confidence_no_citation":
            counts["high_confidence_no_citation"].append(case_id)

        # wrong_action_severity: ideal vs predicted differ in severity rank.
        # Skip cases that errored on infrastructure — there's no predicted
        # action to be wrong about.
        act = s["action_correctness"]
        if act.get("applicable", True) and not act["correct"] and act["ideal"] and act["predicted"]:
            counts["wrong_action_severity"].append(case_id)

    if not counts:
        lines.append("  (no deterministic failure modes triggered)")
        return lines

    for mode in sorted(counts):
        ids = counts[mode]
        lines.append(f"  {mode:<32} {len(ids):>3}   {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}")
    return lines


def _diagnostic_flags_section(scored: dict) -> list[str]:
    """
    Diagnostic flags surfaced by the deterministic scorers but NOT counted as
    failures. These exist so the V1.5 LLM judge can rule on them per-case.

    Currently surfaces: low_conf_with_cite (the responder cited evidence at
    confidence < 0.4 — could be honest hedging or a misleading fix claim;
    judge decides).
    """
    lines = [
        "",
        _hr(),
        "DIAGNOSTIC FLAGS (not failures — surfaced for V1.5 judge inspection)",
        _hr(),
    ]
    flag_cases: dict[str, list[str]] = defaultdict(list)
    for case_id, s in scored["per_case"].items():
        band = s["grounding_band_compliance"]
        flag = band.get("flag")
        if flag:
            flag_cases[flag].append(case_id)

    if not flag_cases:
        lines.append("  (no diagnostic flags triggered)")
        return lines

    for flag in sorted(flag_cases):
        ids = flag_cases[flag]
        label = f"{flag}_flag_count"
        lines.append(
            f"  {label:<32} {len(ids):>3}   {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        )
    return lines


def _judge_grounding_section(judge: dict | None) -> list[str]:
    """
    LLM judge rulings on `low_conf_with_cite` flagged cases (V1.5).

    Each row is a ruling category. The cache hit count is reported ONCE at
    the section header — per-row cache annotations are confusing because
    rows are aggregates, not individual cached objects.
    """
    lines = ["", _hr()]
    if not judge or not judge.get("n_flagged"):
        lines.append("LLM JUDGE — low_conf_with_cite rulings")
        lines.append(_hr())
        lines.append("  (no flagged cases to judge)")
        return lines

    model = judge.get("model", "?")
    n_flagged = judge["n_flagged"]
    n_cached = judge.get("n_from_cache", 0)
    rulings = judge.get("rulings", {})
    per_case = judge.get("per_case", {})

    lines.append(f"LLM JUDGE — low_conf_with_cite rulings  (model: {model})")
    lines.append(f"  cache: {n_cached}/{n_flagged} hit")
    lines.append(_hr())
    lines.append(f"  n_flagged                       {n_flagged:>3}")
    for ruling in ("honest_hedge", "misleading_fix_claim", "unclear"):
        n = rulings.get(ruling, 0)
        # Show case_ids inline for the non-honest_hedge categories so the
        # report points at exactly which drafts the judge flagged.
        if ruling != "honest_hedge" and n > 0:
            ids = [cid for cid, r in per_case.items() if r["ruling"] == ruling]
            id_hint = f"   ← {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        else:
            id_hint = ""
        lines.append(f"  {ruling:<32}{n:>3}{id_hint}")

    if per_case:
        lines.append("")
        lines.append("  Per-case rulings:")
        for case_id in sorted(per_case):
            r = per_case[case_id]
            rationale = r.get("rationale", "")
            if len(rationale) > 80:
                rationale = rationale[:77] + "..."
            lines.append(f"    {case_id:<28} {r['ruling']:<22} {rationale}")
    return lines


def _judge_action_section(judge_action: dict | None) -> list[str]:
    """
    LLM judge rulings on `wrong_action_severity` flagged cases (V1.5).

    Sibling of `_judge_grounding_section`. The four semantic ruling rows
    AND the `judge_error` row are all surfaced — `judge_error > 0` prints
    a ⚠ warning line so judge infrastructure misfires are impossible to
    miss in terminal output.
    """
    lines = ["", _hr()]
    if not judge_action or not judge_action.get("n_flagged"):
        lines.append("LLM JUDGE — wrong_action_severity rulings")
        lines.append(_hr())
        lines.append("  (no flagged cases to judge)")
        return lines

    model = judge_action.get("model", "?")
    n_flagged = judge_action["n_flagged"]
    n_cached = judge_action.get("n_from_cache", 0)
    rulings = judge_action.get("rulings", {})
    per_case = judge_action.get("per_case", {})

    lines.append(f"LLM JUDGE — wrong_action_severity rulings  (model: {model})")
    lines.append(f"  cache: {n_cached}/{n_flagged} hit")
    lines.append(_hr())
    lines.append(f"  n_flagged                       {n_flagged:>3}")
    semantic = ("over_escalation", "missed_escalation", "category_drift", "tolerable_disagreement")
    for ruling in semantic:
        n = rulings.get(ruling, 0)
        if n > 0:
            ids = [cid for cid, r in per_case.items() if r["ruling"] == ruling]
            id_hint = f"   ← {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        else:
            id_hint = ""
        lines.append(f"  {ruling:<32}{n:>3}{id_hint}")

    n_judge_error = rulings.get("judge_error", 0)
    lines.append(f"  {'judge_error':<32}{n_judge_error:>3}")
    if n_judge_error > 0:
        ids = [cid for cid, r in per_case.items() if r["ruling"] == "judge_error"]
        lines.append(
            f"  ⚠ judge infrastructure misfired on {n_judge_error} case(s): "
            f"{', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        )

    if per_case:
        lines.append("")
        lines.append("  Per-case rulings:")
        for case_id in sorted(per_case):
            r = per_case[case_id]
            rationale = r.get("rationale", "")
            if len(rationale) > 80:
                rationale = rationale[:77] + "..."
            lines.append(f"    {case_id:<28} {r['ruling']:<22} {rationale}")
    return lines


def _pairwise_section(pairwise: dict | None) -> list[str]:
    """
    LLM judge rulings on revision-loop improvement (V1.5).

    Sibling of `_judge_action_section`. Surfaces the three semantic ruling
    rows AND `judge_error` separately. Also reports `n_deterministic` —
    how many of the revision_neutral rulings came from the normalize-equal
    shortcut rather than the LLM. A spike in n_deterministic means the
    responder is increasingly being a no-op revision-loop tax; a drop means
    the responder is changing more drafts (good or bad — check the rulings).
    """
    lines = ["", _hr()]
    if not pairwise or not pairwise.get("n_judged"):
        lines.append("LLM JUDGE — revision improvement pairwise")
        lines.append(_hr())
        lines.append("  (no multi-iteration approved cases to judge)")
        return lines

    model = pairwise.get("model", "?")
    n_judged = pairwise["n_judged"]
    n_cached = pairwise.get("n_from_cache", 0)
    n_deterministic = pairwise.get("n_deterministic", 0)
    rulings = pairwise.get("rulings", {})
    per_case = pairwise.get("per_case", {})

    lines.append(f"LLM JUDGE — revision improvement pairwise  (model: {model})")
    lines.append(f"  cache: {n_cached}/{n_judged} hit  |  deterministic shortcut: {n_deterministic}/{n_judged}")
    lines.append(_hr())
    lines.append(f"  n_judged                        {n_judged:>3}")
    semantic = ("revision_improved", "revision_neutral", "revision_regressed")
    for ruling in semantic:
        n = rulings.get(ruling, 0)
        if n > 0:
            ids = [cid for cid, r in per_case.items() if r["ruling"] == ruling]
            id_hint = f"   ← {', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        else:
            id_hint = ""
        lines.append(f"  {ruling:<32}{n:>3}{id_hint}")

    n_judge_error = rulings.get("judge_error", 0)
    lines.append(f"  {'judge_error':<32}{n_judge_error:>3}")
    if n_judge_error > 0:
        ids = [cid for cid, r in per_case.items() if r["ruling"] == "judge_error"]
        lines.append(
            f"  ⚠ judge infrastructure misfired on {n_judge_error} case(s): "
            f"{', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
        )

    if per_case:
        lines.append("")
        lines.append("  Per-case rulings:")
        for case_id in sorted(per_case):
            r = per_case[case_id]
            rationale = r.get("rationale", "")
            if len(rationale) > 80:
                rationale = rationale[:77] + "..."
            det_tag = " [det]" if r.get("deterministic") else ""
            lines.append(f"    {case_id:<28} {r['ruling']:<22}{det_tag} {rationale}")
    return lines


def _retrieval_judges_section(
    scored: dict,
    records: list[dict],
    judge_evd_gold: dict | None,
    judge_evd_draft: dict | None,
) -> list[str]:
    """
    Split retrieval judges. Two parallel rows showing distribution
    plus the dropout chain (predicate → judged), then a disagreement
    bucket subsection.

    Each judge row format:
        supports K  partial K  no_support K  judge_error K  [n_judged=K, predicate=P/M, judge_pool=M]

    M = judge-eligible pool: cases the agent successfully ran retrieval for
    (non-empty relevant_ids). This is intentionally BROADER than the
    deterministic retrieval-eligible base (cases with must_include_chunk_ids),
    because the judges depend only on relevant_ids + notes/draft, not on
    deterministic annotations. A case can be judged without ever having a
    gold chunk slot, and excluding those would silently shrink the judge
    base. The deterministic-eligible (must_include) base is still reported
    in the layered retrieval section above.
    """
    lines = ["", _hr(), "RETRIEVAL JUDGES (split: gold vs draft)", _hr()]

    if not judge_evd_gold and not judge_evd_draft:
        lines.append("  (retrieval judges not run for this report)")
        return lines

    # M = judge-eligible pool: records where the agent produced non-empty
    # relevant_ids. The judges' per-case predicates are strict subsets of
    # this base, so P ≤ M always.
    M = sum(
        1 for r in records
        if r.get("ok")
        and ((r.get("result") or {}).get("evidence_package") or {}).get("relevant_ids")
    )

    def _judge_row(label: str, j: dict | None) -> list[str]:
        out: list[str] = []
        if not j:
            out.append(f"  {label:<32} (not run)")
            return out
        rulings = j.get("rulings", {}) or {}
        n_judged = j.get("n_judged", 0)
        n_pred = j.get("n_predicate_eligible", 0)
        n_cached = j.get("n_from_cache", 0)
        n_err = rulings.get("judge_error", 0)
        out.append(
            f"  {label:<32} supports {rulings.get('supports', 0):>2}  "
            f"partial {rulings.get('partially_supports', 0):>2}  "
            f"no_support {rulings.get('does_not_support', 0):>2}  "
            f"judge_error {n_err:>2}    "
            f"[n_judged={n_judged}, predicate={n_pred}/{M}, judge_pool={M}, cache {n_cached}/{n_judged}]"
        )
        if n_err > 0:
            ids = [
                cid for cid, r in (j.get("per_case", {}) or {}).items()
                if r.get("ruling") == "judge_error"
            ]
            out.append(
                f"  ⚠ judge infrastructure misfired on {n_err} case(s): "
                f"{', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"
            )
        return out

    model = (judge_evd_gold or judge_evd_draft or {}).get("model", "?")
    lines.append(f"  model: {model}")
    lines.extend(_judge_row("pool sufficiency", judge_evd_gold))
    lines.extend(_judge_row("draft grounding", judge_evd_draft))

    # Disagreement bucket subsection — both judges must have run on a case.
    g_per_case = (judge_evd_gold or {}).get("per_case", {}) or {}
    d_per_case = (judge_evd_draft or {}).get("per_case", {}) or {}
    both_ids = [
        cid for cid in g_per_case
        if cid in d_per_case
        and g_per_case[cid].get("ruling") != "judge_error"
        and d_per_case[cid].get("ruling") != "judge_error"
    ]
    n_both = len(both_ids)
    if n_both:
        n_g_sup_d_no = 0
        n_g_no_d_sup = 0
        n_other = 0
        n_agree = 0
        g_sup_d_no_ids: list[str] = []
        g_no_d_sup_ids: list[str] = []
        for cid in both_ids:
            g = g_per_case[cid]["ruling"]
            d = d_per_case[cid]["ruling"]
            if g == d:
                n_agree += 1
            elif g == "supports" and d == "does_not_support":
                n_g_sup_d_no += 1
                g_sup_d_no_ids.append(cid)
            elif g == "does_not_support" and d == "supports":
                n_g_no_d_sup += 1
                g_no_d_sup_ids.append(cid)
            else:
                n_other += 1
        n_total_disagree = n_both - n_agree
        lines.append("")
        lines.append("  retrieval-vs-draft disagreement (per case, both judges ran):")
        lines.append(f"    n_with_both_judges                             : {n_both:>3}")
        lines.append(
            f"    gold=supports         & draft=does_not_support : {n_g_sup_d_no:>3}"
            + (f"   ← {', '.join(g_sup_d_no_ids[:5])}" if g_sup_d_no_ids else "")
            + "   (responder over-claims against good evidence)"
        )
        lines.append(
            f"    gold=does_not_support & draft=supports         : {n_g_no_d_sup:>3}"
            + (f"   ← {', '.join(g_no_d_sup_ids[:5])}" if g_no_d_sup_ids else "")
            + "   (responder under-uses / claims-without-basis)"
        )
        lines.append(f"    other_disagreement                             : {n_other:>3}")
        lines.append(f"    total disagreements                            : {n_total_disagree:>3} / {n_both}")

    return lines


def _critic_health_section(scored: dict) -> list[str]:
    ch = scored["batch"]["critic_health"]
    lines = ["", _hr(), "CRITIC HEALTH (from audit_log_iterations)", _hr()]
    lines.append(f"  Runs joined:               {ch['n_runs_with_audit']}")
    lines.append(f"  Total iterations:          {ch['total_iterations']}")
    lines.append(f"  Approval rate (overall):   {_fmt_float(ch['approval_rate_overall'])}")
    lines.append(f"  Mean iters → approval:     {_fmt_float(ch['mean_iterations_to_approval'])}")
    lines.append(f"  Runs reaching approval:    {ch['n_runs_reaching_approval']}/{ch['n_runs_with_audit']}")
    if ch["approval_rate_by_iteration"]:
        lines.append(f"  Approval rate by iteration:")
        for i in sorted(ch["approval_rate_by_iteration"]):
            lines.append(f"    iter {i}: {_fmt_float(ch['approval_rate_by_iteration'][i])}")
    if ch["rejections_by_reason_type"]:
        lines.append(f"  Rejections by reason_type:")
        for rt, n in sorted(ch["rejections_by_reason_type"].items()):
            lines.append(f"    {rt:<12} {n}")
    return lines


def _gating_section(gating: dict) -> list[str]:
    lines = ["", _hr(), "GATING ACCURACY (Investigator _should_retrieve gate)", _hr()]
    conf = gating["confusion"]
    lines.append(f"  n_scored:           {gating['n_scored']}")
    lines.append(f"  Accuracy:           {_fmt_float(gating['accuracy'])}")
    lines.append(f"  False-skip rate:    {_fmt_float(gating['false_skip_rate'])}   "
                 f"(substantive complaints the gate dumped — gate_misjudged)")
    lines.append(f"  False-retrieve rate:{_fmt_float(gating['false_retrieve_rate'])}   "
                 f"(junk reviews the gate sent through retrieval)")
    lines.append("")
    lines.append(f"  Confusion matrix:")
    lines.append(f"                       actual_skip   actual_retrieve")
    lines.append(f"    expected_skip      {conf['true_skip']:>11}   {conf['false_retrieve']:>15}")
    lines.append(f"    expected_retrieve  {conf['false_skip']:>11}   {conf['true_retrieve']:>15}")
    if gating["false_skip_cases"]:
        lines.append(f"  ⚠ false_skip cases:    {', '.join(gating['false_skip_cases'])}")
    if gating["false_retrieve_cases"]:
        lines.append(f"  ⚠ false_retrieve cases:{', '.join(gating['false_retrieve_cases'])}")
    if gating["unknown_cases"]:
        lines.append(f"  ? unknown cases:       {', '.join(gating['unknown_cases'])}")
    return lines


# ---------- Public entry point --------------------------------------------

def build_report(
    cases: list[dict],
    records: list[dict],
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None = None,
    judge_action: dict[str, Any] | None = None,
    pairwise: dict[str, Any] | None = None,
    judge_evd_gold: dict[str, Any] | None = None,
    judge_evd_draft: dict[str, Any] | None = None,
) -> str:
    """
    Assemble the full stratified report as a single string.

    Args:
        cases:        list of golden.json case dicts
        records:      list of run_evals output records
        scored:       output of deterministic.score_records(cases, records)
        gating:       output of gating_accuracy.gating_accuracy_batch(cases, records)
        judge:        output of judge_grounding.judge_grounding_batch(...) — None
                      means the grounding judge layer was not run for this report
        judge_action: output of judge_action.judge_action_batch(...) — None
                      means the action judge layer was not run for this report
        pairwise:     output of pairwise.pairwise_batch(...) — None
                      means the pairwise judge layer was not run for this report
    """
    sections = [
        _overall_section(records, scored),
        _per_category_section(cases, scored),
        _layered_retrieval_section(scored),
        _multipart_stratum_section(cases, scored, judge_evd_draft),
        _confusion_matrix_section(scored),
        _failure_mode_section(scored),
        _diagnostic_flags_section(scored),
        _judge_grounding_section(judge),
        _judge_action_section(judge_action),
        _pairwise_section(pairwise),
        _retrieval_judges_section(scored, records, judge_evd_gold, judge_evd_draft),
        _critic_health_section(scored),
        _gating_section(gating),
    ]
    body = "\n".join(line for sec in sections for line in sec)
    return body + "\n" + _hr() + "\n"


def print_report(
    cases: list[dict],
    records: list[dict],
    scored: dict[str, Any],
    gating: dict[str, Any],
    judge: dict[str, Any] | None = None,
    judge_action: dict[str, Any] | None = None,
    pairwise: dict[str, Any] | None = None,
    judge_evd_gold: dict[str, Any] | None = None,
    judge_evd_draft: dict[str, Any] | None = None,
) -> None:
    print(build_report(
        cases, records, scored, gating, judge, judge_action, pairwise,
        judge_evd_gold, judge_evd_draft,
    ))
