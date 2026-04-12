"""
evals/scorers/gating_accuracy.py — Gate-decision scorer.

The Investigator's deterministic `_should_retrieve` gate skips retrieval
when classifier_category not in RETRIEVAL_CATEGORIES (i.e., for `other`).
This scorer compares that decision against the golden set's annotated
`gate_should_skip` and surfaces the confusion matrix:

  expected \\ actual    skipped         retrieved
  should_skip=true     true_skip       false_retrieve
  should_skip=false    false_skip*     true_retrieve

  *false_skip is the load-bearing failure: the gate dumped a substantive
   complaint into "no retrieval" because the classifier mislabeled it `other`
   (or something equally lossy). This is the `gate_misjudged` failure mode.

Per-case scorer takes (case, result). Batch scorer takes the full record list.
"""

from __future__ import annotations


# "insufficient" maps to False (not None) because the gate DID run retrieval —
# Self-RAG just gave up afterward.
def _gate_skipped(retrieval_decision: str | None) -> bool | None:
    if not retrieval_decision:
        return None
    if retrieval_decision == "skipped":
        return True
    if retrieval_decision in ("retrieved", "insufficient"):
        return False
    return None


def gating_accuracy(case: dict, result: dict) -> dict:
    """
    Per-case gate-decision tag.

    Returns:
      {
        "expected_skip": bool,
        "actual_skip":   bool | None,
        "outcome":       "true_skip" | "true_retrieve" | "false_skip" | "false_retrieve" | "unknown",
        "correct":       bool,
        "category":      annotated category (for stratification)
      }
    """
    expected_skip = bool(case.get("gate_should_skip", False))
    decision = (result.get("evidence_package") or {}).get("retrieval_decision")
    actual_skip = _gate_skipped(decision)

    if actual_skip is None:
        return {
            "expected_skip": expected_skip,
            "actual_skip": None,
            "outcome": "unknown",
            "correct": False,
            "category": case.get("annotated_category", ""),
        }

    if expected_skip and actual_skip:
        outcome = "true_skip"
    elif not expected_skip and not actual_skip:
        outcome = "true_retrieve"
    elif expected_skip and not actual_skip:
        outcome = "false_retrieve"  # gate ran retrieval on junk
    else:  # not expected_skip and actual_skip
        outcome = "false_skip"      # gate dumped a substantive complaint

    return {
        "expected_skip": expected_skip,
        "actual_skip": actual_skip,
        "outcome": outcome,
        "correct": outcome.startswith("true_"),
        "category": case.get("annotated_category", ""),
    }


def gating_accuracy_batch(cases: list[dict], records: list[dict]) -> dict:
    """
    Batch-level gate accuracy. Computes the 2x2 confusion matrix and the
    derived rates that the reporter prints.

    Returns:
      {
        "n_cases":         int,
        "n_scored":        int,                 # excludes ok=False / unknown decisions
        "confusion": {
          "true_skip":     int,
          "true_retrieve": int,
          "false_skip":    int,                 # gate_misjudged from the input side
          "false_retrieve":int,                 # responded_to_junk territory
          "unknown":       int,
        },
        "accuracy":        float,
        "false_skip_rate":     float | None,    # FN / (FN + TP) for "should retrieve" cases
        "false_retrieve_rate": float | None,    # FP / (FP + TN) for "should skip" cases
        "false_skip_cases":     [case_id, ...], # for the reporter to drill into
        "false_retrieve_cases": [case_id, ...],
        "unknown_cases":        [case_id, ...],
      }
    """
    case_by_id = {c["case_id"]: c for c in cases}
    counts = {
        "true_skip": 0,
        "true_retrieve": 0,
        "false_skip": 0,
        "false_retrieve": 0,
        "unknown": 0,
    }
    false_skip_cases: list[str] = []
    false_retrieve_cases: list[str] = []
    unknown_cases: list[str] = []
    n_scored = 0

    for record in records:
        if not record.get("ok"):
            continue
        case = case_by_id.get(record["case_id"])
        if case is None:
            continue
        scored = gating_accuracy(case, record["result"])
        outcome = scored["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "false_skip":
            false_skip_cases.append(record["case_id"])
        elif outcome == "false_retrieve":
            false_retrieve_cases.append(record["case_id"])
        elif outcome == "unknown":
            unknown_cases.append(record["case_id"])
        if outcome != "unknown":
            n_scored += 1

    # Rates over the relevant slice (skip the unknowns)
    should_retrieve_total = counts["true_retrieve"] + counts["false_skip"]
    should_skip_total = counts["true_skip"] + counts["false_retrieve"]
    false_skip_rate = (
        round(counts["false_skip"] / should_retrieve_total, 3)
        if should_retrieve_total
        else None
    )
    false_retrieve_rate = (
        round(counts["false_retrieve"] / should_skip_total, 3)
        if should_skip_total
        else None
    )
    correct = counts["true_skip"] + counts["true_retrieve"]
    accuracy = round(correct / n_scored, 3) if n_scored else None

    return {
        "n_cases": len(records),
        "n_scored": n_scored,
        "confusion": counts,
        "accuracy": accuracy,
        "false_skip_rate": false_skip_rate,
        "false_retrieve_rate": false_retrieve_rate,
        "false_skip_cases": false_skip_cases,
        "false_retrieve_cases": false_retrieve_cases,
        "unknown_cases": unknown_cases,
    }
