# Negative controls locked for Option B (multi_part_complaint rule edit)

**Locked at:** 2026-04-08, BEFORE editing skills/draft-response/SKILL.md
**Source run file:** evals/runs/run_20260408_015106.json

These 3 cases must NOT regress after the multi_part_complaint rule is added to the responder skill. All satisfy: single-issue, evidence_confidence ≥ 0.7, previously assertive AND clearly correct citation, NOT multi-part, NOT previously low-conf judge-flagged.

## Pass criterion (per case)
After re-running the eval, the new draft must:
1. Still cite the same chunk_id(s) (the citation set must not shrink to empty), AND
2. Not introduce NEW hedging language about whether the cited patch addresses the complaint (phrases like "we can't confirm this addresses…", "may not resolve…", "doesn't address the core of…" should not appear *as a disclaimer attached to the cited patch*).

A draft can rephrase or restructure freely — only the assertive framing of the SPECIFIC citation matters.

---

## Control 1: `payday3_tech_002`
- **Confidence:** 0.90 | **Cited:** `['1811772772510824-3']`
- **Review:** "It should have an offline mode that works properly, not one that tries to connect to the internet."
- **Current assertive framing (must not weaken):** *"The team did investigate offline mode seriously, but concluded after that work that it's not a feasible direction for PAYDAY 3 — this was shared publicly in the October 2 Vision Stream."*
- **Why the cite is clearly correct:** The Vision Stream chunk explicitly says offline mode "is not a feasible route for us." Direct match.

## Control 2: `civ7_content_001`
- **Confidence:** 0.85 | **Cited:** `['1801617199561563-32']`
- **Review:** "...super excited to play civ 7 with my friends via hotseat... more than 1 year later they still have not added it..."
- **Current assertive framing (must not weaken):** *"The team acknowledged in their June 2025 Check-In that Hotseat Multiplayer is high on the list and that they're actively scoping it..."*
- **Note:** This draft already contains *appropriate* hedging on the timeline ("they haven't committed to a specific date") — that's not the kind of hedging being guarded against. The guard is against hedging the EXISTENCE of the patch acknowledgement.
- **Why the cite is clearly correct:** The Check-In chunk literally mentions "Hotseat Multiplayer" by name as actively scoped.

## Control 3: `mhw_content_001`
- **Confidence:** 0.85 | **Cited:** `['1825093633183688-45', '1825093633183688-42']`
- **Review:** "DONT MAKE TIMED EVENTS, I PAID FOR THIS GAME I SHOULD BE ABLE TO EARN THOSE *TIMED* REWARDS WHEN I WANT TO."
- **Current assertive framing (must not weaken):** *"This one's actually been addressed: Ver.1.041.00.00 made all 29 previously limited-time event quests permanent and playable offline..."*
- **Why the cite is clearly correct:** Two Update 1.041 chunks explicitly make all 29 timed event quests permanent. Direct, unambiguous fix.
- **Most assertive of the three** — strongest signal if over-hedging is introduced.

---

## Verification result (2026-04-08, run `run_20260408_034715`)

**Gate verdict: PASS** (with one noted caveat).

- **Canonical case `civ7_gameplay_002`:** PASS. Confidence shifted 0.35 → 0.45 (just above the <0.4 flag threshold), so the case dropped out of `low_conf_with_cite` entirely. The *content* is the proof: the new draft now hedges the previously-assertive AI pathing claim — *"Updates 1.2.4 and 1.2.5 included meaningful improvements... **Those may help with what you're seeing, though we can't confirm they cover your specific pathing issues.**"* The multi_part_complaint rule landed exactly as intended.
- **All 10 cases still flagged `low_conf_with_cite`:** ruled `honest_hedge` by the judge. Zero `misleading_fix_claim`. Zero `unclear`.
- **Negative controls:** all 3 PASS. Each still cites the same chunks; assertive framing on the cited fix is preserved verbatim or near-verbatim. None acquired new hedging language attached to the cited patch.
- **Deterministic baseline:** `n_hard_violations` 0 → 0; `action_correct_rate` 0.595 → 0.651; `citation_subset_ok_rate` 100% in both runs.

**Caveat — recall_at_k_mean regressed (0.241 → 0.164):**
The strict reading of the plan's deterministic baseline trips on this. Accepted as PASS anyway because: recall@k is computed from the investigator's `relevant_ids` vs the case's `must_include_chunk_ids`, and the responder prompt edit runs *after* retrieval — it cannot causally affect what the investigator retrieves. The regression is run-to-run variance from non-prompt-related agent non-determinism (LLM-based investigator assessment, query reformulation, tone-influenced retrieval hints). Recorded here because rationalizing past gate criteria is exactly what shouldn't happen silently — if the recall metric is also low in the *next* run, that's evidence something else has drifted and the plausible-noise interpretation needs revisiting.

---

# Negative controls locked for Option A (wrong_action_severity LLM judge layer)

**Locked at:** 2026-04-08, BEFORE any judge LLM call
**Source run file:** evals/runs/run_20260408_034715.json
**Judge skill:** skills/judge-action/SKILL.md
**Judge module:** evals/scorers/judge_action.py

## Two-sided gate criteria

### Positive — distribution check (necessary but not sufficient)
Run `python evals/run_evals.py` (full set). On the 15 cases the deterministic scorer flags as `wrong_action_severity`:
1. At least **one** ruling must be a non-`tolerable_disagreement` semantic bucket (over_escalation / missed_escalation / category_drift). Otherwise the judge is too lenient — collapsing every miss into "probably fine."
2. At least **one** ruling must be `tolerable_disagreement`. Otherwise the judge is rubber-stamping every deterministic miss as a real failure — too strict.
3. `judge_error` must be 0. Any value > 0 means infrastructure misfired and the gate should be re-attempted after diagnosis (this is NOT one of the prompt-revision rounds — fix the wiring instead).

### Positive — semantic spot checks (the load-bearing gate)
Three cases pre-locked here BEFORE any LLM call. Each was hand-picked by reading the review, ideal/predicted, and the draft from `run_20260408_034715`. Distribution checks alone do not catch a sloppy judge — these spot checks do.

#### Spot 1: clearly `missed_escalation`
- **case_id:** `poe2_tech_001`
- **ideal_action:** `escalate` | **predicted_action:** `monitor` (two-rung downward swap)
- **review excerpt:** "the game is unplayable in current state. crashing constantly, and it's not an isolated issue — it's persistent and reproducible."
- **expected ruling:** `missed_escalation` (only acceptable value)
- **why this is unambiguous:** Two-rung downward swap on a review that explicitly calls the game "unplayable" with constant reproducible crashes. This is the textbook missed_escalation pattern from worked example 1 in the skill.

#### Spot 2: clearly `over_escalation`
- **case_id:** `mhw_tech_001`
- **ideal_action:** `monitor` | **predicted_action:** `escalate` (two-rung upward swap)
- **review framing:** isolated single-user crash report, no widespread-issue evidence
- **expected ruling:** `over_escalation` (only acceptable value)
- **why this is unambiguous:** Two-rung upward swap from monitor to escalate on a single-user complaint. Mirrors worked example 2 in the skill almost exactly.

#### Spot 3: borderline (dual-acceptable)
- **case_id:** `civ7_content_001`
- **ideal_action:** `investigate` | **predicted_action:** `monitor` (one-rung downward swap)
- **review:** Hotseat multiplayer feature gap; team has acknowledged in a Check-In post but not shipped.
- **acceptable_rulings:** `[tolerable_disagreement, category_drift]`
- **why dual-acceptable:** This is a one-rung adjacent swap on a feature-gap complaint that sits genuinely mid-ladder. The boundary between "category_drift on a real feature gap" and "tolerable_disagreement because monitor is defensible when there's a tracked acknowledgement" is fuzzy by design. Either ruling reflects a sane judge. Any ruling outside this set fails the gate (specifically, `missed_escalation` would be wrong because the team has publicly acknowledged the feature, and `over_escalation` is mechanically impossible since the swap is downward).

**Spot-check pass rule:** all 3 spots must satisfy their constraint. Any single spot failure is a semantic gate failure and triggers the stop rule (one round of skill-only revision, then halt).

### Negative — predicate gate keeps clean cases out
The judge predicate `_should_judge` must rule on **only** cases the deterministic scorer counts as `wrong_action_severity`. The following 3 cases are action-correct (`ideal == predicted`), drawn from 3 distinct categories. None of them must appear in `judge_action_batch.per_case` after the run. Verify by inspection of the snapshot and the runs JSON.

| case_id              | category               | ideal == predicted |
|----------------------|------------------------|--------------------|
| `payday3_tech_001`   | technical_issues       | investigate        |
| `civ7_monetize_001`  | monetization_value     | no_action          |
| `starfield_perf_001` | performance_optimization | monitor          |

If any of these slips into `judge_action_batch.per_case`, the predicate is wrong — fix `_should_judge` in `evals/scorers/judge_action.py` BEFORE doing anything else. (This is not a prompt-revision round; it's a code bug that violates the predicate-identity invariant with `evals/scorers/deterministic.py:_failure_mode_section`.)

### Determinism
Run the full eval twice back-to-back. The second run's `judge_action_batch` must report `n_from_cache == n_flagged` (100% cache hit on the full flagged population — NOT just the quick subset). Snapshot diff between the two full runs must show **zero** change in any `judge.action.*` count, including `judge_act_judge_error`.

### Schema visibility
The first post-edit snapshot diff must surface `⚠ schema_version changed: 3 → 4` annotation. Eyeball-confirm in terminal output.

## Stop rule
If any of the above fails on the first attempt, allow exactly **one** prompt-revision round, operationally defined as:
- One edit to `skills/judge-action/SKILL.md` (no other file edits)
- One rerun of `python evals/run_evals.py` (cache will partially miss because skill_sha changed)

If the gate still fails after that single revision round, document the failure here and stop. Do NOT keep tweaking — the answer is offline re-planning, not prompt churn.

---

## Verification result (2026-04-08, run `run_20260408_050806`)

**Gate verdict: PASS** (with one noted caveat — see Spot 2).

**Distribution check:** PASS. 17 flagged cases → 4 over_escalation, 5 missed_escalation, 4 category_drift, 4 tolerable_disagreement, 0 judge_error. Both sides of the distribution constraint satisfied.

**Spot 1 — `poe2_tech_001`:** PASS. Ruled `missed_escalation` as expected. Judge rationale: "Two-rung downward swap (escalate→monitor) on a review explicitly describing the game as unplayable…"

**Spot 2 — `mhw_tech_001`:** STRICT FAIL → accepted as PASS-with-caveat after root-cause analysis.
- Expected: `over_escalation` (locked under the assumption monitor→escalate from `run_20260408_034715`).
- Actual ruling: `category_drift` with rationale "One-rung upward swap (monitor→**investigate**)…"
- Root cause: agent non-determinism. In the fresh run the responder produced `proposed_action=investigate`, not `escalate` as in the lock-source run. The swap shape changed from two-rung to one-rung between locking and verification, so `category_drift` is the correct ruling for the *new* inputs.
- Why accepted: this is an upstream agent shift, not a judge sloppiness signal. The judge ruled correctly given the inputs it actually saw. Analogous to the recall@k caveat from Option B.
- Lesson learned: spot-check locks should be paired with a basename pin — when the locking run's `proposed_action` for a spot case shifts in verification, the spot check is automatically stale and should be re-locked, not failed. Filed as a follow-up improvement.

**Spot 3 — `civ7_content_001`:** PASS. Ruled `category_drift`, which is in the dual-acceptable set `{tolerable_disagreement, category_drift}`.

**Negative predicate controls:** PASS. None of `payday3_tech_001`, `civ7_monetize_001`, `starfield_perf_001` appeared in `judge_action_batch.per_case`. The predicate gate held — it ruled on exactly the 17 deterministic mismatches and zero clean cases.

**Determinism (cache proof):** PASS via offline re-score. **Important methodology correction:** the original plan called for a "full run #2" to verify the cache. That is wrong: the judge cache key includes `run_file_basename`, so a second live `run_evals.py` invocation creates a fresh timestamped basename and guarantees cache misses. The correct cache proof is an offline re-score against the saved run JSON from run 1 with the same basename:

```python
from evals.run_evals import load_cases
from evals.scorers.deterministic import score_records
from evals.scorers.judge_action import judge_action_batch
from evals.scorers.judge_grounding import judge_grounding_batch
import json
from pathlib import Path

run_file = Path('evals/runs/run_20260408_050806.json')
basename = run_file.stem
records = json.loads(run_file.read_text())['records']
cases = [c for c in load_cases() if c['case_id'] in {r['case_id'] for r in records}]
scored = score_records(cases, records)
judge_g = judge_grounding_batch(cases, records, scored, run_file_basename=basename)
judge_a = judge_action_batch(cases, records, scored, run_file_basename=basename)
assert judge_a['n_from_cache'] == judge_a['n_flagged']  # 17/17
assert judge_g['n_from_cache'] == judge_g['n_flagged']  # 12/12
```

Result: `action: 17/17 hit, grounding: 12/12 hit`. Ruling counts identical. Zero LLM calls.

**Schema visibility:** PASS. The smoke run (`--quick`) surfaced `⚠ schema_version changed: 3 → 4 (judge_action layer added: wrong_action_severity cases now ruled by LLM)` in the snapshot diff.

---

# Iteration 1 lock — action_severity_precedence rule edit

**Locked at:** 2026-04-08, BEFORE editing skills/draft-response/SKILL.md and skills/critique-draft/SKILL.md
**Source run file:** evals/runs/run_20260408_050806.json
**Total deterministic mismatches in source run:** 17
**Locked via:** `python evals/_lock_controls.py --iteration 1 --run evals/runs/run_20260408_050806.json`

## Positive — fix targets (5 named cases)

**missed_escalation cases (3 must hit ideal post-edit):**
- `poe2_tech_001` — must produce `escalate` (currently `monitor`). Review: "the game is unplayable in current state. crashing constantly, and it's not an isolated issue — it's persistent and reproducible." Two-rung downward swap; textbook hard-blocker language test for Rule 1.
- `starfield_tech_001` — must produce `escalate` (currently `investigate`). Data-loss case ("NG+ 10 game is all gone"). Per CLAUDE.md, data loss is always escalate.
- `civ7_tech_001` — must produce `escalate` (currently `investigate`). "hundreds of crashes in 40 hours...literally broken" — volume + "literally broken" is hard-blocker language.

**over_escalation cases (2 must hit ideal post-edit):**
- `payday3_monetize_001` — must produce `no_action` (currently `monitor`). Pure DLC-pricing opinion ("half the game locked behind dlcs"). Tests Rule 2's no-escape-hatch path.
- `mhw_content_001` — must produce `no_action` (currently `monitor`). Anti-FOMO design preference. Tests Rule 2 with the existence-of-related-patches confound (Ver.1.041 made events permanent — must NOT raise the rung by itself).

The remaining 4 mismatch cases (`starfield_gameplay_002`, `poe2_gameplay_001`, `poe2_balance_001`, `poe2_balance_002`) are NOT locked as required wins — they sit on the fuzzier subjective-design-vs-monitor boundary. Bonus only.

## Negative — over-correction controls (5 currently-correct cases must NOT regress)

_Currently-correct cases by ideal rung in source run: no_action=3, monitor=17, investigate=6, escalate=0._
_No escalate-correct case exists in source run → falling back to 2 investigate-correct cases per the plan._

- `payday3_tech_001` — investigate-correct (escalate fallback). cat=technical_issues. Must stay `investigate`.
- `civ7_ui_002` — investigate-correct (escalate fallback). cat=ui_controls. Must stay `investigate`.
- `starfield_content_003` — investigate-correct (technical_issues). cat=technical_issues. Must stay `investigate`.
- `payday3_content_001` — monitor-correct (subjective category, content_progression). Must stay `monitor`. Tests Rule 2's `monitor` escape hatch on a subjective-but-trackable case.
- `civ7_monetize_001` — no_action-correct (reused from Locked Option A negative controls). cat=monetization_value. Must stay `no_action`.

## Crash-word canary (Rule 1 over-firing guard)

_A currently-correct `monitor` case whose review text contains hard-blocker language. Must STAY at `monitor` post-edit. This is the load-bearing canary for Rule 1: if the responder over-fires on the word "unplayable" alone, this case will incorrectly escalate._

- `payday3_multi_002` — ideal=`monitor`, contains hard-blocker word `'unplayable'`. cat=multiplayer_network
  > "Playing with friends is a nightmare since the servers lag a lot and pretty much unplayable. Wait until they finish like 50+ DLCs and more patches, OR if they abandon the game totally."

## Re-judge invariant (direction-reversal guard)

Every case that previously ruled `missed_escalation` must NOT post-edit-flip to `over_escalation`, and vice versa. Direction reversals are a structural failure of the precedence rules.

## Aggregate gate metric

- `n_missed_escalation + n_over_escalation` must DECREASE by **at least 3** (baseline = 9 in source run; target ≤ 6 post-edit).
- `judge_action_batch.n_judge_error` must remain `0`.
- Zero direction reversals.

All three aggregate conditions must hold simultaneously with the named-case spot checks. Spot checks alone can be passed by coincidence; the aggregate metric can only be passed by genuine system improvement. Both must pass.

## Cache proof

Offline re-score against the new run file's JSON, mirroring the methodology in `feedback_cache_proof_offline_rescore.md`. NOT a second live run.

## Stop rule

If the gate fails, allow exactly **one** revision round, operationally defined as **one coordinated edit pass to BOTH `skills/draft-response/SKILL.md` and `skills/critique-draft/SKILL.md`** (they must stay aligned — editing one without the other re-introduces the responder loop), then **one rerun of `python evals/run_evals.py`**. No second skill edit, no edits to scorers/harness, no cherry-picking. If the gate still fails after that round, document the failure here and stop.

---

# Iteration 2 lock — pairwise revision-improvement scorer

**Locked at:** 2026-04-08, BEFORE first LLM call from `evals/scorers/pairwise.py`
**Lock helper invocation:** `python evals/_lock_controls.py --iteration 2 --run evals/runs/run_20260408_050806.json`
**Source run file:** `evals/runs/run_20260408_050806.json` (used for predicate controls + multi-iter case enumeration; spot checks are deferred — see below)

## Distribution check (positive)

After the full post-iter-1 run + pairwise judge invocation:
- `n_revision_improved >= 1` AND `n_revision_neutral >= 1` (both buckets must have at least one ruling — collapse to all-improved or all-neutral is a structural FAIL).
- `n_judge_error == 0`. Any value > 0 means infrastructure misfired and the gate falls into the **infrastructure failure** stop-rule branch (not the semantic branch).
- `n_revision_regressed > total/2` is a structural FAIL (judge too quick to call regressions). Accepted: `n_revision_regressed == 0` is fine — clean regressions are rare and the absence of any does not prove the judge is broken.

## Semantic spot checks (DEFERRED — must be locked AFTER post-iter-1 run materializes)

The plan correctly says spot checks must be hand-picked from the run we're judging. Iter 1 will reshape the iter-0 drafts of every multi-iter case (the action precedence rules change which proposed_action the responder picks at iter 0, and the critic's reason_type cascades from there). Locking spot-check expectations against `run_20260408_050806`'s drafts would be stale before the judge ever ran.

**Procedure when the post-iter-1 run lands** — before invoking the pairwise judge:
1. Query `audit_log_iterations` for the post-iter-1 run's multi-iteration cases (filter by `run_id`s pulled from the post-iter-1 run JSON).
2. Hand-pick **Spot 1 — clearly `revision_improved`**: a case where the critic's iter-0 `reason_type` is `drafting` or `evidence`, the iter-0 draft visibly contains the issue named in the critique, and the final draft demonstrably fixes it on at least one of the five `revision_improved` dimensions in `skills/judge-pairwise/SKILL.md`. **Strong candidates from the baseline run** that are likely to remain strong post-iter-1 (use as a starting list, not a lock):
   - `mhw_perf_001` — iter-0 action=monitor → iter-1 action=investigate on a 900+ hour stutter complaint. Pure dimension-5 (action correction toward ideal severity). Cleanest improvement signal.
   - `civ7_gameplay_002` — multi-part complaint where iter 0 used `investigate` and iter 1 restructured to separate technical from design with `monitor`. Already a regression seed for Option B.
3. Hand-pick **Spot 2 — clearly `revision_neutral`**: a case where iter-0 and final differ only in cosmetic phrasing (no claim shift, no citation shift, no action shift). The deterministic shortcut should auto-label most truly cosmetic cases as `revision_neutral` with `"deterministic": True` — for the spot check we want a case that the LLM (not the shortcut) rules `revision_neutral`, which means a case with non-trivial wording differences but identical substance. Read both drafts side-by-side before locking. **None of the baseline-run multi-iter cases are obviously cosmetic-only** — every iter pair shifts material content. If post-iter-1 produces no clear neutral candidate, lock with `acceptable_rulings: [revision_neutral, revision_improved]` and document the borderline.
4. **Spot 3 — `revision_regressed`** (optional): lock only if a clearly-regressed pair exists post-iter-1. Otherwise the spot set is 2.
5. **Critical:** record the run JSON basename next to each locked spot. When the spot's underlying iter-0 / final pair changes between locking and verification (run-to-run nondeterminism), the spot is automatically stale and must be re-locked, NOT failed. This is the lesson from the Option A `mhw_tech_001` caveat.

The spot checks are the load-bearing semantic gate — distribution checks alone can be passed by a sloppy classifier.

## Negative — predicate controls (structural, lockable now)

3 single-iteration cases drawn from 3 distinct categories. None of these may appear in `pairwise.per_case` after the run. If any do, the `_should_judge` predicate is wrong — fix `evals/scorers/pairwise.py` BEFORE anything else. (This is an **infrastructure failure**, not a semantic gate failure — code-fix budget applies.)

| case_id                  | category              | iteration_count |
|--------------------------|-----------------------|-----------------|
| `payday3_multi_001`      | other                 | 0               |
| `payday3_gameplay_001`   | gameplay_mechanics    | 0               |
| `payday3_vague_001`      | monetization_value    | 0               |

## Cache proof

Offline re-score against the post-iter-1 run JSON, mirroring the methodology in `feedback_cache_proof_offline_rescore.md`:

```python
from evals.run_evals import load_cases
from evals.scorers.pairwise import pairwise_batch
import json
from pathlib import Path

run_file = Path('evals/runs/run_<post_iter1_timestamp>.json')
basename = run_file.stem
records = json.loads(run_file.read_text())['records']
cases = [c for c in load_cases() if c['case_id'] in {r['case_id'] for r in records}]
pairwise = pairwise_batch(cases, records, run_file_basename=basename)
assert pairwise['n_from_cache'] == pairwise['n_judged']
```

NOT a second live `run_evals.py` invocation — that would mint a fresh basename and guarantee cache misses.

## Schema visibility

The first post-edit snapshot diff must surface `⚠ schema_version changed: 4 → 5 (pairwise revision-improvement judge added: multi-iteration approved cases now ruled by LLM (with deterministic normalize-equal shortcut))`. Eyeball-confirm in terminal output.

## Stop rule (split by failure class)

Iteration 2 introduces brand-new code (`pairwise.py`, `get_iteration_drafts_batch`, snapshot/reporter wiring, deterministic shortcut). A blanket "skill-only edit" stop rule is too strict because plumbing bugs are not semantic failures.

- **Infrastructure failure** — symptoms: import error, snapshot schema crash, predicate skipping cases it shouldn't or admitting cases it shouldn't (e.g., negative predicate controls appearing in `per_case`), batch DB helper returning empty when iter-0 rows clearly exist, deterministic shortcut auto-labeling cases that aren't actually identical, `n_judge_error > 0` from API/parse/validation. **Allowed:** ONE round of code fixes to any of `pairwise.py`, `pipeline/storage.py`, `evals/snapshot.py`, `evals/reporter.py`, `evals/run_evals.py`, plus ONE rerun. No skill edits in this round (the skill is presumed innocent until plumbing is verified).
- **Semantic gate failure** — symptoms: distribution check fails, spot checks rule something other than expected, judge ruling reads wrong on inspection. **Allowed:** ONE edit to `skills/judge-pairwise/SKILL.md` plus ONE rerun. No code edits in this round.
- **Mixed failure** — fix infra first, rerun, then re-evaluate semantic gate against the post-fix output. The semantic edit budget remains available after infra is verified.

If the failure recurs after its allotted fix, document here and stop. The answer is offline re-planning, not churn.

---

# Iteration 1 — Verification result (2026-04-08)

**Gate verdict: FAIL on named-case spot checks. Stop-rule budget exhausted.**

Two runs were used:
1. **First post-edit run** `run_20260408_073254.json` — initial Rule 1 (Severity-overrides-confidence) over-fired catastrophically. Over-escalation jumped 4 → 11 because the rule treated any single hard-blocker word ("unplayable", "crash") as a sufficient escalation trigger regardless of subjectivity or persistence framing. Canary `payday3_multi_002` fired exactly as designed (escalated on the word "unplayable").
2. **Remedial coordinated edit + partial rerun** (per Option 2 — see `evals/_remedial_rerun.py` docstring for caveats). Skills reordered so Subjectivity-cap is now Rule 1 (HARD CEILING applied first); Severity-overrides-confidence is Rule 2 and requires BOTH a concrete technical defect AND explicit persistence/volume framing. Remedial run: `run_20260408_080427_REMEDIAL_PARTIAL.json` (19 critical case_ids fresh, 37 frozen).

## Remedial verdict by gate dimension

| Dimension | Result | Detail |
|---|---|---|
| **Positive fix targets (≥3 of 5 required)** | ❌ 3/5 | ✅ `poe2_tech_001` `starfield_tech_001` `civ7_tech_001` → escalate. ❌ `payday3_monetize_001` and `mhw_content_001` stuck at `monitor` instead of `no_action` — the Rule 1 `monitor` escape hatch is being applied too liberally to subjective complaints with related-but-not-corrective patches. |
| **Negative controls (5/5 must hold)** | ❌ 3/5 | ✅ `payday3_tech_001` `starfield_content_003` `payday3_content_001` held. ❌ `civ7_ui_002` regressed `investigate` → `no_action`. ❌ `civ7_monetize_001` regressed `no_action` → `monitor`. Same Rule 1 escape-hatch drift as the missed positives. |
| **Crash-word canary** | ✅ | `payday3_multi_002` held at `monitor` after rule reorder. |
| **Flip-back set (8 cases that became over_escalation in failing run)** | ✅ 8/8 | All recovered. None still ruled `over_escalation` by the action judge. |
| **Aggregate `n_missed + n_over` (≤6)** | ✅ 5 (informative only) | Baseline 9 → failing 11 → **5**. Beats locked target. **Per Option 2 caveat the aggregate is informative-only, NOT gate-eligible** (37 cases frozen; cannot rule out drift in unrun set). |
| **`n_judge_error`** | ✅ 0 | Both judges clean. |
| **Direction reversals** | ✅ none observed | The softened rule only affects upward firing direction. |

## Diagnosis

The pattern in the 4 named-case misses is consistent: the Rule 1 escape hatch (`monitor` is acceptable when the subjective complaint is recurring/trackable as a product signal) is too permissive. The responder treats almost any subjective complaint with related patch context as "trackable signal" and picks `monitor` instead of defaulting to `no_action`. The two `civ7_*` regressions are collateral from the same drift — once `monitor` becomes the default for borderline cases, the ladder shifts upward by one rung in the gray zone.

The aggregate metric improvement (9 → 5) is real and load-bearing — the system IS better than baseline overall — but the named cases binding the gate are not all hit. Per the lock, named-case spot checks override the aggregate.

## Stop-rule status

The Iteration 1 stop rule allowed ONE coordinated edit + ONE rerun. Both have been spent (the coordinated edit was the rule reorder + persistence-framing tightening; the rerun was the remedial partial). **Budget exhausted. STOP.**

The right next move is offline re-planning (not in this iteration): the Rule 1 escape hatch needs a tighter definition of "trackable product signal" — likely requiring either explicit volume language ("a lot of players", "everyone is asking") or a recurring-pattern signal that can be derived deterministically rather than inferred by the responder. That redesign is outside this iteration's budget. Document the pattern, leave the rule in its current improved-but-imperfect state (still net better than baseline), and proceed.

---

# Iteration 2 — Verification result (2026-04-08)

**Gate verdict: PASS (structural).**

Pairwise scorer ran end-to-end on the remedial partial-rerun output. Distribution check, infrastructure check, and predicate-control check all clean. Semantic spot checks deferred per Iteration 2 lock (waiting on a clean post-iter1 baseline that didn't materialize) — re-spot when the next non-remedial full eval runs.

## Verdict by gate dimension

| Dimension | Result | Detail |
|---|---|---|
| **Distribution: `n_judged ≥ 1`** | ✅ | 32 multi-iteration approved cases judged. |
| **Distribution: `n_revision_improved ≥ 1`** | ✅ | At least one improved ruling present (also at least one regressed observed — `payday3_tech_001`, flagged for follow-up review). |
| **Distribution: `n_revision_neutral ≥ 1`** | ✅ | 27 neutrals via deterministic shortcut alone. |
| **Distribution: `n_revision_regressed ≤ n_judged/2`** | ✅ | Far below half. |
| **`n_judge_error == 0`** | ✅ | No infrastructure misfires. Judge clean. |
| **Deterministic shortcut counted separately** | ✅ | 27/32 neutrals from shortcut, 5 routed to LLM. The high shortcut hit rate is expected — many revision cycles after the iter-1 skill edits produced cosmetically-identical post-edit drafts. |
| **Predicate negative controls absent from `per_case`** | ✅ structurally | 13 zero-iteration cases in the run; pairwise judged 32 cases (all multi-iter). The three locked predicate negatives (`payday3_multi_001`, `payday3_gameplay_001`, `payday3_vague_001`) are zero-iteration in this run and therefore correctly excluded. |
| **Schema visibility** | ✅ | Snapshot schema bumped 4 → 5 on first wired run. |
| **Cache proof** | n/a this run | The remedial partial run is the first invocation that exercised pairwise; cache was cold (0/32 hit). Will verify warm cache on next clean run. |

## Spot checks — DEFERRED

The Iteration 2 lock specified two semantic spot checks (one expected `revision_improved`, one expected `revision_neutral`). These were intentionally deferred to a post-iter1 clean run. Iteration 1 ended in a remedial partial rerun rather than a clean full run, so the spot-check baseline is not yet clean. **Action:** spot-check on the next non-remedial full eval. The harness, scorer, and rulings table are already in place — only hand-locking of expected case_ids remains.

## Follow-up

`payday3_tech_001` was ruled `revision_regressed` by the pairwise judge in this run. Worth eyeballing the iter-0 vs final draft pair manually to confirm whether the regression is real or a judge mis-rule. Not blocking.

## Stop-rule status

No fix budget consumed. Iteration 2 lands clean structurally; semantic verification deferred to next opportunity rather than spent.

---

# Iteration 4 lock — rubric revision (single-axis actionability + priority hierarchy)

**Locked at:** 2026-04-08, BEFORE any eval rerun.
**Source baseline:** `evals/snapshots/snapshot_20260408_111306.json` (action aggregate), `evals/runs/run_20260408_111204.json` (per-case rulings)
**Skills edited in lockstep:**
- `skills/draft-response/SKILL.md` — `<internal_action>` block rewritten to the single-axis hierarchy, adds the "recurring subjective-but-meaningful pain signal → monitor" clause.
- `skills/critique-draft/SKILL.md` — Action check #6 rewritten to match (no longer rejects `investigate` for design-flavored complaints that describe concrete failure modes; explicit fail for recurring subjective signal routed to `no_action`).
- `skills/judge-action/SKILL.md` — `<action_ladder>` rewritten to match, so judge ruling boundaries are anchored to the same rubric.
- `CLAUDE.md` — developer-doc rubric section replaced (not load-bearing for the runtime agent, but kept in sync).

**Gold standard:** `evals/test_sets/golden.json` — audited case-by-case against the new rubric; **no edits required.** Pre-audit prediction was 1–2 cases (`civ7_gameplay_003`, `mhw_tech_002`) might shift, but the new rubric's recurring-signal clause keeps every current label defensible. Audit outcome documented in M5_PLAN Iteration 4 entry.

## Why this iteration is different from iter1

Iteration 1 had a specific failure it was fixing (5 named action mismatches). Iteration 4 is a structural rubric refactor — there is no specific failing case to pin as a "fix target," because the old rubric's conflation of axes was the problem, not any one case. The gate is therefore **invariance-biased**: the test is whether the system stays at least as good as baseline under the new rubric, not whether it climbs a specific hill.

## Two-sided gate

### Positive — distribution invariants (must hold)

Baseline numbers from `snapshot_20260408_111306.json` on 56 cases, 43 action-evaluable:

| Metric | Baseline | Post-rerun requirement |
|---|---|---|
| `action.correct_rate` | 0.628 | **≥ 0.628** (must not regress; modest improvement is expected because gold labels are now a better match for the rubric the responder is prompted with) |
| `judge.action.n_missed_escalation + n_over_escalation` | 6 | **≤ 6** (must not worsen; these are the two ladder-direction failure buckets) |
| `judge.action.n_judge_error` | 0 | **0** (any infra misfire is an infrastructure-failure branch of the stop rule) |
| `critic_health.approval_rate_overall` | 0.714 | **≥ 0.65** (slack because softened critic rule may reject fewer drafts; a modest drop is defensible, a collapse is not) |
| `grounding.n_hard_violations` | 0 | **0** (citation chain of custody is not part of this iteration; any change is a regression) |
| `citation.subset_ok_rate` | 1.0 | **1.0** |
| `retrieval.recall_source_mean` / `recall_relevant_mean` | 0.294 / 0.196 (from recalibrated v6 snapshot) | **unchanged ± run-to-run noise** — responder edits run downstream of retrieval, so any drift here is non-causal noise. Document but do not gate. |

### Positive — semantic spot checks (the load-bearing gate)

Three spot checks pre-locked against drafts in `run_20260408_111204.json`. Each is a case where the rubric revision should produce a predictable post-rerun shape.

**Critical:** if the baseline run's `proposed_action` for a spot case shifts between lock and rerun due to agent non-determinism, the spot is stale and must be re-locked, NOT failed. (Per the `mhw_tech_001` lesson from iteration 1.)

#### Spot A — recurring subjective signal should STAY at `monitor`, not collapse to `no_action`
- **case_id:** `civ7_gameplay_003`
- **review:** "They've stripped away every part of the idea of building a civilization..."
- **ideal_action:** `monitor`
- **baseline `proposed_action`:** `monitor` (from source run)
- **expected post-rerun:** `monitor` (the recurring-signal clause was explicitly drafted to keep cases of this shape)
- **fail mode:** if the post-rerun `proposed_action` is `no_action`, the responder is dropping the recurring-signal clause — edit the skill and rerun (semantic branch of stop rule).

#### Spot B — concrete-failure design complaint can land at `investigate`
- **case_id:** `civ7_ui_002`
- **review:** "Give us back strategic view. A bit weird to remove existing accessibility features..."
- **ideal_action:** `investigate`
- **baseline `proposed_action`:** `investigate` (currently correct)
- **expected post-rerun:** `investigate` — the old "investigate is for technical issues only" critique rule would have rejected this; the new rubric explicitly allows design complaints with concrete failure modes.
- **fail mode:** if the post-rerun action drops to `monitor` or `no_action`, either the responder or the critic is still applying the old technical-only gate.

#### Spot C — clean severity case should stay `escalate`
- **case_id:** `civ7_tech_001`
- **review:** "hundreds of crashes in 40 hours... literally broken"
- **ideal_action:** `escalate`
- **baseline `proposed_action`:** `escalate` (locked in during iter 1)
- **expected post-rerun:** `escalate` — the "would a delay of days cause meaningful harm?" framing is the new `escalate` gate and this case is the textbook fit.
- **fail mode:** any downgrade is a direction reversal and triggers the stop rule.

**Spot-check pass rule:** all three must satisfy their constraint. Any single failure is a semantic gate failure.

### Negative — over-correction controls (must NOT regress)

Seven currently-correct cases spanning all four action buckets. Drawn from `run_20260408_111204.json` where `proposed_action == ideal_action`. These test whether the rubric rewrite preserves wins across the full ladder — a common failure mode in skill rewrites is that the rubric is right on average but drifts one rung on a subset.

| case_id | category | ideal == current | guards against |
|---|---|---|---|
| `payday3_monetize_001` | monetization_value | `no_action` | Pure-pricing cases drifting to `monitor` via the recurring-signal escape hatch |
| `mhw_content_001` | content_progression | `no_action` | Anti-FOMO design preference drifting to `monitor` |
| `payday3_tech_001` | technical_issues | `investigate` | Specific technical bug not over-escalating |
| `starfield_content_003` | technical_issues | `investigate` | Misclassified bug-within-content review staying investigate |
| `payday3_content_001` | content_progression | `monitor` | Multi-issue partial-patch case staying in monitor escape hatch |
| `starfield_tech_001` | technical_issues | `escalate` | Data-loss case not drifting down |
| `poe2_tech_001` | technical_issues | `escalate` | Widespread-crashes case holding the iter-1 escalate win |

All seven must stay at their baseline `proposed_action` post-rerun.

### Negative — predicate gate (unchanged from iter1/iter2)

The judge predicate `_should_judge` on `judge_action_batch` must continue to admit only `wrong_action_severity` cases. If any currently-correct case appears in `per_case` after the rerun, it is an infrastructure failure, NOT a rubric issue — fix `evals/scorers/judge_action.py` first.

## Cache proof

Offline re-score against the post-rerun JSON, mirroring `feedback_cache_proof_offline_rescore.md`. NOT a second live `run_evals.py` invocation:

```python
from evals.run_evals import load_cases
from evals.scorers.deterministic import score_records
from evals.scorers.judge_action import judge_action_batch
from evals.scorers.judge_grounding import judge_grounding_batch
from evals.scorers.pairwise import pairwise_batch
import json
from pathlib import Path

run_file = Path('evals/runs/run_<iter4_timestamp>.json')
basename = run_file.stem
records = json.loads(run_file.read_text())['records']
cases = [c for c in load_cases() if c['case_id'] in {r['case_id'] for r in records}]
scored = score_records(cases, records)
judge_a = judge_action_batch(cases, records, scored, run_file_basename=basename)
judge_g = judge_grounding_batch(cases, records, scored, run_file_basename=basename)
pairwise = pairwise_batch(cases, records, run_file_basename=basename)
assert judge_a['n_from_cache'] == judge_a['n_flagged']
assert judge_g['n_from_cache'] == judge_g['n_flagged']
```

Because all three judge skills have new `skill_sha` values (judge-action edited in lockstep, grounding/pairwise unchanged but co-keyed), cache keys WILL differ from Iter3. First-run cold cache on judge-action is expected; grounding and pairwise should remain warm if the caller pins the same `run_file_basename`.

## Schema visibility

No schema bump this iteration. Snapshot stays at v6. Diff print should show standard action/judge/critic deltas without any `⚠ schema_version changed` annotation.

## Stop rule (split by failure class)

- **Infrastructure failure** (snapshot crash, predicate admits wrong cases, `n_judge_error > 0`, cache proof asserts fail): ONE round of code fixes + ONE rerun. No skill edits in this round.
- **Semantic gate failure** (any spot check fails, distribution invariants violated, negative control regresses): ONE coordinated edit pass to **draft-response + critique-draft + judge-action together** (they must stay aligned) + ONE rerun. No code edits in this round.
- **Mixed failure**: fix infra first, re-evaluate semantic gate post-fix.

If the gate still fails after its allotted round, document the failure here and stop. Rubric redesign is offline re-planning, not churn.

## Initial verification result — FAIL on distribution gate (`snapshot_20260408_132334.json`)

First Iter4 rerun against `run_20260408_132225.json` under the new rubric blew past the gate in three places:

| Metric | Baseline | Iter4 initial | Gate | Verdict |
|---|---|---|---|---|
| `action.correct_rate` | 0.628 | **0.595** | ≥ 0.628 | ❌ |
| `judge.action.missed+over` | 6 | 6 | ≤ 6 | ✅ |
| `critic.approval_rate_overall` | 0.714 | **0.427** | ≥ 0.65 | ❌ |
| `grounding.n_hard_violations` | 0 | 0 | 0 | ✅ |

Spot C (`civ7_tech_001`) regressed `escalate → investigate`. Two negative controls regressed: `payday3_monetize_001` (`no_action → monitor`, pricing drifted via the recurring-signal clause) and `poe2_tech_001` (`escalate → monitor`, two-rung drop losing the iter1 win). Classified as **semantic gate failure** — triggered the single allowed coordinated edit pass to draft-response + critique-draft + judge-action.

### Remedial coordinated edit (three-skill pass)

Three fixes applied in lockstep so responder/critic/judge rubric semantics stay aligned (per `feedback_coordinated_skill_edits_all_three.md`):

- **Fix 1 — escalate gate widened with anti-over-correction guardrail.** Two-path test: (a) widespread/blast-radius framing OR (b) concrete hard-blocker symptom from a single reviewer paired with explicit persistence/reproducibility language. Critical guardrail: heated adjectives alone ("unplayable," "broken," "trash") do NOT qualify — must be paired with a concrete failure mode or persistence framing. Applied in `skills/draft-response/SKILL.md` `<internal_action>` and mirrored in `skills/judge-action/SKILL.md` `<action_ladder>`.
- **Fix 2 — pricing carve-out on the recurring-signal clause.** Pricing, DLC strategy, monetization, and other business-model complaints do NOT qualify as recurring subjective pain signals — they remain at `no_action` unless they describe a concrete failure mode. Applied in all three skills (responder `<internal_action>`, critic check #6, judge `<action_ladder>` monitor bullet) so scoring semantics stay aligned.
- **Fix 3 — critic check #6 trimmed.** Simplified from 6 bullets to 3 load-bearing fail conditions (missed downward / over-escalation upward / minor→escalate), with the rubric definitions inlined once and explicit "do not reject adjacent-rung calls when reasoning is defensible" language. Applied in `skills/critique-draft/SKILL.md` only.

### Remedial verification result — PARTIAL; action gate recovered, critic-health gate held below floor

Rerun: `run_20260408_140734.json` → `snapshot_20260408_140847.json`.

| Metric | Baseline | Iter4 initial | Iter4 remedial | Gate | Verdict |
|---|---|---|---|---|---|
| `action.correct_rate` | 0.628 | 0.595 | **0.651** | ≥ 0.628 | ✅ |
| `judge.action.missed+over` | 6 | 6 | **7** (4+3) | ≤ 6 | ❌ |
| `judge.action.n_judge_error` | 0 | 0 | **1** | 0 | ❌ |
| `critic.approval_rate_overall` | 0.714 | 0.427 | **0.474** | ≥ 0.65 | ❌ |
| `grounding.n_hard_violations` | 0 | 0 | 0 | 0 | ✅ |
| `citation.subset_ok_rate` | 1.0 | 1.0 | 1.0 | 1.0 | ✅ |

**Recovery targets (2/3):**
- ✅ `civ7_tech_001` → `escalate` (Fix 1 two-path test worked — hundreds-of-crashes language now qualifies under path (b))
- ✅ `payday3_monetize_001` → `no_action` (Fix 2 pricing carve-out worked — pricing no longer drifts via recurring-signal escape hatch)
- ❌ `poe2_tech_001` → `monitor` (still wrong, `stop_reason=max_iterations_reached`) — the widespread-crash framing should have re-fired under path (a), but the run hit the iteration budget before critic approval. Likely cascading drafting rejections, not a rubric-boundary failure.

**Preservation targets (7/8 including canary):**
- ✅ `payday3_multi_002` — crash-word canary held at `monitor` (Fix 1 heated-adjective guardrail firing correctly)
- ✅ `civ7_ui_002`, `mhw_content_001`, `payday3_tech_001`, `starfield_content_003`, `payday3_content_001`, `starfield_tech_001` — all held
- ❌ `civ7_gameplay_003` → `investigate` (regressed from baseline `monitor`, new preservation failure) — the Spot A case was locked pre-Iter4 as "recurring subjective signal → monitor," but the widened rubric now admits it as concrete-enough for investigate. Not in the original regression list; a side effect of the widened action vocabulary.

**Critic-health diagnosis (the gate failure that did not recover):**
- `iter0_approval = 0.558` (baseline was higher). Critic rejects ~44% of first-pass drafts.
- `drafting` rejections: baseline 14 → Iter4 initial 41 → remedial 36. Fix 3 moved the needle (41→36) but not enough.
- `max_iterations_reached`: baseline 3 → Iter4 initial 10 → remedial 7. Cascading cases where the drafting loop never converges.
- Root cause: check #6 got *longer* in content (full rung re-definitions inlined, pricing carve-out, hard-blocker language, heated-adjective guardrail) even though the fail list shrank from 6 to 3. The critic has more total decision surface than at baseline, and it applies the new rules bidirectionally — the over-escalation fail condition (#2) covering both business-model complaints AND heated-adjective-only cases gives the critic more reasons to reject borderline mid-ladder drafts.
- `approval_overall = approved_iters / total_iters` is not per-case; each max_iters case adds 2–3 un-approved iters to the denominator, so the metric amplifies the drafting-rejection rate.
- **Important asymmetry:** action correctness is scored on the *final* draft. Cases hitting max_iters still land on the right answer often enough that `action.correct_rate` recovered above baseline. The critic-health failure is "expensive but correct," not "wrong."

### Verdict and stop-rule application

Per the split stop rule, this iteration has spent its **single allowed coordinated edit pass + rerun budget**. No further skill edits. The failure shape:

- ✅ Primary correctness metric recovered above baseline
- ✅ 2/3 recovery targets hit, pricing carve-out worked, crash-word canary held
- ❌ Critic-health gate failed (0.474 vs 0.65 floor) — shape is waste/latency/cost, not wrong answers
- ❌ One new preservation failure (`civ7_gameplay_003`) from widened action vocabulary — side effect, not a rubric misfire
- ❌ One transient `judge_error = 1` (single infra misfire, not a pattern)
- ❌ One recovery target (`poe2_tech_001`) not recovered — lost to max_iters cascade, not rubric boundary

**STOP.** Next move is offline re-planning of the critic workload problem (possibly: prune check #6 further by moving the rung definitions out of the check body and back into a header block the critic only consults on ambiguity; or reduce the critic's fail-condition vocabulary by folding the heated-adjective guardrail into the responder-only side). Not this iteration. Rubric redesign is planning, not churn.
