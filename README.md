# Steam Review Triage & Response Agent

Turns noisy Steam reviews into evidence-backed draft replies — with an iterative critic loop and a human approval gate before anything ships.

---

## Quickstart

```bash
# 1. Install (Python 3.12)
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 2. Set CLAUDE_API_KEY (required for any agent run). Pick ONE of these two paths:
#    (a) write a .env file — config.py loads it via python-dotenv:
echo 'CLAUDE_API_KEY=sk-ant-...' >> .env
#    (b) OR export the variable directly in your shell:
export CLAUDE_API_KEY=sk-ant-...

# 3. End-to-end smoke test on a hardcoded review.
#    Runs the full agent graph (investigator → responder → critic) against a fake
#    "game crashes in dungeon" review and auto-approves at the human gate.
#    Makes ~3-5 real LLM calls. Wall clock ~30s. Costs a few cents on Haiku+Sonnet.
python test_graph.py
```

For the full data pipeline, eval suite, runtime/cost estimates, and other entry points, see [Run it yourself](#10-run-it-yourself) below.

---

## What it does

Player feedback on Steam is high-volume, noisy, and emotionally loaded. Manual triage doesn't scale; tone-deaf automated responses are worse than no response. The interesting problem isn't generation — it's *evidence-grounded* generation that can be defended back to the player.

This project ingests Steam reviews and routes each one through a multi-agent workflow: classify, retrieve patch notes that might address the underlying complaint, draft a player-facing reply grounded in what the retriever actually found, and pause for human approval before anything ships.

Every approved or rejected draft feeds back into the system — approvals become few-shot examples for future runs, rejections become cluster notes that warn the next investigator about known issues in the same complaint category.

This is **not** a chatbot and **not** a customer-service autoresponder. The human-in-the-loop gate is non-negotiable. The agent's job is to do the research and produce a defensible draft — the human's job is to ship it.

<details>
<summary><strong>Worked example — one real review through the full graph</strong></summary>

An actual approved run pulled from `audit_log` (app `2246340`, review `222426809`, run `e6d61c72`). Classifier → `technical_issues`, confidence 0.7. Retrieval hit 5 chunks across 5 different patch versions; the critic approved on the first iteration.

**Review (input):**

> Enjoyable, but fiancee cant play because shes using an AMD card this needs to be fixed (because people with AMD cards deserve to play games too)

**Evidence package (investigator output — what the retriever actually found):**

- `[Ver.1.030.02.02] Bug Fixes: Fixed an issue where the game might fail to launch on certain PCs using AMD Radeon graphics cards.`
- `[Ver.1.040.03.02] Video Driver Version: Some issues have been identified with AMD Radeon drivers versions 25.10.2 and higher on certain PCs using AMD Radeon graphics cards such as the AMD Radeon RX 5500 XT/7800 XT.`
- `[Ver.1.041.00.00] Video Driver Version: (same note, repeated)`
- `[Ver.1.041.01.00] Video Driver Version: (same note, repeated)`
- `[Ver.1.041.02.00] Video Driver Version: (same note, repeated)`

**Investigator summary:** Ver.1.030.02.02 fixed a launch failure, but subsequent patches flag ongoing driver-compatibility issues on specific AMD cards that remain unresolved. Partial fix + persistent unresolved issue → confidence 0.7.

**Draft response (responder output, Sonnet 4.6):**

> Your fiancée absolutely deserves to play — AMD compatibility is something we've been actively working on. Ver.1.030.02.02 fixed a launch failure affecting certain AMD Radeon cards, but we've since identified ongoing issues with AMD Radeon driver versions 25.10.2 and higher on specific cards including the RX 5500 XT and RX 7800 XT, flagged across several recent patch updates. If her card falls into that category, rolling back to a driver version below 25.10.2 may help in the meantime. We don't have a confirmed permanent fix for the newer driver compatibility issue yet, but the team is aware and it's being tracked.

**Proposed action:** `monitor` — known area, partially addressed, no confirmed fix yet.

**Critic verdict (approved, iteration 0):**

> Cites specific patches and driver versions directly from the evidence, appropriately hedges the unresolved issue, offers a practical workaround (driver rollback). All claims trace to the evidence package. Tone matches the review's constructive nature. `monitor` is appropriate given 0.7 confidence and an ongoing, tracked issue without a confirmed fix.

Citation chain of custody: 5 source chunks retrieved, 5 relevant, 5 cited — `source_ids_cited ⊆ relevant_ids` verified deterministically by the critic. The responder cannot cite a patch the investigator did not retrieve.

</details>

---

## How the agent processes a single review

1. **Ingest** — fetch reviews from the Steam Web API and persist them
2. **Clean + dedupe** — strip markup and drop near-duplicates above the configured threshold
3. **Classify** — assign one of 10 review categories with a confidence score (Haiku)
4. **Cluster + stats** — group by category in a rolling time window and compute priority signals
5. **Coordinator entry** — mint a `run_id` and route into the agent graph
6. **Investigate** — run hybrid retrieval (vector + BM25 → reciprocal-rank fusion → cross-encoder rerank), with up to 2 self-RAG retries on insufficient evidence; load any active cluster notes as additional context
7. **Draft** — generate a player-facing reply citing only chunks the investigator retrieved (Sonnet 4.6 — the only generative node)
8. **Critique → Human approval** — validate the evidence chain, tone, and action choice; on approval, the graph interrupts for a manual decision; on rejection, route back to the coordinator for a revise loop (max 3 iterations)

See the [annotated file tree](#9-project-layout) for exact file locations.

---

## Architecture

```
          ┌────────────────────┐
     ┌──► │    coordinator     │── done ─────┐
     │    │   (plain Python)   │             │
     │    └─────────┬──────────┘             │
     │              │ investigate            │
     │              ▼                        │
     │    ┌────────────────────┐             │
     │    │    investigator    │             │
     │    │       (LLM)        │             │
     │    └─────────┬──────┬───┘             │
     │              │      └── skipped* ─────┤
     │              │ respond                │
     │              ▼                        │
     │    ┌────────────────────┐             │
     │    │     responder      │             │
     │    │       (LLM)        │             │
     │    └─────────┬──────┬───┘             │
     │              │      └── error* ───────┤
     │              ▼                        │
     │    ┌────────────────────┐             │
     │    │       critic       │             │
     │    │       (LLM)        │             │
     │    └──┬──────┬──────┬───┘             │
     │       │      │      └── error* ───────┤
     ├──◄────┘      │ approved               │
     │              ▼                        │
     │    ┌────────────────────┐             │
     │    │   human_approval   │             │
     │    │    [interrupt]     │             │
     │    └──┬─────────────┬───┘             │
     │       │             └── approved ─────┤
     └──◄────┘                                │
                                              ▼
                                             END

  Back-edges (◄) = critic / human rejection → revision loop.
  * skipped and error paths route back through the
    coordinator, which then takes "done" → END.
```

Five nodes — `coordinator`, `investigator`, `responder`, `critic`, `human_approval`. The coordinator is plain Python routing logic, never an LLM call. The graph compiles with `interrupt_before=["human_approval"]`, so every run pauses for a human decision before completing.

**Per-node model assignments are deliberate.** The classifier, tone classifier, cluster summarizer, investigator, critic, and all eval judges run on **Haiku 4.5** (`claude-haiku-4-5-20251001`) — these are narrow extractive or classifier tasks. Only the responder runs on **Sonnet 4.6** (`claude-sonnet-4-6`, temperature 0.4), because it's the only node generating prose that a player will actually read. The heavyweight model lives where tone matters, and nowhere else.

<details>
<summary><strong>Key design decisions and alternatives rejected</strong></summary>

Seven choices that look like accidents until you know why:

- **Deterministic graph orchestration, not model-side tool selection.** The workflow is fixed — classify, retrieve, draft, critique, human-review — and the branching is knowable in advance. Letting the model pick its next step via tool-use / function calling would trade a five-line Python router for a stochastic dispatcher that costs tokens on every hop, fails in ways that are harder to test, and makes the control flow impossible to lock down in evals. Deterministic orchestration is cheaper, more reliable, and actually testable. Model-side tool calling earns its place when the path isn't knowable in advance; this path is.
- **Coordinator is plain Python, not an LLM.** Routing logic that decides "did the critic approve" or "are we past max iterations" is a five-line if-statement. Making it an LLM call would burn tokens and introduce non-determinism on the *control flow*, which is exactly where you want determinism.
- **Cross-encoder rerank scores are not surfaced to the investigator LLM.** They're uncalibrated absolute floats — anchoring the LLM's reasoning on them would amplify reranker noise into draft quality. The reranker orders results; the investigator reasons about *content*.
- **Pydantic at the LLM trust boundary, TypedDict inside the graph.** Pydantic validation on every state transition would be expensive and would catch nothing the LLM-output validators don't already catch. The boundary is where data crosses from LLM → Python; everything inside the graph is already Python.
- **Three sibling judge files instead of a base class.** `judge_grounding.py`, `judge_action.py`, and `pairwise.py` were intentionally cloned. The right shape of `_judge_base.py` is now visible because three exercised files exist — guessing it from one example would have produced the wrong abstraction.
- **`judge_error` is its own ruling, not a fallback to a substantive bucket.** If a judge LLM fails (API/parse/validation), collapsing it into `tolerable_disagreement` would silently undercount real failures. Routing infrastructure failures into a dedicated bucket makes them visible in the snapshot diff.
- **Cluster-note staleness excludes at read time, not at write time.** A 90-day-old note isn't deleted — it's filtered out of the investigator context. Preserves the audit trail; reactivation is a one-row update.

</details>

<details>
<summary><strong>Critic ↔ revision loop and run identity</strong></summary>

The critic produces two kinds of rejection. A **drafting** rejection routes the responder into a re-draft cycle without re-investigating; an **evidence** rejection routes back through the coordinator to the investigator with the critic's `retrieval_hint` seeding the next query. Each iteration writes a row to the `audit_log_iterations` table with the draft, critique, rejection reason type, and retrieval hint, so per-iteration behavior can be analyzed offline.

Every run mints a UUID `run_id` on first coordinator entry and threads it through state. Both the per-run `audit_log` row and the per-iteration `audit_log_iterations` rows carry the same `run_id`, so the full revision history of any given review can be reassembled from the database alone.

The graph terminates on: human approval, max iterations reached, human approval after max iterations, or a terminal LLM/parse error from the responder or critic.

</details>

---

<details>
<summary><strong>Retrieval pipeline (hybrid RAG)</strong></summary>

Patch notes are fetched from the Steam News API and passed through a section-aware chunker that respects update headings and bullet structure. Each chunk is dual-indexed: ChromaDB stores dense vectors from `all-MiniLM-L6-v2`, and an in-memory BM25 index handles lexical matches. At query time the two result sets are fused with **reciprocal rank fusion** (top 12), then re-ordered by a **cross-encoder reranker** (`cross-encoder/ms-marco-MiniLM-L-6-v2`, top 5). The final 5 chunks are what the investigator sees.

A deliberate choice worth noting: cross-encoder absolute scores are *not* passed to the investigator LLM. They're uncalibrated and would anchor the LLM's reasoning on uninterpretable floats. The reranker's job is ordering, not scoring; the investigator reasons about content.

If the investigator judges the retrieved evidence insufficient, it can reformulate the query and retry — up to 2 self-RAG retries before falling back to the original results. Embedding and reranker models are lazy-loaded and cached at module level so import time stays cheap.

Implementation lives in `pipeline/retrieve.py`.

</details>

<details>
<summary><strong>Feedback memory: audit log + cluster notes</strong></summary>

Two complementary stores feed information back into future runs.

The **audit log** records every completed agent run with full evidence package, draft, critique, human decision, and `run_id`. The responder loads recently approved drafts from the audit log as few-shot examples in its user message — the system gets better at sounding like the human approver over time, automatically.

**Cluster notes** are per-category institutional knowledge with a real lifecycle. Notes have a `status` of `active` or `resolved` (manually transitioned via `resolve_note.py`), and active notes older than 90 days are excluded at read time but never deleted from the database. There are four note types — `known_issue` (auto-written by the investigator when it finds sufficient evidence with at least 2 sources), `response_history` (auto-written on human approval), `human_feedback` (written on human rejection with the reviewer's comment), and `investigation`. Dedup is keyed first by `source_review_id`, then falls back to a 24-hour time window. The investigator loads active, non-stale notes for the current category as additional context in its prompt.

Both stores live in `pipeline/storage.py` (schema and DAO functions) and are managed via `resolve_note.py` for the manual lifecycle operations.

</details>

---

## Eval system

The agent matters, but the eval system is what made it iterable. Six things are worth describing because each one is a discipline boundary, not a feature.

<details>
<summary><strong>Layered scoring, cache key, and judge_error isolation</strong></summary>

**Layered scoring.** Deterministic scorers run first — they catch hard violations cheaply (`evals/scorers/deterministic.py`, `evals/scorers/gating_accuracy.py`). LLM judges only see cases the deterministic scorers flag. Three judges exist as collision-safe sibling files: `judge_grounding.py` rules on low-confidence-with-citation cases, `judge_action.py` rules on action-severity disagreements, and `pairwise.py` rules on whether the revision loop actually improved the draft.

**Cache key.** Each judge ruling is cached on disk under a 6-component sha256 key covering `run_file_basename`, `case_id`, `JUDGE_MODEL`, `skill_sha8`, `JUDGE_INPUT_VERSION`, and `user_message_sha8`. Re-runs with identical inputs are free; any input change invalidates cleanly. The three judges share the same cache directory; collisions are prevented by `skill_sha8` differing between them.

**Isolated `judge_error` bucket.** When a judge LLM fails (API error, malformed JSON, schema-validation failure), the case is ruled `judge_error` — *not* silently absorbed into a substantive bucket like `tolerable_disagreement`. The error count is reported separately in the snapshot, in the diff, and in the terminal reporter. Judge infrastructure misfires are visible immediately and can never be confused for the model genuinely thinking the case is fine.

</details>

<details>
<summary><strong>Lock-then-edit discipline and snapshot diff</strong></summary>

**Lock the gate before any prompt edit.** Before every prompt edit driven by an eval finding, the acceptance gate is written to `evals/_negative_controls_locked.md`: positive cases that must improve, negative cases that must NOT regress, semantic spot checks with hand-picked expected rulings, and an aggregate metric. The stop rule per iteration is one coordinated edit + one rerun. **Cache proof = offline re-score against the saved JSON, not a second live run** — a second live run mints a fresh `run_file_basename` and forces 100% cache miss, which proves nothing.

**Snapshot diff with schema versioning.** Every snapshot in `evals/snapshots/` carries a `schema_version` field, and the diff between two snapshots visibly annotates the version transition (e.g., `4 → 5: pairwise revision-improvement judge added`). No silent metric drift; if the schema changes, the diff says so.

**Iteration log.** `evals/M5_PLAN.md` records every edit pass with motivation, two-sided gate, and verified result. The project log doubles as a postmortem record for prompt-editing decisions — later iterations cite earlier iterations by name.

</details>

<details>
<summary><strong>Eval iteration arc (chronological)</strong></summary>

The story of how the eval system evolved through eight iterations, each producing labeled signal that the next one acted on:

1. **Baseline** — deterministic scorers only. Established gating accuracy 94.6%, action correctness, and citation chain-of-custody. No LLM judges yet.
2. **Grounding judge** — `judge_grounding.py` ruled the cases per run flagged `low_conf_with_cite`. Moved guesswork ("is this hedge dishonest?") into a labeled distribution (`honest_hedge | misleading_fix_claim | unclear`). Schema v2 → v3.
3. **Action judge** — `judge_action.py` split the `wrong_action_severity` cases into `over_escalation | missed_escalation | category_drift | tolerable_disagreement`. Schema v3 → v4. Concrete signal for the next iteration.
4. **`multi_part_complaint` prompt edit** — first eval-driven prompt edit. The case `civ7_gameplay_002` flipped `misleading_fix_claim` → `honest_hedge`, all 3 negative controls held. Clean win. The discipline pattern: lock the gate before the edit.
5. **Iteration 1 reverted — `action_severity_precedence` rule edit.** Attempted to fix the over-escalation half of `wrong_action_severity` with a prompt-level severity-overrides-confidence rule. The full eval caught the tradeoff: the narrow locked metric improved 9 → 5, but aggregate action correctness regressed 65.1% → 48.8% and 12 runs hit `max_iterations_reached` where the pre-edit state had zero. The edit was reverted to the pre-Iter1 skill state. The failure is documented in the project log rather than papered over — and its root-cause diagnosis (the rubric itself conflated two axes) is what motivated Iteration 4.
6. **Iteration 2 structural pass — pairwise revision-improvement scorer.** Answers "is the revision loop earning its tokens?" by comparing the iter-0 draft against the final approved draft per case. A deterministic normalize-equal shortcut handles cosmetic-identical revisions; the LLM judges the rest. Schema v4 → v5. On the current run it surfaced a clear signal: 34 of 40 judged cases were byte-identical after normalization, meaning the revision loop is mostly cosmetic.
7. **Iteration 3 — retrieval scorer + gold recalibration.** Split the old `recall_at_k_mean` into co-equal source-level and post-filter recall numbers (the old single number conflated retriever output with the investigator's Self-RAG filter), added a concept hit-rate companion metric (did the retriever land on the right patch family at all, independent of how many chunks it covered), separated out gate false-skip cases from the recall denominator, added a source→relevant drop diagnostic for the filter, and introduced explicit `{"any_of": [...]}` equivalence groups to the gold so chunk-ID-strict matching doesn't penalize the retriever for finding a semantically equivalent chunk from a different patch version. Labeled as a **scorer recalibration pass**: no agent or pipeline changes, offline rescore against the frozen run JSON, all non-retrieval metrics byte-identical to the prior snapshot (machine-asserted in `evals/_recalibrate_rescore.py`). Schema v5 → v6.
8. **Iteration 4 — single-axis rubric revision (partial success).** Root-cause fix for Iteration 1: refactor the four actions from the old mixed "technical vs design" + "severe vs mild" axes into a clean single-axis hierarchy along **actionability + priority** (`no_action < monitor < investigate < escalate`), with a `monitor` escape hatch for recurring subjective-but-meaningful pain signals. Coordinated edit across all three rubric-touching skills (`draft-response`, `critique-draft`, `judge-action`) so responder, critic, and judge share the same grading semantics. Initial rerun regressed both action correctness and critic approval; the remedial coordinated edit (three fixes in lockstep: escalate gate widened with a heated-adjective guardrail, pricing carve-out on the recurring-signal clause across all three skills, critic check #6 trimmed to 3 load-bearing fail conditions) recovered action correctness above baseline (0.628 → 0.651) and held the crash-word canary, but the critic-health gate did not recover (0.714 → 0.474 approval overall, 7 cases hit `max_iterations_reached`). The failure shape is "expensive but correct" — cases hitting max_iters still land on the right final answer, so correctness recovered while the critic's first-pass reject rate stayed elevated because check #6's inlined rung definitions gave it more decision surface even after the fail list shrank. Stop-rule budget exhausted; the critic-workload gap is now the live open problem (see Open gaps).

The point of the arc: each iteration produced labeled signal *and* the discipline kept the failures visible. **Iteration 1 reverted** and **Iteration 4 partial success** are named as such in the project log, not papered over — and the naming itself is a win, because the eval infrastructure is what makes an honest mid-ladder verdict possible at all.

</details>

---

## Results

The table below reports the latest full-eval run (56 cases) after the Iteration 4 remedial coordinated edit. Provenance: `evals/snapshots/snapshot_20260408_140847.json` (schema v6, run file `run_20260408_140734.json`). The rubric refactor recovered action correctness above baseline (0.628 → 0.651) but the critic-health gate did not recover — the failure shape is "expensive but correct" (see Iteration 4 in the arc above and the critic-workload bullet in [Open gaps](#open-gaps) for the full diagnosis).

| Metric | Value |
|---|---|
| Cases evaluated | **56** (across 5 games, 11–12 cases each) |
| Stop reasons | 36 human_approved, 13 no_response_needed, 7 max_iterations_reached |
| Gating accuracy | **94.6%** (10 true_skip / 43 true_retrieve / 3 false_skip / 0 false_retrieve) |
| Action correctness | **65.1%** (28 / 43, excludes 13 no-response cases) |
| Citation chain of custody (`subset_ok_rate`) | **100%** |
| Hard grounding violations | **0** |
| Retrieval — source recall@5 | **0.381** (raw retriever top-5, 26 cases eligible) |
| Retrieval — post-filter recall@5 | **0.272** (after investigator's Self-RAG filter, same cases) |
| Retrieval — concept hit-rate (source) | **16 of 26** cases (≥1 required concept in raw top-5) |
| Chunks lost to investigator filter | **5** across 4 cases |
| First-pass critic approval | 55.8% |
| Critic approval overall (all iterations) | 47.4% |
| Mean iterations to approval | 0.528 |
| `wrong_action_severity` failure mode | 15 → judge breakdown: **3 over_escalation**, 4 missed_escalation, 6 category_drift, 1 tolerable_disagreement, 1 judge_error |
| Pairwise revision scorer | 36 judged → **10 improved**, 24 neutral (24 via deterministic shortcut), 2 regressed |
| `judge_error` (all judges) | **1** (action judge; single transient infra misfire, not a pattern) |
| Total tokens | 1,320,190 |

Earlier snapshots reported a single `recall@k` number; this has been split into source-level and post-filter recall because the original metric conflated retriever output with investigator filtering. 2 cases are excluded from the recall denominator because the runtime gate skipped retrieval (already counted as a gating false-skip). See Iteration 3 in the iteration arc below for the full rationale.

#### Why these weaker numbers look the way they do

- **Retrieval recall (0.381 source / 0.272 post-filter).** The 0.109 source→post-filter gap is the investigator's Self-RAG filter being conservative — it drops 5 chunks across 4 cases where the retriever *did* surface the right evidence — and the hard-zero cases reflect section-aware chunking fragmenting a single fix description across multiple version-stamped patch chunks (e.g., the same AMD driver compat fix appears in Ver.1.030, Ver.1.040, and Ver.1.041 as separate IDs), so even after Iteration 3's `{any_of: [...]}` gold recalibration some cases stay at zero because the required fact lives in a chunk the retriever didn't rank into the top 5. The tiny source/post-filter movement vs the prior snapshot (0.385→0.381, 0.269→0.272) is run-to-run noise from agent non-determinism — the responder/critic edits in Iteration 4 run downstream of retrieval and cannot causally affect it.
- **Action correctness (65.1%).** Improved from the pre-Iter4 baseline of 62.8%. Iteration 4's single-axis rubric refactor was built to address the old "technical vs design" / "severe vs mild" conflation that Iteration 1 tried and failed to patch. The judge breakdown now shows 4 missed_escalation + 3 over_escalation + 6 category_drift + 1 tolerable_disagreement; the dominant remaining error is still category drift on the subjective `monitor` ↔ `investigate` boundary (6 of 14 non-error wrong-severity cases), but the missed/over imbalance no longer points at a clean directional bias.
- **Critic approvals (55.8% iter-0, 47.4% overall) and 7 `max_iterations_reached`.** This is the Iteration 4 critic-health regression and it did not recover in the remedial pass. Drafting-type rejections (36) are ~2.5× the pre-Iter4 baseline; `approval_rate_overall` is computed per-iteration not per-case, so the 7 max-iter cases each add 2–3 un-approved iters to the denominator and amplify the drop. Root cause diagnosed in the Iter4 M5_PLAN entry: the critic's check #6 got longer in content (inlined rung definitions, pricing carve-out, heated-adjective guardrail) even though the fail list shrank from 6 to 3, giving the critic more total decision surface than at baseline. The *asymmetry* is what makes this a partial success rather than an outright revert: the action correctness number is scored on the *final* draft, and cases that hit max_iters still land on the right final answer often enough to pull `action.correct_rate` above baseline. The failure shape is "expensive but correct," not "wrong" — offline re-planning rather than another skill edit is the next move.
- **Cosmetic revisions (24 of 36 neutral).** Still dominant but with more substantive movement than the pre-Iter4 run (6→10 improved, 0→2 regressed out of 36 judged). Because the critic is rejecting more drafts on tone/structure and those rejections now cascade through more iterations, the pairwise judge is seeing more genuinely-revised final drafts — net: more real improvements surfaced AND two regressions that were not present before, a direct cost of the extra revision work.

### Key takeaways

- **Grounding and citation discipline are strong.** 100% citation chain-of-custody, zero hard grounding violations. The evidence-tracking design (`source_ids → relevant_ids → source_ids_cited`, critic verifies the subset relationship) is doing its job — the responder cannot fabricate citations.
- **Iteration 4 partial success: correctness up, critic-health regressed.** Action correctness recovered to 65.1% (above the 62.8% pre-Iter4 baseline) after the single-axis rubric refactor, and 2 of 3 recovery targets + 7 of 8 preservation targets including the crash-word canary held. But the critic's first-pass reject rate climbed — 55.8% iter-0 approval, 7 cases hitting `max_iterations_reached` vs 3 before — because the critic's action check got more decision surface even as its fail list shrank. The eval caught both halves of the tradeoff: the rubric refactor is right on its own terms, but the critic workload problem is now the live open gap.
- **Multi-iteration revisions — more real work, mostly still cosmetic.** Of 36 multi-iteration approved cases judged, 24 were `revision_neutral` (all caught by the deterministic normalize-equal shortcut), 10 were `revision_improved`, and 2 were `revision_regressed`. The improved count roughly doubled vs the pre-Iter4 run (6 → 10) because the critic is now rejecting more drafts on tone/structure and the resulting longer revision chains produce materially different final drafts — but the cost is that two of those chains ended worse than where they started. Tightening the critic's rejection bar is the same lever that would fix the critic-health gap.

<details>
<summary><a id="open-gaps"></a><strong>Open gaps</strong></summary>

- **Retrieval: recall vs concept hit.** Two companion numbers: source-level recall@5 is **0.381**, post-filter recall@5 is **0.272**. The 0.109 gap is the investigator's Self-RAG filter dropping **5 must-include chunks across 4 cases** — worth auditing whether that filter is over-pruning. The concept hit-rate (≥1 required concept retrieved) is **16 of 26 cases** on the source side and 14 of 26 after the filter, which tells a different story than recall: the retriever is landing on the right patch family for most cases (reach) but not surfacing every required chunk within that family (coverage). The cases that remain at hard zero after gold recalibration are legitimate retrieval misses concentrated in `civ7_gameplay_002`, `starfield_gameplay_001`, `poe2_balance_001/003`. Next attempts: (a) inspect the filter drops, (b) tighten section-aware chunking on multi-version patches to reduce chunk-ID fragmentation, (c) defer recall@10 until it can be populated without changing investigator inputs.

- **Critic workload — the live Iteration 4 open problem.** The remedial rubric refactor recovered action correctness (0.628 → 0.651) but the critic's first-pass reject rate climbed and `approval_rate_overall` dropped from 0.714 → 0.474. Drafting-type rejections went from 14 → 36 and `max_iterations_reached` from 3 → 7. Root cause: critic check #6 got *longer in content* (inlined rung definitions, pricing carve-out, heated-adjective guardrail) even though the fail list shrank from 6 to 3, giving the critic more total decision surface than at baseline. The failure shape is "expensive but correct" — cases that hit max_iters still land on the right final answer, which is why correctness recovered while critic-health did not. Stop-rule budget for Iteration 4 is exhausted; the next move is offline re-planning (candidates: move rung definitions out of check #6 body and into a header block the critic only consults on ambiguity, or fold the heated-adjective guardrail into the responder-only side so the critic's fail-condition vocabulary stays smaller). Full diagnosis in `evals/_negative_controls_locked.md` Iteration 4 section and `evals/M5_PLAN.md` Iteration 4 entry.

- **Iteration 1 reverted — context for Iteration 4.** The `action_severity_precedence` rule edit targeted the over-escalation half of the `wrong_action_severity` bucket and moved the narrow locked metric 9 → 5, but the full eval caught that it regressed aggregate action correctness from 65.1% → 48.8% and introduced 12 non-convergent runs. The edit was reverted. Its root-cause diagnosis — that the original rubric conflated "technical vs design" with "severe vs mild" — is what motivated the Iteration 4 single-axis refactor, which recovered correctness above the post-revert baseline. The history is documented here as the through-line from Iter1 → Iter4 rather than as a standalone revert story.

- **Iteration 2 structural pass.** The pairwise revision-improvement scorer ran clean structurally on the current run: 36 judged, 0 `judge_error` on pairwise (the one `judge_error` this run was on the action judge, not pairwise), 24 caught by the deterministic normalize-equal shortcut. Semantic spot checks (hand-picked case IDs with expected rulings) are still queued.

- **`_judge_base.py` extraction is queued but not done.** Three sibling judge files exist now (`judge_grounding.py`, `judge_action.py`, `pairwise.py`); the abstraction shape is finally visible, but the cost of premature DRY is higher than the cost of one more clone.

</details>

---

## Tech stack

- **Language:** Python 3.12
- **LLM API:** Anthropic Claude (Haiku 4.5 for classifiers/investigator/critic/judges, Sonnet 4.6 for the responder)
- **Agent framework:** LangGraph (used directly, not via LangChain)
- **Validation:** Pydantic (only at LLM trust boundaries)
- **Storage:** SQLite (reviews, audit log, audit log iterations, cluster notes, classifications, schema version)
- **Vector store:** ChromaDB
- **Embeddings:** sentence-transformers (`all-MiniLM-L6-v2`)
- **Reranker:** cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`)
- **Lexical search:** rank-bm25 (in-memory, rebuilt per run)
- **Other:** pandas, python-frontmatter (for skill files), python-dotenv

---

<details>
<summary><a id="9-project-layout"></a><strong>Project layout (annotated file tree)</strong></summary>

```
steam-review-agent/
├── main.py                      # Pipeline entry point — fetch → clean → classify → cluster → stats
├── test_agent.py                # Single-review smoke test against the live agent graph
├── test_graph.py                # End-to-end graph smoke test on a hardcoded review (real LLM calls; ~30s; auto-approves at human gate)
├── resolve_note.py              # CLI for cluster-notes lifecycle (list/resolve/reactivate)
├── config.py                    # All configuration — models, temperatures, thresholds, env vars
├── utils.py                     # Shared helpers (load_skill with frontmatter parsing, etc.)
├── requirements.txt             # Pinned dependency list (Python 3.12)
├── CLAUDE.md                    # Internal spec for Claude Code (project conventions)
│
├── pipeline/                    # Data ingestion + retrieval indexing
│   ├── ingest_reviews.py          # Fetch Steam reviews via Web API
│   ├── ingest_patch_notes.py      # Fetch + classify Steam patch notes
│   ├── clean.py                   # Markup stripping, near-duplicate filtering
│   ├── classify.py                # Review category classification (Haiku)
│   ├── cluster.py                 # Time-windowed category clustering + priority signals
│   ├── stats.py                   # Aggregate statistics over reviews + clusters
│   ├── chunk.py                   # Section-aware patch-note chunking
│   ├── retrieve.py                # Hybrid RAG: vector + BM25 → RRF → cross-encoder rerank
│   ├── storage.py                 # SQLite schema, DAO functions, cluster-note lifecycle
│   ├── keywords.py                # Keyword extraction helpers
│   └── retry.py                   # Retry decorator for flaky API calls
│
├── agent/                       # LangGraph multi-agent system
│   ├── state.py                   # AgentState TypedDict
│   ├── models.py                  # Pydantic models for LLM-output trust boundaries
│   ├── graph.py                   # StateGraph construction, conditional edges, checkpointing
│   ├── utils.py                   # Shared agent helpers (token accumulation, evidence formatting)
│   └── nodes/
│       ├── coordinator.py         # Plain-Python routing (mints run_id, decides next node)
│       ├── investigator.py        # Hybrid retrieval + self-RAG retries + cluster-note loading
│       ├── responder.py           # Drafts player-facing reply (Sonnet 4.6, only LLM-generative node)
│       ├── critic.py              # Validates evidence chain, tone, action — writes per-iter audit
│       └── human_approval.py      # Human-in-the-loop interrupt gate
│
├── skills/                      # Agent skills — SKILL.md files loaded by Python via load_skill()
│   ├── classify-review/           # Category classifier prompt
│   ├── classify-tone/             # Tone classifier prompt
│   ├── analyze-cluster/           # Cluster summarization prompt
│   ├── investigate-evidence/      # Investigator retrieval-reasoning prompt
│   ├── draft-response/            # Responder draft template
│   ├── critique-draft/            # Critic quality-gate checklist
│   ├── judge-grounding/           # Eval judge: low-confidence citation classifier
│   ├── judge-action/              # Eval judge: action severity classifier
│   └── judge-pairwise/            # Eval judge: revision improvement classifier
│
├── evals/                       # Evaluation harness
│   ├── run_evals.py               # Main eval runner (loads cases, runs agent, scores, snapshots)
│   ├── reporter.py                # Terminal-friendly score report
│   ├── snapshot.py                # Snapshot writer + schema versioning + diff annotation
│   ├── failure_modes.py           # Deterministic failure-mode taxonomy
│   ├── M5_PLAN.md                 # Iteration log — every edit, gate, verification result
│   ├── _negative_controls_locked.md  # Pre-edit gate locks for every prompt iteration
│   ├── _lock_controls.py          # CLI helper to materialize lock blocks from a run JSON
│   ├── _remedial_rerun.py         # Partial rerun harness used by Iteration 1 remediation
│   ├── refresh_classifier_fields.py
│   ├── scorers/
│   │   ├── deterministic.py       # Deterministic scorers (action_correctness, etc.)
│   │   ├── gating_accuracy.py     # Retrieval-gating scorer
│   │   ├── judge_grounding.py     # LLM judge — low_conf_with_cite ruling
│   │   ├── judge_action.py        # LLM judge — wrong_action_severity ruling
│   │   └── pairwise.py            # Iteration 2 LLM judge — revision improvement
│   └── test_sets/
│       ├── golden.json            # Hand-curated eval cases with expected actions + must_include sources
│       └── regression.json        # Regression seeds added during eval-driven prompt edits
│
└── .claude/
    └── skills/                  # Claude Code skills — project conventions (NOT loaded by Python)
```

The naming clash between `skills/` and `.claude/skills/` is a project gotcha. The two are completely different systems despite the shared directory name: `skills/` holds SKILL.md files loaded by Python at runtime via `utils.load_skill()`, while `.claude/skills/` holds project-convention skills read by Claude Code only.

</details>

---

<details>
<summary><a id="10-run-it-yourself"></a><strong>Run it yourself (full setup and entry points)</strong></summary>

### Prerequisites

- Python 3.12 (developed against 3.12.7)
- Anthropic API key (`CLAUDE_API_KEY`) — required for any agent run. Note: this repo reads `CLAUDE_API_KEY` rather than Anthropic's default `ANTHROPIC_API_KEY`, to stay compatible with Claude Code's environment.
- Steam Web API key (`STEAM_API_KEY`) — required only for fetching reviews and patch notes
- HuggingFace token (`HF_TOKEN`) — required for downloading the embedding and reranker models on first use

### Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set env vars in .env (loaded by python-dotenv) OR export them in your shell:
export CLAUDE_API_KEY=sk-ant-...
export STEAM_API_KEY=...
export HF_TOKEN=...
```

### Entry points

| Command | What it does |
|---|---|
| `python main.py <app_id> [max_reviews]` | Run the data pipeline end-to-end: fetch → clean → classify → cluster → stats. Default `max_reviews=500`. Add `--skip-fetch` to reuse rows already in the database. |
| `python test_agent.py [--category CAT] [--review-id ID]` | Run a single real review through the live agent graph with real LLM calls. Pauses at the human approval gate for a manual decision. Use `--list` to print the available cases. |
| `python test_graph.py` | End-to-end graph smoke test on a hardcoded "game crashes in dungeon" review. Auto-approves at the human gate. ~3-5 real LLM calls, ~30s wall clock, costs a few cents. |
| `python evals/run_evals.py` | Run the full eval suite (56 golden cases). `--quick` for a subset, `--case-id <id>` for a single case, `--app-id <id>` and `--category <cat>` for filters. Writes a snapshot to `evals/snapshots/` and a raw run JSON to `evals/runs/`. |
| `python resolve_note.py {list <app_id> <category> \| resolve <note_id> \| reactivate <note_id>}` | Manage the cluster notes lifecycle. |

### Expected runtime and cost (rough)

- **Data pipeline** (500 reviews, fresh fetch): ~5–10 min wall clock; cost dominated by classification (~500 Haiku calls × ~300 input tokens ≈ ~$0.10).
- **Single-review agent run** (`test_agent.py`): ~30–60 sec including retrieval; ~5–15K total tokens depending on revision iterations; <$0.05 per review (Sonnet on the responder, Haiku elsewhere).
- **Full eval suite** (`run_evals.py`, 56 cases): ~25–35 min wall clock; ~1.32M total tokens (per the latest snapshot); on the order of $1–2 per full run with all judges enabled.
- **Cached judge re-score** (offline against a saved run JSON): ~30 sec; $0 (cache hit on every flagged case).

Numbers are approximations grounded in the latest snapshot's `total_tokens=1,320,190`. Actual cost depends on Anthropic pricing at run time.

</details>

---

<details>
<summary><strong>Design notes — what I learned</strong></summary>

Three things I'd carry into the next project of this shape:

- **Eval before prompt edit, every time.** The first prompt edit driven by an eval finding (the `multi_part_complaint` edit) was a clean win because the gate was locked before the edit. The first edit *not* preceded by careful negative-control locking (Iteration 1 first pass) blew up immediately — over_escalation jumped from 4 to 11. The lock file is the discipline that makes prompt edits safe; without it, "I'll just tweak the prompt" is gambling with stochastic feedback.
- **Isolated `judge_error` buckets are non-negotiable.** Collapsing infrastructure failures into a substantive bucket silently undercounts real failures. Every judge in this project routes API/parse/validation errors into a dedicated `judge_error` ruling that surfaces in the snapshot diff. The cost is one extra column; the benefit is that a misfiring judge can never look like a passing eval.
- **Sibling-clone, then extract.** Three judge files (`judge_grounding.py`, `judge_action.py`, `pairwise.py`) were intentionally cloned rather than DRY'd up front. The right shape of the abstraction is now visible because the three files exist *and have been exercised on real failing cases*, not because someone guessed at the abstraction from a single example. The extraction is queued; the cost of one more clone was lower than the cost of locking in the wrong base class.

</details>

<details>
<summary><strong>What's next</strong></summary>

- **Fix the Iteration 4 critic-workload regression.** Action correctness recovered to 65.1% but `critic.approval_rate_overall` dropped from 0.714 → 0.474 and `max_iterations_reached` climbed from 3 → 7 because the critic's check #6 got more decision surface (inlined rung definitions, pricing carve-out, heated-adjective guardrail) even after the fail list shrank from 6 to 3. This is the live open gap. Candidate fixes (offline re-planning, not another skill edit this cycle): move rung definitions out of check #6 body and into a header block the critic only consults on ambiguity; or fold the heated-adjective guardrail into the responder-only side so the critic's fail-condition vocabulary stays smaller.
- **Improve retrieval recall.** Source recall@5 of 0.381 / post-filter recall@5 of 0.272 are the weakest numbers on the board. Iteration 3 split the old single `recall@k` into these two companion metrics and added a concept hit-rate — the next attempt is to inspect the 5 chunks the investigator's Self-RAG filter drops across 4 cases and tighten section-aware chunking on multi-version patches.
- **Finish semantic verification for the pairwise revision judge.** The scorer ran clean structurally but hand-validated spot checks are deferred to the next clean (non-remedial) eval run.
- **Extract shared judge infrastructure.** Three sibling judge files exist now (`judge_grounding.py`, `judge_action.py`, `pairwise.py`) — the right shape of a `_judge_base.py` abstraction is finally visible and ready to pull out.

</details>
