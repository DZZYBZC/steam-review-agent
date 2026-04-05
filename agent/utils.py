"""
agent/utils.py — Shared helpers for agent nodes.
"""

import json


def accumulate_tokens(existing: dict | None, new: dict[str, int]) -> dict[str, int]:
    """Add new token counts to existing ones, or return new if no prior entry."""
    if not existing:
        return new
    return {
        "input": existing.get("input", 0) + new.get("input", 0),
        "output": existing.get("output", 0) + new.get("output", 0),
    }


def format_evidence_sources(evidence: dict) -> str:
    """
    Format evidence package sources into XML-tagged text for LLM prompts.

    Used by both Responder and Critic to build their user messages.
    """
    sources = evidence.get("sources", [])
    if not sources:
        return "<evidence_sources>No relevant sources retrieved.</evidence_sources>"

    source_lines = []
    for i, s in enumerate(sources):
        meta = s.get("metadata", {})
        source_lines.append(
            f"[{i}] chunk_id={s.get('chunk_id', 'unknown')} "
            f"version={meta.get('patch_version', 'unknown')} "
            f"section={meta.get('section', 'unknown')}"
        )
        source_lines.append(f'    "{s.get("text", "")}"')
    return f"<evidence_sources>\n{chr(10).join(source_lines)}\n</evidence_sources>"
