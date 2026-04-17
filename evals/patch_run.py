"""
evals/patch_run.py — Rerun failed cases, patch them into an existing run file,
and re-score + snapshot.

Usage:
    python evals/patch_run.py <run_file>

Finds all parse_error / llm_error / non-ok records in the run file, reruns
them through the agent graph (with retries), replaces the records in-place,
writes the patched run file, and produces a fresh snapshot + report.
"""

import concurrent.futures
import json
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.graph import build_graph
from evals.reporter import print_report
from evals.run_evals import (
    GOLDEN_PATH,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    build_initial_state,
    load_cases,
    run_case,
)
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


def _is_failed(record: dict) -> bool:
    if not record.get("ok"):
        return True
    stop = (record.get("result") or {}).get("stop_reason", "")
    return stop in ("llm_error", "parse_error")


def main():
    if len(sys.argv) < 2:
        print("Usage: python evals/patch_run.py <run_file>")
        sys.exit(1)

    run_path = Path(sys.argv[1])
    if not run_path.exists():
        print(f"Run file not found: {run_path}")
        sys.exit(1)

    with run_path.open() as f:
        run_data = json.load(f)

    records = run_data["records"]
    failed_ids = [r["case_id"] for r in records if _is_failed(r)]

    if not failed_ids:
        print("No failed cases found — nothing to patch.")
        return

    print(f"Found {len(failed_ids)} failed case(s): {', '.join(failed_ids)}")

    # Load golden cases for the failed IDs
    all_cases = load_cases()
    case_by_id = {c["case_id"]: c for c in all_cases}
    failed_cases = [case_by_id[cid] for cid in failed_ids if cid in case_by_id]

    if not failed_cases:
        print("No matching cases in golden.json — aborting.")
        sys.exit(1)

    print("Building agent graph...")
    app = build_graph()

    # Warmup retrieval models
    try:
        from pipeline.retrieve import retrieve as _warmup_retrieve
        _warmup_retrieve(query_text="warmup", app_id=failed_cases[0]["app_id"])
        logger.info("Retrieval models warmed.")
    except Exception as e:
        logger.warning(f"Retrieval warmup failed (non-fatal): {type(e).__name__}: {e}")

    # Rerun failed cases with retries
    new_records = {}
    for case in failed_cases:
        cid = case["case_id"]
        for attempt in range(1, MAX_RETRIES + 2):
            logger.info(f"[{cid}] Attempt {attempt}...")
            record = run_case(app, case)
            if not _is_failed(record):
                logger.info(f"[{cid}] Success: stop_reason={record['result']['stop_reason']}")
                new_records[cid] = record
                break
            if attempt <= MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                reason = record.get("error") or f"stop_reason={record['result']['stop_reason']}"
                logger.warning(f"[{cid}] Attempt {attempt} failed: {reason}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                reason = record.get("error") or f"stop_reason={record['result']['stop_reason']}"
                logger.error(f"[{cid}] All attempts failed: {reason}")
                new_records[cid] = record  # keep last attempt even if failed

    # Patch records into the run data
    patched_count = 0
    for i, r in enumerate(records):
        if r["case_id"] in new_records:
            records[i] = new_records[r["case_id"]]
            patched_count += 1

    print(f"\nPatched {patched_count} record(s).")

    # Write patched run file (overwrite in place)
    with run_path.open("w") as f:
        json.dump(run_data, f, indent=2)
    print(f"Updated run file: {run_path}")

    # Re-score everything
    print("\nRe-scoring...")
    cases = all_cases  # full golden set for scoring
    scored = score_records(cases, records)
    gating = gating_accuracy_batch(cases, records)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as judge_pool:
        f_judge = judge_pool.submit(
            judge_grounding_batch, cases, records, scored,
            run_file_basename=run_path.stem,
        )
        f_judge_action = judge_pool.submit(
            judge_action_batch, cases, records, scored,
            run_file_basename=run_path.stem,
        )
        f_pairwise = judge_pool.submit(
            pairwise_batch, cases, records,
            run_file_basename=run_path.stem,
        )
        f_judge_evd_gold = judge_pool.submit(
            judge_pool_sufficiency_batch, cases, records,
            run_file_basename=run_path.stem,
        )
        f_judge_evd_draft = judge_pool.submit(
            judge_draft_grounding_batch, cases, records,
            run_file_basename=run_path.stem,
        )
    judge = f_judge.result()
    judge_action = f_judge_action.result()
    pairwise = f_pairwise.result()
    judge_evd_gold = f_judge_evd_gold.result()
    judge_evd_draft = f_judge_evd_draft.result()

    print()
    filters = run_data.get("filters", {})
    print_report(
        cases, records, scored, gating, judge, judge_action, pairwise,
        judge_evd_gold, judge_evd_draft,
    )

    prev_snapshot = load_latest_snapshot()
    snap_path = write_snapshot(
        scored, gating, judge, judge_action, pairwise,
        judge_evd_gold, judge_evd_draft,
        records, run_file=run_path, filters=filters,
    )
    print(f"\nPatched run file: {run_path}")
    print(f"Snapshot:         {snap_path}")
    if prev_snapshot is not None:
        with snap_path.open() as f:
            curr_snapshot = json.load(f)
        diff_lines = diff_snapshots(prev_snapshot, curr_snapshot)
        print(f"\nDiff vs previous snapshot ({prev_snapshot.get('timestamp')}):")
        if diff_lines:
            for line in diff_lines:
                print(line)
        else:
            print("  (no metric changes)")


if __name__ == "__main__":
    main()
