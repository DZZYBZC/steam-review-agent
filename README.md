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
#    First run also downloads embedding + reranker models (~5GB).
python test_graph.py
```

For the full data pipeline, eval suite, runtime/cost estimates, and other entry points, see [Run it yourself](#run-it-yourself) below.

---

## What it does

Ingests Steam reviews and routes each one through a LangGraph workflow: classify → retrieve patch notes → draft an evidence-grounded reply → critic gate → human approval. The interesting problem isn't generation — it's *defensible* generation that traces every claim back to a retrieved patch chunk.

- Investigator / responder / critic agents + a plain-Python coordinator and human approval gate
- Approved drafts become few-shot examples for future runs
- Human-rejected drafts become cluster notes that warn the next investigator about known issues in the same category
- **Not** a chatbot. **Not** an autoresponder. The human-in-the-loop gate is non-negotiable — the agent does the research and drafts; the human ships it.

<details>
<summary><strong>Project layout (annotated file tree)</strong></summary>

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
│   ├── classify.py                # Review category classification + secondary aspect extraction (Haiku)
│   ├── cluster.py                 # Time-windowed category clustering + priority signals
│   ├── stats.py                   # Aggregate statistics over reviews + clusters
│   ├── chunk.py                   # Parent-child section-aware patch-note chunking
│   ├── retrieve.py                # Hybrid RAG: vector + BM25 + optional HyDE → RRF → Gemma rerank
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
│       ├── investigator.py        # Tool-use retrieval + self-RAG retries + cluster-note loading + secondary aspect probing
│       ├── responder.py           # Drafts player-facing reply (Sonnet 4.6)
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
│   ├── generate-hyde/             # HyDE hypothetical patch-note generator
│   ├── judge-grounding/           # Eval judge: low-confidence citation classifier
│   ├── judge-action/              # Eval judge: action severity classifier
│   ├── judge-pairwise/            # Eval judge: revision improvement
│   ├── judge-pool-sufficiency/    # Eval judge: retrieval-only — would the pool support an ideal answer?
│   └── judge-draft-grounding/    # Eval judge: joint retrieval+drafting — does the pool support what the draft claims?
│
├── evals/                       # Evaluation harness
│   ├── run_evals.py               # Main eval runner (loads cases, runs agent, scores, snapshots)
│   ├── reporter.py                # Terminal-friendly score report
│   ├── snapshot.py                # Snapshot writer + schema versioning + diff annotation
│   ├── failure_modes.py           # Deterministic failure-mode taxonomy
│   ├── ITERATION_LOG.md                 # Iteration log — every edit, gate, verification result
│   ├── _negative_controls_locked.md  # Pre-edit gate locks for every prompt iteration
│   ├── _lock_controls.py          # CLI helper to materialize lock blocks from a run JSON
│   ├── refresh_classifier_fields.py
│   ├── migrate_slots_to_concepts.py # One-shot 1:1 backfill of must_include_chunk_ids → required_concepts (Phase A1)
│   ├── scorers/
│   │   ├── deterministic.py       # Deterministic scorers (action_correctness, concept_recall, evidence_sufficiency, ...)
│   │   ├── gating_accuracy.py     # Retrieval-gating scorer
│   │   ├── judge_grounding.py     # LLM judge — low_conf_with_cite ruling
│   │   ├── judge_action.py        # LLM judge — wrong_action_severity ruling
│   │   ├── pairwise.py            # LLM judge — revision improvement
│   │   └── judge_retrieval.py     # LLM judge — split: pool_sufficiency (retrieval-only) + draft_grounding (joint retrieval+drafting)
│   └── test_sets/
│       ├── golden.json            # Hand-curated eval cases with expected actions + must_include sources
│       └── regression.json        # Regression seeds added during eval-driven prompt edits
│
└── .claude/
    └── skills/                  # Claude Code skills — project conventions (NOT loaded by Python)
```

**Naming clash gotcha:** the top-level skills directory holds runtime skill files loaded by the agent; the .claude/skills directory holds project-convention skills read by Claude Code only. Different systems, same directory name.

</details>

<details>
<summary><strong>Worked example — one real review through the full graph</strong></summary>

An actual approved run (app 2246340, review 222426809). Classifier → technical_issues, confidence 0.7. Retrieval hit 5 chunks across 5 different patch versions; the critic approved on the first iteration.

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

Citation chain of custody: 5 source chunks retrieved, 5 relevant, 5 cited — cited sources verified as a subset of relevant sources deterministically by the critic. The responder cannot cite a patch the investigator did not retrieve.

</details>

---

## How the agent processes a single review

1. **Ingest** — fetch reviews from the Steam Web API and persist them
2. **Clean + dedupe** — strip markup and drop near-duplicates above the configured threshold
3. **Classify** — assign one of 10 review categories with a confidence score (Haiku); for multi-part reviews, extract secondary aspect phrases for downstream investigation
4. **Cluster + stats** — group by category in a rolling time window and compute priority signals
5. **Coordinator entry** — mint a unique run identifier and route into the agent graph
6. **Investigate**
   - Classify the review's emotional tone (Haiku)
   - Check a deterministic category gate — some categories skip retrieval entirely
   - Load active cluster notes for the category
   - If the LLM judges from notes alone that no response is needed, exit early
   - Otherwise, the investigator LLM (Sonnet 4.6) drives retrieval via Anthropic's tool-use API: formulate a search query, call the retrieval tool (hybrid vector + BM25 + HyDE → RRF → Gemma rerank), inspect results, and optionally reformulate and call again (up to 4 total calls). Each retrieval call also generates a hypothetical patch note from the query (HyDE), embeds it, and feeds matching chunks into RRF as a third retriever source — bridging the vocabulary gap between player complaints and developer patch-note language. For multi-part reviews, the classifier extracts secondary aspect phrases; the investigator may use its final call to probe the top secondary aspect if primary evidence is already sufficient
7. **Draft** — generate a player-facing reply citing only chunks the investigator retrieved (Sonnet 4.6)
8. **Critique → Human approval**
   - Validate the evidence chain, tone, and action choice
   - On approval → graph interrupts for a manual human decision
   - On action-only rejection → coordinator freezes the responder's action and routes directly to human approval (no revision)
   - On evidence or drafting rejection → route back to the coordinator for a revise loop (max 3 iterations)

---

## Architecture

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

Five nodes: coordinator (plain Python), investigator, responder, critic, and human approval. The graph compiles with an interrupt before the human approval node — every run pauses for human decision before completing.

**Why the coordinator is plain Python, not an LLM:** the workflow is fixed and the branching is knowable in advance. Model-side tool-calling would trade a five-line Python router for a stochastic dispatcher that costs tokens every hop and can't be locked down in evals. Tool-calling earns its place when the path isn't knowable; this path is.

**Per-node model assignments:**
- **Haiku 4.5** — classifier, tone classifier, cluster summarizer, critic, and all eval judges (narrow extractive/classifier tasks)
- **Sonnet 4.6** — investigator and responder
  - Investigator: query formulation and evidence evaluation benefit from a stronger model (sufficiency lifted ~14pp when upgraded from Haiku)
  - Responder: generates player-visible prose

---

## Under the hood

<details>
<summary><strong>Retrieval pipeline (hybrid RAG)</strong></summary>

Patch notes → **parent-child section-aware chunker** → dual index (ChromaDB vector embeddings + in-memory BM25). Query time: **HyDE** (hypothetical patch note via Haiku, embedded and searched as a third retriever source) + **RRF fusion** (top 12) → **Gemma rerank** (top 5) → **parent context attachment**. Each retrieval call returns a top-5 reranked pool, merged across up to 4 calls into a single accumulated evidence set. HyDE runs on every retrieval call by default; a BM25-richness gate (currently off) can skip it when keyword search alone returns strong results.

**Parent-child chunking.** Each bullet point is a child chunk (embedded and searched); each section is a parent chunk (stored as metadata, never embedded). After reranking, each matched child gets a context window of its parent section — the matched bullet plus up to 3 sibling bullets before/after, centered on the match position. This gives the investigator surrounding context without inflating the embedding index. Chunk identifiers are unchanged, so citation chain-of-custody and eval scorers work unmodified. Three independent feature flags control rollout: investigator parent context (on), responder/critic parent context (on), and same-parent dedup (off — dedup dropped concept-carrying chunks and regressed retrieval metrics).

Reranker scores are **not** passed to the investigator — they're uncalibrated and anchoring on them would amplify noise. The reranker orders; the investigator reasons about content.

The investigator formulates each search query and calls the retrieval tool up to 4 times total, reformulating between calls based on what it has seen. For multi-part reviews, the classifier extracts secondary aspect phrases; if the investigator's primary evidence is sufficient and a call remains, it may probe the top secondary aspect. Each call is tagged with a role (initial, reformulation, or secondary probe) for provenance tracking, and that provenance follows the results through the evidence package for eval attribution. Embedding and reranker models are lazy-loaded and cached at module level.

</details>

<details>
<summary><a id="critic-revision-loop"></a><strong>Critic ↔ revision loop and run identity</strong></summary>

**Three rejection kinds.**
- **Drafting** — routes the responder into a re-draft without re-investigating
- **Evidence** — routes back through the coordinator to the investigator, with the critic's retrieval hint seeding the next query
- **Action** — only the action check failed, all other checks passed; intercepted by the coordinator before it reaches the responder
- Each iteration is logged (draft, critique, reason type, hint) for offline analysis

**Action-freeze override.**
- When the critic rejects solely on action grounds, the coordinator freezes the responder's current proposed action and routes directly to human approval, skipping the revision loop
- Approval ends the run with the frozen action; rejection clears the freeze and re-enters the revision loop normally
- Action-only thrash loops are broken at the first rejection

**Human action override at the gate.**
- Not a binary approve/reject — on approval, the caller can inject an optional action override to swap the action label in a single shot
- Useful when the draft reads fine but the reviewer disagrees with the proposed action, which otherwise would cost a full revision cycle
- Precedence on approve: valid override > frozen action > current action (invalid override falls through to frozen-action restore)
- Rejection ignores the override and clears it on the way back into the revision loop

**Run identity.** The coordinator generates a unique identifier on first entry; both the run-level and per-iteration audit tables carry it, so a review's full revision history can be reassembled from the database alone.

**Termination.** Human approval, max iterations reached, human approval after max iterations, or terminal LLM/parse error.

</details>

<details>
<summary><strong>Memory architecture</strong></summary>

Three memory layers, each with a different lifetime and scope.

**Working memory (single run)**
- LangGraph state threaded through every node in the graph
- Holds the current review, evidence package, drafted response, critique, iteration count, and all routing signals
- Scoped to a single run — created at coordinator entry, discarded at termination
- Checkpointed to SQLite between nodes so the graph can resume at the human-approval interrupt

**Episodic memory — audit log + cluster notes (cross-run)**
- **Audit log** — every completed run with full evidence, draft, critique, human decision, and run identifier. The responder loads recent approvals as few-shot examples, so the system adapts to the human approver's voice over time.
- **Cluster notes** — per-category institutional knowledge with a real lifecycle:
  - Active/resolved status; notes older than 90 days filtered at read time, never deleted
  - Four types: known issue (auto from investigator, ≥2 sources), response history (auto on approval), human feedback (on rejection), investigation
  - Dedup: source review match first, then 24h time window
  - Investigator loads active, non-stale notes for the current category — can exit early if notes alone resolve the issue

**Semantic memory — patch note vector store (persistent)**
- Vector store indexed with dense embeddings, plus a parallel in-memory keyword index
- Built from parent-child section-aware chunked patch notes — each bullet is a child chunk (embedded), each section is a parent chunk (metadata only)
- Queried at run time via reciprocal rank fusion (top 12) → cross-encoder rerank (top 5) → parent context attachment
- The investigator drives retrieval through Anthropic's tool-use API — it formulates queries, inspects results, and can reformulate up to 4 total calls (the 4th reserved for secondary aspect probes on multi-part reviews)
- Vector store persisted to disk; keyword index is rebuilt each run

</details>

---

## Eval system

The agent matters; the eval system is what made it iterable. Each block below is a discipline boundary, not a feature.

<details>
<summary><strong>Layered scoring, cache key, and judge_error isolation</strong></summary>

**Layered scoring.**
- Deterministic scorers run first and catch hard violations cheaply
- LLM judges gate on different criteria: grounding and action judges only see cases the deterministic scorers flag; pairwise and retrieval judges run on all eligible cases independently
- Five sibling judges:
  - **Grounding** — low-confidence citations
  - **Action severity** — action-severity disagreements
  - **Pairwise** — did the revision loop improve the draft
  - **Retrieval** — houses **two** judges that share infrastructure but score independently: pool sufficiency (would the pool support an ideal answer?) and draft grounding (does the pool support what the draft actually claims?)
- The delta between the two retrieval judges surfaces responder over-claim vs under-use

**Cache key.**
- Content hash over a stable set of inputs: run file, case ID, judge model, skill hash, input schema version, and user message hash
- Retrieval judges add a seventh component (judge kind) so the two sibling judges share a cache directory without collision
- Identical inputs → free re-run; any input change invalidates cleanly

**Isolated judge-error bucket.** API / parse / schema failures get a dedicated ruling, not absorbed into tolerable disagreement. Surfaces separately in the snapshot, diff, and reporter — a misfiring judge can never pass as healthy.

</details>

<details>
<summary><strong>Lock-then-edit discipline and snapshot diff</strong></summary>

**Lock the gate before any prompt edit.**
- Acceptance gate written before every eval-driven edit
- Gate includes: positive cases that must improve, negative cases that must not regress, semantic spot checks, aggregate metric
- Stop rule: one coordinated edit + one rerun
- Cache proof = offline re-score against the saved JSON, not a second live run (a second live run creates a fresh cache key and forces 100% miss)

**Snapshot diff with schema versioning.** Every snapshot carries a schema version; the diff annotates version transitions. No silent metric drift.

**Iteration log.** Records every edit pass — motivation, two-sided gate, verified result. Later iterations cite earlier ones by name.

</details>

<details>
<summary><strong>Eval iteration arc (chronological)</strong></summary>

Twenty-five iterations. Four reverted, one partial success, twenty shipped — honest mid-ladder verdicts are what the eval infrastructure exists to make possible.

| # | Name | Outcome | Key signal |
|---|------|---------|------------|
| 1 | Baseline | shipped | Deterministic scorers only. Established gating accuracy, action correctness, citation chain-of-custody |
| 2 | Grounding judge | shipped | LLM judge classifies low-confidence citation cases. Schema v3 |
| 3 | Action judge | shipped | LLM judge splits action-severity disagreements into 4 buckets. Schema v4 |
| 4 | Multi-part complaint edit | shipped | First eval-driven prompt edit. Locked gate, clean win, established lock-before-edit discipline |
| 5 | Action severity precedence | **reverted** | Narrow metric improved but aggregate action correctness regressed 65→49% and 12 runs hit max_iterations |
| 6 | Pairwise scorer | shipped | "Is the revision loop earning its tokens?" Surfaced that revisions were mostly cosmetic. Schema v5 |
| 7 | Retrieval scorer + gold recall | shipped | Source vs post-filter recall split, concept hit-rate, equivalence groups. Schema v6 |
| 8 | Single-axis rubric | partial | Refactored 4 actions into one axis. Action correctness recovered but critic-workload regressed |
| 9 | Rubric remedial (4R) | shipped | Tightened Iter8's rubric. Action correctness held; critic regression persisted |
| 10 | Header-block rearrangement | **reverted** | Critic still cited rung definitions verbatim. Hypothesis falsified at smoke test |
| 11 | Full rung-definition removal | **reverted** | Critic reconstructed rung semantics from action names alone. Closed prompt-text edits as a lever |
| 12 | Tool-use investigator | shipped | Investigator drives retrieval via tool-use API with self-directed retries. Source recall 0.381 → 0.580 |
| 13 | Action-freeze | shipped | Graph-level interception of action-only critic rejections. Action correctness 0.651 → 0.780, max iterations 7 → 0 |
| 14 | Few-shot examples | shipped | Responder few-shot examples + JSON parsing robustness fix. Infra errors 5 → 0 |
| 15 | Boundary sharpening | shipped | 7 disambiguation rules across all skills + 3 golden corrections. Action correctness 71.1% → 76.9%, category drift 4 → 1 |
| 16 | [infra] Parallel eval execution | shipped | SQLite write-ahead logging + thread pool parallelism. Wall-clock 25–35 min sequential → ~3 min at 10 workers. Iteration speed only — no quality movement |
| 17 | Phase A1 — concept recall + 1:1 migration | shipped | New concept recall scorer; mechanical backfill of slots → required concepts. Identity check vs retrieval recall byte-equal per case (the whole point of A1 in isolation) |
| 18 | Phase A2 — manual concept cleanup + sufficiency | shipped | Manual concept merges, equivalent-chunk additions, sufficient-set authoring (100% coverage across retrieval-eligible). Adds evidence sufficiency + relevant-concept precision. First snapshot where concept recall legitimately diverges from slot recall |
| 19 | Phase B — split retrieval judges | shipped | Pool sufficiency (retrieval-only) and draft grounding (joint), sharing scorer infrastructure. Schema v7. Disentangles "is the evidence enough for an ideal answer?" from "does the evidence back the draft's actual claims?" |
| 20 | Multi-category retrieval | shipped | Investigator queries across all categories the review touches (primary + secondary aspects), not just the primary. Schema v8 |
| 21 | Parent-child context | shipped | Investigator and responder see sibling bullets around matched chunks. Concept recall lifted; dedup tested and reverted (dropped concept-carrying chunks) |
| 22 | HyDE retrieval | shipped | Hypothetical patch note generation (Haiku) as third retriever source into rank fusion. Source recall +0.10, concept recall +0.05, sufficiency +0.08 vs pre-HyDE baseline |
| 23 | Action-ladder boundary sharpening | shipped | Three targeted disambiguation rules across responder/critic/judge: escalate-boundary scoping of "patches don't reduce," evidence-richness ≠ complaint-specificity guardrail, vague/monetization carve-out reinforcement |
| 24 | [infra] Critic batching + judge parallelization | shipped | Critic evaluates full checklist before ruling; judges run in parallel thread pools. Drafting rejections dropped, token usage down ~13%. No quality movement |
| 25 | Reranker HyDE augmentation | **reverted** | Augmented reranker query with truncated HyDE hypothetical doc. Mechanism worked (net +250 rank gain for HyDE chunks, 68% of calls changed top-5), but displaced load-bearing chunks — concept recall -0.10, sufficiency -0.13, action correctness -0.06 |

*Why Iter16 used threads, not asyncio:* the agent graph, Anthropic SDK calls, LangGraph checkpointer, and SQLite are all synchronous; switching to asyncio would have meant rewriting every node, every tool call, every database access, and every test, for an I/O-bound workload where threads give effectively the same throughput. Write-ahead logging on SQLite handles the only real shared-state contention — a two-line configuration change instead of a graph rewrite.

Full detail in the iteration log under the evals directory.

</details>

---

## Results

Latest full-eval run (56 cases, schema v8).

The metrics below cite four distinct populations:
- **Total cases** (56) — the full golden set
- **Action-eligible** (varies run-to-run) — total minus no-response cases and infra errors
- **Retrieval-eligible** (22) — cases with hand-annotated must-include chunk IDs
- **Judge-eligible** (varies run-to-run) — cases where the agent retrieved a non-empty post-filter pool, scored by the two retrieval judges

**Coverage & actions** (56 total cases across 5 games, 11–12 each)

| Metric | Value |
|---|---|
| Action correctness | **75.6%** (34 / 45 action-eligible cases; 0 infra errors) |
| Effective first-pass rate | **97.8%** (across action-eligible cases — critic approved or action-freeze override at iter0) |
| Gating accuracy | **92.2%** (across all 56 cases — 5 true_skip / 42 true_retrieve / 1 false_skip / 3 false_retrieve / 5 unknown) |

**Retrieval quality** (22 retrieval-eligible cases — cases with hand-annotated must-include chunk IDs)

| Metric | Value |
|---|---|
| Concept recall — source / post-filter | **0.750 / 0.727** |
| Sufficiency — at-source / post-filter | **0.708 / 0.667** |
| Relevant-concept precision (pool) | **0.320** (cases with a non-empty post-filter pool) |
| Citation-concept precision (cited) | **0.319** (cases where the responder cited at least one chunk) |

**Judge rulings** (43 judge-eligible cases — cases where the agent retrieved a non-empty post-filter pool)

| Metric | Value |
|---|---|
| Pool sufficiency | supports 18 / partial 18 / no_support 7 / judge_error 0 |
| Draft grounding | supports 15 / partial 28 / no_support 0 / judge_error 0 |
| Disagreement | 17 agreement / 26 disagreement — within disagreements: **0 strong under-use, 2 over-claim**, 24 other |
| Low-confidence citations | 10 flagged — **10 honest hedge** / 0 misleading claim / 0 unclear |

#### Where the numbers come from

- **Retrieval — concept recall, sufficiency, and precision.**
  - Concept row generalizes hand-listed slots into named concepts — equivalent chunks the original annotator missed no longer mark a case as a miss
  - Sufficiency answers the operational question directly: *did the retriever bring back enough evidence to support the right answer?* Pre-declared at full annotation coverage, promoted to a primary retrieval metric
  - Legacy slot recall lives in the snapshot but is omitted from the table for brevity (kept as a debuggability floor so historical snapshots remain comparable)
  - Pool precision = share of investigator-kept chunks mapping to any annotated concept
  - Citation precision = same metric scoped to chunks the responder actually cited
  - When citation > pool, the responder is selectively picking better chunks out of a noisy pool. When they converge (as in the current run), the responder is citing roughly in proportion to pool quality
- **Retrieval-vs-draft disagreement is the new dominant signal.**
  - Two retrieval judges split usefulness into pure retrieval signal (could an ideal responder produce the right answer from this evidence?) and joint retrieval+drafting signal (does the evidence back what the draft actually claims?)
  - Strong under-use diagonal: gold says evidence is enough, draft fails to use it — flags responses that left load-bearing evidence on the floor
  - Strong over-claim diagonal: gold says evidence isn't enough, draft asserts a fix anyway — flags fabricated claims
  - Under-use dropped to zero after upgrading the investigator to Sonnet and adding HyDE retrieval (better evidence packages)
  - Over-claim remains low — the responder is conservative, though a small number of cases show the draft asserting coverage the pool doesn't fully support

### Open gaps

- **Citation precision.**
  - HyDE and parent context lifted retrieval recall and sufficiency materially, but citation precision dropped (pool and citation precision converged around 0.32, down from a prior gap where citation precision exceeded pool precision)
  - The responder is citing more broadly from a richer pool rather than selectively picking the best chunks — the next lever is responder evidence consumption, not more retrieval augmentation
  - Candidates: require the responder to explicitly reference load-bearing chunks before drafting, add an evidence checklist in the investigator output, or force mention of unresolved contradictions when multiple patch versions disagree

- **Critic node-level over-rejection.**
  - System-level churn is fully solved — action-freeze intercepts action-only rejections and effective first-pass rate is near-ceiling at 97.8%, with zero cases reaching max iterations
  - The critic *node itself* still over-rejects on action grounds (22 action rejections vs 2 drafting rejections), but action-freeze renders this operationally harmless
  - Broad rubric rewrites are closed as a lever (the critic reconstructs rung semantics from action names alone); targeted boundary-sharpening rules improved action correctness but did not lift raw iter-0 approval
  - Further improvement would need a different approach (e.g., critic fine-tuning, separate action-evaluation node), but the cost-benefit is low given action-freeze's effectiveness

---

## Tech stack

- **Language:** Python 3.12
- **LLM API:** Anthropic Claude (Haiku 4.5 for classifiers/critic/judges, Sonnet 4.6 for investigator and responder)
- **Agent framework:** LangGraph (used directly, not via LangChain)
- **Validation:** Pydantic (only at LLM trust boundaries)
- **Storage:** SQLite (reviews, audit log, audit log iterations, cluster notes, classifications, schema version)
- **Vector store:** ChromaDB
- **Embeddings:** sentence-transformers (`BAAI/bge-base-en-v1.5`)
- **Reranker:** Gemma reranker (`BAAI/bge-reranker-v2-gemma`)
- **Lexical search:** rank-bm25 (in-memory, rebuilt per run)
- **Other:** pandas, python-frontmatter (for skill files), python-dotenv

---

## Run it yourself

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
| `python evals/run_evals.py` | Run the full eval suite (56 golden cases). `--quick` for a subset, `--case-id <id>` for a single case, `--app-id <id>` and `--category <cat>` for filters, `--workers N` to control parallelism (default 10). Writes a snapshot to `evals/snapshots/` and a raw run JSON to `evals/runs/`. |
| `python resolve_note.py {list <app_id> <category> \| resolve <note_id> \| reactivate <note_id>}` | Manage the cluster notes lifecycle. |
