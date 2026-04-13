"""
agent/nodes/investigator.py — Gathers evidence for a review response.

Flow:
1. Deterministic category gate: skip retrieval for categories unlikely to have patch note evidence.
2. Load cluster notes for context (always, when app_id + category present).
3. Tool-use loop: the Investigator LLM drives retrieval via the `retrieve_patches`
   tool, rewriting the complaint into keyword queries and optionally refining
   after seeing results. Hard cap at INVESTIGATOR_MAX_TOOL_CALLS.
4. Notes-sufficient skip: the LLM may return `skip_response: true` WITHOUT
   calling the tool, in which case the node routes through the same early-return
   shape the category gate uses (stop_reason="no_response_needed"). Mixing
   tool calls with skip_response=true is a hard contract violation.
"""

import json
import logging
from pathlib import Path

import anthropic

from agent.state import AgentState
from agent.models import EvidencePackage
from pipeline.retrieve import retrieve
from pipeline.classify import classify_tone
from pipeline.storage import (
    get_connection, load_cluster_notes,
    save_cluster_note, find_recent_similar_note,
)
from utils import load_skill, parse_llm_json
from config import (
    CLAUDE_API_KEY,
    INVESTIGATOR_MODEL,
    INVESTIGATOR_TEMPERATURE,
    INVESTIGATOR_MAX_TOKENS,
    INVESTIGATOR_MAX_TOOL_CALLS,
    PARENT_CONTEXT_INVESTIGATOR,
    RETRIEVAL_CATEGORIES,
    CLUSTER_NOTE_AUTO_MIN_SOURCES,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, max_retries=5)
SYSTEM_PROMPT = load_skill("investigate-evidence")


RETRIEVE_PATCHES_TOOL = {
    "name": "retrieve_patches",
    "description": (
        "Search the game's patch notes for chunks relevant to a query. Runs the "
        "full hybrid retrieval pipeline (vector + BM25 → RRF fusion → Gemma "
        "rerank). Use search-style keywords (3-10 tokens), not full sentences or raw "
        "review text. Rewrite the complaint into keywords before calling. May be "
        "called up to INVESTIGATOR_MAX_TOOL_CALLS times per investigation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword-style search query, 3-10 tokens. Include version "
                    "numbers, feature names, or error terms when relevant."
                ),
            },
            "call_role": {
                "type": "string",
                "enum": ["primary", "secondary"],
                "description": (
                    "Tag this call as 'primary' (investigating the main complaint) "
                    "or 'secondary' (probing a secondary aspect from a multi-part review). "
                    "Defaults to 'primary' if omitted."
                ),
            },
        },
        "required": ["query"],
    },
}


_INVESTIGATOR_LOG_DIR = Path(__file__).resolve().parents[2] / "evals" / "logs"


def _emit_self_rag_log(
    run_id: str,
    iteration_attempt: int,
    query: str,
    retrieval_hint_used: bool,
    fell_back_to_default: bool,
    retrieved_ids: list[str],
    accepted_ids: list[str],
    is_sufficient: bool,
    reformulated_query: str | None,
    notes_sufficient_skip: bool = False,
    call_role: str = "primary_initial",
    aspect_phrase_used: str | None = None,
    role_coerced: bool = False,
) -> None:
    """
    Append one JSONL line per Investigator tool call (or one line total on a
    notes-sufficient skip) to evals/logs/investigator_<run_id>.jsonl.

    Always-on observability hook. Wrapped so any failure (missing dir, disk full,
    permission error) is logged-and-swallowed — a logging fault must NEVER take
    down the node.

    On a notes-sufficient skip, emit a single line with iteration_attempt=0,
    empty retrieved_ids/accepted_ids, and notes_sufficient_skip=True.

    call_role: "primary_initial" | "primary_reformulation" | "secondary_probe"
    aspect_phrase_used: the secondary aspect phrase (only when call_role is secondary_probe)
    role_coerced: True if the LLM-reported call_role was overridden by a safeguard
    """
    if not run_id:
        return
    try:
        _INVESTIGATOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
        rejected = [c for c in retrieved_ids if c not in set(accepted_ids)]
        line = {
            "run_id": run_id,
            "iteration_attempt": iteration_attempt,
            "query": query,
            "retrieval_hint_used": retrieval_hint_used,
            "fell_back_to_default": fell_back_to_default,
            "retrieved_ids": retrieved_ids,
            "accepted_ids": accepted_ids,
            "rejected_ids": rejected,
            "is_sufficient": is_sufficient,
            "reformulated_query": reformulated_query or None,
            "notes_sufficient_skip": notes_sufficient_skip,
            "call_role": call_role,
            "aspect_phrase_used": aspect_phrase_used,
            "role_coerced": role_coerced,
        }
        path = _INVESTIGATOR_LOG_DIR / f"investigator_{run_id}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception as e:
        logger.warning(f"Investigator: JSONL log write failed (non-fatal): {e}")


def _should_retrieve(category: str) -> bool:
    """Deterministic gate: only retrieve for categories likely to have patch note evidence."""
    return category in RETRIEVAL_CATEGORIES


def _format_evidence_for_llm(results: list[dict]) -> str:
    """Format retrieved chunks into the text format the skill prompt expects."""
    if not results:
        return "(no chunks retrieved — retrieval returned empty results)"

    lines = []
    for i, r in enumerate(results):
        meta = r["metadata"]
        lines.append(
            f"[{i}] chunk_id={r['chunk_id']} "
            f"version={meta.get('patch_version', 'unknown')} "
            f"section={meta.get('section', 'unknown')}"
        )

        parent_context = r.get("parent_context", "")
        child_text = r.get("child_text", "")
        if PARENT_CONTEXT_INVESTIGATOR and parent_context and child_text:
            lines.append(f'    [matched] "{child_text}"')
            lines.append(f'    [context] "{parent_context}"')
        else:
            lines.append(f'    "{r["text"]}"')

    return "\n".join(lines)


def _build_user_message(
    review_text: str,
    category: str,
    cluster_notes_text: str,
    retrieval_hint: str,
    secondary_aspects: list[dict] | None = None,
) -> str:
    """
    Build the initial user message for the Investigator LLM.

    Cluster notes are injected verbatim (same as pre-tool-use behavior).
    The critic retrieval hint, if present, is included as a dedicated block
    telling the LLM to seed its first retrieve_patches call from it.
    Secondary aspects, when non-empty, are injected as a dedicated block
    for bounded multi-aspect investigation. Single-issue reviews never see
    this block.
    """
    parts = [
        f"<category>{category}</category>",
        "",
        f"<complaint>{review_text}</complaint>",
    ]

    if cluster_notes_text:
        parts += ["", cluster_notes_text]

    if retrieval_hint:
        parts += [
            "",
            "<critic_retrieval_hint>",
            f"The critic flagged missing evidence and suggested this search direction: {retrieval_hint}",
            "Start your first retrieve_patches call using this hint (or a close keyword variant) as the query.",
            "</critic_retrieval_hint>",
        ]

    if secondary_aspects:
        aspect_lines = []
        for asp in secondary_aspects:
            phrase = asp.get("phrase", "")
            cat = asp.get("category", "")
            if phrase:
                entry = f'- "{phrase}"'
                if cat:
                    entry += f" ({cat})"
                aspect_lines.append(entry)
        if aspect_lines:
            parts += [
                "",
                "<secondary_aspects>",
                "If your primary investigation has reached a natural stopping point "
                "and you have tool calls remaining, consider probing one of these "
                "secondary issues from the same review:",
                *aspect_lines,
                "Use keyword-style queries just like primary queries. Tag this call "
                'with call_role: "secondary". Include any relevant results in your '
                "assessment.",
                "</secondary_aspects>",
            ]

    return "\n".join(parts)


def _extract_final_text(content_blocks: list) -> str:
    """Extract the final text block from an assistant response's content list."""
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            return block.text.strip()  # type: ignore[union-attr]
    return ""


def _run_investigator_tool_loop(
    user_message: str,
    run_id: str,
    retrieval_hint_used: bool,
    fell_back_to_default: bool,
    app_id: str,
    secondary_aspects: list[dict] | None = None,
) -> tuple[dict, dict, dict[str, int], int]:
    """
    Drive the Investigator LLM with the retrieve_patches tool.

    Returns:
        assessment: parsed final JSON assessment from the LLM
        accumulated_chunks: dict[chunk_id, chunk_dict] union across all tool calls
        token_totals: {"input": N, "output": N}
        tool_calls_used: count of successful retrieve_patches invocations

    Raises:
        ValueError on contract violation (skip_response=true with tool_calls_used>0),
        API/parse errors (caller wraps these).
    """
    messages: list[dict] = [{"role": "user", "content": user_message}]
    accumulated_chunks: dict[str, dict] = {}
    token_totals = {"input": 0, "output": 0}
    tool_calls_used = 0
    tool_call_log_buffer: list[dict] = []

    while True:
        response = client.messages.create(
            model=INVESTIGATOR_MODEL,
            max_tokens=INVESTIGATOR_MAX_TOKENS,
            temperature=INVESTIGATOR_TEMPERATURE,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[RETRIEVE_PATCHES_TOOL],
            messages=messages,  # type: ignore[arg-type]
        )

        token_totals["input"] += response.usage.input_tokens
        token_totals["output"] += response.usage.output_tokens

        if response.stop_reason == "refusal":
            raise ValueError("Investigator model refused the request.")

        if response.stop_reason in ("end_turn", "max_tokens"):
            # Final turn — parse the text block as JSON. On max_tokens the
            # response may be truncated; we still try to parse what we have.
            if response.stop_reason == "max_tokens":
                logger.warning(
                    "Investigator response cut off (max_tokens). "
                    "Consider increasing INVESTIGATOR_MAX_TOKENS."
                )
            raw_text = _extract_final_text(response.content)
            if not raw_text:
                raise ValueError(
                    f"Investigator stop_reason={response.stop_reason} with no text content."
                )
            try:
                assessment = parse_llm_json(raw_text)
            except json.JSONDecodeError:
                # Tolerate preamble before/after JSON: locate the outermost
                # {...} object and retry. Handles Haiku occasionally emitting
                # chain-of-thought despite the "JSON only" instruction.
                start = raw_text.find("{")
                end = raw_text.rfind("}")
                if start == -1 or end == -1 or end <= start:
                    logger.error("Investigator final response has no JSON object.")
                    logger.error(f"Raw response was: {raw_text[:500]}")
                    raise
                try:
                    assessment = parse_llm_json(raw_text[start : end + 1])
                except json.JSONDecodeError as e:
                    logger.error(f"Investigator final response is not valid JSON: {e}")
                    logger.error(f"Raw response was: {raw_text[:500]}")
                    raise

            # Flush buffered per-call observability lines with post-hoc
            # accepted_ids (relevant_ids ∩ retrieved_ids) attribution.
            final_relevant = set(assessment.get("relevant_ids") or [])
            final_is_sufficient = bool(assessment.get("is_sufficient", False))
            for entry in tool_call_log_buffer:
                retrieved = entry["retrieved_ids"]
                _emit_self_rag_log(
                    run_id=run_id,
                    iteration_attempt=entry["iteration_attempt"],
                    query=entry["query"],
                    retrieval_hint_used=entry["retrieval_hint_used"],
                    fell_back_to_default=entry["fell_back_to_default"],
                    retrieved_ids=retrieved,
                    accepted_ids=[c for c in retrieved if c in final_relevant],
                    is_sufficient=final_is_sufficient,
                    reformulated_query=None,
                    notes_sufficient_skip=False,
                    call_role=entry.get("call_role", "primary_initial"),
                    aspect_phrase_used=entry.get("aspect_phrase_used"),
                    role_coerced=entry.get("role_coerced", False),
                )
            return assessment, accumulated_chunks, token_totals, tool_calls_used

        if response.stop_reason != "tool_use":
            raise ValueError(f"Investigator unexpected stop_reason: {response.stop_reason}")

        # Append assistant turn (including tool_use blocks) to the conversation.
        messages.append({"role": "assistant", "content": response.content})

        tool_result_blocks: list[dict] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if block.name != "retrieve_patches":  # type: ignore[union-attr]
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # type: ignore[union-attr]
                    "content": f"(unknown tool: {block.name})",  # type: ignore[union-attr]
                    "is_error": True,
                })
                continue

            if tool_calls_used >= INVESTIGATOR_MAX_TOOL_CALLS:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # type: ignore[union-attr]
                    "content": (
                        "(max retrieval calls reached — synthesize final "
                        "assessment from the chunks already retrieved and "
                        "return your final JSON now)"
                    ),
                    "is_error": True,
                })
                continue

            tool_input = block.input or {}  # type: ignore[union-attr]
            query = tool_input.get("query", "").strip()
            if not query:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # type: ignore[union-attr]
                    "content": "(empty query — provide a keyword-style search query of 3-10 tokens)",
                    "is_error": True,
                })
                continue

            results = retrieve(query, app_id, run_id=run_id)
            tool_calls_used += 1

            # Call-role provenance: derive call_role label with safeguards.
            has_secondary = bool(secondary_aspects)
            raw_role = tool_input.get("call_role", "primary")
            role_coerced = False
            aspect_phrase_used: str | None = None

            if raw_role == "secondary":
                if not has_secondary:
                    # Single-issue review — coerce secondary→primary
                    logger.warning(
                        f"Investigator: call_role='secondary' on a review with no "
                        f"secondary_aspects — coercing to 'primary' (run_id={run_id})"
                    )
                    raw_role = "primary"
                    role_coerced = True
                else:
                    # Log which aspect phrase was probed (best-effort: first aspect)
                    aspect_phrase_used = secondary_aspects[0].get("phrase", "") if secondary_aspects else None

            # Derive granular call_role label
            if raw_role == "secondary":
                call_role = "secondary_probe"
            elif tool_calls_used == 1:
                call_role = "primary_initial"
            else:
                call_role = "primary_reformulation"

            # Attach probe provenance to result dicts before accumulation
            probe_kind = "secondary" if call_role == "secondary_probe" else "primary"
            for r in results:
                r["call_role"] = call_role
                r["probe_kind"] = probe_kind
                r["probe_index"] = tool_calls_used
                r["aspect_label"] = aspect_phrase_used or ""
                accumulated_chunks.setdefault(r["chunk_id"], r)

            # Observability: buffer per-call metadata for post-hoc flush
            # once `relevant_ids` is known (enables per-call attribution).
            tool_call_log_buffer.append({
                "iteration_attempt": tool_calls_used - 1,
                "query": query,
                "retrieval_hint_used": (tool_calls_used == 1 and retrieval_hint_used),
                "fell_back_to_default": (tool_calls_used == 1 and fell_back_to_default),
                "retrieved_ids": [r["chunk_id"] for r in results],
                "call_role": call_role,
                "aspect_phrase_used": aspect_phrase_used,
                "role_coerced": role_coerced,
            })

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,  # type: ignore[union-attr]
                "content": _format_evidence_for_llm(results),
            })

        if not tool_result_blocks:
            # Haiku quirk: occasionally returns stop_reason="tool_use" with
            # zero tool_use blocks in content (only text). Tag the warning so
            # frequency can be greppe later — see CLAUDE.md "known gotchas".
            logger.warning(
                "[haiku-quirk:no-tool-blocks] Investigator stop_reason=tool_use "
                "with no tool_use blocks (run_id=%s, tool_calls_used=%d)",
                run_id,
                tool_calls_used,
            )
            raise ValueError("Investigator tool_use response had no tool_use blocks.")

        messages.append({"role": "user", "content": tool_result_blocks})


def investigator_node(state: AgentState) -> dict:
    """
    Gather evidence relevant to the review and its cluster.

    1. Deterministic category gate — skips retrieval entirely for non-retrievable categories.
    2. Loads cluster notes (always, when app_id + category present).
    3. Runs the tool-use loop: LLM drives retrieve_patches up to a hard cap.
    4. Handles two special exits: notes-sufficient skip (routes to no-response
       path) and contract-violation (logged and falls through to insufficient).
    """
    review_text = state.get("review_text", "")
    app_id = state.get("app_id", "")
    cluster = state.get("cluster_summary", {})
    category = cluster.get("category", "other")

    logger.info(f"Investigator: processing review ({len(review_text)} chars), category={category}")

    # Tone classification (before retrieval — independent of evidence).
    # Reuse the tone from a prior pass if already set (avoids a redundant
    # Haiku API call on evidence-type re-investigation cycles).
    existing_tone = state.get("review_tone", "")
    if existing_tone and existing_tone not in ("", "unknown"):
        review_tone = existing_tone
        logger.info(f"Investigator: reusing prior tone '{review_tone}'")
    elif category == "other":
        review_tone = "skipped"
        logger.info("Investigator: skipped tone classification for category 'other'")
    else:
        review_tone = classify_tone(review_text)
        if review_tone == "unknown":
            logger.warning("Investigator: tone classification failed (API error fallback)")
        else:
            logger.info(f"Investigator: classified tone as '{review_tone}'")

    total_tokens = {"input": 0, "output": 0}

    # Load cluster notes for additional context
    cluster_notes_text = ""
    notes_count = 0
    if app_id and category:
        try:
            conn = get_connection()
            try:
                notes = load_cluster_notes(conn, app_id, category)
                notes_count = len(notes)
            finally:
                conn.close()
            if notes:
                lines = ["<cluster_notes>"]
                for n in notes:
                    date = n.get("created_at", "")[:10]
                    lines.append(f"[{n['note_type']}] ({date}): {n['note_text']}")
                lines.append("</cluster_notes>")
                cluster_notes_text = "\n".join(lines)
        except Exception as e:
            logger.warning(f"Investigator: failed to load cluster notes: {e}")

    notes_log = f"investigator: loaded {notes_count} cluster notes"

    # Stage 1: Deterministic category gate
    if not _should_retrieve(category):
        logger.info(f"Investigator: skipping retrieval for category '{category}'")
        evidence = EvidencePackage(
            retrieval_decision="skipped",
            retrieval_reasoning=f"Category '{category}' is not in RETRIEVAL_CATEGORIES — no patch note evidence expected.",
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "drafted_response": "",
            "proposed_action": "",            # Empty — semantically distinct from "no_action".
            "source_ids_cited": [],
            "stop_reason": "no_response_needed",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, skipped retrieval — category '{category}'", notes_log],
        }

    # Prepare retrieval hint tracking (used for the JSONL log on tool call 0)
    retrieval_hint = state.get("retrieval_hint", "") or ""
    is_evidence_revision = state.get("reason_type", "") == "evidence"
    retrieval_hint_used = bool(retrieval_hint)
    fell_back_to_default = is_evidence_revision and not retrieval_hint
    run_id = state.get("run_id", "")

    secondary_aspects = cluster.get("secondary_aspects", []) or []

    user_message = _build_user_message(
        review_text=review_text,
        category=category,
        cluster_notes_text=cluster_notes_text,
        retrieval_hint=retrieval_hint,
        secondary_aspects=secondary_aspects,
    )

    # Stage 2: Tool-use loop
    try:
        assessment, accumulated_chunks, loop_tokens, tool_calls_used = _run_investigator_tool_loop(
            user_message=user_message,
            run_id=run_id,
            retrieval_hint_used=retrieval_hint_used,
            fell_back_to_default=fell_back_to_default,
            app_id=app_id,
            secondary_aspects=secondary_aspects,
        )
        total_tokens["input"] += loop_tokens["input"]
        total_tokens["output"] += loop_tokens["output"]
    except anthropic.APIError as e:
        logger.error(f"Investigator API call failed: {e}")
        evidence = EvidencePackage(
            retrieval_decision="insufficient",
            retrieval_reasoning=f"Investigator API error: {e}",
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, api_error — {e}", notes_log],
        }
    except Exception as e:
        logger.error(f"Investigator: tool loop failed: {e}")
        evidence = EvidencePackage(
            retrieval_decision="insufficient",
            retrieval_reasoning=f"Tool-use loop failed: {e}",
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, failed — {e}", notes_log],
        }

    skip_response = bool(assessment.get("skip_response", False))
    is_sufficient = bool(assessment.get("is_sufficient", True))
    relevant_ids = assessment.get("relevant_ids", []) or []

    # Stage 3a: Contract violation — skip_response=true with tool_calls_used>0.
    # Treat as hard failure during prototype to catch prompt drift loudly.
    if skip_response and tool_calls_used > 0:
        msg = (
            f"Investigator contract violation: skip_response=true with "
            f"tool_calls_used={tool_calls_used} (>0). Rejecting as insufficient."
        )
        logger.error(msg)
        evidence = EvidencePackage(
            retrieval_decision="insufficient",
            retrieval_reasoning=msg,
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [f"investigator: tone={review_tone}, contract_violation", notes_log],
        }

    # Stage 3b: Notes-sufficient no-response path.
    # Route through the same early-return shape the category gate uses so
    # Responder/Critic are never invoked — no empty evidence package leaks downstream.
    if skip_response and tool_calls_used == 0:
        logger.info("Investigator: notes-sufficient skip — no drafted response needed")
        _emit_self_rag_log(
            run_id=run_id,
            iteration_attempt=0,
            query="",
            retrieval_hint_used=False,
            fell_back_to_default=fell_back_to_default,
            retrieved_ids=[],
            accepted_ids=[],
            is_sufficient=True,
            reformulated_query=None,
            notes_sufficient_skip=True,
        )
        summary = assessment.get("summary", "") or "LLM judged from cluster notes that no drafted response is warranted."
        evidence = EvidencePackage(
            summary=summary,
            confidence=0.0,
            retrieval_decision="skipped_notes_sufficient",
            retrieval_reasoning="LLM judged from cluster notes that no drafted response is warranted.",
        )
        return {
            "review_tone": review_tone,
            "evidence_package": evidence.to_dict(),
            "retrieval_hint": "",
            "drafted_response": "",
            "proposed_action": "",
            "source_ids_cited": [],
            "stop_reason": "no_response_needed",
            "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
            "node_log": [
                f"investigator: tone={review_tone}, notes_sufficient_skip — no response drafted",
                notes_log,
            ],
        }

    # Stage 4: Normal path — build EvidencePackage from accumulated tool results.
    retrieval_decision = "retrieved" if relevant_ids and is_sufficient else "insufficient"
    sources = [accumulated_chunks[cid] for cid in relevant_ids if cid in accumulated_chunks]

    if is_sufficient:
        reasoning = (
            f"Retrieved {len(sources)} relevant chunks across {tool_calls_used} tool call(s) "
            f"with confidence {assessment.get('confidence', 0.0):.2f}"
        )
    else:
        reasoning = f"Evidence insufficient after {tool_calls_used} tool call(s)"

    evidence = EvidencePackage(
        summary=assessment.get("summary", ""),
        confidence=assessment.get("confidence", 0.0),
        relevant_ids=relevant_ids,
        source_ids=list(accumulated_chunks.keys()),
        sources=sources,
        known_unknowns=assessment.get("known_unknowns", []),
        retrieval_decision=retrieval_decision,
        retrieval_reasoning=reasoning,
        query_used="",  # no single query under tool-use; JSONL log has per-call queries
    )

    # Auto-save known_issue note when the investigator commits to sufficiency
    # with enough relevant sources. Deterministic gate — the LLM-self-reported
    # confidence float is not used here.
    if (
        retrieval_decision == "retrieved"
        and is_sufficient
        and len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES
    ):
        try:
            conn = get_connection()
            try:
                existing = find_recent_similar_note(
                    conn, app_id, category, "known_issue",
                    source_review_id=state.get("review_id", ""),
                )
                if not existing:
                    save_cluster_note(
                        conn, app_id=app_id, category=category,
                        note_type="known_issue",
                        note_text=evidence.summary,
                        source_review_id=state.get("review_id", ""),
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Investigator: failed to auto-save known_issue note: {e}")

    return {
        "review_tone": review_tone,
        "evidence_package": evidence.to_dict(),
        "retrieval_hint": "",  # consumed — do not reuse across cycles
        "token_usage": {**state.get("token_usage", {}), "investigator": total_tokens},
        "node_log": [
            f"investigator: tone={review_tone}, {retrieval_decision} — confidence={evidence.confidence:.2f}, "
            f"sources={len(sources)}, tool_calls={tool_calls_used}",
            notes_log,
        ],
    }
