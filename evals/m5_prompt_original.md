I'm working through a multi-milestone Steam review triage agent. My project files are uploaded as project sources. My project instructions are unchanged.

## What I built in Milestone 1 (done, don't rebuild):

- Refactored v1 pipeline code into `pipeline/` (ingest_reviews, ingest_patch_notes, clean, classify, cluster, storage, stats, chunk, retrieve)
- LangGraph agent skeleton in `agent/` with `state.py` (AgentState TypedDict), `graph.py` (StateGraph with four nodes + conditional edges + checkpointing), and nodes in `agent/nodes/` (coordinator, investigator, responder, critic)
- Coordinator is plain Python routing — no LLM call. Routes via conditional edges based on `approved`, `iteration_count`, `AGENT_MAX_ITERATIONS` (read from config, not state)
- On revision cycles (iteration > 0), coordinator routes based on `reason_type`: `"evidence"` → investigator (re-retrieve), `"drafting"` or empty → responder
- Checkpointing configured with MemorySaver (dev), SqliteSaver option in config (with check_same_thread=False)
- Skills migrated to `skills/` with YAML frontmatter, loaded by `utils.load_skill()` using python-frontmatter
- Claude Code skills in `.claude/skills/` for project conventions and LangGraph patterns
- `CLAUDE.md` at project root with project structure and invariants
- `test_graph.py` passes — graph compiles and runs
- `config.py` has all configuration (models, temperatures, thresholds, checkpoint settings)

## What I built in Milestone 2 (done, don't rebuild):

- `pipeline/ingest_patch_notes.py` — Fetches game news from Steam News API, tags as patch/content_update/event, filters to developer feeds, drops events. Uses shared retry logic from `pipeline/retry.py`.
- `pipeline/chunk.py` — Section-aware chunker. Strips BBCode/HTML/Steam markup. Each fix becomes its own chunk with version header prepended. PatchChunk dataclass with full metadata. Sentence-boundary splitting for long chunks.
- `pipeline/retrieve.py` — Full hybrid retrieval pipeline:
  - `embed_chunks()` upserts into ChromaDB (cosine distance, batched)
  - `query_similar()` vector search with all-MiniLM-L6-v2
  - BM25 index cached at module level by app_id (built from ChromaDB documents, not re-fetched)
  - `reciprocal_rank_fusion()` merges vector + BM25 results
  - `rerank()` with cross-encoder/ms-marco-MiniLM-L-6-v2
  - `retrieve(query, app_id)` — single function wrapper for the full pipeline
- Config: EMBEDDING_MODEL, CHROMA_PERSIST_DIR, SIMILARITY_THRESHOLD=0.3, VECTOR_TOP_K=8, BM25_TOP_K=8, RRF_K=60, RERANKER_MODEL, RERANKER_TOP_N=5
- Tested on Monster Hunter Wilds (app 2246340) patch notes — 1085 chunks indexed

## What I built in Milestone 3 (done, don't rebuild):

### Pydantic models (`agent/models.py`):
- `EvidencePackage` — handoff contract between Investigator → Responder → Critic. Fields: summary, confidence (0.0-1.0 clamped), relevant_ids, source_ids, sources (list[dict]), known_unknowns, retrieval_decision (Literal["retrieved", "skipped", "insufficient"]), retrieval_reasoning, query_used. Has `to_dict()` and `from_dict()` convenience methods.

### Agent skills (in `skills/`):
- `investigate-evidence/SKILL.md` — Investigator's Self-RAG prompt. Judges relevance, checks sufficiency, reformulates queries. JSON output with relevant_ids, summary, confidence, known_unknowns, is_sufficient, reformulated_query. Includes confidence rubric, assessment process checklist, 3 few-shot examples using real MHW data.
- `draft-response/SKILL.md` — Responder's drafting prompt. Tone matching rules (frustrated, angry, constructive, neutral, sarcastic, disappointed, confused, appreciative). Confidence-to-language grounding rules. Known unknowns handling. Revision instructions section. JSON output with response_text, proposed_action, source_ids_cited. 4 few-shot examples.
- `critique-draft/SKILL.md` — Critic's evaluation prompt. 6-point checklist (hallucination, overconfidence, known unknowns, tone, completeness, action). Decision rules for approve/reject. Verifies source_ids_cited ⊆ relevant_ids. JSON output with approved, critique, revision_reason, reason_type (evidence/drafting), retrieval_hint. 6 few-shot examples covering approval, hallucination rejection, tone rejection, evidence-type rejection.

### Agent nodes (in `agent/nodes/`):
- **Coordinator** (`coordinator.py`) — Plain Python. Sets stop_reason ("max_iterations_reached", "revising", "llm_error", "parse_error"). Coordinator never sees approved=True — Critic routes directly to human_approval on approval, bypassing Coordinator. route_from_coordinator() returns "investigate" (first pass or evidence-type revision), "respond" (drafting-type revision), or "done" (max iterations/terminal errors). Terminal error stop_reasons skip downstream nodes.
- **Investigator** (`investigator.py`) — Three-layer evidence gathering:
  - Layer 1: Deterministic gate — skips retrieval for "other" category (checks against RETRIEVAL_CATEGORIES from config)
  - Layer 2: Calls `retrieve(query, app_id)` from `pipeline/retrieve.py`
  - Layer 3: Self-RAG LLM call — judges relevance, sufficiency, reformulates query. Up to SELF_RAG_MAX_RETRIES=2 retries.
  - Loads cluster notes (active, non-stale) as additional context for Self-RAG calls
  - Auto-saves `known_issue` cluster notes when `is_sufficient` is true AND `len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES` (deterministic gate, replaces old confidence-based threshold)
  - Reranker scores excluded from LLM input (scores used for ranking only, not shown to the model)
  - On re-entry (evidence-type revision), uses `retrieval_hint` from Critic as query seed; falls back to original query construction if hint is empty
  - Produces EvidencePackage, tracks token usage, LLM/parse errors return terminal stop_reason instead of burning iterations
  - LLM call separated into `_call_investigator_llm()` (same pattern as classify.py's `call_classifier()`)
- **Responder** (`responder.py`) — Evidence-grounded drafting:
  - Reads evidence_package, review_text, review_tone, cluster_summary
  - On first draft (iteration 0): loads feedback examples from audit log as dynamic few-shot
  - On revision cycles: includes previous draft + revision_reason in the prompt (skips feedback examples to save tokens)
  - Returns drafted_response, proposed_action, source_ids_cited, incremented iteration_count
  - Error fallback: returns terminal stop_reason="llm_error" or "parse_error" (does not increment iteration)
- **Critic** (`critic.py`) — Grounding check + quality gate:
  - Reads evidence_package, drafted_response, proposed_action, source_ids_cited
  - Verifies claims against evidence, checks confidence alignment, tone matching, action appropriateness
  - Returns approved, critique, revision_reason, reason_type (evidence/drafting), retrieval_hint, stop_reason
  - Writes per-iteration audit row (fire-and-forget via `save_audit_iteration`)
  - Error fallback: returns terminal stop_reason="llm_error"

### Shared utilities:
- `utils.py` (project root) — `load_skill()`, `strip_code_fences()`, `parse_llm_json()` (shared JSON parsing with code-fence stripping used by all LLM-calling nodes)
- `agent/utils.py` — `accumulate_tokens()` (merges token dicts, accumulates across revision cycles), `format_evidence_sources()` (shared evidence formatting for Responder and Critic)
- `pipeline/retry.py` — shared exponential backoff logic used by both ingest modules
- `pipeline/keywords.py` — shared stop words list and keyword extraction used by cluster.py and stats.py

### Config additions:
- INVESTIGATOR_MODEL = claude-haiku-4-5-20251001, INVESTIGATOR_TEMPERATURE = 0.1, INVESTIGATOR_MAX_TOKENS = 400
- RESPONDER_MODEL = claude-sonnet-4-6, RESPONDER_TEMPERATURE = 0.4, RESPONDER_MAX_TOKENS = 1000
- CRITIC_MODEL = claude-haiku-4-5-20251001, CRITIC_TEMPERATURE = 0.1, CRITIC_MAX_TOKENS = 400
- SELF_RAG_MAX_RETRIES = 2
- RETRIEVAL_CATEGORIES = all categories except "other"
- CLUSTER_NOTE_AUTO_MIN_SOURCES = 2 (replaced CLUSTER_NOTE_AUTO_CONFIDENCE for auto-save gating)
- TEST_APP_ID = "2246340" (consolidated from hardcoded values in test files)

### State (`agent/state.py`):
- Input: `app_id`, `review_id`, `review_text`, `cluster_summary`, `review_tone`
- Output: `evidence_package`, `drafted_response`, `proposed_action`, `source_ids_cited`, `critique`
- Control: `iteration_count`, `approved`, `revision_reason`, `reason_type` (evidence/drafting/''), `retrieval_hint`, `stop_reason`, `run_id` (UUID, minted once by Coordinator on first entry), `node_log` (Annotated append-only), `token_usage`
- Human-in-the-loop: `human_decision`, `human_feedback`

### Evidence chain of custody (verified working):
```
Investigator: source_ids (all retrieved) → relevant_ids (LLM filtered)
Responder:    source_ids_cited (chunks referenced in draft) ⊆ relevant_ids
Critic:       verifies source_ids_cited ⊆ relevant_ids, rejects if violated
```

## What I built in Milestone 4 (done, don't rebuild):

### Human-in-the-loop:
- `agent/nodes/human_approval.py` — Plain Python node. Reads `human_decision`/`human_feedback` from state. Routes: approved → END, rejected → coordinator (revision loop), empty → END (interrupt stops before here).
- Graph flow: critic approves → conditional edge to `human_approval` node → `interrupt_before=["human_approval"]` pauses the graph → caller injects decision via `app.update_state()` → `app.invoke(None, config)` resumes.
- Critic rejection still goes directly to coordinator (no human gate for Critic rejections).
- Coordinator clears `human_decision`/`human_feedback` when re-entering revision loop.
- `CHECKPOINT_BACKEND` switched to `"sqlite"` as default.

### Audit log:
- `audit_log` table in SQLite (via storage.py `create_tables()`): records every completed agent run with run_id (UUID, minted by Coordinator), review, draft, evidence summary/confidence, critique, human decision/feedback, iteration count, stop reason, token usage.
- `save_audit_entry(conn, state)` extracts from AgentState dict, serializes lists/dicts as JSON.
- `_log_audit()` helper in human_approval.py — called on both approve and reject, wrapped in try/except so DB errors never crash the graph.
- Index on `(app_id, category)` for feedback memory queries.

### Feedback memory (dynamic few-shot):
- `load_feedback_examples(conn, app_id, category, n=3)` — queries audit log for approved drafts matching app+category, ordered by recency.
- Responder loads examples on first draft only (iteration 0), appends to user message after `_build_user_message()` returns.
- Format: "Here are examples of previously approved responses..." with review text (truncated to 200 chars) and full drafted response.

### Structured cluster notes:
- `cluster_notes` table with columns: id, app_id, category, note_type (known_issue/response_history/investigation/human_feedback), tags (JSON), note_text, created_by (system/human), status (active/resolved), source_review_id, created_at, updated_at.
- Schema migrations via numbered `_apply_migration()` system with `schema_version` table (idempotent, replaces ad-hoc ALTER pattern).
- `save_cluster_note()` — with source_review_id parameter for traceability.
- `load_cluster_notes()` — filters by status (default: active) and TTL (default: exclude notes older than CLUSTER_NOTE_STALENESS_DAYS=90).
- `find_recent_similar_note()` — two-tier dedup: checks source_review_id first (exact match), falls back to time-window (CLUSTER_NOTE_DEDUP_WINDOW_HOURS=24h).
- `update_cluster_note_text()` and `update_cluster_note_status()` for lifecycle management.
- Three write sources:
  1. Human rejection → saves `human_feedback` note (with dedup)
  2. Human approval → saves `response_history` note (full response text, with dedup)
  3. Investigator → auto-saves `known_issue` note when confidence >= 0.5 (with dedup)
- Investigator loads active non-stale notes before retrieval, passes to every Self-RAG LLM call (including retries).
- Config: CLUSTER_NOTE_STALENESS_DAYS=90, CLUSTER_NOTE_DEDUP_WINDOW_HOURS=24, CLUSTER_NOTE_AUTO_CONFIDENCE=0.5, CLUSTER_NOTE_STATUSES=["active", "resolved"]

### Post-M4 fixes:
- Investigator dedup now passes `source_review_id` to `find_recent_similar_note()` for tier-1 exact match
- Coordinator saves an audit entry on `max_iterations_reached` exit — no agent run goes untracked
- `resolve_note.py` CLI for cluster note lifecycle management (list/resolve/reactivate subcommands)
- `update_cluster_note_status()` returns False on zero rows affected (rowcount check)

### Pre-eval design fixes (F1-F4, F6):
- **F2**: `audit_log_iterations` table — Critic writes one fire-and-forget row per pass (draft, critique, reason_type, retrieval_hint, tokens). Both `audit_log` and `audit_log_iterations` carry `run_id` for unambiguous joins across multi-run reviews. Per-iteration data now queryable for eval analysis.
- **F4**: Responder and Critic error branches return terminal `stop_reason="llm_error"` / `"parse_error"` instead of incrementing iteration. Coordinator and graph.py route terminal errors to END. `max_iterations_reached` no longer polluted by transient API errors.
- **F6**: Dropped `relevance=0.82` fragment from `_format_evidence_for_llm` — reranker scores used for ranking only, not shown to LLM. Same pattern stripped from investigate-evidence skill examples.
- **F3**: Auto-save gate replaced: `confidence >= 0.5` → `is_sufficient AND len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES` (default 2). `CLUSTER_NOTE_AUTO_CONFIDENCE` deprecated but kept.
- **F1**: Critic emits `reason_type` (evidence/drafting) and `retrieval_hint`. Coordinator routes evidence-type rejections to Investigator. Investigator uses hint as query seed on re-entry, falls back to default construction if empty. Bad hints recovered by existing Self-RAG retry loop. Updated critique-draft skill with new schema + evidence-type rejection example.

### Testing:
- `test_graph.py` — compiles graph, runs with interrupt + auto-approve, asserts stop_reason="human_approved"
- `test_agent.py` — interactive human review loop with [y/n/auto] prompt, full diagnostic report showing human decision, audit log entry confirmed saved
- End-to-end verified: graph pauses at interrupt, `update_state` injects decision, `invoke(None, config)` resumes, audit entry written, cluster notes loaded/saved

### Known state after M4:
- main.py doesn't run the agent graph yet — pipeline ends at clustering.
- All six memory types active: Working (AgentState), Semantic (RAG), Procedural (skills), Episodic (audit log), Feedback (dynamic few-shot), Structured notes (cluster notes).

## Corrected workflow (established during M2-M4):
- **This project chat**: concepts, design decisions, architecture, what to tell Claude Code
- **Claude Code in VS Code terminal**: writes the actual code, I review diffs
- **You give me**: requirements and Claude Code prompts, not finished Python files

## I'm ready for Milestone 5 — Evals. Per my project instructions:

### Design principles (established during pre-M5 review):
- **Categorical failure tags > scalar scores.** A judge score of 3.8/5 is not actionable. "6 of 30 runs cited a patch that doesn't address the complaint" is a fixable bug. The failure mode taxonomy is the single most valuable eval artifact — build it first, before any scoring.
- **Don't eval dead telemetry.** The Investigator's `confidence` field is not consumed by any routing decision (auto-note gate uses `len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES`). Drop confidence calibration. Either make confidence drive a decision or stop measuring it.
- **Validate the judge before trusting judge scores.** The LLM-as-judge is an unvalidated instrument until you human-rate ~10 cases and check correlation. Run a one-time judge consistency check (same 5 cases × 3 runs, check variance per dimension) before any scored eval run. Gate V1.5 on this.
- **Quick is a strict subset of full.** The `--quick` set is the first 5 cases of the golden set, not a different sample. This prevents "quick passed but full failed" surprises caused by sample variance instead of regressions.
- **Budget your eval runs.** V1 evals are all deterministic (zero LLM cost beyond running the agent itself). V1.5 adds the LLM judge — 50 golden cases × revision iterations × judge call × token cost adds up. Know the per-run cost before making it a habit. This is why V1.5 is gated on V1 signal: don't spend on LLM scoring until deterministic metrics show the agent is worth scoring.

### V1 evals (build first):

1. **Test sets — golden + regression split:**
   - **Golden set**: ~50 curated reviews, balanced across categories (~5 per category). Each annotated with: expected classification category, expected proposed_action, must-include chunk IDs (for retrieval recall), failure mode tags (from taxonomy). The first 5 cases are the `--quick` subset — frozen, most representative.
   - **Regression set**: starts empty, grows over time. Add past failures, edge cases, and reviews that exposed bugs. Both sets run on every change.
   - Versioned by git SHA (not manual version numbers).

2. **Failure mode taxonomy:**
   Define 5-10 specific ways the agent fails. Tag every eval run against these. Examples:
   - `cited_irrelevant_patch` — cites a patch that doesn't address the complaint
   - `overclaimed_fix` — claims fix when patch only mitigates
   - `defensive_tone` — defensive response to a legitimate complaint
   - `wrong_action_severity` — action doesn't match complaint severity (e.g., "no_action" for crash reports)
   - `hallucinated_claim` — claims something not in any retrieved evidence
   - `ignored_main_complaint` — response addresses a side issue, skips the primary complaint
   - `investigate_for_subjective` — proposed "investigate" for a pure design/opinion complaint
   - `retrieval_hint_fallback` — Investigator fell back from Critic's retrieval_hint to default query (Critic↔Investigator handoff failure)
   This taxonomy is more actionable than any scalar score. Extend it as new failure patterns emerge.

3. **Eval runner skeleton** (`evals/run_evals.py`):
   - `--quick` flag runs the 5-case quick subset; full run without flag runs all golden + regression cases.
   - Runs the agent graph on each test case (auto-approves at human gate).
   - **JSONL observability log**: every eval run dumps a structured log per Investigator pass: `{query, retrieved_ids, accepted_ids, rejected_ids, reformulated_query, sufficiency_verdict, retrieval_hint_used, fell_back_to_default}`. Free debugging — when a score drops, open the log and see why without re-running. Also log when the Investigator falls back from `retrieval_hint` to default query construction (silent Critic↔Investigator handoff failure).

4. **Deterministic scorers** (no LLM calls, computed from existing state):
   - **Retrieval recall@k**: annotate only "must-include" chunks per test case. Report what fraction were found in `relevant_ids`. Skip precision — exhaustive relevance annotation is too expensive for now.
   - **proposed_action correctness**: compare agent's `proposed_action` against annotated expected action. Report as a confusion matrix (4 classes: no_action, monitor, investigate, escalate). Cheapest ground truth, highest signal — one label per review.
   - **Deterministic citation audit**: programmatically verify `source_ids_cited ⊆ relevant_ids` on every eval run. The Critic already enforces this, but an out-of-band check confirms the Critic is doing its job and catches chain-of-custody regressions if you refactor.
   - **Evidence utilization rate**: `len(source_ids_cited) / len(relevant_ids)` per run. Diagnoses Investigator over-retrieval vs Responder under-citation. Zero annotation cost.
   - **Token cost per run**: median and p95 token cost, broken down by node. Already in `token_usage` state field. Immediately actionable for finding waste (e.g., Self-RAG retries burning 3x tokens for marginal quality).
   - **Critic health check**: approval rate by iteration number, stratified by `reason_type` (evidence vs drafting). Diagnoses over-critical (never approves iter 0), rubber-stamp (approves everything), and whether evidence-type rejections actually lead to better re-retrieval.

5. **Revision improvement eval** (pairwise):
   - Compare pre-revision draft (iteration 0, from `audit_log_iterations`) vs final draft. LLM picks a winner. Track win rate by category.

6. **Stratified reporting + versioned snapshots:**
   - All metrics broken down by category. Caveat small-n per category (~5 cases each) — these are directional, not statistically significant.
   - Snapshot saved as JSON with git SHA, timestamp, test set version, all metric values. Compare across runs.

### V1.5 evals (after first signal from V1):

7. **Judge consistency check** (one-time gate):
   - Run the LLM-as-judge on 5 cases × 3 runs. Report standard deviation per dimension. If variance is high, fix the judge prompt before trusting any scores. This gates all subsequent LLM-judge evals.

8. **eval-judge skill + multi-dimensional LLM scorer:**
   - `eval-judge/SKILL.md` — scores grounding accuracy, tone match, actionability, completeness (each 1-5). Brief rationale per dimension.
   - Report pass rate (≥ 3.5/5) and excellent rate (≥ 4.5/5) per dimension alongside averages. An average of 3.8 could mean "everything mediocre" or "half excellent, half terrible" — thresholds tell you which.
   - Also tags each run with failure mode(s) from the taxonomy.

9. **Win rate vs approved baseline** (pairwise):
   - For test reviews with a human-approved response in the audit log, show both to the judge without labels, ask which is better. **Run both orderings** (agent-first and human-first) and average to control for position bias.
   - Report win/loss/tie percentages. Note: this compares "agent v2" against "agent v1 + human filter" — useful but not a clean A/B.

10. **Retrieval gating accuracy:**
    - Annotate ~10 cases where the deterministic gate skips retrieval, ~10 where it retrieves. Check: would retrieval have helped the skipped cases? Was it worth it for the retrieved cases? Catches miscategorized gate logic (e.g., a "story_presentation" review that's actually about a cutscene crash).

11. **OOD canary set:**
    - 3-5 adversarial/weird reviews: off-topic, multilingual, 5-word reviews, prompt injection attempts. Run but don't include in main metrics. Cheap robustness insurance. Check for graceful degradation (terminal stop_reason, not a crash).

### One-time experiments (not recurring evals):
- **Feedback memory ablation**: run each test case with and without feedback examples. Compare LLM-as-judge scores. If feedback examples don't move scores, simplify or remove the loop. Don't make this a recurring metric — 2x runs per case is expensive.
- **Judge calibration against human ratings**: you grade 10 cases yourself, compare to LLM-judge scores. This validates the instrument. Also plan for periodic human spot-checks (5 cases/week) to catch judge drift over months.

### Deferred:
- **Self-RAG annotated evals** (relevance/sufficiency accuracy with ground truth labels) — highest annotation cost, lowest immediate ROI. The JSONL observability log gives qualitative signal for free. Promote to a scored eval only when qualitative review shows retrieval/sufficiency failing systematically.
- **Latency tracking** — not needed until shipping to users. Single-developer batch workflow.

### Dropped:
- **Confidence calibration** — the Investigator's confidence field is not consumed by any routing decision. Don't eval dead telemetry.

### Build order:
1. Show the updated architecture diagram (M4 complete, M5 highlighted)
2. Explain new concepts before building (failure mode taxonomy, golden vs regression sets, recall@k, LLM-as-judge validation, pairwise position bias)
3. Give me what to tell Claude Code, step by step — V1 evals first, V1.5 after first signal
4. Walk me through each component after it's built

Go step by step — only move to the next step after I confirm.