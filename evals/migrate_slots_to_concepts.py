"""
evals/migrate_slots_to_concepts.py — One-shot mechanical backfill of
`required_concepts` from `must_include_chunk_ids` in golden.json.

This is the Phase A1 migration script for the layered retrieval evaluation
plan. It performs strictly 1:1 mechanical backfill — NO semantic merging,
NO renames, NO equivalent-chunk inference. Every existing slot becomes
exactly one concept; concept_ids are auto-assigned `c_001`, `c_002`, ...
in slot order, per case; `name` always defaults to `concept_id`.

The semantic work (merging concepts, adding missed equivalents, authoring
sufficient_sets, renaming `name`) is deferred to the Phase A2 manual
annotator pass. Doing it here would risk masking bugs in the concept_recall
scorer — the A1 identity check requires that, before any semantic edits,
concept_recall produces numbers byte-equal to retrieval_recall.

Idempotent: re-running on an unchanged golden set produces zero diff.

Usage:
    python evals/migrate_slots_to_concepts.py                     # all cases
    python evals/migrate_slots_to_concepts.py --dry-run           # report only
    python evals/migrate_slots_to_concepts.py --case-id payday3_tech_001
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

GOLDEN_PATH = Path(__file__).parent / "test_sets" / "golden.json"


def _slot_to_any_of(slot, idx: int) -> list[str]:
    """Convert one must_include_chunk_ids entry to an any_of list."""
    if isinstance(slot, str):
        return [slot]
    if isinstance(slot, dict) and "any_of" in slot:
        any_of = slot["any_of"]
        if not isinstance(any_of, list) or not any_of or not all(isinstance(x, str) for x in any_of):
            raise ValueError(
                f"slot[{idx}]: 'any_of' must be a non-empty list of strings, got {any_of!r}"
            )
        return list(any_of)
    raise ValueError(
        f"slot[{idx}]: must be a string or {{'any_of': [...]}} dict, got {type(slot).__name__}"
    )


def _build_concepts_for_case(case: dict) -> list[dict]:
    """1:1 mechanical backfill: each slot → one concept with auto concept_id."""
    must = case.get("must_include_chunk_ids") or []
    concepts: list[dict] = []
    for idx, slot in enumerate(must, start=1):
        cid = f"c_{idx:03d}"
        any_of = _slot_to_any_of(slot, idx - 1)
        concepts.append({"concept_id": cid, "name": cid, "any_of": any_of})
    return concepts


def main():
    parser = argparse.ArgumentParser(description="Backfill required_concepts in golden.json (1:1 mechanical).")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing golden.json")
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Migrate only the given case_id. Repeatable.",
    )
    args = parser.parse_args()

    if not GOLDEN_PATH.exists():
        logger.error(f"Golden set not found at {GOLDEN_PATH}")
        sys.exit(1)

    with GOLDEN_PATH.open() as f:
        data = json.load(f)

    cases = data.get("cases", [])
    if not cases:
        logger.error("Golden set has no cases.")
        sys.exit(1)

    selected: set[str] | None = set(args.case_id) if args.case_id else None
    if selected:
        present = {c.get("case_id") for c in cases}
        missing = selected - present
        if missing:
            logger.error(f"Unknown case_id(s): {sorted(missing)}")
            sys.exit(1)

    n_added = 0       # cases where required_concepts is being created
    n_unchanged = 0   # cases already migrated AND byte-equal to mechanical output
    n_diverged = 0    # cases where existing required_concepts ≠ mechanical output
    n_skipped_empty = 0  # cases with empty must_include_chunk_ids
    diverged_ids: list[str] = []
    added_summaries: list[tuple[str, int]] = []

    for i, case in enumerate(cases, start=1):
        case_id = case.get("case_id", f"<no-id-{i}>")
        if selected is not None and case_id not in selected:
            continue

        new_concepts = _build_concepts_for_case(case)
        if not new_concepts:
            n_skipped_empty += 1
            continue

        existing = case.get("required_concepts")
        if existing is None:
            case["required_concepts"] = new_concepts
            n_added += 1
            added_summaries.append((case_id, len(new_concepts)))
            logger.info(f"[{i}/{len(cases)}] {case_id}: + {len(new_concepts)} concept(s)")
        elif existing == new_concepts:
            n_unchanged += 1
        else:
            # Already-migrated case whose existing concepts differ from a
            # fresh mechanical backfill. Phase A2 manual edits are expected
            # to land here — leave them alone, just report.
            n_diverged += 1
            diverged_ids.append(case_id)

    # Checkpoint summary
    print()
    print("=" * 70)
    print("MIGRATION SUMMARY")
    print("=" * 70)
    print(f"Cases scanned:                {len(cases) if selected is None else len(selected)}")
    print(f"Concepts added (newly migrated): {n_added}")
    print(f"Already mechanical (no change):  {n_unchanged}")
    print(f"Diverged from mechanical (kept): {n_diverged}")
    print(f"Skipped (empty must_include):    {n_skipped_empty}")

    if added_summaries:
        print("\nNEWLY MIGRATED CASES:")
        for case_id, n in added_summaries:
            print(f"  {case_id:<32} {n} concept(s)")

    if diverged_ids:
        print("\nCASES WITH NON-MECHANICAL required_concepts (left untouched):")
        for case_id in diverged_ids:
            print(f"  {case_id}")

    if args.dry_run:
        print("\n--dry-run: golden.json NOT modified")
        return

    if n_added == 0:
        print("\nNo changes to write (idempotent re-run).")
        return

    with GOLDEN_PATH.open("w") as f:
        json.dump(data, f, indent=2)
    print(f"\nWrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
