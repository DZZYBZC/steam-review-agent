"""
evals/_recalibrate_audit.py — Iter3 Phase 2 audit helper.

Load run_20260408_111204.json + golden.json, identify cases where:
  - must_include_chunk_ids has non-zero slots
  - zero slots landed in source_ids (i.e., the strict chunk-ID gold missed
    entirely on the raw retriever output)
  - the must_include entries share a visible version-split pattern (e.g.,
    "Ver.1.030", "Ver.1.040" appear across required chunk IDs)

For each candidate, print the required chunk texts next to the actual
retrieved chunks so a human can judge whether any retrieved chunk expresses
the same fact as a missed required one (→ equivalence group candidate).

Throwaway. Does NOT edit golden.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chromadb
from config import CHROMA_PERSIST_DIR

_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)


def _fetch_chunk_text(app_id: str, chunk_id: str) -> str:
    try:
        coll = _client.get_collection(name=f"patches_{app_id}")
    except Exception:
        return "(collection missing)"
    try:
        res = coll.get(ids=[chunk_id], include=["documents", "metadatas"])
    except Exception as e:
        return f"(fetch error: {e})"
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    if not docs:
        return "(chunk_id not found in collection)"
    meta = metas[0] if metas else {}
    title = meta.get("title", "")
    section = meta.get("section", "")
    return f"{title} | {section}: {docs[0]}"

RUN = Path(__file__).resolve().parent / "runs" / "run_20260408_111204.json"
GOLD = Path(__file__).resolve().parent / "test_sets" / "golden.json"


def main() -> None:
    with RUN.open() as f:
        run = json.load(f)
    with GOLD.open() as f:
        gold = json.load(f)

    case_by_id = {c["case_id"]: c for c in gold["cases"]}
    records_by_id = {r["case_id"]: r for r in run["records"]}

    zero_hit: list[str] = []
    for cid, case in case_by_id.items():
        must = case.get("must_include_chunk_ids") or []
        if not must:
            continue
        rec = records_by_id.get(cid)
        if not rec or not rec.get("ok"):
            continue
        ep = (rec["result"] or {}).get("evidence_package") or {}
        source_ids = set(ep.get("source_ids") or [])
        if not source_ids:
            continue  # gate false-skip — separate issue
        # Check if any slot hit source
        hit = False
        for slot in must:
            if isinstance(slot, str) and slot in source_ids:
                hit = True
                break
        if not hit:
            zero_hit.append(cid)

    print(f"Zero-hit cases (strict chunk_id, must_include ∩ source_ids = ∅): {len(zero_hit)}")
    print()

    for cid in zero_hit:
        case = case_by_id[cid]
        rec = records_by_id[cid]
        ep = rec["result"]["evidence_package"]
        must = case["must_include_chunk_ids"]
        source_ids = ep.get("source_ids") or []
        sources = ep.get("sources") or []
        src_by_id = {
            (s.get("chunk_id") or s.get("id")): s for s in sources
        }

        print("=" * 78)
        print(f"CASE {cid}   app={case['app_id']}   cat={case.get('annotated_category')}")
        print(f"review: {(case.get('review_text') or '')[:140]}")
        print()
        print(f"REQUIRED ({len(must)} slots) — NOT retrieved:")
        for m in must:
            text = _fetch_chunk_text(case["app_id"], m)[:240]
            print(f"  [{m}] {text}")
        print()
        print(f"RETRIEVED ({len(source_ids)} chunks in source_ids top-5):")
        for sid in source_ids:
            s = src_by_id.get(sid) or {}
            text = (s.get("text") or s.get("content") or "")[:180]
            print(f"  [{sid}] {text}")
        print()


if __name__ == "__main__":
    main()
