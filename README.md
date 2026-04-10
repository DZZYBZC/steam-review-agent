# Steam Review Triage & Response Agent

Turns noisy Steam reviews into evidence-backed draft replies — with an iterative critic loop and a human approval gate before anything ships.

---

## Quickstart

```bash
# 1. Install (Python 3.12)
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 2. Set API keys. CLAUDE_API_KEY is required for any agent run.
#    HF_TOKEN is needed on first run to download embedding/reranker models.
#    Pick ONE of these two paths:
#    (a) write a .env file — config.py loads it via python-dotenv:
echo 'CLAUDE_API_KEY=sk-ant-...' >> .env
echo 'HF_TOKEN=hf_...' >> .env
#    (b) OR export the variables directly in your shell:
export CLAUDE_API_KEY=sk-ant-...
export HF_TOKEN=hf_...

# 3. End-to-end smoke test on a hardcoded review.
#    Runs the full agent graph (investigator → responder → critic) against a fake
#    "game crashes in dungeon" review and auto-approves at the human gate.
#    Makes ~3-5 real LLM calls. Wall clock ~30s. Costs a few cents on Haiku+Sonnet.
#    First run also downloads embedding + reranker models (~100MB).
python test_graph.py
```

For the full data pipeline, eval suite, runtime/cost estimates, and other entry points, see [Run it yourself](#10-run-it-yourself) below.

---

## What it does

Ingests Steam reviews and routes each one through a LangGraph workflow with investigator / responder / critic agents plus a plain-Python coordinator and human approval gate: classify → retrieve patch notes → draft an evidence-grounded reply → critic gate → human approval. The interesting problem isn't generation — it's *defensible* generation that traces every claim back to a retrieved patch chunk.

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
6. **Investigate** — classify the review's emotional tone (Haiku), then check a deterministic category gate (some categories like `other` skip retrieval entirely). Load active cluster notes for the category. If the LLM judges from notes alone that no response is needed, exit early (`no_response_needed`). Otherwise, the investigator LLM drives retrieval via Anthropic's tool-use API: it formulates a search query, calls `retrieve_patches` (hybrid vector + BM25 → RRF → cross-encoder rerank), inspects results, and can reformulate and call again (up to 3 total calls)
7. **Draft** — generate a player-facing reply citing only chunks the investigator retrieved (Sonnet 4.6 — the only generative node)
8. **Critique → Human approval** — validate the evidence chain, tone, and action choice. On approval, the graph interrupts for a manual decision. On action-only rejection, the coordinator freezes the responder's action and routes directly to human approval (no revision). On evidence or drafting rejection, route back to the coordinator for a revise loop (max 3 iterations)

See the [annotated file tree](#9-project-layout) for exact file locations.

---

## Architecture

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
│       ├── coordinator.py         # Plain-Python routing (mints run_id, action-freeze interception)
│       ├── investigator.py        # Tool-use retrieval + self-RAG retries + cluster-note loading
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
│   └── judge-pairwise/            # Eval judge: revision improvement
│
├── evals/                       # Evaluation harness
│   ├── run_evals.py               # Main eval runner (loads cases, runs agent, scores, snapshots)
│   ├── reporter.py                # Terminal-friendly score report
│   ├── snapshot.py                # Snapshot writer + schema versioning + diff annotation
│   ├── failure_modes.py           # Deterministic failure-mode taxonomy
│   ├── M5_PLAN.md                 # Iteration log — every edit, gate, verification result
│   ├── _negative_controls_locked.md  # Pre-edit gate locks for every prompt iteration
│   ├── _lock_controls.py          # CLI helper to materialize lock blocks from a run JSON
│   ├── refresh_classifier_fields.py
│   ├── scorers/
│   │   ├── deterministic.py       # Deterministic scorers (action_correctness, etc.)
│   │   ├── gating_accuracy.py     # Retrieval-gating scorer
│   │   ├── judge_grounding.py     # LLM judge — low_conf_with_cite ruling
│   │   ├── judge_action.py        # LLM judge — wrong_action_severity ruling
│   │   └── pairwise.py            # LLM judge — revision improvement
│   └── test_sets/
│       ├── golden.json            # Hand-curated eval cases with expected actions + must_include sources
│       └── regression.json        # Regression seeds added during eval-driven prompt edits
│
└── .claude/
    └── skills/                  # Claude Code skills — project conventions (NOT loaded by Python)
```

**Naming clash gotcha:** `skills/` holds runtime SKILL.md files loaded by `utils.load_skill()`; `.claude/skills/` holds project-convention skills read by Claude Code only. Different systems, same directory name.

</details>

```
          ┌────────────────────┐
     ┌──► │    coordinator     │── done ─────┐
     │    │   (plain Python)   │             │
     │    └──┬──────────────┬──┘             │
     │       │              │ action-freeze  │
     │       │ investigate  └───────┐        │
     │       ▼                      │        │
     │    ┌────────────────────┐    │        │
     │    │    investigator    │    │        │
     │    │    (LLM tools)     │    │        │
     │    └─────────┬──────┬───┘    │        │
     │              │      └── skipped* ─────┤
     │              │ respond       │        │
     │              ▼               │        │
     │    ┌────────────────────┐    │        │
     │    │     responder      │    │        │
     │    │       (LLM)        │    │        │
     │    └─────────┬──────┬───┘    │        │
     │              │      └── error* ───────┤
     │              ▼               │        │
     │    ┌────────────────────┐    │        │
     │    │       critic       │    │        │
     │    │       (LLM)        │    │        │
     │    └──┬──────┬──────┬───┘    │        │
     │       │      │      └── error* ───────┤
     ├──◄────┘      │ approved      │        │
     │              ▼               │        │
     │    ┌────────────────────┐◄───┘        │
     │    │   human_approval   │             │
     │    │    [interrupt]     │             │
     │    └──┬─────────────┬───┘             │
     │       │             └── approved ─────┤
     └──◄────┘                               │
                                             ▼
                                            END

  Back-edges (◄) = critic / human rejection → revision loop.
  action-freeze = coordinator intercepts action-only critic
    rejections and routes directly to human_approval.
  * skipped and error paths route back through the
    coordinator, which then takes "done" → END.
```

Five nodes: `coordinator` (plain Python), `investigator`, `responder`, `critic`, `human_approval`. Graph compiles with `interrupt_before=["human_approval"]` — every run pauses for human decision before completing.

**Per-node model assignments.** Classifier, tone classifier, cluster summarizer, investigator, critic, and eval judges all run on **Haiku 4.5** — narrow extractive/classifier tasks. Only the responder runs on **Sonnet 4.6** (temp 0.4) because it's the only node generating player-visible prose. Heavyweight model where tone matters, nowhere else.

---

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
<summary><strong>Retrieval pipeline (hybrid RAG)</strong></summary>

Patch notes → section-aware chunker → dual index (ChromaDB `all-MiniLM-L6-v2` + in-memory BM25). Query time: **RRF fusion** (top 12) → **cross-encoder rerank** (`ms-marco-MiniLM-L-6-v2`, top 5). The investigator sees the final 5 chunks.

Reranker absolute scores are **not** passed to the investigator — they're uncalibrated and anchoring on them would amplify noise. The reranker orders; the investigator reasons about content.

The investigator LLM formulates each search query and calls the tool up to 3 times total, reformulating between calls based on what it has seen. Embedding and reranker models lazy-loaded and cached at module level.

Implementation: `pipeline/retrieve.py`.

</details>

<details>
<summary><a id="critic-revision-loop"></a><strong>Critic ↔ revision loop and run identity</strong></summary>

**Three rejection kinds.** A `drafting` rejection routes the responder into a re-draft without re-investigating; an `evidence` rejection routes back through the coordinator to the investigator, with the critic's `retrieval_hint` seeding the next query; an `action` rejection (only the action check failed, all other checks passed) is intercepted by the coordinator before it reaches the responder. Each iteration writes to `audit_log_iterations` (draft, critique, reason type, hint) for offline analysis.

**Action-freeze override.** When the critic rejects solely on action grounds (`reason_type="action"`), the coordinator freezes the responder's current `proposed_action` and routes directly to `human_approval`, skipping the revision loop entirely. The freeze persists until the human acts: approval ends the run with the frozen action; rejection clears the freeze and re-enters the revision loop normally. Action-only thrash loops are broken at the first rejection.

**Run identity.** Coordinator mints a UUID `run_id` on first entry; both `audit_log` and `audit_log_iterations` carry it, so a review's full revision history can be reassembled from the DB alone.

**Termination.** Human approval, max iterations reached, human approval after max iterations, or terminal LLM/parse error.

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

Fifteen iterations. Three reverted, one partial success, eleven shipped — honest mid-ladder verdicts are what the eval infrastructure exists to make possible.

| # | Name | Outcome | Key signal |
|---|------|---------|------------|
| 1 | Baseline | shipped | Deterministic scorers only. Established gating accuracy, action correctness, citation chain-of-custody |
| 2 | Grounding judge | shipped | `judge_grounding.py` classifies `low_conf_with_cite` cases. Schema v3 |
| 3 | Action judge | shipped | `judge_action.py` splits `wrong_action_severity` into 4 buckets. Schema v4 |
| 4 | `multi_part_complaint` edit | shipped | First eval-driven prompt edit. Locked gate, clean win, established lock-before-edit discipline |
| 5 | `action_severity_precedence` | **reverted** | Narrow metric improved but aggregate action correctness regressed 65→49% and 12 runs hit max_iterations |
| 6 | Pairwise scorer | shipped | "Is the revision loop earning its tokens?" Surfaced that revisions were mostly cosmetic. Schema v5 |
| 7 | Retrieval scorer + gold recal | shipped | Source vs post-filter recall split, concept hit-rate, `any_of` equivalence groups. Schema v6 |
| 8 | Single-axis rubric | partial | Refactored 4 actions into one axis. Action correctness recovered but critic-workload regressed |
| 9 | Rubric remedial (4R) | shipped | Tightened Iter8's rubric. Action correctness held; critic regression persisted |
| 10 | Header-block rearrangement | **reverted** | Critic still cited rung definitions verbatim. Hypothesis falsified at smoke test |
| 11 | Full rung-definition removal | **reverted** | Critic reconstructed rung semantics from action names alone. Closed prompt-text edits as a lever |
| 12 | Tool-use investigator | shipped | Anthropic tool-use API with self-RAG retries. Source recall 0.381 → 0.580 |
| 13 | Action-freeze | shipped | Graph-level interception of action-only critic rejections. Action correctness 0.651 → 0.780, max-iter 7 → 0 |
| 14 | Few-shot examples | shipped | Responder few-shot examples + `parse_llm_json` robustness fix. Infra errors 5 → 0 |
| 15 | Boundary sharpening | shipped | 7 disambiguation rules across all skills + 3 golden corrections. Action correctness 71.1% → 76.9%, category_drift 4 → 1 |

Full detail in `evals/M5_PLAN.md`.

</details>

---

## Results

Latest full-eval run (56 cases). Source: `snapshot_20260410_052557.json` (schema v6). This snapshot reflects boundary-sharpening disambiguation rules added to all skill prompts, plus three golden annotation corrections to align with the sharpened rubric. Comparisons to earlier snapshots are informative but not perfectly apples-to-apples.

| Metric | Value |
|---|---|
| Cases evaluated | **56** (across 5 games, 11–12 cases each) |
| Action correctness | **76.9%** (30 / 39, excludes 17 no-response cases) |
| Effective first-pass rate | **87.2%** (critic approved or action-freeze override at iter0) |
| Gating accuracy | **94.2%** (10 true_skip / 39 true_retrieve / 3 false_skip / 0 false_retrieve / 4 unknown) |
| Hard grounding violations | **0** |
| Retrieval — source recall@5 | **0.453** (retrieved top-5 before investigator filter, eligible cases) |
| Retrieval — post-filter recall@5 | **0.272** (after investigator's Self-RAG filter) |
| Total tokens | 1,574,723 |

#### Where the numbers come from

- **Retrieval recall (0.453 source / 0.272 post-filter).** The 0.181 gap is the investigator's Self-RAG filter — it aggressively prunes low-relevance chunks. The retrieval stack was unchanged in this iteration; the observed recall swing from earlier runs is most plausibly explained by investigator query stochasticity and small-pool variance (on a 23-case eligible pool, a single case flipping from 1.0 → 0.0 moves the mean by ~0.04). Remaining hard-zero cases are chunking fragmentation across version-stamped patches.
- **Action correctness (76.9%).** Action-freeze (see [Critic ↔ revision loop](#critic-revision-loop)) preserves the responder's action when the critic over-corrects at the `monitor` ↔ `investigate` boundary. Judge breakdown of 9 remaining mismatches: **3 over + 3 missed + 1 drift + 2 tolerable + 0 judge_error**. Boundary-sharpening disambiguation rules collapsed category_drift from 4 → 1 by resolving ambiguous mid-ladder cases. The metric has ~5pp run-to-run variance from Haiku stochasticity across full runs with identical code.
- **Critic approvals (53.8% raw iter-0, 87.2% effective).** The critic over-rejects on action grounds at the node level, but action-freeze intercepts those before they cause thrash. Boundary-sharpening rules were added to the critic prompt, but the node-level over-rejection pattern persists; action-freeze still does the heavy lifting at the system level. Zero cases hit max_iterations.
- **Revisions — almost entirely cosmetic.** 34 of 39 neutral via the deterministic shortcut, 5 improved, 0 regressed. Only drafting/evidence issues trigger revisions.

### Key takeaways

- **Grounding and citation discipline are strong.** 100% citation chain-of-custody, zero hard grounding violations. The `source_ids → relevant_ids → source_ids_cited` subset check is doing its job — the responder cannot fabricate citations.
- **Action correctness and throughput are healthy.** 76.9% action correctness, 87.2% effective first-pass rate, zero max-iterations cases.
- **Revision loop is clean.** 0 regressions, 5 genuine improvements (grounding fixes caught by the critic).

<details>
<summary><a id="open-gaps"></a><strong>Open gaps</strong></summary>

- **Retrieval recall.** Source 0.453 / post-filter 0.272. Still the weakest metric on the board. The retrieval stack was unchanged in this iteration; the observed swing from earlier runs is most plausibly explained by investigator query stochasticity and small-pool variance. The core gap remains the investigator's Self-RAG filter aggressively pruning useful chunks before drafting. Next: (a) audit filter drops on cases where the retriever surfaced the right chunk but the investigator discarded it, (b) tighten section-aware chunking on multi-version patches.

- **Critic node-level over-rejection.** The system-level churn is solved (action-freeze), but the critic *node itself* still over-rejects at the `monitor` ↔ `investigate` boundary (53.8% raw iter-0 approval). Disambiguation rules were added to the critic prompt but the over-rejection pattern persists; action-freeze still does the heavy lifting. Broad rubric rewrites look closed as a lever for the critic's raw over-rejection problem; targeted boundary-sharpening rules improved action correctness but did not materially lift raw iter-0 approval. Any further improvement would need a different approach (e.g., critic fine-tuning, separate action-evaluation node).

- **Pairwise semantic spot checks — deferred.** Structurally clean (39 judged, 0 pairwise judge_error), but hand-validated spot checks are queued for the next clean run.

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
- **Full eval suite** (`run_evals.py`, 56 cases): ~25–35 min wall clock; ~1.57M total tokens (per the latest snapshot); on the order of $2–3 per full run with all judges enabled.
- **Cached judge re-score** (offline against a saved run JSON): ~30 sec; $0 (cache hit on every flagged case).

Numbers are approximations grounded in the latest snapshot's `total_tokens=1,574,723`. Actual cost depends on Anthropic pricing at run time.

</details>

---

<details>
<summary><strong>Design notes — what I learned</strong></summary>

Five things I'd carry into the next project:

- **Lock the eval gate before every prompt edit.** The clean wins were locked first; the blow-ups weren't. Without the lock file, prompt editing is gambling with stochastic feedback.
- **Prompt-text edits have limits — know when to move to graph-level interventions.** Rearranging and removing the critic's rung definitions both failed identically: the model reconstructed the semantics from action names alone. Two failed experiments closed the class empirically. Accepting the critic's judgment as-is and intercepting at the graph level was a smaller, more targeted fix than any prompt edit could have been.
- **`judge_error` gets its own bucket.** Collapsing infra failures into a substantive ruling silently undercounts real failures. One extra column in the snapshot is a small price for misfiring judges that can't pass as healthy.
- **Sibling-clone, then extract.** Three judge files were cloned intentionally. The right `_judge_base.py` shape is only visible because three real usages exist — guessing from one example would have picked the wrong abstraction.
- **Eval-driven boundary analysis beats broad prompt rewrites.** Analyzing the specific wrong cases to extract targeted disambiguation rules (7 boundary patterns from 11 mismatches) moved action correctness more reliably than broad rubric refactors. Narrow contrastive rules ("X, not Y") sharpen decision boundaries without destabilizing adjacent cases.

</details>

<details>
<summary><strong>What's next</strong></summary>

- **Improve retrieval recall.** 0.453 source / 0.272 post-filter — still the weakest metric. Small retrieval interventions (reranker top-N increase, zero-keep fallback) were hard to measure cleanly in the full-agent harness — Haiku's run-to-run variance across 56 cases swamped the signal from changes affecting 5-7 cases. A retrieval-only replay eval (isolating the retrieval pipeline from LLM stochasticity) is a plausible next step before attempting further runtime changes.
- **Finish pairwise semantic spot checks.** Structurally clean; hand-validated spot checks deferred to the next clean run.
- **Extract `_judge_base.py`.** Three sibling judge files exist — abstraction shape is visible and ready to pull out.
- **Vs-baseline pairwise comparison.** Current pairwise scorer compares iter-0 vs final draft within a run. Cross-run comparison (before/after a prompt edit) would surface whether iteration-level improvements compound across the eval suite.

</details>
