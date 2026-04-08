"""
evals/reporter.py — Stratified terminal report (M5 Step 7).

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

    header = f"  {'category':<22} {'n':>3}  {'action✓':>10}  {'recall@k':>10}  {'cite⊆rel':>10}  {'bandH✓':>8}  {'lc⚑':>4}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for cat in sorted(grouped):
        rows = grouped[cat]
        n = len(rows)
        # Exclude infrastructure-error cases from action denominator only.
        action_rows = [s for _, s in rows if s["action_correctness"].get("applicable", True)]
        n_action_evaluable = len(action_rows)
        n_act = sum(1 for s in action_rows if s["action_correctness"]["correct"])
        recall_vals = [s["recall_at_k"]["recall"] for _, s in rows if s["recall_at_k"]["recall"] is not None]
        recall_avg = sum(recall_vals) / len(recall_vals) if recall_vals else None
        n_cite_ok = sum(1 for _, s in rows if s["citation_audit"]["subset_ok"])
        # bandH✓ counts only HARD band violations (compliant=True under v2 schema).
        n_band_ok = sum(1 for _, s in rows if s["grounding_band_compliance"]["compliant"])
        # lc⚑ is the diagnostic flag count (low_conf_with_cite); not a failure.
        n_lc_flag = sum(
            1 for _, s in rows
            if s["grounding_band_compliance"].get("flag") == "low_conf_with_cite"
        )
        lines.append(
            f"  {cat:<22} {n:>3}  {_pct(n_act, n_action_evaluable):>10}  {_fmt_float(recall_avg, 2):>10}  {_pct(n_cite_ok, n):>10}  {_pct(n_band_ok, n):>8}  {n_lc_flag:>4}"
        )
    return lines


def _confusion_matrix_section(scored: dict) -> list[str]:
    lines = ["", _hr(), "ACTION CONFUSION MATRIX (rows=ideal, cols=predicted)", _hr()]
    actions = ["no_action", "monitor", "investigate", "escalate"]
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
        # OR explicitly forbidden_cited
        cite = s["citation_audit"]
        if cite["out_of_set_ids"] or cite["forbidden_cited"]:
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
        _confusion_matrix_section(scored),
        _failure_mode_section(scored),
        _diagnostic_flags_section(scored),
        _judge_grounding_section(judge),
        _judge_action_section(judge_action),
        _pairwise_section(pairwise),
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
) -> None:
    print(build_report(cases, records, scored, gating, judge, judge_action, pairwise))
