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

This project takes a stream of Steam reviews and pushes each one through a pipeline that classifies it, clusters it against recent reviews of the same kind, retrieves patch notes that might address the underlying complaint, drafts a player-facing reply that cites only what the retriever actually found, has a separate critic evaluate the draft against an evidence-chain checklist, and finally pauses for a human to approve or reject. Approved drafts feed forward as few-shot examples for future runs; rejected drafts feed forward as cluster notes that warn the next investigator about known issues.

This is **not** a chatbot and **not** a customer-service autoresponder. The human-in-the-loop gate is non-negotiable. The agent's job is to do the research and produce a defensible draft — the human's job is to ship it.

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
              ┌─────────────────┐
              │   coordinator   │ ◀──── (revise loop, skip_response)
              │ (plain Python)  │
              └────────┬────────┘
                       │ route_from_coordinator
            ┌──────────┼──────────┐
            ▼          ▼          ▼
        investigate  respond     done ─▶ END
            │          │
            │ route_after_investigator
            │  ┌───────┴────────┐
            │  │                │
            │  ▼ skip_response  ▼ respond
            │  coordinator      │
            │                   ▼
            │              ┌─────────┐
            │              │responder│
            │              └────┬────┘
            │                   │ (terminal err? → coordinator)
            │                   ▼
            │              ┌─────────┐
            │              │ critic  │
            │              └────┬────┘
            │                   │ approved? terminal err?
            │           ┌───────┴────────┐
            │           ▼                ▼
            │    human_approval     coordinator
            │      [interrupt]      (revise loop)
            │           │
            │           ▼ route_from_human_approval
            │     ┌─────┴─────┐
            │     ▼           ▼
            │    done         coordinator
            │     │           (rejected → revise)
            │     ▼
            │    END
```

Five nodes — `coordinator`, `investigator`, `responder`, `critic`, `human_approval`. The coordinator is plain Python routing logic, never an LLM call. The graph compiles with `interrupt_before=["human_approval"]`, so every run pauses for a human decision before completing.

**Per-node model assignments are deliberate.** The classifier, tone classifier, cluster summarizer, investigator, critic, and all eval judges run on **Haiku 4.5** (`claude-haiku-4-5-20251001`) — these are narrow extractive or classifier tasks. Only the responder runs on **Sonnet 4.6** (`claude-sonnet-4-6`, temperature 0.4), because it's the only node generating prose that a player will actually read. The heavyweight model lives where tone matters, and nowhere else.

<details>
<summary><strong>Key design decisions and alternatives rejected</strong></summary>

Six choices that look like accidents until you know why:

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

The story of how the eval system evolved through six iterations, each producing labeled signal that the next one acted on:

1. **V1 baseline** — deterministic scorers only. Established gating accuracy 94.6%, action correctness, and citation chain-of-custody. No LLM judges yet.
2. **V1.5 grounding judge** — `judge_grounding.py` ruled the cases per run flagged `low_conf_with_cite`. Moved guesswork ("is this hedge dishonest?") into a labeled distribution (`honest_hedge | misleading_fix_claim | unclear`). Schema v2 → v3.
3. **Option A: action judge** — `judge_action.py` split the `wrong_action_severity` cases into `over_escalation | missed_escalation | category_drift | tolerable_disagreement`. Schema v3 → v4. Concrete signal for the next iteration.
4. **Option B: `multi_part_complaint` prompt edit** — first eval-driven prompt edit. The case `civ7_gameplay_002` flipped `misleading_fix_claim` → `honest_hedge`, all 3 negative controls held. Clean win. The discipline pattern: lock the gate before the edit.
5. **Iteration 1 partial — `action_severity_precedence` rule edit.** First attempt over-fired the new severity rule and was caught immediately by the locked negative-control gate (over_escalation jumped from 4 to 11). A coordinated remedial edit reordered the rules so subjectivity caps the action ladder before severity overrides confidence; the partial rerun verified 3 of 5 named positives, 3 of 5 named negatives held, and the aggregate failure metric dropped 9 → 5. The `monitor` escape hatch remains too permissive — documented as the open lever.
6. **Iteration 2 structural pass — pairwise revision-improvement scorer.** Answers "is the revision loop earning its tokens?" by comparing the iter-0 draft against the final approved draft per case. A deterministic normalize-equal shortcut handles cosmetic-identical revisions; the LLM judges the rest. Schema v4 → v5. Semantic spot checks deferred to the next clean (non-remedial) full eval run.

The point of the arc: each iteration produced labeled signal *and* the discipline kept the failures visible. **Iteration 1 partial** is named as such in the project log, not papered over.

</details>

---

## Results

The table below reports the latest fully comparable full-eval snapshot (`evals/snapshots/snapshot_20260408_073529.json`, 56 cases). Iteration 1's partial remedial rerun produced a different post-edit state — those findings are discussed separately in [Open gaps](#open-gaps). Numbers from the two states are not directly comparable and are kept apart on purpose.

| Metric | Value |
|---|---|
| Cases evaluated | **56** |
| Stop reasons | 31 human_approved, 13 no_response_needed, 12 max_iterations_reached |
| Gating accuracy | **94.6%** (10 true_skip / 43 true_retrieve / 3 false_skip / 0 false_retrieve) |
| Action correctness | **48.8%** (21 / 43, excludes 13 no-response cases) |
| Citation chain of custody (`subset_ok_rate`) | **100%** |
| Hard grounding violations | **0** |
| Recall@k mean | 0.187 (28 cases had `must_include_chunk_ids`) |
| First-pass critic approval | 55.8% |
| Approval after revision | 39.7% |
| Mean iterations to approval | 0.355 |
| `wrong_action_severity` failure mode | 22 → judge breakdown: **11 over_escalation**, 0 missed_escalation, 5 category_drift, 6 tolerable_disagreement |
| Pairwise revision scorer | 31 judged → **5 improved**, 25 neutral (24 via deterministic shortcut), 1 regressed |
| `judge_error` (both judges) | **0** |
| Total tokens | 1,286,789 |

### Key takeaways

- **Grounding and citation discipline are strong.** 100% citation chain-of-custody, zero hard grounding violations. The evidence-tracking design (`source_ids → relevant_ids → source_ids_cited`, critic verifies the subset relationship) is doing its job — the responder cannot fabricate citations.
- **Action selection is the main weakness.** 48.8% action correctness with 22 cases flagged `wrong_action_severity`. The judge breakdown shows the bias is over-escalation (11 cases) rather than missed escalation (0 cases) — the agent leans toward "do something" rather than "stand down." Iteration 1 attempted to fix this and partially succeeded; the `monitor` escape hatch is the open lever.
- **Most approved multi-iteration revisions were neutral by the current pairwise scorer, suggesting the revision loop is often cosmetic rather than substantive.** Of 31 multi-iteration approved cases, 25 were `revision_neutral` (24 of those flagged by the deterministic normalize-equal shortcut — meaning the iter-0 and final drafts were byte-identical after whitespace normalization). Only 5 were `revision_improved`. A candidate target for the next iteration.

<details>
<summary><a id="open-gaps"></a><strong>Open gaps (named, not hidden)</strong></summary>

- **Recall@k of 0.187 is honestly low.** The section-aware chunking and hybrid retrieval are working but the `must_include` sets in `golden.json` are stricter than what the cross-encoder converges on. The next iteration would either loosen the gold standard or tighten the chunker.

- **Iteration 1 partial.** The `action_severity_precedence` rule edit hit 3 of 5 named positives, 3 of 5 named negatives held, and the aggregate failure metric dropped 9 → 5 — but four named cases miss, all sharing one root cause (the `monitor` escape hatch is too permissive):
  - **Positive misses (stuck at `monitor` instead of the expected `no_action`):** `payday3_monetize_001`, `mhw_content_001`
  - **Negative regressions:** `civ7_ui_002` regressed `investigate` → `no_action`; `civ7_monetize_001` regressed `no_action` → `monitor`
  - All four are borderline subjective complaints with related-but-not-corrective patches; the responder is treating them as "trackable product signal" instead of `no_action`. Stop-rule budget exhausted; documented as known-state in `evals/_negative_controls_locked.md`. The fix is offline re-planning of the escape-hatch definition, not more prompt churn.

- **Iteration 2 structural pass.** The pairwise revision-improvement scorer ran clean structurally, but **semantic spot checks deferred** to the next clean (non-remedial) full eval run.

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
│   ├── draft-response/            # Responder draft template (with action_severity_precedence)
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
│   │   ├── deterministic.py       # V1 deterministic scorers (action_correctness, etc.)
│   │   ├── gating_accuracy.py     # V1 retrieval-gating scorer
│   │   ├── judge_grounding.py     # V1.5 LLM judge — low_conf_with_cite ruling
│   │   ├── judge_action.py        # Option A LLM judge — wrong_action_severity ruling
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
- Anthropic API key (`CLAUDE_API_KEY`) — required for any agent run
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
- **Full eval suite** (`run_evals.py`, 56 cases): ~25–35 min wall clock; ~1.3M total tokens (per the latest snapshot); on the order of $1–2 per full run with all judges enabled.
- **Cached judge re-score** (offline against a saved run JSON): ~30 sec; $0 (cache hit on every flagged case).

Numbers are approximations grounded in the latest snapshot's `total_tokens=1,286,789`. Actual cost depends on Anthropic pricing at run time.

</details>

---

<details>
<summary><strong>Design notes — what I learned</strong></summary>

Three things I'd carry into the next project of this shape:

- **Eval before prompt edit, every time.** The first prompt edit driven by an eval finding (Option B `multi_part_complaint`) was a clean win because the gate was locked before the edit. The first edit *not* preceded by careful negative-control locking (Iteration 1 first pass) blew up immediately — over_escalation jumped from 4 to 11. The lock file is the discipline that makes prompt edits safe; without it, "I'll just tweak the prompt" is gambling with stochastic feedback.
- **Isolated `judge_error` buckets are non-negotiable.** Collapsing infrastructure failures into a substantive bucket silently undercounts real failures. Every judge in this project routes API/parse/validation errors into a dedicated `judge_error` ruling that surfaces in the snapshot diff. The cost is one extra column; the benefit is that a misfiring judge can never look like a passing eval.
- **Sibling-clone, then extract.** Three judge files (`judge_grounding.py`, `judge_action.py`, `pairwise.py`) were intentionally cloned rather than DRY'd up front. The right shape of the abstraction is now visible because the three files exist *and have been exercised on real failing cases*, not because someone guessed at the abstraction from a single example. The extraction is queued; the cost of one more clone was lower than the cost of locking in the wrong base class.

</details>

<details>
<summary><strong>What's next</strong></summary>

- Tighten the `monitor` escape-hatch definition in the responder skill (Iteration 1 follow-up — likely requires explicit volume language or a deterministic recurring-pattern signal rather than the responder's own judgment)
- Extract `evals/scorers/_judge_base.py` from the three sibling judges
- Re-baseline `recall@k` after either chunker or gold-standard tightening
- Wire the pairwise judge's semantic spot checks on the next clean (non-remedial) full eval run

</details>
