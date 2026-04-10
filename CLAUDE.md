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
├── evals/                 # Eval harness (M5)
│   ├── run_evals.py           # Eval runner — loads golden.json, runs graph, writes run JSON
│   ├── reporter.py            # Stratified terminal report
│   ├── snapshot.py            # Versioned eval snapshots (schema v6) with cross-snapshot diffing
│   ├── scorers/               # Deterministic + LLM judge scorers
│   │   ├── deterministic.py   # Action correctness, retrieval recall, citation audit, grounding band
│   │   ├── gating_accuracy.py # Investigator gate confusion matrix
│   │   ├── judge_grounding.py # LLM judge for low_conf_with_cite flag
│   │   ├── judge_action.py    # LLM judge for wrong_action_severity
│   │   └── pairwise.py        # LLM judge for revision-loop improvement
│   └── test_sets/             # Golden set + regression seeds
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
- Run evals (full): `python evals/run_evals.py`
- Run evals (quick subset): `python evals/run_evals.py --quick`
- Run evals (single case): `python evals/run_evals.py --case-id <case_id>`
- Run evals (by category): `python evals/run_evals.py --category <category>`

## Agent graph flow
- First pass (iteration 0): coordinator → investigator → responder → critic → (if approved) → **[interrupt]** → human_approval → END
- Critic rejection — **drafting type**: critic → coordinator → responder → critic → ... (re-draft only, evidence_package unchanged)
- Critic rejection — **evidence type**: critic → coordinator → investigator → responder → critic → ... (re-investigate using critic's `retrieval_hint` as query seed; falls back to default query construction if hint is empty)
- Critic rejection — **action type** (Iter7 action-freeze): critic → coordinator → **[interrupt]** → human_approval → END. When `reason_type="action"` (the ONLY failing check is action correctness), the coordinator freezes the responder's action in `frozen_action`, sets `action_freeze_applied=True`, and routes directly to human_approval — bypassing the revision loop. Human rejection clears the freeze and re-enters the normal revision loop.
- Terminal LLM/parse errors: responder or critic → coordinator → END with stop_reason="llm_error" or "parse_error" (skips rest of graph; coordinator audit-logs the errored run)
- Human rejection: human_approval → coordinator → responder → critic → ... (re-enters revision loop as drafting type)
- Terminates on: human approval, AGENT_MAX_ITERATIONS reached, human approval after max iterations, or terminal LLM/parse error
- The graph uses `interrupt_before=["human_approval"]` — pauses for human decision injection via `app.update_state()`
- Evidence chain of custody: source_ids → relevant_ids → source_ids_cited → Critic verifies source_ids_cited ⊆ relevant_ids
- Per-iteration observability: Critic writes one row per pass to `audit_log_iterations` (fire-and-forget), capturing draft, critique, rejection reason, and `reason_type`/`retrieval_hint` for eval analysis
- Run identity: Coordinator mints a UUID `run_id` on first entry and threads it through state; both `audit_log` and `audit_log_iterations` carry it so per-iteration rows can be joined back to the completed run (same `review_id` can have multiple runs). Filter `WHERE run_id IS NOT NULL` in eval queries to exclude pre-fix legacy rows.

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
Hybrid RAG: vector (ChromaDB + all-MiniLM-L6-v2) + BM25 → RRF fusion (top 12) → cross-encoder rerank (top 5) → Investigator node (Anthropic tool-use API with `retrieve_patches` tool, up to `INVESTIGATOR_MAX_TOOL_CALLS` = 3 calls per invocation for query reformulation). Reranker scores are internal to the retrieval pipeline — they order results but are not passed to the Investigator LLM (to avoid anchoring on uncalibrated floats).

## Proposed actions

The four actions form a clean hierarchy along a single axis: **actionability + priority**. Earlier versions of this rubric mixed "technical vs design" with "severe vs mild," which overlapped badly at the `investigate`/`monitor` and `no_action`/`monitor` boundaries. The current version separates them:

- `no_action` = not actionable, or already resolved
- `monitor` = actionable signal is weak / partial / emerging
- `investigate` = actionable and unresolved
- `escalate` = actionable, unresolved, and urgent/high-impact

Full definitions:

- **no_action** — no follow-up warranted: already addressed or explained by shipped patches, or the feedback is too vague / purely emotional / taste-only / non-diagnostic to support a concrete next step. Pure preference complaints ("I don't like the art style") land here. Pricing complaints land here unless they describe a concrete failure mode (e.g. a broken checkout flow).
- **monitor** — plausible or recurring issue signal that is not yet specific or severe enough for immediate investigation. Includes partially resolved known issues, resurfacing complaints after a patch where the evidence is mixed, repeated pain points with weak specificity, and recurring subjective/product feedback that overlaps with measurable symptoms. Mental model: "keep eyes on this; don't ignore it, but don't open an urgent triage ticket yet."
- **investigate** — specific unresolved issue with enough concrete detail to actively triage or reproduce, not clearly addressed by patches or known-issue notes. Usually bugs, crashes, performance regressions, progression blockers, broken UX flows, or other actionable defects. The gate is "specific and actionable vs vague and preference-based" — a design complaint that describes a concrete failure mode (e.g. "the crafting menu needs 4 clicks to exit, breaks keyboard nav") can land here; a pure taste complaint ("crafting feels clunky") cannot.
- **escalate** — urgent high-priority issue requiring immediate attention because of severity, blast radius, or risk. The distinguishing feature versus `investigate` is not "technical vs design" — it's "would a delay of days cause meaningful harm?" — and "harm" counts when it applies to the individual reviewer, not only when blast radius is explicit. Two paths qualify:
  - **(a) Widespread/blast-radius framing.** Widespread crash-on-launch, save/data loss at scale, security/privacy/payment issues, account lockouts, or major post-patch regressions affecting many users.
  - **(b) Concrete hard-blocker symptom from a single reviewer** — paired with explicit persistence, reproducibility, or blocker framing. Examples: "constant crashes that reproduce every session," "save file is gone," "game won't launch after reinstalling," "can't progress past X after multiple attempts," "hundreds of crashes in 40 hours." The symptom must be concrete AND the review must convey persistence/reproducibility (not a one-off).

  **Heated adjectives alone do NOT qualify for escalate.** Words like "unplayable," "broken," "trash," "terrible" in isolation are not enough — they must be paired with a concrete failure mode or explicit persistence/reproducibility language. A server-lag venting review that says "pretty much unplayable" without a specific reproducible hard blocker stays at `monitor`, not `escalate`.

Important nuance on subjective feedback: **do not automatically route all subjective/design-level feedback to `no_action`.** Purely taste-based single-player comments go to `no_action`; recurring subjective-but-meaningful pain signals ("combat feels floaty" reported by many players after an update) go to `monitor` because the recurrence itself is a real product signal even when the individual comment is non-diagnostic.

**Exclusion on the recurring-signal clause:** pricing, DLC strategy, monetization structure, and other business-model complaints do NOT qualify under this clause. They stay at `no_action` unless they describe a concrete failure mode (e.g. a broken checkout flow, a paywall bug). The clause is for subjective pain that overlaps with measurable product symptoms (combat feel, performance perception, difficulty feel, UI friction), not for value-judgment disagreements with the business model.

## Known gotchas
- Agent skill files use YAML frontmatter — `load_skill()` strips it before sending to API
- BM25 index is in-memory, rebuilt each run — not persisted like ChromaDB
- Cross-encoder absolute scores are not calibrated — do not threshold on them