"""One-shot materializer for two-sided gate lock blocks.

Reads a run JSON + golden.json, prints a markdown block ready to paste into
evals/_negative_controls_locked.md. Does NOT auto-append — operator eyeballs
the output and pastes it. Logic is intentionally narrow and not abstracted.

Usage:
    python evals/_lock_controls.py --iteration 1 --run evals/runs/run_<ts>.json
    python evals/_lock_controls.py --iteration 2 --run evals/runs/run_<ts>.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from config import PROPOSED_ACTIONS

LADDER = list(PROPOSED_ACTIONS)
HARDWORDS = re.compile(
    r"\b(crash|crashing|unplayable|data loss|save deleted|lost progress|completely broken|pervasive)\b",
    re.I,
)


def _load(run_path: Path) -> tuple[list[dict], dict[str, dict]]:
    run = json.loads(run_path.read_text())
    golden_path = Path(__file__).parent / "test_sets" / "golden.json"
    golden = {c["case_id"]: c for c in json.loads(golden_path.read_text())["cases"]}
    return run["records"], golden


def _classify(records: list[dict], golden: dict[str, dict]) -> tuple[list[tuple], list[tuple]]:
    """Returns (mismatches, correct) lists. Each row: (cid, ideal, pred, category, review_text)."""
    mismatches, correct = [], []
    for r in records:
        cid = r["case_id"]
        if not r.get("ok"):
            continue
        pa = (r.get("result") or {}).get("proposed_action") or ""
        if pa not in LADDER:
            continue
        g = golden.get(cid)
        if not g:
            continue
        row = (cid, g["ideal_action"], pa, g["annotated_category"], g["review_text"])
        (correct if pa == g["ideal_action"] else mismatches).append(row)
    return mismatches, correct


def _emit_iter1(records: list[dict], golden: dict[str, dict], run_path: Path) -> str:
    mismatches, correct = _classify(records, golden)
    out: list[str] = []
    out.append(f"# Iteration 1 lock — action_severity_precedence rule edit")
    out.append(f"\n**Locked at:** BEFORE editing skills/draft-response/SKILL.md and skills/critique-draft/SKILL.md")
    out.append(f"**Source run file:** {run_path.as_posix()}")
    out.append(f"**Total deterministic mismatches in source run:** {len(mismatches)}")

    out.append("\n## Positive — fix targets (5 named cases)")
    out.append("\n**missed_escalation cases (3 must hit ideal post-edit):**")
    for cid in ("poe2_tech_001", "starfield_tech_001", "civ7_tech_001"):
        row = next((m for m in mismatches if m[0] == cid), None)
        if row:
            out.append(f"- `{cid}` — must produce `{row[1]}` (currently `{row[2]}`)")
    out.append("\n**over_escalation cases (2 must hit ideal post-edit):**")
    for cid in ("payday3_monetize_001", "mhw_content_001"):
        row = next((m for m in mismatches if m[0] == cid), None)
        if row:
            out.append(f"- `{cid}` — must produce `{row[1]}` (currently `{row[2]}`)")

    out.append("\n## Negative — over-correction controls (5 currently-correct cases must NOT regress)")
    by_rung = {rung: [c for c in correct if c[1] == rung] for rung in LADDER}
    out.append(f"\n_Counts of currently-correct cases by ideal rung: " + ", ".join(f"{k}={len(v)}" for k, v in by_rung.items()) + "_")

    picks: list[tuple[str, tuple]] = []
    if by_rung["escalate"]:
        picks.append(("escalate-correct", by_rung["escalate"][0]))
    else:
        out.append("\n_No escalate-correct case in source run → falling back to 2 investigate-correct cases._")
        for c in by_rung["investigate"][:2]:
            picks.append(("investigate-correct (escalate fallback)", c))
    tech_inv = [c for c in by_rung["investigate"] if c[3] == "technical_issues" and c[0] not in {p[1][0] for p in picks}]
    for c in tech_inv[:2]:
        picks.append(("investigate-correct (technical_issues)", c))
    subjective_cats = {"gameplay_mechanics", "story_presentation", "balance_difficulty", "content_progression"}
    subjective_monitor = [c for c in by_rung["monitor"] if c[3] in subjective_cats]
    if subjective_monitor:
        picks.append(("monitor-correct (subjective category)", subjective_monitor[0]))
    if by_rung["no_action"]:
        # Reuse civ7_monetize_001 if present (already a Locked Option A control).
        reuse = next((c for c in by_rung["no_action"] if c[0] == "civ7_monetize_001"), by_rung["no_action"][0])
        picks.append(("no_action-correct (reused)", reuse))

    for label, row in picks[:6]:
        out.append(f"- `{row[0]}` — {label}; ideal=`{row[1]}` (must stay correct). cat={row[3]}")

    out.append("\n## Crash-word canary (Rule 1 over-firing guard)")
    out.append("_A currently-correct `monitor` case whose review text contains hard-blocker language. Must STAY at `monitor` post-edit._\n")
    canary = None
    for c in by_rung["monitor"]:
        m = HARDWORDS.search(c[4])
        if m:
            canary = (c, m.group(0))
            break
    if canary:
        c, hit = canary
        out.append(f"- `{c[0]}` — ideal=`monitor`, contains hard-blocker word `{hit!r}`. cat={c[3]}")
        out.append(f"  > {c[4][:200]}")
    else:
        out.append("- (none found — relax HARDWORDS regex or hand-pick a canary)")

    out.append("\n## Aggregate gate metric")
    out.append("- `n_missed_escalation + n_over_escalation` must DECREASE by **at least 3** (baseline = 9 in source run; target ≤ 6 post-edit)")
    out.append("- `judge_action_batch.n_judge_error` must remain `0`")
    out.append("- Zero direction reversals: no case may flip from `missed_escalation` ⇄ `over_escalation`")
    return "\n".join(out)


def _emit_iter2(records: list[dict], golden: dict[str, dict], run_path: Path) -> str:
    out: list[str] = []
    multi = [r for r in records if r.get("ok")
             and (r.get("result") or {}).get("stop_reason") == "human_approved"
             and ((r.get("result") or {}).get("iteration_count") or 0) >= 1]
    single = [r for r in records if r.get("ok")
              and ((r.get("result") or {}).get("iteration_count") or 0) == 0]

    out.append(f"# Iteration 2 lock — pairwise revision-improvement scorer")
    out.append(f"\n**Locked at:** BEFORE first LLM call from evals/scorers/pairwise.py")
    out.append(f"**Source run file:** {run_path.as_posix()}")
    out.append(f"**Multi-iteration cases (eligible for pairwise judgment):** {len(multi)}")
    out.append(f"**Single-iteration cases (predicate must skip):** {len(single)}")

    out.append("\n## Positive — distribution check")
    out.append("- `n_revision_improved >= 1` AND `n_revision_neutral >= 1`")
    out.append("- `n_judge_error == 0`")
    out.append("- `n_revision_regressed > total/2` is a structural FAIL (judge too quick to call regressions)")

    out.append("\n## Positive — semantic spot checks (HAND-PICK BEFORE RUNNING)")
    out.append("After locking, hand-pick by reading audit_log_iterations DB:")
    out.append("- **Spot 1 (revision_improved):** one multi-iter case where critic flagged a specific drafting issue and the iter-0 draft visibly contained it. Lock case_id and expected ruling=`revision_improved`.")
    out.append("- **Spot 2 (revision_neutral):** one multi-iter case where iter-0 and final differ only in cosmetic phrasing. Lock case_id and expected ruling=`revision_neutral`.")
    out.append("- **Spot 3 (revision_regressed, optional):** lock if a clear hand-found example exists.")
    out.append("\nMulti-iter case_ids in this run (for spot-check selection):")
    for r in multi:
        cid = r["case_id"]
        ic = r["result"].get("iteration_count")
        rt = r["result"].get("reason_type") or "?"
        out.append(f"- `{cid}` (iterations={ic}, last_reason_type={rt})")

    out.append("\n## Negative — predicate controls (3 single-iteration cases must NOT appear in pairwise.per_case)")
    seen_cats: set[str] = set()
    picks = []
    for r in single:
        cid = r["case_id"]
        g = golden.get(cid)
        if not g:
            continue
        cat = g["annotated_category"]
        if cat in seen_cats:
            continue
        seen_cats.add(cat)
        picks.append((cid, cat))
        if len(picks) == 3:
            break
    for cid, cat in picks:
        out.append(f"- `{cid}` (category={cat}, iteration_count=0)")

    out.append("\n## Cache + schema visibility")
    out.append("- After full run: offline re-score must show `pairwise['n_from_cache'] == pairwise['n_judged']`")
    out.append("- First post-edit snapshot diff must surface `⚠ schema_version changed: 4 → 5`")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iteration", type=int, choices=[1, 2], required=True)
    ap.add_argument("--run", type=Path, required=True)
    args = ap.parse_args()
    records, golden = _load(args.run)
    if args.iteration == 1:
        print(_emit_iter1(records, golden, args.run))
    else:
        print(_emit_iter2(records, golden, args.run))


if __name__ == "__main__":
    main()
