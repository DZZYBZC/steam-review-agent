"""
evals/audit_retrieval_drops.py — Per-case audit table for retrieval recall drops.

Reads a run JSON + golden.json, computes where golden slots were retrieved but
dropped by the investigator's Self-RAG filter, and prints a table for diffing
across iterations.

Usage:
    python evals/audit_retrieval_drops.py <run_json>

Output columns:
    case_id             — golden case identifier
    gold_slots          — number of must_include requirement slots
    source_hits         — slots found in source_ids (pre-filter)
    kept_hits           — slots found in relevant_ids (post-filter)
    dropped_gold_ids    — chunk IDs that were in source but not relevant
    zero_keep           — True if investigator returned 0 relevant_ids
    sibling_fragment    — True if a kept and dropped chunk share a source_gid
    fix_bucket          — FM1 (over-strict), FM2 (zero-keep), FM3 (sibling frag)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


GOLDEN_PATH = Path(__file__).parent / "test_sets" / "golden.json"


def _slot_hit(slot: Any, pool: set[str]) -> bool:
    if isinstance(slot, str):
        return slot in pool
    if isinstance(slot, dict) and "any_of" in slot:
        return any(x in pool for x in slot["any_of"])
    return False


def _slot_label(slot: Any, idx: int) -> str:
    if isinstance(slot, str):
        return slot
    if isinstance(slot, dict) and "any_of" in slot:
        any_of = slot["any_of"]
        if isinstance(any_of, list) and any_of:
            return f"any_of[{'|'.join(str(x) for x in any_of)}]"
    return f"slot_{idx}"


def _slot_chunk_ids(slot: Any) -> list[str]:
    """Return all chunk IDs that would satisfy this slot."""
    if isinstance(slot, str):
        return [slot]
    if isinstance(slot, dict) and "any_of" in slot:
        return list(slot["any_of"])
    return []


def _extract_gid(chunk_id: str) -> str:
    """Extract source_gid from chunk_id format '{gid}-{chunk_index}'."""
    parts = chunk_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else chunk_id


def audit(run_path: str) -> list[dict]:
    with open(GOLDEN_PATH) as f:
        golden = json.load(f)
    cases_by_id = {c["case_id"]: c for c in golden["cases"]}

    with open(run_path) as f:
        run = json.load(f)
    records = run["records"]

    rows = []
    for rec in records:
        case_id = rec["case_id"]
        case = cases_by_id.get(case_id, {})
        must = list(case.get("must_include_chunk_ids", []) or [])
        if not must:
            continue

        result = rec.get("result", {})
        ep = result.get("evidence_package") or {}
        source_ids = set(ep.get("source_ids", []) or [])
        relevant_ids = set(ep.get("relevant_ids", []) or [])

        # Gate-skipped cases (no retrieval at all)
        if not source_ids:
            continue

        source_hits = 0
        kept_hits = 0
        dropped_ids: list[str] = []

        for idx, slot in enumerate(must):
            in_source = _slot_hit(slot, source_ids)
            in_relevant = _slot_hit(slot, relevant_ids)
            if in_source:
                source_hits += 1
            if in_relevant:
                kept_hits += 1
            if in_source and not in_relevant:
                # Find which specific chunk(s) were in source but not relevant
                for cid in _slot_chunk_ids(slot):
                    if cid in source_ids and cid not in relevant_ids:
                        dropped_ids.append(cid)

        zero_keep = len(relevant_ids) == 0

        # Detect sibling fragmentation: a dropped chunk shares source_gid
        # with a kept chunk
        kept_gids = {_extract_gid(cid) for cid in relevant_ids}
        sibling_fragment = any(
            _extract_gid(cid) in kept_gids for cid in dropped_ids
        )

        # Assign fix bucket
        if zero_keep:
            fix_bucket = "FM2"
        elif sibling_fragment:
            fix_bucket = "FM3"
        elif dropped_ids:
            fix_bucket = "FM1"
        else:
            fix_bucket = "-"

        rows.append({
            "case_id": case_id,
            "gold_slots": len(must),
            "source_hits": source_hits,
            "kept_hits": kept_hits,
            "dropped_gold_ids": dropped_ids,
            "zero_keep": zero_keep,
            "sibling_fragment": sibling_fragment,
            "fix_bucket": fix_bucket,
        })

    return rows


def print_table(rows: list[dict]) -> None:
    # Header
    header = (
        f"{'case_id':35s} {'slots':>5s} {'src':>4s} {'kept':>4s} "
        f"{'zero':>5s} {'sib':>4s} {'bucket':>6s}  dropped_gold_ids"
    )
    print(header)
    print("-" * len(header) + "-" * 40)

    # Sort: FM2 first, then FM3, then FM1, then clean
    bucket_order = {"FM2": 0, "FM3": 1, "FM1": 2, "-": 3}
    rows_sorted = sorted(rows, key=lambda r: (bucket_order.get(r["fix_bucket"], 9), r["case_id"]))

    n_drops = 0
    n_zero = 0
    n_sibling = 0
    total_dropped = 0

    for r in rows_sorted:
        dropped_str = ", ".join(r["dropped_gold_ids"]) if r["dropped_gold_ids"] else "-"
        print(
            f"{r['case_id']:35s} {r['gold_slots']:5d} {r['source_hits']:4d} {r['kept_hits']:4d} "
            f"{'YES' if r['zero_keep'] else '-':>5s} {'YES' if r['sibling_fragment'] else '-':>4s} "
            f"{r['fix_bucket']:>6s}  {dropped_str}"
        )
        if r["dropped_gold_ids"]:
            n_drops += 1
            total_dropped += len(r["dropped_gold_ids"])
        if r["zero_keep"]:
            n_zero += 1
        if r["sibling_fragment"]:
            n_sibling += 1

    print()
    print(f"Cases with retrieval: {len(rows)}")
    print(f"Cases with filter drops: {n_drops}")
    print(f"Total golden slots dropped: {total_dropped}")
    print(f"Zero-keep cases: {n_zero}")
    print(f"Sibling-fragment cases: {n_sibling}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <run_json>", file=sys.stderr)
        sys.exit(1)
    rows = audit(sys.argv[1])
    print_table(rows)
