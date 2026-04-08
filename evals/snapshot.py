"""
evals/snapshot.py — Versioned eval snapshots (M5 Step 8).

A snapshot is a small JSON file that captures the aggregate metrics from one
eval run, plus the git SHA of the code that produced them. Two snapshots
taken at different commits can be diffed to answer "did this change move
the numbers, and in which direction?"

Files land in evals/snapshots/snapshot_<timestamp>.json (gitignored — see
D7 for the snapshots_archive/ pattern used at milestones).

Public surface:
  - write_snapshot(scored, gating, records, run_file, filters) -> Path
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
#
#   v1: original M5 schema (had grounding.compliant_rate as a single rate
#       that conflated low_conf_with_cite + high_conf_no_cite into one
#       "violations" bucket).
#   v2: low_conf_with_cite demoted to a diagnostic flag (not a failure).
#       grounding.compliant_rate replaced with grounding.n_hard_violations
#       (count, not rate). New diagnostics.low_conf_with_cite_flag_count.
SNAPSHOT_SCHEMA_VERSION = 2

# Metrics surfaced in the snapshot summary AND used for the one-line diff.
# Order is preserved in the diff output. Each entry is a dotted path into
# the snapshot["aggregates"] dict (resolved by _get_path).
DIFF_METRICS: list[tuple[str, str, str]] = [
    # (label, dotted_path, format_spec)
    ("action_correct_rate",          "action.correct_rate",                ".3f"),
    ("recall_at_k_mean",             "retrieval.recall_at_k_mean",         ".3f"),
    ("citation_subset_ok_rate",      "citation.subset_ok_rate",            ".3f"),
    # Schema v2: grounding.compliant_rate (a rate that mixed two different
    # things) replaced by an explicit count of HARD violations only, plus a
    # parallel diagnostic count for the low_conf_with_cite flag.
    ("grounding_band_hard_violations","grounding.n_hard_violations",       "d"),
    ("low_conf_with_cite_flag_count","diagnostics.low_conf_with_cite_flag_count", "d"),
    ("critic_approval_overall",      "critic_health.approval_rate_overall",".3f"),
    ("critic_iter0_approval",        "critic_health.iter0_approval",       ".3f"),
    ("gating_accuracy",              "gating.accuracy",                    ".3f"),
    ("gating_false_skip_rate",       "gating.false_skip_rate",             ".3f"),
    ("gating_false_retrieve_rate",   "gating.false_retrieve_rate",         ".3f"),
    ("total_tokens",                 "cost.total_tokens",                  ",d"),
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


# ---------- Aggregate builder ---------------------------------------------

def _build_aggregates(
    scored: dict[str, Any],
    gating: dict[str, Any],
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

    # Recall @ k (only over cases with non-empty must_include)
    recall_vals = [
        s["recall_at_k"]["recall"] for s in per_case.values()
        if s["recall_at_k"]["recall"] is not None
    ]
    recall_mean = (sum(recall_vals) / len(recall_vals)) if recall_vals else None

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
        if cite["out_of_set_ids"] or cite["forbidden_cited"]:
            failure_modes["cited_irrelevant_patch"] += 1
        hv = s["grounding_band_compliance"].get("hard_violation")
        if hv:
            failure_modes[hv] += 1
        act = s["action_correctness"]
        # Skip infra-error cases — no predicted action to be wrong about.
        if act.get("applicable", True) and not act["correct"] and act["ideal"] and act["predicted"]:
            failure_modes["wrong_action_severity"] += 1

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
            "correct_rate": action_correct_rate,
        },
        "retrieval": {
            "recall_at_k_mean": recall_mean,
            "n_with_must_include": len(recall_vals),
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
        "critic_health": {
            "approval_rate_overall": ch["approval_rate_overall"],
            "iter0_approval": iter0,
            "mean_iters_to_approval": ch["mean_iterations_to_approval"],
            "rejections_by_reason_type": ch["rejections_by_reason_type"],
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
    }


# ---------- Read / write --------------------------------------------------

def write_snapshot(
    scored: dict[str, Any],
    gating: dict[str, Any],
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
        "aggregates": _build_aggregates(scored, gating, records),
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
        lines.append(
            f"  ⚠ schema_version changed: {prev_v} → {curr_v} "
            f"(grounding metric split: low_conf_with_cite is now a flag, not a violation)"
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
