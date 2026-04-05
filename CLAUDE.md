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

## Agent graph flow
- First pass (iteration 0): coordinator → investigator → responder → critic → coordinator
- Revision cycles (iteration > 0): coordinator → responder → critic → coordinator (skips investigator)
- Terminates on: critic approval or AGENT_MAX_ITERATIONS reached
- Evidence chain of custody: source_ids → relevant_ids → source_ids_cited → Critic verifies source_ids_cited ⊆ relevant_ids

## Retrieval pipeline
Hybrid RAG: vector (ChromaDB + all-MiniLM-L6-v2) + BM25 → RRF fusion (top 12) → cross-encoder rerank (top 5) → Investigator node (up to 2 self-RAG retries with query reformulation). Reranker scores are for ranking only — relevance judgment is left to the downstream agent, not a score threshold.

## Proposed actions
- **no_action** — fully addressed by patches, or subjective/design-level feedback (pricing, story, design direction)
- **monitor** — known area not fully resolved, or design feedback overlapping with trackable technical concerns
- **investigate** — specific *technical* issue (bugs, crashes, performance) not addressed by patches. NOT for design opinions
- **escalate** — severe/widespread issue (crashes, data loss, security)

## Known gotchas
- Agent skill files use YAML frontmatter — `load_skill()` strips it before sending to API
- BM25 index is in-memory, rebuilt each run — not persisted like ChromaDB
- Cross-encoder absolute scores are not calibrated — do not threshold on them