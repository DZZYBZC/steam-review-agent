# Steam Review Triage & Response Agent

## Project overview
Multi-agent LangGraph system that ingests Steam game reviews, classifies them, clusters complaints, and generates player-facing responses with evidence-based reasoning.

## Terminology
- `skills/` = agent skills (Python loads these via `utils.load_skill()`, sent to Anthropic API as system prompts)
- `.claude/skills/` = Claude Code skills (read by Claude Code for project conventions)
- These are different systems — do not confuse them

## Tech stack
Python, Anthropic API (Claude), LangGraph, SQLite, Pydantic, pandas, python-frontmatter, ChromaDB, sentence-transformers, rank-bm25

## Project structure
```
steam-review-agent/
├── pipeline/              # Data pipeline (ingest, clean, classify, cluster, stats)
│   ├── ingest_reviews.py      # Fetch Steam reviews via API
│   ├── ingest_patch_notes.py  # Fetch & classify Steam patch notes
│   ├── chunk.py               # Markup stripping, section-aware chunking
│   └── retrieve.py            # Vector/BM25 indexing, RRF fusion, cross-encoder reranking
├── agent/                 # LangGraph multi-agent system
│   ├── state.py           # AgentState TypedDict
│   ├── models.py          # Pydantic models for node I/O (EvidencePackage, etc.)
│   ├── graph.py           # StateGraph construction, compilation, checkpointing
│   ├── utils.py           # Shared agent helpers (accumulate_tokens, format_evidence_sources)
│   └── nodes/             # One file per agent node
├── skills/                # Agent skills — SKILL.md files loaded by Python
├── .claude/skills/        # Claude Code skills — project conventions
├── config.py              # All configuration (models, temperatures, thresholds)
├── utils.py               # Shared utilities (load_skill with frontmatter parsing)
├── main.py                # Pipeline entry point
└── requirements.txt
```

## Core invariants
- Coordinator is ALWAYS plain Python routing logic — never an LLM call
- Pydantic for data crossing trust boundaries (LLM output, API responses)
- Pydantic models live in agent/models.py — one model per LLM-calling node's output
- TypedDict for LangGraph state (lightweight, no validation overhead)
- Nodes return state update dicts — never mutate state in place
- All LLM API calls in their own functions, separate from business logic
- All configuration in config.py, never hardcoded in modules
- Anthropic API (Claude) only — not OpenAI
- Use LangGraph directly — not LangChain
- Embedding/reranker models are lazy-loaded and cached at module level (not on import)

## What NOT to do
- Do not make the Coordinator an LLM node
- Do not scatter config values across files
- Do not add unnecessary abstractions or extra files beyond what was requested
- Do not mutate LangGraph state in place
- Do not confuse `skills/` (agent prompts) with `.claude/skills/` (Claude Code skills)

## Commands
- Activate venv: `source .venv/bin/activate`
- Run pipeline: `python main.py <app_id> [max_reviews]`
- Run pipeline (skip fetch): `python main.py <app_id> --skip-fetch`
- Test agent (single review, real LLM calls): `python test_agent.py [--category <cat>] [--review-id <id>] [--list]`
- Test graph compilation: `python test_graph.py`
- Manage cluster notes: `python resolve_note.py {list <app_id> <category> | resolve <note_id> | reactivate <note_id>}`

## Agent graph flow
- First pass (iteration 0): coordinator → investigator → responder → critic → (if approved) → **[interrupt]** → human_approval → END
- Critic rejection — **drafting type**: critic → coordinator → responder → critic → ... (re-draft only, evidence_package unchanged)
- Critic rejection — **evidence type**: critic → coordinator → investigator → responder → critic → ... (re-investigate using critic's `retrieval_hint` as query seed; falls back to default query construction if hint is empty)
- Terminal LLM/parse errors: responder or critic → coordinator → END with stop_reason="llm_error" or "parse_error" (skips rest of graph; coordinator audit-logs the errored run)
- Human rejection: human_approval → coordinator → responder → critic → ... (re-enters revision loop as drafting type)
- Terminates on: human approval, AGENT_MAX_ITERATIONS reached, human approval after max iterations, or terminal LLM/parse error
- The graph uses `interrupt_before=["human_approval"]` — pauses for human decision injection via `app.update_state()`
- Evidence chain of custody: source_ids → relevant_ids → source_ids_cited → Critic verifies source_ids_cited ⊆ relevant_ids
- Per-iteration observability: Critic writes one row per pass to `audit_log_iterations` (fire-and-forget), capturing draft, critique, rejection reason, and `reason_type`/`retrieval_hint` for eval analysis

## Human-in-the-loop
- Human review gate sits between Critic approval and graph termination
- `human_decision`: "approved", "rejected", or "" (awaiting)
- `human_feedback`: free-text revision guidance (used as revision_reason on rejection)
- On approval: saves audit log entry, graph ends with stop_reason="human_approved"
- On rejection with feedback: saves audit log entry + cluster note (type="human_feedback"), re-enters revision loop

## Feedback memory
- Audit log (`audit_log` table): records every completed agent run with evidence, draft, critique, and human decision
- Cluster notes (`cluster_notes` table): per-category institutional knowledge with lifecycle management
  - **Note types**: known_issue (auto from investigator), response_history (auto on approval), human_feedback (on rejection), investigation
  - **Status lifecycle**: `active` (default) → `resolved` (manual). Statuses defined in `CLUSTER_NOTE_STATUSES` config.
  - **TTL**: notes older than `CLUSTER_NOTE_STALENESS_DAYS` (90d) are excluded at read time (not deleted)
  - **Dedup**: `find_recent_similar_note()` checks source_review_id first, then falls back to time-window (`CLUSTER_NOTE_DEDUP_WINDOW_HOURS`, 24h)
  - **Write sources**: human_approval (human_feedback on reject, response_history on approve), investigator (known_issue when `is_sufficient` and `len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES` — deterministic gate; LLM-self-reported confidence is not used)
  - **Traceability**: `source_review_id` links notes to the review that triggered them
- Responder loads approved examples from audit log as few-shot context in the user message
- Investigator loads active, non-stale cluster notes as additional context in the LLM call

## Retrieval pipeline
Hybrid RAG: vector (ChromaDB + all-MiniLM-L6-v2) + BM25 → RRF fusion (top 12) → cross-encoder rerank (top 5) → Investigator node (up to 2 self-RAG retries with query reformulation). Reranker scores are internal to the retrieval pipeline — they order results but are not passed to the Investigator LLM (to avoid anchoring on uncalibrated floats).

## Proposed actions
- **no_action** — fully addressed by patches, or subjective/design-level feedback (pricing, story, design direction)
- **monitor** — known area not fully resolved, or design feedback overlapping with trackable technical concerns
- **investigate** — specific *technical* issue (bugs, crashes, performance) not addressed by patches. NOT for design opinions
- **escalate** — severe/widespread issue (crashes, data loss, security)

## Known gotchas
- Agent skill files use YAML frontmatter — `load_skill()` strips it before sending to API
- BM25 index is in-memory, rebuilt each run — not persisted like ChromaDB
- Cross-encoder absolute scores are not calibrated — do not threshold on them