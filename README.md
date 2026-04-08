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

Ingests Steam reviews and routes each one through a multi-agent workflow: classify → retrieve patch notes → draft an evidence-grounded reply → critic gate → human approval. The interesting problem isn't generation — it's *defensible* generation that traces every claim back to a retrieved patch chunk.

Approved drafts become few-shot examples for future runs; human-rejected drafts become cluster notes that warn the next investigator about known issues in the same category.

**Not** a chatbot. **Not** an autoresponder. The human-in-the-loop gate is non-negotiable — the agent does the research and drafts; the human ships it.

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
     └──◄────┘                               │
                                             ▼
                                            END

  Back-edges (◄) = critic / human rejection → revision loop.
  * skipped and error paths route back through the
    coordinator, which then takes "done" → END.
```

Five nodes: `coordinator` (plain Python), `investigator`, `responder`, `critic`, `human_approval`. Graph compiles with `interrupt_before=["human_approval"]` — every run pauses for human decision before completing.

**Per-node model assignments.** Classifier, tone classifier, cluster summarizer, investigator, critic, and eval judges all run on **Haiku 4.5** — narrow extractive/classifier tasks. Only the responder runs on **Sonnet 4.6** (temp 0.4) because it's the only node generating player-visible prose. Heavyweight model where tone matters, nowhere else.

<details>
<summary><strong>Key design decisions and alternatives rejected</strong></summary>

Seven choices that look like accidents until you know why:

- **Deterministic graph orchestration, not model-side tool selection.** The workflow is fixed and the branching is knowable in advance. Model-side tool-calling would trade a five-line Python router for a stochastic dispatcher that costs tokens every hop and can't be locked down in evals. Tool-calling earns its place when the path isn't knowable; this path is.
- **Coordinator is plain Python, not an LLM.** Routing logic is a five-line if-statement. An LLM here would burn tokens and add non-determinism to the *control flow* — exactly where you want determinism.
- **Reranker scores are not passed to the investigator.** Uncalibrated floats anchor LLM reasoning on reranker noise. Reranker orders; investigator reasons about content.
- **Pydantic at LLM trust boundary, TypedDict inside the graph.** Validation on every state transition is expensive and catches nothing new. The boundary is LLM → Python; inside the graph is already Python.
- **Three sibling judge files, not a base class.** Cloned intentionally. The right `_judge_base.py` shape is visible *because* three exercised files exist — guessing from one would pick the wrong abstraction.
- **`judge_error` is its own ruling.** Collapsing API/parse/validation failures into `tolerable_disagreement` silently undercounts real failures. Dedicated bucket surfaces infra misfires in the snapshot diff.
- **Cluster-note staleness filters at read time, not write time.** 90-day-old notes are filtered out of investigator context, not deleted. Preserves audit trail; reactivation is one row.

</details>

<details>
<summary><strong>Critic ↔ revision loop and run identity</strong></summary>

**Two rejection kinds.** A `drafting` rejection routes the responder into a re-draft without re-investigating; an `evidence` rejection routes back through the coordinator to the investigator, with the critic's `retrieval_hint` seeding the next query. Each iteration writes to `audit_log_iterations` (draft, critique, reason type, hint) for offline analysis.

**Run identity.** Coordinator mints a UUID `run_id` on first entry; both `audit_log` and `audit_log_iterations` carry it, so a review's full revision history can be reassembled from the DB alone.

**Termination.** Human approval, max iterations reached, human approval after max iterations, or terminal LLM/parse error.

</details>

---

<details>
<summary><strong>Retrieval pipeline (hybrid RAG)</strong></summary>

Patch notes → section-aware chunker → dual index (ChromaDB `all-MiniLM-L6-v2` + in-memory BM25). Query time: **RRF fusion** (top 12) → **cross-encoder rerank** (`ms-marco-MiniLM-L-6-v2`, top 5). The investigator sees the final 5 chunks.

Reranker absolute scores are **not** passed to the investigator — they're uncalibrated and anchoring on them would amplify noise. The reranker orders; the investigator reasons about content.

Up to 2 self-RAG retries on insufficient evidence (query reformulation) before falling back. Embedding and reranker models lazy-loaded and cached at module level.

Implementation: `pipeline/retrieve.py`.

</details>

<details>
<summary><strong>Feedback memory: audit log + cluster notes</strong></summary>

Two stores feed forward into future runs.

**Audit log** — every completed run with full evidence, draft, critique, human decision, `run_id`. The responder loads recent approvals as few-shot examples, so the system adapts to the human approver's voice over time.

**Cluster notes** — per-category institutional knowledge with a real lifecycle. `active`/`resolved` status (transitioned via `resolve_note.py`); notes older than 90 days are filtered at read time, never deleted. Four types: `known_issue` (auto from investigator, ≥2 sources), `response_history` (auto on approval), `human_feedback` (on rejection), `investigation`. Dedup: `source_review_id` first, then 24h time window. Investigator loads active, non-stale notes for the current category.

Both live in `pipeline/storage.py`.

</details>

---

## Eval system

The agent matters; the eval system is what made it iterable. Each block below is a discipline boundary, not a feature.

<details>
<summary><strong>Layered scoring, cache key, and judge_error isolation</strong></summary>

**Layered scoring.** Deterministic scorers run first (`deterministic.py`, `gating_accuracy.py`) and catch hard violations cheaply. LLM judges only see cases the deterministic scorers flag. Three sibling judges: `judge_grounding.py` (low-confidence citations), `judge_action.py` (action-severity disagreements), `pairwise.py` (did the revision loop improve the draft).

**Cache key.** 6-component sha256 over `run_file_basename`, `case_id`, `JUDGE_MODEL`, `skill_sha8`, `JUDGE_INPUT_VERSION`, `user_message_sha8`. Identical inputs → free re-run; any input change invalidates cleanly. `skill_sha8` prevents cross-judge collisions in the shared cache directory.

**Isolated `judge_error` bucket.** API / parse / schema failures are ruled `judge_error`, not absorbed into `tolerable_disagreement`. Surfaces separately in the snapshot, diff, and reporter — a misfiring judge can never pass as healthy.

</details>

<details>
<summary><strong>Lock-then-edit discipline and snapshot diff</strong></summary>

**Lock the gate before any prompt edit.** Acceptance gate written to `evals/_negative_controls_locked.md` before every eval-driven edit: positive cases that must improve, negative cases that must not regress, semantic spot checks, aggregate metric. Stop rule: one coordinated edit + one rerun. **Cache proof = offline re-score against the saved JSON**, not a second live run (a second live run mints a fresh `run_file_basename` and forces 100% cache miss — proves nothing).

**Snapshot diff with schema versioning.** Every snapshot carries `schema_version`; the diff annotates version transitions (e.g., `4 → 5: pairwise judge added`). No silent metric drift.

**Iteration log.** `evals/M5_PLAN.md` records every edit pass — motivation, two-sided gate, verified result. Later iterations cite earlier ones by name.

</details>

<details>
<summary><strong>Eval iteration arc (chronological)</strong></summary>

Eight iterations, each producing labeled signal the next one acted on:

1. **Baseline** — deterministic scorers only. Established gating accuracy, action correctness, citation chain-of-custody. No LLM judges.
2. **Grounding judge** — `judge_grounding.py` rules on `low_conf_with_cite` cases: `honest_hedge | misleading_fix_claim | unclear`. Schema v2 → v3.
3. **Action judge** — `judge_action.py` splits `wrong_action_severity` into `over_escalation | missed_escalation | category_drift | tolerable_disagreement`. Schema v3 → v4.
4. **`multi_part_complaint` prompt edit** — first eval-driven prompt edit. `civ7_gameplay_002` flipped `misleading_fix_claim` → `honest_hedge`, all negative controls held. Clean win. Established the lock-before-edit discipline.
5. **Iteration 1 reverted — `action_severity_precedence` rule.** Narrow locked metric improved 9 → 5 but aggregate action correctness regressed 65.1% → 48.8% and 12 runs hit max_iterations. Reverted. Root-cause diagnosis (rubric conflated two axes) motivated Iteration 4.
6. **Iteration 2 — pairwise revision-improvement scorer.** Answers "is the revision loop earning its tokens?" Deterministic normalize-equal shortcut for cosmetic revisions, LLM judges the rest. Schema v4 → v5. Surfaced that the revision loop was mostly cosmetic.
7. **Iteration 3 — retrieval scorer + gold recalibration.** Split `recall_at_k_mean` into source-level and post-filter recall, added concept hit-rate, introduced `{"any_of": [...]}` equivalence groups in gold. Scorer-only pass: no agent changes, offline rescore, non-retrieval metrics byte-identical to prior snapshot. Schema v5 → v6.
8. **Iteration 4 — single-axis rubric revision (partial success).** Root-cause fix for Iter1: refactor the four actions into one axis (`actionability + priority`). Coordinated edit across `draft-response` + `critique-draft` + `judge-action`. Recovered action correctness above baseline (0.628 → 0.651) but critic-health regressed (0.714 → 0.474 approval overall, 7 max-iter cases). Failure shape: "expensive but correct" — see Open gaps for the critic-workload diagnosis.

Each iteration produced labeled signal *and* kept failures visible. **Iteration 1 reverted** and **Iteration 4 partial success** are named as such in the project log — honest mid-ladder verdicts are what the eval infrastructure exists to make possible.

</details>

---

## Results

Latest full-eval run (56 cases) after the Iteration 4 remedial edit. Source: `snapshot_20260408_140847.json` (schema v6). Action correctness recovered above baseline (0.628 → 0.651), but the critic-health gate did not. See [Open gaps](#open-gaps) for the "expensive but correct" diagnosis.

| Metric | Value |
|---|---|
| Cases evaluated | **56** (across 5 games, 11–12 cases each) |
| Gating accuracy | **94.6%** (10 true_skip / 43 true_retrieve / 3 false_skip / 0 false_retrieve) |
| Action correctness | **65.1%** (28 / 43, excludes 13 no-response cases) |
| Citation chain of custody (`subset_ok_rate`) | **100%** |
| Hard grounding violations | **0** |
| Retrieval — source recall@5 | **0.381** (raw retriever top-5, 26 cases eligible) |
| Retrieval — post-filter recall@5 | **0.272** (after investigator's Self-RAG filter, same cases) |
| First-pass critic approval | 55.8% |
| Critic approval overall (all iterations) | 47.4% |
| Pairwise revision scorer | 36 judged → **10 improved**, 24 neutral (24 via deterministic shortcut), 2 regressed |
| Total tokens | 1,320,190 |

The old single `recall@k` has been split into source-level and post-filter recall (the original metric conflated retriever output with investigator filtering). 2 gate-false-skip cases are excluded from the recall denominator. Rationale in Iteration 3 above.

#### Why these weaker numbers look the way they do

- **Retrieval recall (0.381 source / 0.272 post-filter).** The 0.109 gap is the investigator's Self-RAG filter dropping **5 chunks across 4 cases** where the retriever did surface the right evidence. Concept hit-rate (did the retriever land on the right patch family at all) is **16 / 14 of 26** source / post-filter — reach is better than recall suggests. Remaining hard-zero cases are chunking fragmentation across version-stamped patches. Movement vs the prior snapshot (0.385→0.381, 0.269→0.272) is run-to-run noise — Iter4 edits run downstream of retrieval.
- **Action correctness (65.1%).** Up from 62.8% pre-Iter4. Judge breakdown of the 15 `wrong_action_severity` cases: **3 over + 4 missed + 6 drift + 1 tolerable + 1 judge_error** (transient infra misfire, not a pattern). Dominant remaining failure is `monitor` ↔ `investigate` drift (6 / 14 non-error cases).
- **Critic approvals (55.8% iter-0, 47.4% overall) and 7 `max_iterations_reached`.** The Iter4 regression that didn't recover (pre-Iter4: 79.1 / 71.4 / 3 max-iter). Drafting-type rejections went 14 → 36. Root cause: check #6 got more decision surface (inlined rung definitions, pricing carve-out, heated-adjective guardrail) even as the fail list shrank from 6 to 3. Key asymmetry: correctness is scored on the final draft, so max-iter cases still land right — failure shape is "expensive but correct," not "wrong."
- **Cosmetic revisions (24 of 36 neutral).** All 24 neutrals hit the deterministic normalize-equal shortcut. The remaining 12 split 10 improved / 2 regressed (pre-Iter4: 6 / 0) — more real revision work, but at the cost of two regressed chains. Same lever — tightening the critic's rejection bar — fixes both this and the critic-health gap.

### Key takeaways

- **Grounding and citation discipline are strong.** 100% citation chain-of-custody, zero hard grounding violations. The `source_ids → relevant_ids → source_ids_cited` subset check is doing its job — the responder cannot fabricate citations.
- **Iteration 4 partial success.** Correctness recovered above baseline (62.8% → 65.1%), crash-word canary held, 2/3 recovery + 7/8 preservation targets hit. But the critic's first-pass reject rate climbed and 7 cases hit max-iter. The rubric refactor is right on its own terms; critic workload is the new live open gap.
- **Revisions — more real work, mostly still cosmetic.** 24/36 neutral via the deterministic shortcut, 10 improved, 2 regressed. The improved count doubled vs pre-Iter4 but at the cost of two regressed chains. Tightening the critic's rejection bar fixes both halves.

<details>
<summary><a id="open-gaps"></a><strong>Open gaps</strong></summary>

- **Retrieval recall vs concept hit.** Source 0.381 / post-filter 0.272; concept hit-rate 16 / 14 of 26 cases. Reach is better than recall — the retriever lands on the right patch family but misses coverage within it. Hard-zero cases concentrate in `civ7_gameplay_002`, `starfield_gameplay_001`, `poe2_balance_001/003`. Next: (a) audit filter drops, (b) tighten section-aware chunking on multi-version patches, (c) defer recall@10.

- **Critic workload — live Iter4 problem.** Approval 0.714 → 0.474, drafting rejections 14 → 36, max-iter 3 → 7. Root cause: check #6 got longer in content (inlined rung definitions, pricing carve-out, heated-adjective guardrail) even as the fail list shrank from 6 to 3. Failure shape: "expensive but correct." Candidate fixes (offline re-planning, not another skill edit this cycle): move rung definitions into a header block the critic only consults on ambiguity, or fold the heated-adjective guardrail into the responder-only side. Full diagnosis in `evals/_negative_controls_locked.md` and `evals/M5_PLAN.md` Iter4 entries.

- **Iteration 1 reverted — Iter4 context.** `action_severity_precedence` moved the locked metric 9 → 5 but regressed aggregate correctness 65.1% → 48.8% and introduced 12 non-convergent runs. Reverted. Its diagnosis (rubric conflated two axes) motivated the Iter4 single-axis refactor.

- **Pairwise semantic spot checks — deferred.** Structurally clean (36 judged, 0 pairwise judge_error), but hand-validated spot checks are queued for the next clean run.

- **`_judge_base.py` extraction — queued.** Three sibling judge files exist; the abstraction shape is visible, but one more clone is cheaper than premature DRY.

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

**Naming clash gotcha:** `skills/` holds runtime SKILL.md files loaded by `utils.load_skill()`; `.claude/skills/` holds project-convention skills read by Claude Code only. Different systems, same directory name.

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

Three things I'd carry into the next project:

- **Lock the eval gate before every prompt edit.** The clean wins were locked first; the blow-ups (Iter1 first pass, over_escalation 4 → 11) weren't. Without the lock file, prompt editing is gambling with stochastic feedback.
- **`judge_error` gets its own bucket.** Collapsing infra failures into a substantive ruling silently undercounts real failures. One extra column in the snapshot is a small price for misfiring judges that can't pass as healthy.
- **Sibling-clone, then extract.** Three judge files were cloned intentionally. The right `_judge_base.py` shape is only visible because three real usages exist — guessing from one example would have picked the wrong abstraction.

</details>

<details>
<summary><strong>What's next</strong></summary>

- **Fix the Iter4 critic-workload regression.** Live open gap. Offline re-planning, not another skill edit this cycle. Candidates: move rung definitions out of check #6 body into a header block consulted only on ambiguity; or fold the heated-adjective guardrail into the responder-only side.
- **Improve retrieval recall.** 0.381 / 0.272 are the weakest numbers on the board. Next attempts: audit the 5 chunks the investigator's Self-RAG filter drops; tighten section-aware chunking on multi-version patches.
- **Finish pairwise semantic spot checks.** Structurally clean; hand-validated spot checks deferred to the next clean run.
- **Extract `_judge_base.py`.** Three sibling judge files exist — abstraction shape is visible and ready to pull out.

</details>
