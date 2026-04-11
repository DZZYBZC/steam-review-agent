"""
evals/a2_first_pass_merge_rename.py — Phase A2 FIRST PASS only.

Scope: this script does merge / rename / sufficient_sets ONLY. It does NOT
add new equivalent chunk_ids and does NOT drop existing ones. Chunk-set
additions/drops are deferred to a separate second-pass script
(`evals/a2_second_pass_chunk_additions.py`, not yet authored), which will
require reading the patch-notes corpus to confirm equivalence — that work
cannot be done responsibly from case notes alone.

The first-pass / second-pass split exists so the conservative semantic edits
(merging concepts that demonstrably support a single load-bearing claim,
giving concepts readable names, authoring sufficient_sets) can be reviewed
and committed independently from the harder, evidence-gathering work of
finding chunk equivalents the original annotator missed.

Per-case rules enforced by the validator:
  - new concept_ids must be a subset of the original auto-migrated ids
    (so any merge survivor keeps its original id; absorbed ids vanish)
  - the union of all chunk_ids across new concepts must EXACTLY equal the
    union across original concepts — no additions, no drops
  - every concept's any_of must be a non-empty list of strings
  - every concept_id referenced from sufficient_sets must resolve

Idempotent. Re-running on an already-annotated golden produces zero diff.

Usage:
    python evals/a2_first_pass_merge_rename.py --dry-run
    python evals/a2_first_pass_merge_rename.py
"""

import argparse
import json
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "test_sets" / "golden.json"


# Per-case annotations. Structure:
#   case_id: {
#     "concepts": [ {concept_id, name, any_of}, ... ],   # full replacement
#     "sufficient_sets": [ [concept_id, ...], ... ],     # optional
#   }
ANNOTATIONS: dict[str, dict] = {
    # ---- payday3 ----
    "payday3_multi_002": {
        # KEEP SPLIT (user call): "P2P shipped" and "less lag observed in
        # playtest" are related but not obviously interchangeable for the
        # first pass. Stricter sufficient_set requires both.
        "concepts": [
            {"concept_id": "c_001", "name": "p2p_patch_shipped",
             "any_of": ["1827626365763153-0", "1827626365763153-1"]},
            {"concept_id": "c_002", "name": "p2p_playtest_lag_reduction",
             "any_of": ["1823825466499688-7"]},
        ],
        "sufficient_sets": [["c_001", "c_002"]],
    },
    "payday3_gameplay_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "skills_2_0_rework_shipped",
             "any_of": ["1817483467048073-0", "1811138915522063-3"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "payday3_content_001": {
        # Reverted to original 3-concept split — conservative principle
        # says don't merge unless explicitly walked through.
        "concepts": [
            {"concept_id": "c_001", "name": "p2p_addresses_online_only",
             "any_of": ["1827626365763153-0"]},
            {"concept_id": "c_002", "name": "future_of_payday_roadmap_a",
             "any_of": ["1823191198599924-0"]},
            {"concept_id": "c_003", "name": "future_of_payday_roadmap_b",
             "any_of": ["1823191198599924-5"]},
        ],
        # Online-only AND content roadmap both required. Either roadmap
        # chunk satisfies the heist-variety axis.
        "sufficient_sets": [["c_001", "c_002"], ["c_001", "c_003"]],
    },
    "payday3_content_003": {
        "concepts": [
            {"concept_id": "c_001", "name": "future_of_payday_content_plans",
             "any_of": ["1823191198599924-0", "1823191198599924-1"]},
            {"concept_id": "c_002", "name": "player_count_acknowledgment",
             "any_of": ["1802354289595370-5"]},
        ],
        "sufficient_sets": [["c_001", "c_002"]],
    },
    # ---- civ7 ----
    "civ7_gameplay_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "advanced_setting_for_age_transitions",
             "any_of": ["1805431065499578-25"]},
            {"concept_id": "c_002", "name": "team_acknowledged_age_transition_concern",
             "any_of": ["1814309641574128-1", "1801617199561563-37"]},
        ],
        # Advanced setting alone is the load-bearing fact; check-ins
        # are supporting dialogue.
        "sufficient_sets": [["c_001"]],
    },
    "civ7_gameplay_002": {
        # KEEP SPLIT (user call): too much uncertainty around
        # 1808061939503633-9 to merge responsibly.
        "concepts": [
            {"concept_id": "c_001", "name": "ai_commander_pathing_fix",
             "any_of": ["1800357164569792-77"]},
            {"concept_id": "c_002", "name": "ai_pathing_alternate_evidence",
             "any_of": ["1808061939503633-9", "1800357164569792-76"]},
        ],
        # Either concept alone covers the AI pathing claim (only
        # addressable axis in this case).
        "sufficient_sets": [["c_001"], ["c_002"]],
    },
    "civ7_ui_001": {
        # KEEP SPLIT. Two distinct patches at two distinct versions;
        # honest response benefits from both.
        "concepts": [
            {"concept_id": "c_001", "name": "ui_cleanup_in_1_2_5",
             "any_of": ["1811772772410550-11", "1811772772410550-10"]},
            {"concept_id": "c_002", "name": "yield_breakdown_fix_1_2_1",
             "any_of": ["1800991756447780-1"]},
        ],
        "sufficient_sets": [["c_001", "c_002"]],
    },
    "civ7_tech_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "corrupt_state_crash_fix_1_2_1",
             "any_of": ["1800991756447780-2"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "civ7_other_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "ui_cleanup_in_1_2_5",
             "any_of": ["1811772772410550-11", "1811772772410550-10"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    # ---- starfield ----
    "starfield_gameplay_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "outpost_qol_improvements",
             "any_of": ["1828894815567746-17", "1828894815567715-51"]},
            {"concept_id": "c_002", "name": "ship_optimization_terminal",
             "any_of": ["1828894815567746-8"]},
        ],
        # Distinct axes (base building + ship). Both needed for honest
        # multi-axis acknowledgment. Flight controls have no chunk.
        "sufficient_sets": [["c_001", "c_002"]],
    },
    "starfield_story_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "shattered_space_dlc",
             "any_of": ["6909171012614938755-2"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "starfield_content_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "more_planetary_pois_free_lanes",
             "any_of": ["1826992588604218-32"]},
            {"concept_id": "c_002", "name": "crash_fixes_1_16_236",
             "any_of": ["1828894815567746-31"]},
        ],
        # Two of three axes covered (POI density + crashes); loading
        # has no chunk. Both concepts needed.
        "sufficient_sets": [["c_001", "c_002"]],
    },
    # ---- mhw ----
    "mhw_perf_001": {
        # KEEP SPLIT. c_001 is the explicit Feb 2026 commitment post —
        # load-bearing for this Feb-regression case. c_002 is an earlier
        # patch and is supporting context only; citing only c_002 here
        # would be cited_irrelevant_patch.
        "concepts": [
            {"concept_id": "c_001", "name": "feb_2026_stability_commitment_post",
             "any_of": ["1818752592127214-15"]},
            {"concept_id": "c_002", "name": "ver_1_040_earlier_stability_fix",
             "any_of": ["1818752592127214-11", "1823191198598921-6"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "mhw_perf_002": {
        # MERGE: player references "3 updates didn't help" — the
        # operative claim is the cumulative shipped work, not any
        # specific patch.
        "concepts": [
            {"concept_id": "c_001", "name": "ongoing_perf_optimization_patches",
             "any_of": [
                 "1818752592134338-37",
                 "1818752592127214-9",
                 "1818752592127214-15",
             ]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "mhw_perf_003": {
        # MERGE: "less specific than mhw_perf_001 but still actionable.
        # Same patches apply." Any subset of perf chunks satisfies it.
        "concepts": [
            {"concept_id": "c_001", "name": "ongoing_perf_optimization_patches",
             "any_of": [
                 "1818752592127214-9",
                 "1818752592127214-15",
                 "1818752592134338-37",
                 "1825093633183688-24",
                 "1825093633183688-20",
                 "1823191198598921-7",
             ]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "mhw_tech_002": {
        # KEEP SPLIT. c_001 (AMD-specific Adrenalin guidance) is
        # load-bearing; without it, citing only c_002 (general bug fixes)
        # would be cited_irrelevant_patch on an AMD-specific complaint.
        "concepts": [
            {"concept_id": "c_001", "name": "amd_adrenalin_guidance",
             "any_of": ["1811772772359267-0", "1811772772359267-19"]},
            {"concept_id": "c_002", "name": "ver_1_030_bug_fixes",
             "any_of": ["1816849002009391-3"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "mhw_balance_001": {
        # KEEP SPLIT. Two distinct patches at two distinct versions
        # (1.021 and 1.040), both buffing IG. Either alone supports
        # the claim "IG has been buffed."
        "concepts": [
            {"concept_id": "c_001", "name": "ig_buffs_in_ver_1_021",
             "any_of": [
                 "1808061939339147-68",
                 "1808061939339147-69",
                 "1808061939339147-70",
             ]},
            {"concept_id": "c_002", "name": "ig_buffs_in_ver_1_040",
             "any_of": ["1818752592134338-208"]},
        ],
        "sufficient_sets": [["c_001"], ["c_002"]],
    },
    # ---- poe2 ----
    "poe2_balance_002": {
        "concepts": [
            {"concept_id": "c_001", "name": "last_lament_lich_build_context",
             "any_of": ["1810503566531744-0"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "poe2_balance_003": {
        "concepts": [
            {"concept_id": "c_001", "name": "endgame_rewards_breach_rings_improved",
             "any_of": ["1800357164389149-2", "1800357164389149-14"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "poe2_tech_001": {
        "concepts": [
            {"concept_id": "c_001", "name": "ongoing_crash_hotfixes_third_edict",
             "any_of": ["1809869179987885-1", "1809869179987885-96"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "poe2_tech_002": {
        "concepts": [
            {"concept_id": "c_001", "name": "ongoing_crash_hotfixes_third_edict",
             "any_of": ["1809869179987885-1"]},
        ],
        "sufficient_sets": [["c_001"]],
    },
    "poe2_multi_001": {
        # Reverted to split — conservative principle says don't merge
        # unless explicitly walked through.
        "concepts": [
            {"concept_id": "c_001", "name": "perf_improvements_planned_0_2_1",
             "any_of": ["1800357164389149-45"]},
            {"concept_id": "c_002", "name": "third_edict_ongoing_hotfixes",
             "any_of": ["1809869179987885-1"]},
        ],
        # Notes say neither is a direct fix; either alone establishes
        # "team is actively working on related issues."
        "sufficient_sets": [["c_001"], ["c_002"]],
    },
}


def _validate_annotation(case_id: str, ann: dict, original_concepts: list[dict]) -> None:
    """Sanity-check the annotation against the original auto-migrated concepts."""
    new_concepts = ann["concepts"]

    new_ids = [c["concept_id"] for c in new_concepts]
    if len(new_ids) != len(set(new_ids)):
        raise ValueError(f"{case_id}: duplicate concept_id in annotation")

    original_ids = {c["concept_id"] for c in original_concepts}
    for cid in new_ids:
        if cid not in original_ids:
            raise ValueError(
                f"{case_id}: annotation introduces new concept_id {cid!r} "
                f"(not in original auto-migrated set {sorted(original_ids)})"
            )

    original_chunks: set[str] = set()
    for c in original_concepts:
        original_chunks.update(c["any_of"])
    new_chunks: set[str] = set()
    for c in new_concepts:
        if not c["any_of"] or not all(isinstance(x, str) for x in c["any_of"]):
            raise ValueError(
                f"{case_id}: concept {c['concept_id']} has empty/invalid any_of"
            )
        new_chunks.update(c["any_of"])
    if original_chunks != new_chunks:
        added = new_chunks - original_chunks
        dropped = original_chunks - new_chunks
        raise ValueError(
            f"{case_id}: a2_first_pass requires the chunk set to be exactly "
            f"preserved. dropped={sorted(dropped)} added={sorted(added)}. "
            f"To intentionally add or drop chunks, use the second-pass script "
            f"(a2_second_pass_chunk_additions.py) — see this file's docstring."
        )

    for s in ann.get("sufficient_sets", []):
        for cid in s:
            if cid not in new_ids:
                raise ValueError(
                    f"{case_id}: sufficient_sets references unknown concept_id {cid!r}"
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = json.loads(GOLDEN_PATH.read_text())
    cases = data["cases"]
    by_id = {c["case_id"]: c for c in cases}

    n_updated = 0
    n_unchanged = 0
    n_missing = 0
    n_no_concepts = 0
    updates: list[str] = []

    for case_id, ann in ANNOTATIONS.items():
        case = by_id.get(case_id)
        if case is None:
            print(f"WARN: {case_id} not found in golden.json")
            n_missing += 1
            continue
        original = case.get("required_concepts") or []
        if not original:
            print(f"WARN: {case_id} has no auto-migrated required_concepts; skipping")
            n_no_concepts += 1
            continue

        _validate_annotation(case_id, ann, original)

        new_concepts = ann["concepts"]
        new_sufficient = ann.get("sufficient_sets")

        existing_sufficient = case.get("sufficient_sets")
        changed = (original != new_concepts) or (existing_sufficient != new_sufficient)

        if not changed:
            n_unchanged += 1
            continue

        case["required_concepts"] = new_concepts
        if new_sufficient is not None:
            case["sufficient_sets"] = new_sufficient
        elif "sufficient_sets" in case:
            del case["sufficient_sets"]
        n_updated += 1
        updates.append(
            f"  {case_id}: {len(original)} → {len(new_concepts)} concept(s)"
            + (f", sufficient_sets: {new_sufficient}" if new_sufficient else "")
        )

    if updates:
        print("\nUPDATES:")
        for line in updates:
            print(line)

    print()
    print("=" * 60)
    print(f"Updated:        {n_updated}")
    print(f"Unchanged:      {n_unchanged}")
    print(f"Missing case:   {n_missing}")
    print(f"No concepts:    {n_no_concepts}")
    print(f"Annotated set:  {len(ANNOTATIONS)}")

    if args.dry_run:
        print("\n--dry-run: golden.json NOT modified")
        return

    if n_updated == 0:
        print("\nNo changes to write.")
        return

    GOLDEN_PATH.write_text(json.dumps(data, indent=2))
    print(f"\nWrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
