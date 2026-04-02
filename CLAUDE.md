# Steam Review Triage & Response Agent

## Project overview
Multi-agent LangGraph system that ingests Steam game reviews, classifies them, clusters complaints, and generates player-facing responses with evidence-based reasoning.

## Terminology
- `skills/` = agent skills (Python loads these via `utils.load_skill()`, sent to Anthropic API as system prompts)
- `.claude/skills/` = Claude Code skills (read by Claude Code for project conventions)
- These are different systems — do not confuse them

## Tech stack
Python, Anthropic API (Claude), LangGraph, SQLite, Pydantic, pandas, python-frontmatter

## Project structure
```
steam-review-agent/
├── pipeline/              # Data pipeline (ingest, clean, classify, cluster, stats)
├── agent/                 # LangGraph multi-agent system
│   ├── state.py           # AgentState TypedDict
│   ├── graph.py           # StateGraph construction, compilation, checkpointing
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
- TypedDict for LangGraph state (lightweight, no validation overhead)
- Nodes return state update dicts — never mutate state in place
- All LLM API calls in their own functions, separate from business logic
- All configuration in config.py, never hardcoded in modules
- Anthropic API (Claude) only — not OpenAI
- Use LangGraph directly — not LangChain

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

## Known gotchas
- Agent skill files use YAML frontmatter — `load_skill()` strips it before sending to API