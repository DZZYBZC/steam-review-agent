Milestone 5 — Evals: full implementation plan                                                                                                                   
                                                                                                                                                               
 Context

 M1–M4 are complete: agent graph, retrieval, HITL, audit log, cluster notes, and the pre-eval run_id linkage are all live. The system can be exercised
 end-to-end via test_agent.py but has no automated quality measurement. M5 builds the eval infrastructure that lets us answer "did this prompt change actually
 help?" instead of eyeballing diffs.

 The full eval design is in m5_prompt_v3.md at the project root. This plan converts that design into a numbered, executable build order. Two phases:

 - V1 = deterministic + free metrics. Zero LLM cost beyond running the agent itself. Gives usable signal on day one.
 - V1.5 = LLM-as-judge layer. Gated on (a) V1 producing actionable signal, and (b) a one-time judge consistency check.

 Design principles (from m5_prompt_v3.md, established during pre-M5 review — these are non-negotiable):
 - Categorical failure tags > scalar scores. Build the taxonomy first.
 - Don't eval dead telemetry. Confidence calibration is dropped.
 - Validate the judge before trusting judge scores. Consistency check gates V1.5.
 - --quick is a strict subset of --full — first 5 cases of the golden set, frozen.
 - Snapshot results are versioned by git SHA, not manual numbers.

 ---
 Findings from full codebase review (improvements over the original draft)

 A complete sweep of the codebase surfaced six issues the original plan missed. Each is folded into the relevant step below, but they're worth flagging up-front
  since they change non-trivial decisions.

 1. load_classified_reviews excludes the other category (pipeline/storage.py:367).
 The standard loader filters WHERE c.primary_category != 'other'. The gating accuracy eval (Step 13) specifically needs other cases — those are where the gate
 skips retrieval. The gating eval must use load_classifications joined with reviews manually, or write a dedicated loader. Same caveat applies if any golden
 case wants to test the other path.
 2. cluster_summary is read for category only — by Investigator (investigator.py:139–140), Responder (responder.py:148), human_approval (human_approval.py:26),
 and save_audit_entry (storage.py:403). Nothing reads total_reviews, priority_score, or summary. The golden test case schema can shrink to {"category": "..."}.
 Decision 5 below is updated.
 3. source_ids_cited is JSON-serialized when written to audit_log_iterations (storage.py:460). Scorers that read from the DB (the pairwise eval, the
 critic_health scorer reading audit_log_iterations) must json.loads() it. Scorers that read from the in-memory result dict can use the list directly.
 4. audit_log_iterations does NOT store evidence_package (storage.py:122–140). It stores per-iteration drafted_response, proposed_action, source_ids_cited,
 critique — but the evidence the responder saw is not preserved. Implication for the pairwise revision improvement eval (Step 12): when comparing iter-0 vs
 final draft, the judge will see the final evidence package from the result, used as proxy for both drafts. This is fine for drafting-type rejections (where
 evidence is unchanged across iterations) but slightly impure for evidence-type rejections. Acceptable limitation; document it in the scorer's docstring rather
 than reworking the schema.
 5. The Critic already evaluates 4 of the 4 proposed eval-judge dimensions. skills/critique-draft/SKILL.md runs a 6-point checklist covering hallucination,
 overconfidence/grounding, tone, completeness, and action appropriateness. The proposed eval-judge dimensions (grounding accuracy, tone match, actionability,
 completeness) are the same checks the Critic already runs. This is a major framing issue for Step 10 — building eval-judge as currently scoped means
 re-implementing the Critic and calling it an eval. Two consequences:
   - Reframe eval-judge as parallel-but-different: keep the four dimensions, but add a 5th dimension the Critic doesn't measure — perceived_player_satisfaction
 (would a player who wrote this complaint feel heard by this response? this is a holistic player-experience score that the Critic's task doesn't include).
   - Use Critic↔eval-judge agreement as a calibration signal. When the Critic approves a draft but the eval-judge scores it ≤ 2 on any dimension, that's
 evidence the Critic prompt is too lenient (or the judge is too strict). Add a Critic-vs-judge agreement metric to the reporter (Step 11). This re-uses
 information you'd compute anyway and turns judge cost into Critic-tuning signal.
 6. classifications.needs_review flags low-confidence classifications (storage.py:295–297, set when confidence < CONFIDENCE_THRESHOLD = 0.7). These are
 pre-existing edge cases where the classifier itself was uncertain — perfect natural seeds for the regression set in Step 3. Worth surfacing as an annotation
 aid: SELECT review_id, primary_category FROM classifications WHERE app_id IN ('2246340','2694490') AND needs_review = 1.
 7. skills/draft-response/SKILL.md encodes grounding rules by confidence band that are currently only enforced by the Critic LLM. Specifically: confidence
 0.0–0.4 → no patch claim, no version cite, source_ids_cited should be empty; confidence 0.4–0.7 → may reference improvements without claiming a complete fix;
 confidence 0.7–1.0 → may directly cite. These bands are deterministically checkable against the run result without an LLM. Adds a 7th deterministic scorer to
 Step 5 (grounding_band_compliance) — cheap, catches a real failure mode (citing patches at low confidence) without needing the judge.

 ---
 Design decisions to confirm before building

 These need a yes/no from you. Each is small, but they shape file contents and would be expensive to undo later. Flagged with [DECIDE].

 1. [DECIDE] Failure mode taxonomy v0 — apply prior critique or keep prompt as-is?
 m5_prompt_v3.md lists 8 modes including retrieval_hint_fallback and hallucinated_claim. In our earlier conversation I argued:
   - retrieval_hint_fallback is an internal flow event, not a failure of the agent's output. It belongs in the JSONL observability log, not as a tag on a
 drafted response. Annotators won't be able to apply it consistently to a final response.
   - hallucinated_claim overlaps with cited_irrelevant_patch and overclaimed_fix. Three modes covering the same territory muddies tagging.
   - Recommendation: drop retrieval_hint_fallback (move it to JSONL log only), and either drop hallucinated_claim or sharpen it to "claim with no source at all"
  (vs. "claim with wrong source" = cited_irrelevant_patch, "claim that exaggerates source" = overclaimed_fix).
   - Add a top-of-file comment marking the taxonomy as v0 hypothesis, expect to revise after first 10 golden runs.
   - Default for this plan: apply the recommendation. Tell me if you want to keep the prompt's list verbatim instead.
 2. [DECIDE] Golden set source — single app or multi-app?
 TEST_APP_ID = "2246340" (Monster Hunter Wilds) is the canonical test app. PoE2 (2694490) was added recently to fill the three missing categories. ~50 reviews ×
  10 categories ≈ 5 per category. MHW alone may not cover content_progression, multiplayer_network, ui_controls. Recommendation: multi-app golden set, with
 app_id recorded per case. Both apps have classified reviews and embedded patch notes ready.
 3. [DECIDE] JSONL observability log — eval-only or always-on?
 Prompt says "every eval run dumps a structured log." But the log is also useful outside evals (debugging real runs). Recommendation: always-on, written by the
 Investigator node directly to evals/logs/investigator_<run_id>.jsonl. Cheap, no eval/prod split, gives historical depth. Alternative: eval-only via a wrapper
 that hooks the runner. Less code in agent/, but loses production observability. Default for this plan: always-on, written from inside investigator_node.
 4. [DECIDE] Quick subset = first 5 by index, or hand-picked 5?
 Prompt says "frozen, most representative." Recommendation: hand-picked — one easy case, one critic-rejection case, one retrieval-skipped (other) case, one
 multi-iteration case, one borderline case. Mark them with quick: true in the golden JSON. By-index is simpler but less representative.
 5. [DECIDE] Annotation schema for a golden test case.
 Each entry needs: review fields + ground truth. Proposed JSON shape (simplified per Finding 2):
 {
   "id": "golden-001",
   "app_id": "2246340",
   "review_id": "...",
   "review_text": "...",
   "category": "technical_issues",
   "quick": true,
   "expected": {
     "proposed_action": "investigate",
     "must_include_chunk_ids": ["chunk_42", "chunk_77"],
     "failure_modes_acceptable": []
   },
   "notes": "free-text annotator commentary"
 }
 5. At runtime, the runner builds initial AgentState by mapping category → cluster_summary = {"category": category}. No total_reviews, priority_score, or
 summary needed — none of the agent nodes read those fields.

 5. Note on review_tone: it does not need to live in the golden case schema. Despite being grouped under "Input" in agent/state.py:23, review_tone is actually
 populated by the Investigator node (investigator.py:144–153 calls classify_tone() itself), and test_agent.py:124 already passes "" as initial state. The runner
  will mirror this — pass empty string, let the Investigator fill it in. Annotating tone manually would also be redundant work for no scorer.
 6. [DECIDE] Snapshot storage — evals/snapshots/ gitignored, or committed?
 Prompt says gitignored. Committing gives long-term history but bloats the repo over time. Recommendation: gitignored for now; manually copy a snapshot into
 evals/snapshots_archive/ (committed) when crossing a milestone.
 7. [DECIDE] eval-judge model and temperature.
 Recommendation: claude-sonnet-4-6 at temperature 0.1 for the judge. The Critic uses Haiku, but the judge needs higher fidelity since its output drives
 prompt-iteration decisions. Add EVAL_JUDGE_MODEL, EVAL_JUDGE_TEMPERATURE, EVAL_JUDGE_MAX_TOKENS to config.py.

 ---
 V1 build steps (deterministic — zero new LLM cost)

 Step 1: Directory scaffolding

 Create:
 - evals/__init__.py — empty
 - evals/test_sets/golden.json — empty list []
 - evals/test_sets/regression.json — empty list []
 - evals/snapshots/.gitkeep — placeholder
 - evals/logs/.gitkeep — placeholder for JSONL observability logs
 - evals/scorers/__init__.py — empty

 Modify:
 - .gitignore — add evals/snapshots/ and evals/logs/ (but commit .gitkeep files)

 Why: locks in the directory shape so subsequent steps have a home. No logic yet.
 Depends on: nothing.

 ---
 Step 2: Failure mode taxonomy

 Create:
 - evals/failure_modes.py — FailureMode enum (or dict[str, str]) with name + description per mode. Top-of-file comment marking as v0 hypothesis.

 Final list (pending Decision 1) — recommended:
 - cited_irrelevant_patch — cites a patch that doesn't address the complaint
 - overclaimed_fix — claims fix when patch only mitigates
 - unsourced_claim — claim with no supporting chunk at all (replaces hallucinated_claim)
 - defensive_tone — defensive response to a legitimate complaint
 - tone_too_apologetic — over-apologizes on a mild complaint
 - wrong_action_severity — proposed_action doesn't match complaint severity (covers both directions: no_action for crash, escalate for typo)
 - ignored_main_complaint — response addresses a side issue, skips primary
 - investigate_for_subjective — investigate action for a pure design/opinion complaint

 retrieval_hint_fallback moves to the JSONL observability log only (Step 6).

 Why: tagging vocabulary that every downstream scorer and snapshot field will reference. Defining it second (after directories) means everything else can import
  it.
 Depends on: Step 1.

 ---
 Step 3: Golden test set (manual annotation)

 Modify:
 - evals/test_sets/golden.json — populate ~50 entries, balanced across the 10 categories (~5 each). 5 entries marked quick: true.

 Process (manual, runs once with periodic additions later):
 1. Query classified reviews for both 2246340 and 2694490, group by primary_category, sample candidates. Note: load_classified_reviews (storage.py:351) filters
 out the other category — if you want any other cases in the golden set (e.g. to test the deterministic gate's skip path), use load_classifications joined with
 reviews manually, or adjust the loader. The gating accuracy eval (Step 13) hits the same constraint; consider whether other-category cases live in golden.json
 or only in gating_cases.json.
   - Tip: also pull SELECT review_id, primary_category FROM classifications WHERE app_id IN (...) AND needs_review = 1 (Finding 6). These are pre-flagged
 low-confidence classifications and make natural seeds for the regression set.
 2. For each candidate, you (the user) read the review, run pipeline.retrieve.retrieve() against it to see what chunks come back, read those chunks, and decide
 which should appear in must_include_chunk_ids. This is the slow part — annotating retrieval ground truth means actually reading patch note chunks, not just
 labeling the action. Realistic budget: ~1–2 minutes per case for action only, ~3–5 minutes per case once you include must_include_chunk_ids. Roughly 1–2 hours
 of focused work for 50 cases.
 3. Annotate expected.proposed_action (cheap) for every case; annotate must_include_chunk_ids only for cases where retrieval is expected to fire (i.e. category
 in RETRIEVAL_CATEGORIES).
 4. Pick 5 representative cases for the quick subset (per Decision 4).
 5. Empty expected.failure_modes_acceptable initially — populate after first runs reveal which modes are acceptable per case.

 Sequencing — to avoid the annotation bottleneck blocking everything else:
 - Annotate the 5 quick cases first, in one sitting. This unblocks Steps 4–8 immediately. The runner and scorers can be built and tested against just those 5.
 - Then annotate the remaining ~45 cases incrementally, in batches of 5–10, between other build steps. The runner already works; you're just feeding it more
 inputs.
 - Do not try to annotate all 50 before building the runner. You'll burn out on annotation with nothing to run them through, and the first runs always reveal
 annotation issues you'd want to fix (e.g., chunks you missed, actions you'd reconsider).

 evals/test_sets/regression.json stays empty ([]); it grows organically as bugs are found.

 Why: this is the foundation. Every V1 scorer needs at least some entries here to produce meaningful output. Front-loading the 5 quick cases is the minimum
 viable annotation set.
 Depends on: Step 1.

 ---
 Step 4: Eval runner skeleton

 Create:
 - evals/run_evals.py — argparse CLI with:
   - --quick flag → load only entries with quick: true
   - --app-id filter (optional)
   - --category filter (optional)
   - default → load golden.json + regression.json, run all
   - For each case: build initial AgentState (mirroring test_agent.py:115), invoke graph with auto-approve at the human gate (mirror test_agent.py:160 but
 always pass human_decision="approved" without prompting), collect result.
   - After all runs: hand off to scorers (Step 5) and reporter (Step 7).

 Reuse:
 - agent.graph.build_graph() — graph compilation
 - agent.state.AgentState — initial state shape (copy from test_agent.py:115–140)
 - pipeline.storage.get_connection, load_classified_reviews — only used to validate review_id exists; the golden JSON is the source of truth for review text
 - config.AGENT_MAX_ITERATIONS — already respected by the graph

 Why: harness skeleton lets you run cases end-to-end and collect raw results before any scorer exists.
 Depends on: Steps 1, 3.

 ---
 Step 5: Deterministic scorers

 Create:
 - evals/scorers/deterministic.py — pure functions, no LLM calls. Each takes (test_case, run_result) and returns a scored dict.

 Functions (one per scorer):
 def recall_at_k(case, result) -> dict:           # |must_include ∩ relevant_ids| / |must_include|
 def action_correctness(case, result) -> dict:    # exact match on proposed_action; also tracks confusion-matrix cell
 def citation_audit(case, result) -> dict:        # source_ids_cited ⊆ relevant_ids (bool + offending ids)
 def evidence_utilization(case, result) -> dict:  # len(source_ids_cited) / len(relevant_ids)
 def token_cost(case, result) -> dict:            # per-node breakdown from result["token_usage"]
 def critic_health(all_results) -> dict:          # batch-level: approval rate by iteration, stratified by reason_type
 def grounding_band_compliance(case, result) -> dict:  # confidence-band rule check (Finding 7)

 critic_health is batch-level — takes the full list of results, not one case. All others are per-case.

 grounding_band_compliance (per Finding 7): reads result["evidence_package"]["confidence"] and checks against result["source_ids_cited"] per the rules baked
 into skills/draft-response/SKILL.md:
 - confidence < 0.4 → expect source_ids_cited == [] (no claim, no cite). Violations = "low_confidence_with_citation".
 - 0.4 ≤ confidence < 0.7 → citations allowed but Responder is told not to claim a complete fix (this part is judge-territory, not deterministic).
 - confidence ≥ 0.7 → citations expected if any relevant_ids exist. Violations of source_ids_cited == [] and len(relevant_ids) > 0 =
 "high_confidence_no_citation" (responder ignored available evidence).
 - Returns {compliant: bool, violation: str | None, confidence: float}.

 Per Finding 3: any scorer that reads audit_log_iterations.source_ids_cited from the DB must json.loads() the column — it's stored as a JSON string. Scorers
 that read result["source_ids_cited"] from the in-memory result dict get the list directly.

 Reuse:
 - result["token_usage"] — already populated by every node
 - result["evidence_package"]["relevant_ids"] and ["source_ids"] — from Investigator
 - result["source_ids_cited"] — from Responder
 - result["proposed_action"] — from Responder
 - audit_log_iterations table (pipeline/storage.py:122–140) — for critic_health, query rows by run_id to get approval pattern per iteration

 Why: turns raw results into numbers. All free — no annotation beyond what Step 3 produced.
 Depends on: Steps 2 (failure modes), 3, 4.

 ---
 Step 6: JSONL observability log (always-on, per Decision 3)

 Modify:
 - agent/nodes/investigator.py — inside investigator_node, after each Self-RAG iteration, append a JSON line to evals/logs/investigator_<run_id>.jsonl. Fields
 per line:
 {
   "run_id": "...",
   "iteration_attempt": 0,
   "query": "...",
   "retrieval_hint_used": true | false,
   "fell_back_to_default": true | false,
   "retrieved_ids": [...],
   "accepted_ids": [...],
   "rejected_ids": [...],
   "is_sufficient": true | false,
   "reformulated_query": "..." | null
 }
 - Wrap the file write in try/except — a logging failure must not crash the node.
 - Hook points in investigator.py:
   - After the _call_investigator_llm return inside the while loop (lines ~218–243).
   - Capture retrieval_hint (line 201), the fallback decision (lines 205–210), and the assessment dict (lines 218–243).

 Why: gives qualitative debugging signal for free, captures the silent Critic↔Investigator handoff failure (retrieval_hint_fallback), and feeds the deferred
 Self-RAG eval if we ever build it. Removed from the failure mode taxonomy because a log file is the right place for it.
 Depends on: nothing structurally, but only useful once Step 4 runs cases through the agent.
 Note: this is the only V1 step that touches a production node. Behavior is otherwise unchanged.
 File-shape rationale: one file per run_id (not per eval batch) is intentional. The dominant debug operation is "show me the investigator trace for one failing
 case" — cat investigator_<run_id>.jsonl answers it directly, and the same shape works for prod invocations where no batch concept exists. If accumulation ever
 becomes painful, the fix is a retention policy (e.g. find evals/logs/ -mtime +30 -delete), not consolidation into per-batch files.

 ---
 Step 7: Stratified reporter

 Create:
 - evals/reporter.py — takes a list of scored results and prints a stratified report. Functions:
   - report_overall(scored) — aggregate metrics across all cases
   - report_by_category(scored) — same metrics broken down by cluster_summary.category
   - report_failure_modes(scored) — count of each tag across the run (tags come from manual review for V1; from the LLM judge in V1.5)
   - report_critic_health(all_results) — approval rate by iteration, stratified by reason_type
   - print_confusion_matrix(scored, key="proposed_action") — for action correctness
   - All output to stdout. No HTML/dashboard — keep it terminal-friendly.

 Why: turns numbers into something a human can scan in 30 seconds. Stratification is what catches "overall 3.8 hides technical_issues=4.5 and
 monetization_value=2.1."
 Depends on: Step 5.

 ---
 Step 8: Versioned snapshots

 Create:
 - evals/snapshot.py — functions to write/read snapshot JSONs. One snapshot per run:
 {
   "git_sha": "...",
   "timestamp": "ISO8601",
   "test_set_versions": {"golden": "<sha>", "regression": "<sha>"},
   "config": {"AGENT_MAX_ITERATIONS": 3, "INVESTIGATOR_MODEL": "...", ...},
   "per_case": [{"id": "golden-001", "scores": {...}, "tags": [...], "result": {...}}],
   "aggregates": {...}
 }
 - File path: evals/snapshots/<git_sha>_<timestamp>.json
 - Use subprocess.check_output(["git", "rev-parse", "HEAD"]) for the SHA; fall back to "unstaged" if git is dirty.

 Modify:
 - evals/run_evals.py — at end of run, call snapshot.write(...) and print the snapshot path. Also print a one-line diff vs the previous snapshot if one exists.

 Why: comparable across code changes. Lets you say "this prompt change moved recall@5 from 0.62 to 0.71."
 Depends on: Steps 4, 5, 7.

 ---
 V1 verification gate

 Before moving to V1.5, confirm:
 1. python evals/run_evals.py --quick runs the 5 quick cases without errors.
 2. python evals/run_evals.py runs the full golden set, prints stratified report, writes snapshot.
 3. JSONL logs land in evals/logs/ with one line per Self-RAG iteration.
 4. Re-running --quick produces a snapshot whose deterministic numbers (recall, action_correctness, citation_audit) are identical (within token-cost noise).
 5. You've manually scanned a few JSONL logs and feel they capture what you need.

 If anything in V1 is failing or unclear, fix before V1.5. Do not build the LLM judge on a shaky deterministic foundation.

 ---
 V1.5 build steps (LLM judge layer)

 Step 9: Judge consistency check (one-time gate, no permanent code)

 Create:
 - evals/judge_consistency_check.py — script that:
   a. Picks 5 cases from the golden set
   b. For each, runs the (not-yet-built) eval-judge skill 3 times via direct API call
   c. Reports per-dimension std dev and per-case score range
   d. Prints a verdict: "judge stable" (std dev ≤ 0.5 on all dimensions) or "judge unstable, fix prompt before proceeding"

 This script is one-shot. It depends on Step 10 existing in draft form, so technically Step 10 happens first — but the script is the gate, not part of the
 recurring eval suite. Run it, fix the prompt if needed, re-run, then delete or archive.

 Why: the judge is an unvalidated instrument until measured. Skipping this means every downstream score is noise.
 Depends on: Step 10 (draft of eval-judge skill).
 Decision point: after running, do you trust the judge enough to continue? If not, iterate on the SKILL.md prompt.

 ---
 Step 10: eval-judge skill

 IMPORTANT framing change from Finding 5: the Critic (skills/critique-draft/SKILL.md) already runs a 6-point checklist covering hallucination, overconfidence,
 tone, completeness, action — which is exactly the same surface as the originally proposed eval-judge dimensions (grounding accuracy, tone match, actionability,
  completeness). Building eval-judge as originally scoped means re-implementing the Critic and calling it an eval. To make the judge actually add information:

 1. Add a 5th dimension the Critic does not measure: perceived_player_satisfaction (1–5). Would a player who wrote this complaint feel heard by this response?
 This is a holistic player-experience score the Critic's checklist explicitly excludes (the Critic only checks evidence-grounding correctness, not whether the
 response lands with the player). This is the dimension that gives the judge unique value.
 2. Use Critic↔judge agreement as a calibration signal, not just the raw scores. When the Critic approved a draft but the judge scores it ≤ 2 on any dimension,
 that's a Critic-prompt-tuning signal. Surface this in the reporter (Step 11) as a "Critic-judge disagreement" count.

 Create:
 - skills/eval-judge/SKILL.md — agent skill (loaded by utils.load_skill() like every other skill). Frontmatter + system prompt. Mirror the structure of
 skills/critique-draft/SKILL.md (XML-tagged sections: <identity>, <task>, <evaluation_dimensions>, <output_format>, <examples>). Inputs (passed in user
 message): review text, evidence package, drafted response, proposed action. Output JSON:
 {
   "grounding_accuracy": 1-5,
   "tone_match": 1-5,
   "actionability": 1-5,
   "completeness": 1-5,
   "perceived_player_satisfaction": 1-5,
   "rationale": {"grounding_accuracy": "...", ...},
   "failure_mode_tags": ["cited_irrelevant_patch", ...]
 }
 - Include 4–6 few-shot examples (mirror the format used by critique-draft examples). Reference the failure mode taxonomy by name in the prompt so tags are
 drawn from a fixed vocabulary.

 Modify:
 - config.py — add EVAL_JUDGE_MODEL, EVAL_JUDGE_TEMPERATURE, EVAL_JUDGE_MAX_TOKENS (per Decision 7).

 Reuse:
 - utils.load_skill() — strips frontmatter, returns prompt
 - utils.parse_llm_json() — used by every other LLM-calling node
 - Pattern: see agent/nodes/critic.py for the canonical "load skill, call API, parse JSON, handle errors" structure

 Why: the prompt that drives all V1.5 metrics. Has to be drafted before consistency check, but treat the consistency check as the ship gate.
 Depends on: Step 2 (taxonomy names).

 ---
 Step 11: LLM scorer

 Create:
 - evals/scorers/llm_judge.py — wraps the eval-judge skill into a per-case scorer. Function judge_case(case, result) -> dict. Returns the four dimension scores
 + rationale + tags. Wraps API errors so a single judge failure doesn't crash the whole run.

 Modify:
 - evals/run_evals.py — add --judge flag. When set, runs the LLM judge after deterministic scorers.
 - evals/reporter.py — extend report_overall and report_by_category to include judge scores: average, pass rate (≥3.5), excellent rate (≥4.5) per dimension. Per
  Finding 5, also add a Critic↔judge disagreement report: count of cases where result["approved"] == True AND any judge dimension scored ≤ 2. This is the
 calibration signal.

 Why: turns the eval-judge skill into something the runner can call.
 Depends on: Steps 9 (consistency gate passed), 10.

 ---
 Step 12: Pairwise revision improvement eval

 Create:
 - evals/scorers/pairwise.py — function revision_improvement(case, result, conn) -> dict. Logic:
   a. Query audit_log_iterations by run_id for all iteration rows
   b. If iteration_count > 0: pull iteration 0 draft and final draft
   c. Show both to the judge in randomized order, ask which is better
   d. Run both orderings and average to control for position bias
   e. Return {winner: "iter0" | "final" | "tie", confidence: ...}
 - A second pairwise function, vs_approved_baseline(case, result, conn), for comparing against audit_log approved drafts (V1.5 win-rate eval).

 Modify:
 - evals/run_evals.py — call pairwise scorers when --judge is set
 - evals/reporter.py — add win/loss/tie aggregation per category

 Reuse:
 - audit_log_iterations rows queried by run_id (the linkage we added in the prior plan). Remember to json.loads() the source_ids_cited column (Finding 3).
 - Same eval-judge skill from Step 10, but with a different prompt template (pairwise mode)

 Limitation per Finding 4: audit_log_iterations does not store evidence_package. The pairwise judge will see only the final run's evidence (from
 result["evidence_package"]), used as proxy for both the iter-0 draft and the final draft. This is fine for drafting-type rejections (where evidence is
 unchanged across iterations) but slightly impure for evidence-type rejections (where the iter-0 draft was actually evaluated against a different, smaller
 evidence set). Acceptable limitation — document it in the scorer's docstring rather than reworking the schema. If this turns out to matter, the fix is to add
 an evidence_package_json column to audit_log_iterations later.

 Decision in this step: does the eval-judge skill have one prompt with a "pairwise mode" branch, or two separate skills (eval-judge, eval-pairwise-judge)?
 Recommendation: two separate skill files. Cleaner prompts, easier to iterate on independently.

 Why: tests whether the revision loop earns its cost, and whether the agent has caught up to the human-in-the-loop baseline.
 Depends on: Steps 10, 11.

 ---
 Step 13: Retrieval gating accuracy

 Create:
 - evals/test_sets/gating_cases.json — ~20 manually annotated cases: 10 where the deterministic gate skipped retrieval (category = other, since
 RETRIEVAL_CATEGORIES in config.py:95–105 is "all categories except other"), 10 where it retrieved. Each annotated with would_retrieval_have_helped: true |
 false.
 - evals/scorers/gating_accuracy.py — runs the agent on each case, checks gate decision against annotation, reports confusion matrix.

 Per Finding 1: the standard load_classified_reviews loader filters out the other category. To populate the 10 skipped cases, the gating eval needs a different
 loader — either load_classifications joined with reviews manually, or a small dedicated query in this scorer file. Worth a one-line helper in
 pipeline/storage.py if it's reused: load_classified_reviews_with_other(conn, app_id).

 Why: catches miscategorized gate logic (e.g., a "story_presentation" review that's actually about a cutscene crash). Cheap binary annotation, real failure
 mode.
 Depends on: Step 4 (runner exists).
 Note: separate test set from golden.json — these are gate-specific edge cases, not balanced sample.

 ---
 Step 14: OOD canary set

 Create:
 - evals/test_sets/canary.json — 3-5 hand-picked weird inputs:
   - off-topic review (food review for a game)
   - non-English review
   - 5-word review
   - prompt injection attempt ("ignore previous instructions...")
   - encoded/garbage text

 Modify:
 - evals/run_evals.py — --canary flag runs this set, reports stop_reasons and any crashes. Not included in main metrics — pass/fail is "did the agent terminate
 gracefully?"

 Why: cheap robustness insurance. Catches regressions in error handling without polluting main quality scores.
 Depends on: Step 4.
 Decision in this step: you supply the canary cases manually (recommended) vs. generate them.

 ---
 V1.5 verification gate

 1. python evals/judge_consistency_check.py reports stable scores (or you've iterated until it does).
 2. python evals/run_evals.py --quick --judge runs without errors and produces judge scores.
 3. python evals/run_evals.py --judge produces a snapshot with both deterministic and judge metrics.
 4. python evals/run_evals.py --canary reports graceful termination on all canary cases.
 5. Pairwise eval picks a winner (or ties) on multi-iteration runs.

 ---
 Critical files map

 ┌─────────────────────────────────────┬──────────────────┬──────────────────┬─────────────────────────────────────────────────────────────┐
 │                File                 │ Created/Modified │       Step       │                           Purpose                           │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/__init__.py                   │ C                │ 1                │ Package marker                                              │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/test_sets/golden.json         │ C                │ 1, 3             │ Annotated test cases                                        │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/test_sets/regression.json     │ C                │ 1                │ Past failures (grows over time)                             │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/test_sets/gating_cases.json   │ C                │ 13               │ Gate-specific edge cases                                    │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/test_sets/canary.json         │ C                │ 14               │ OOD adversarial inputs                                      │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/snapshots/.gitkeep            │ C                │ 1                │ Placeholder; dir is gitignored                              │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/logs/.gitkeep                 │ C                │ 1                │ Placeholder; dir is gitignored                              │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/failure_modes.py              │ C                │ 2                │ Taxonomy enum/dict                                          │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/run_evals.py                  │ C                │ 4, 8, 11, 12, 14 │ Main runner                                                 │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/scorers/__init__.py           │ C                │ 1                │ Package marker                                              │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/scorers/deterministic.py      │ C                │ 5                │ Free metrics                                                │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/scorers/llm_judge.py          │ C                │ 11               │ Wraps eval-judge skill                                      │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/scorers/pairwise.py           │ C                │ 12               │ Revision + baseline pairwise                                │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/scorers/gating_accuracy.py    │ C                │ 13               │ Gate confusion matrix                                       │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/reporter.py                   │ C                │ 7, 11, 12        │ Stratified terminal output                                  │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/snapshot.py                   │ C                │ 8                │ Snapshot read/write                                         │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ evals/judge_consistency_check.py    │ C                │ 9                │ One-time gate script                                        │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ agent/nodes/investigator.py         │ M                │ 6                │ Add JSONL log emission                                      │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ skills/eval-judge/SKILL.md          │ C                │ 10               │ Judge prompt                                                │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ skills/eval-pairwise-judge/SKILL.md │ C                │ 12               │ Pairwise judge prompt (if Decision in Step 12 → two skills) │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ config.py                           │ M                │ 10               │ Add EVAL_JUDGE_* settings                                   │
 ├─────────────────────────────────────┼──────────────────┼──────────────────┼─────────────────────────────────────────────────────────────┤
 │ .gitignore                          │ M                │ 1                │ Ignore evals/snapshots/, evals/logs/                        │
 └─────────────────────────────────────┴──────────────────┴──────────────────┴─────────────────────────────────────────────────────────────┘

 ---
 Verification (end-to-end)

 After V1:
 source .venv/bin/activate
 python evals/run_evals.py --quick
 python evals/run_evals.py
 ls evals/snapshots/                    # at least one snapshot
 ls evals/logs/                         # at least one JSONL per run_id
 sqlite3 reviews.db "SELECT COUNT(*) FROM audit_log WHERE run_id IS NOT NULL"  # should grow

 After V1.5:
 python evals/judge_consistency_check.py    # report std dev ≤ 0.5 per dimension
 python evals/run_evals.py --quick --judge
 python evals/run_evals.py --judge
 python evals/run_evals.py --canary

 Ad-hoc inspection:
 cat evals/logs/investigator_<run_id>.jsonl | jq .   # walk one run's Self-RAG decisions
 jq .aggregates evals/snapshots/<sha>_<timestamp>.json

 ---
 Out of scope (deferred or dropped)

 - Self-RAG annotated evals — JSONL log gives qualitative signal for free; promote only if review shows systematic failure.
 - Latency tracking — single-developer batch workflow doesn't need it.
 - Confidence calibration — confidence field is not consumed by any routing decision; calibrating dead telemetry is wasted effort.
 - Feedback memory ablation — one-time experiment, not a recurring eval. Run separately if/when feedback memory's value comes into question.
 - Judge calibration against your own ratings — also one-time. Plan for 5 cases/week of human spot-checks once V1.5 is running, but not part of the build.

 ---
 Next action after plan approval

 Start with Step 1 (directory scaffolding) and Step 2 (failure mode taxonomy). They're tiny, unblock everything else, and let you start annotating the golden
 set in parallel with the runner build.