"""
evals/refresh_classifier_fields.py — One-shot refresh of classifier_category in golden.json.

After editing skills/classify-review/SKILL.md, the frozen classifier_category
values in golden.json no longer reflect what the live classifier would produce.
This script re-runs the production `classify_review()` on every case's
review_text and writes the new category back into golden.json. All other fields
(annotated_category, gate_should_skip, must_include, review_text, etc.) are
ground truth and are NOT touched.

Idempotent: re-running on an unchanged classifier produces an empty diff.

Prints a checkpoint summary listing case_ids whose classifier_category FLIPPED
(old → new) before exiting.

Usage:
    python evals/refresh_classifier_fields.py                              # all cases
    python evals/refresh_classifier_fields.py --dry-run                    # report only, no write
    python evals/refresh_classifier_fields.py --case-id poe2_vague_001     # single case
    python evals/refresh_classifier_fields.py --case-id a --case-id b      # multiple cases

Subset runs still write to golden.json so subsequent `run_evals.py --case-id ...`
picks up the new value. Use --dry-run to opt out of the write.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow `python evals/refresh_classifier_fields.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.classify import classify_review

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

GOLDEN_PATH = Path(__file__).parent / "test_sets" / "golden.json"


def main():
    parser = argparse.ArgumentParser(description="Refresh classifier_category in golden.json.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing golden.json")
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Refresh only the given case_id. Repeatable: --case-id a --case-id b",
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

    # If --case-id given, narrow the loop to those cases. We still iterate
    # `cases` (the live list inside `data`) so writes mutate the right objects;
    # we just skip the ones that don't match.
    selected_ids: set[str] | None = set(args.case_id) if args.case_id else None
    if selected_ids:
        present = {c.get("case_id") for c in cases}
        missing = selected_ids - present
        if missing:
            logger.error(f"Unknown case_id(s): {sorted(missing)}")
            sys.exit(1)
        logger.info(f"Refreshing {len(selected_ids)} selected case(s): {sorted(selected_ids)}")
    else:
        logger.info(f"Refreshing classifier_category on {len(cases)} cases from {GOLDEN_PATH.name}")

    flipped: list[tuple[str, str, str]] = []          # (case_id, old_cat, new_cat)
    failed: list[str] = []

    for i, case in enumerate(cases, start=1):
        case_id = case.get("case_id", f"<no-id-{i}>")
        if selected_ids is not None and case_id not in selected_ids:
            continue
        review_text = case.get("review_text", "")
        if not review_text:
            logger.warning(f"[{i}/{len(cases)}] {case_id}: empty review_text, skipping")
            failed.append(case_id)
            continue

        old_cat = case.get("classifier_category", "")

        result = classify_review(review_text)
        if result is None:
            logger.warning(f"[{i}/{len(cases)}] {case_id}: classifier returned None")
            failed.append(case_id)
            continue

        new_cat = result.primary_category
        case["classifier_category"] = new_cat

        if new_cat != old_cat:
            flipped.append((case_id, old_cat, new_cat))
            logger.info(f"[{i}/{len(cases)}] {case_id}: {old_cat!r} → {new_cat!r}")
        else:
            logger.info(f"[{i}/{len(cases)}] {case_id}: unchanged ({new_cat})")

    # Checkpoint summary
    print()
    print("=" * 70)
    print("REFRESH SUMMARY")
    print("=" * 70)
    print(f"Total cases:     {len(cases)}")
    print(f"Category flips:  {len(flipped)}")
    print(f"Failures:        {len(failed)}")

    if flipped:
        print("\nCATEGORY FLIPS (case_id: old → new):")
        for case_id, old, new in flipped:
            print(f"  {case_id:<28} {old:<24} → {new}")
    else:
        print("\n(no category flips — if SKILL.md changed, this is suspicious; debug before running full eval)")

    if failed:
        print("\nFAILED CASES:")
        for case_id in failed:
            print(f"  {case_id}")

    if args.dry_run:
        print("\n--dry-run: golden.json NOT modified")
        return

    with GOLDEN_PATH.open("w") as f:
        json.dump(data, f, indent=2)
    print(f"\nWrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
