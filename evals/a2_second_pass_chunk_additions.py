"""
evals/a2_second_pass_chunk_additions.py — Phase A2 SECOND PASS, Tier 1 only.

Scope: this script ONLY adds equivalent chunk_ids to existing concepts'
`any_of` lists. It does NOT:
  - drop chunk_ids
  - rename concepts
  - merge or split concepts
  - create new concepts
  - touch sufficient_sets

The first-pass script (`evals/a2_first_pass_merge_rename.py`) handled
merges/renames/sufficient_sets and explicitly preserved the chunk set.
This second pass complements it by expanding the chunk set in the strict
direction only — additions to existing `any_of` lists.

Tier 1 selection rule (the only additions in this script):
  A candidate chunk_id qualifies iff it is a same-article sibling of an
  existing concept member, OR a different-article chunk that makes the
  *same independently meaningful claim* as an existing member, AND the
  receiving concept's name is generic enough to absorb it without
  re-scoping. Borderline cases (different-patch evidence under a
  patch-pinned concept name, narrow feature concepts, header chunks
  whose claim is generic) are deferred to a Tier 2 review.

Candidate set was enumerated from the most recent eval run by selecting
chunks present in `relevant_ids` but absent from any concept's `any_of`
(the "relevant_concept_precision = 0.319" tripwire population). 14 cases
had candidates (56 total chunks); Tier 1 keeps 18 of those across 6 cases.

Validator rules (fail loud, not silent):
  - every concept_id referenced exists in the case's required_concepts
  - the new any_of must be a strict superset of the original any_of
    (additions only — never removes)
  - every added chunk_id is unique within the concept
  - case_ids and concept_ids must resolve in golden.json
  - sufficient_sets and concept ordering are NOT touched

Idempotent. Re-running on an already-updated golden produces zero diff.

Usage:
    python evals/a2_second_pass_chunk_additions.py --dry-run
    python evals/a2_second_pass_chunk_additions.py
"""

import argparse
import json
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "test_sets" / "golden.json"


# Per-case Tier 1 additions. Structure:
#   case_id: { concept_id: [chunk_ids_to_add, ...], ... }
TIER1_ADDITIONS: dict[str, dict[str, list[str]]] = {
    # civ7_ui_001: same patch (1.2.5) as existing -10/-11; same yield-losses
    # surfacing claim as -11.
    "civ7_ui_001": {
        "c_001": ["1811772772410550-100"],
    },
    # poe2_tech_001: same Third Edict article (1809869179987885) as existing
    # -1 and -96. All five candidates are sibling "Fixed a client crash..."
    # entries from the same article.
    "poe2_tech_001": {
        "c_001": [
            "1809869179987885-94",
            "1809869179987885-95",
            "1809869179987885-97",
            "1809869179987885-98",
            "1809869179987885-99",
        ],
    },
    # poe2_tech_002: same Third Edict article as existing -1; sibling crash
    # fixes. Note -96 is included here because it's NOT already in this
    # case's any_of (concepts are per-case).
    "poe2_tech_002": {
        "c_001": [
            "1809869179987885-94",
            "1809869179987885-95",
            "1809869179987885-96",
            "1809869179987885-97",
            "1809869179987885-98",
        ],
    },
    # mhw_tech_002: same article (1811772772359267) as existing -0 and -19;
    # same driver-version-recommendation claim.
    "mhw_tech_002": {
        "c_001": ["1811772772359267-1"],
    },
    # payday3_content_001: different article (Vision Stream 1811772772510824)
    # but exactly the same independent claim as the existing P2P-live chunk
    # ("we acknowledge online-only is a problem and are working on it").
    "payday3_content_001": {
        "c_001": [
            "1811772772510824-3",
            "1811772772510824-4",
        ],
    },
    # mhw_perf_003: concept name "ongoing_perf_optimization_patches" is
    # explicitly plural; existing 6 members already span multiple patches.
    # All four candidates are perf-optimization commitments in the same
    # ongoing series, same independent claim type.
    "mhw_perf_003": {
        "c_001": [
            "1815034432855905-8",
            "1816215235335741-0",
            "1818752592127214-11",
            "1823191198598921-0",
        ],
    },
}


def _load() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def _apply(golden: dict) -> tuple[dict, list[str]]:
    """Return (updated_golden, log_lines). Raises ValueError on validation."""
    log: list[str] = []
    cases_by_id = {c["case_id"]: c for c in golden["cases"]}

    for case_id, per_concept in TIER1_ADDITIONS.items():
        if case_id not in cases_by_id:
            raise ValueError(f"unknown case_id {case_id!r}")
        case = cases_by_id[case_id]
        concepts = case.get("required_concepts") or []
        if not concepts:
            raise ValueError(f"case {case_id!r}: no required_concepts")
        by_id = {c["concept_id"]: c for c in concepts}

        for concept_id, additions in per_concept.items():
            if concept_id not in by_id:
                raise ValueError(
                    f"case {case_id!r}: concept_id {concept_id!r} not found"
                )
            concept = by_id[concept_id]
            original = list(concept.get("any_of") or [])
            existing_set = set(original)
            new_any_of = list(original)
            added_now: list[str] = []
            for chunk_id in additions:
                if not isinstance(chunk_id, str) or not chunk_id:
                    raise ValueError(
                        f"case {case_id!r} concept {concept_id!r}: "
                        f"bad chunk_id {chunk_id!r}"
                    )
                if chunk_id in existing_set:
                    continue  # idempotent: already present
                new_any_of.append(chunk_id)
                existing_set.add(chunk_id)
                added_now.append(chunk_id)

            # Validate additions-only invariant: new is strict superset of old.
            if not set(original).issubset(set(new_any_of)):
                raise ValueError(
                    f"case {case_id!r} concept {concept_id!r}: "
                    f"second-pass tier1 forbids removals; original "
                    f"chunk_ids must all survive"
                )

            concept["any_of"] = new_any_of
            if added_now:
                log.append(
                    f"  {case_id} / {concept_id}: +{len(added_now)} "
                    f"({', '.join(added_now)})"
                )
            else:
                log.append(f"  {case_id} / {concept_id}: (no-op, already present)")

    return golden, log


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    golden = _load()
    updated, log = _apply(golden)
    n_adds = sum(
        len(adds) for per_c in TIER1_ADDITIONS.values() for adds in per_c.values()
    )
    print(f"Tier 1 second-pass: {len(TIER1_ADDITIONS)} cases, {n_adds} chunk additions")
    for line in log:
        print(line)

    if args.dry_run:
        print("\n[dry-run] no file written.")
        return

    GOLDEN_PATH.write_text(json.dumps(updated, indent=2) + "\n")
    print(f"\nwrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
