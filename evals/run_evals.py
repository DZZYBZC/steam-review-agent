"""
evals/run_evals.py — Eval runner.

Loads cases from evals/test_sets/golden.json, runs each through the agent
graph end-to-end with auto-approval at the human gate, and writes raw
results to evals/runs/run_<timestamp>.json for downstream scorers.

Usage:
    python evals/run_evals.py                       # full golden set
    python evals/run_evals.py --quick               # quick subset only
    python evals/run_evals.py --app-id 1716740      # filter by app
    python evals/run_evals.py --category technical_issues
    python evals/run_evals.py --case-id mhw_tech_002
    python evals/run_evals.py --workers 10          # parallel execution
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

# Allow `python evals/run_evals.py` from the repo root by ensuring the repo
# root is on sys.path before importing first-party modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.graph import build_graph
from agent.state import AgentState
from evals.reporter import print_report
from evals.scorers.deterministic import score_records
from evals.scorers.gating_accuracy import gating_accuracy_batch
from evals.scorers.judge_action import judge_action_batch
from evals.scorers.judge_grounding import judge_grounding_batch
from evals.scorers.judge_retrieval import (
    judge_draft_grounding_batch,
    judge_pool_sufficiency_batch,
)
from evals.scorers.pairwise import pairwise_batch
from evals.snapshot import diff_snapshots, load_latest_snapshot, write_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).parent
GOLDEN_PATH = EVALS_DIR / "test_sets" / "golden.json"
REGRESSION_PATH = EVALS_DIR / "test_sets" / "regression.json"
RUNS_DIR = EVALS_DIR / "runs"

TERMINAL_STOP_REASONS = {
    "human_approved",
    "max_iterations_reached",
    "llm_error",
    "parse_error",
    "no_response_needed",
}

MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 5


def load_cases(
    quick: bool = False,
    app_id: str | None = None,
    category: str | None = None,
    case_id: str | None = None,
) -> list[dict]:
    """
    Load and filter cases. Merges golden.json with regression.json seeds:
    each regression seed references a case_id; if that case_id is present
    in golden.json (the common case today), no-op. If it is NOT in golden
    — meaning a case was dropped from the golden set since the seed was
    locked — log a warning so the orphaned regression seed is visible.
    The regression seeds are how we guard the golden set from silently
    losing cases that were locked as failure patterns we must not regress.
    """
    if not GOLDEN_PATH.exists():
        raise FileNotFoundError(f"Golden set not found at {GOLDEN_PATH}")

    with GOLDEN_PATH.open() as f:
        data = json.load(f)
    cases: list[dict] = data.get("cases", [])

    # Merge regression seeds. Today every seed has in_golden=true so this is
    # a no-op on the merged list, but the loader now surfaces drift loudly.
    if REGRESSION_PATH.exists():
        with REGRESSION_PATH.open() as f:
            regression_data = json.load(f)
        seed_ids = {s["case_id"] for s in regression_data.get("seeds", [])}
        case_ids = {c.get("case_id") for c in cases}
        orphaned = sorted(seed_ids - case_ids)
        if orphaned:
            logger.warning(
                "regression.json seed(s) reference case_id(s) NOT in golden.json: "
                f"{orphaned}. These regression patterns are no longer being "
                "exercised. Either re-add the cases to golden.json or remove "
                "the seeds."
            )

    if case_id:
        cases = [c for c in cases if c.get("case_id") == case_id]
    if quick:
        cases = [c for c in cases if c.get("quick") is True]
    if app_id:
        cases = [c for c in cases if c.get("app_id") == app_id]
    if category:
        cases = [
            c for c in cases
            if c.get("annotated_category") == category
            or c.get("classifier_category") == category
        ]

    return cases


def build_initial_state(case: dict) -> AgentState:
    """Build the AgentState entry shape for a golden-set case."""
    return {
        "app_id": case["app_id"],
        "review_id": case["review_id"],
        "review_text": case["review_text"],
        "cluster_summary": {
            # Use the classifier's category, not the annotated one — the agent
            # sees what the production classifier produced. The annotated
            # category is the eval's ground truth, not graph input.
            "category": case.get("classifier_category", "other"),
            "total_reviews": 1,
            "priority_score": 0.0,
            # For multipart reviews, pass pre-annotated secondary aspects from
            # golden.json. These are the eval source of truth, independent of
            # live classifier output.
            "secondary_aspects": case.get("secondary_aspects", []),
        },
        "review_tone": "",
        "iteration_count": 0,
        "approved": False,
        "revision_reason": "",
        "reason_type": "",
        "retrieval_hint": "",
        "stop_reason": "",
        "run_id": "",
        "evidence_package": {},
        "drafted_response": "",
        "proposed_action": "",
        "source_ids_cited": [],
        "critique": "",
        "node_log": [],
        "token_usage": {},
        "human_decision": "",
        "human_feedback": "",
        "human_action_override": "",
        "frozen_action": "",
        "action_freeze_applied": False,
        "action_override_count": 0,
        "first_override_at_iteration": -1,
    }


def run_case(app, case: dict) -> dict:
    """
    Run a single case through the graph, auto-approving at the human gate.

    Returns the final state dict (LangGraph result), augmented with
    elapsed_seconds and any error info.
    """
    state = build_initial_state(case)
    thread_config = {"configurable": {"thread_id": f"eval-{case['case_id']}"}}
    started = time.time()

    try:
        result = app.invoke(state, config=thread_config)

        # Auto-approve loop — bound by AGENT_MAX_ITERATIONS via the graph; we add a hard cap as a
        # safety net in case interrupt cycles ever exceed expected count.
        gate_passes = 0
        while result.get("stop_reason") not in TERMINAL_STOP_REASONS:
            gate_passes += 1
            if gate_passes > 10:
                logger.warning(
                    f"[{case['case_id']}] Exceeded 10 human-gate passes; aborting."
                )
                break
            app.update_state(thread_config, {"human_decision": "approved"})
            result = app.invoke(None, config=thread_config)

        elapsed = time.time() - started
        return {
            "case_id": case["case_id"],
            "ok": True,
            "elapsed_seconds": round(elapsed, 2),
            "result": _serialize_result(result),
        }

    except Exception as e:
        elapsed = time.time() - started
        logger.exception(f"[{case['case_id']}] Run failed: {e}")
        return {
            "case_id": case["case_id"],
            "ok": False,
            "elapsed_seconds": round(elapsed, 2),
            "error": f"{type(e).__name__}: {e}",
            "result": None,
        }


def _serialize_result(result: dict) -> dict:
    """
    Pull the fields scorers actually need out of the LangGraph result.
    Keeps the runs JSON small and stable across state-shape changes.
    """
    return {
        "run_id": result.get("run_id", ""),
        "stop_reason": result.get("stop_reason", ""),
        "iteration_count": result.get("iteration_count", 0),
        "approved": result.get("approved", False),
        "review_tone": result.get("review_tone", ""),
        "evidence_package": result.get("evidence_package", {}),
        "drafted_response": result.get("drafted_response", ""),
        "proposed_action": result.get("proposed_action", ""),
        "source_ids_cited": result.get("source_ids_cited", []),
        "critique": result.get("critique", ""),
        "revision_reason": result.get("revision_reason", ""),
        "reason_type": result.get("reason_type", ""),
        "node_log": result.get("node_log", []),
        "token_usage": result.get("token_usage", {}),
        "human_decision": result.get("human_decision", ""),
        "frozen_action": result.get("frozen_action", ""),
        "action_freeze_applied": result.get("action_freeze_applied", False),
        "action_override_count": result.get("action_override_count", 0),
        "first_override_at_iteration": result.get("first_override_at_iteration", -1),
    }


def write_run_file(records: list[dict], filters: dict) -> Path:
    """Write the raw run records to evals/runs/run_<timestamp>.json."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RUNS_DIR / f"run_{timestamp}.json"
    payload = {
        "timestamp": timestamp,
        "filters": filters,
        "case_count": len(records),
        "records": records,
    }
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Run the eval harness over golden.json cases.")
    parser.add_argument("--quick", action="store_true", help="Only run cases marked quick: true")
    parser.add_argument("--app-id", type=str, default=None, help="Filter by Steam app id")
    parser.add_argument("--category", type=str, default=None, help="Filter by annotated/classifier category")
    parser.add_argument("--case-id", type=str, default=None, help="Run a single case by case_id")
    parser.add_argument("--workers", type=int, default=10, help="Parallel workers for case execution (default: 10)")
    args = parser.parse_args()

    filters = {
        "quick": args.quick,
        "app_id": args.app_id,
        "category": args.category,
        "case_id": args.case_id,
    }

    cases = load_cases(
        quick=args.quick,
        app_id=args.app_id,
        category=args.category,
        case_id=args.case_id,
    )

    if not cases:
        logger.error("No cases matched the filters. Nothing to run.")
        return

    logger.info(f"Loaded {len(cases)} case(s) from {GOLDEN_PATH.name}")
    logger.info("Building agent graph...")
    app = build_graph()

    # Best-effort warmup of the retrieval lazy-loaders (sentence-transformer
    # embedder, Gemma reranker, ChromaDB client) on the main thread,
    # so worker threads don't race on first init. Performance-only — never
    # fatal. If warmup fails for any reason, the first real case pays the
    # lazy-load cost itself.
    try:
        from pipeline.retrieve import retrieve as _warmup_retrieve
        _warmup_retrieve(query_text="warmup", app_id=cases[0]["app_id"])
        logger.info("Retrieval models warmed.")
    except Exception as e:
        logger.warning(f"Retrieval warmup failed (non-fatal): {type(e).__name__}: {e}")

    workers = max(1, args.workers)
    logger.info(f"Running {len(cases)} case(s) with workers={workers}")

    records: list[dict | None] = [None] * len(cases)
    n_ok = 0
    n_err = 0
    completed = 0

    def _is_retryable(record: dict) -> bool:
        """A run is retryable if it threw an exception OR the graph terminated with llm_error."""
        if not record["ok"]:
            return True
        stop = (record.get("result") or {}).get("stop_reason", "")
        return stop == "llm_error"

    def _run_with_retry(idx_case):
        idx, case = idx_case
        for attempt in range(1, MAX_RETRIES + 2):  # 1-indexed, up to MAX_RETRIES+1
            record = run_case(app, case)
            if not _is_retryable(record):
                return idx, record
            # Transient failure — retry unless exhausted
            if attempt <= MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                reason = record.get("error") or f"stop_reason={record['result']['stop_reason']}"
                logger.warning(
                    f"[{case['case_id']}] Attempt {attempt} failed: {reason}. "
                    f"Retrying in {wait}s ({MAX_RETRIES - attempt + 1} retries left)..."
                )
                time.sleep(wait)
            else:
                reason = record.get("error") or f"stop_reason={record['result']['stop_reason']}"
                logger.error(
                    f"[{case['case_id']}] All {MAX_RETRIES + 1} attempts failed: {reason}"
                )
                return idx, record
        return idx, record  # unreachable, but satisfies type checkers

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_run_with_retry, (i, case)): i
            for i, case in enumerate(cases)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx, record = future.result()
            records[idx] = record
            completed += 1
            case = cases[idx]
            if record["ok"]:
                n_ok += 1
                r = record["result"]
                logger.info(
                    f"[{completed}/{len(cases)}] {case['case_id']} "
                    f"(app={case['app_id']}, category={case.get('annotated_category', '?')}) "
                    f"-> stop={r['stop_reason']} action={r['proposed_action'] or '-'} "
                    f"iters={r['iteration_count']} elapsed={record['elapsed_seconds']}s"
                )
            else:
                n_err += 1
                logger.info(
                    f"[{completed}/{len(cases)}] {case['case_id']} -> ERROR: {record['error']}"
                )

    # All futures completed — records is now order-aligned with cases.
    records = [r for r in records if r is not None]

    out_path = write_run_file(records, filters)

    # Score and report. Reporter handles its own headers — we only print the
    # raw-output path and snapshot info afterward.
    scored = score_records(cases, records)
    gating = gating_accuracy_batch(cases, records)
    judge = judge_grounding_batch(
        cases, records, scored, run_file_basename=out_path.stem
    )
    judge_action = judge_action_batch(
        cases, records, scored, run_file_basename=out_path.stem
    )
    pairwise = pairwise_batch(cases, records, run_file_basename=out_path.stem)
    judge_evd_gold = judge_pool_sufficiency_batch(
        cases, records, run_file_basename=out_path.stem
    )
    judge_evd_draft = judge_draft_grounding_batch(
        cases, records, run_file_basename=out_path.stem
    )
    print()
    print_report(
        cases, records, scored, gating, judge, judge_action, pairwise,
        judge_evd_gold, judge_evd_draft,
    )

    # Snapshot. Look up the previous snapshot BEFORE writing the new one so
    # we don't compare it against itself.
    prev_snapshot = load_latest_snapshot()
    snap_path = write_snapshot(
        scored, gating, judge, judge_action, pairwise,
        judge_evd_gold, judge_evd_draft,
        records, run_file=out_path, filters=filters,
    )
    print(f"Raw run file: {out_path}")
    print(f"Snapshot:     {snap_path}")
    if prev_snapshot is not None:
        with snap_path.open() as f:
            curr_snapshot = json.load(f)
        diff_lines = diff_snapshots(prev_snapshot, curr_snapshot)
        print(f"\nDiff vs previous snapshot ({prev_snapshot.get('timestamp')}, sha={prev_snapshot.get('git_sha')}):")
        if diff_lines:
            for line in diff_lines:
                print(line)
        else:
            print("  (no metric changes)")
    else:
        print("\n(no previous snapshot — this is the first run)")


if __name__ == "__main__":
    main()
