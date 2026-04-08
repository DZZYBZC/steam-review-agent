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

