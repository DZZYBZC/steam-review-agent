
import json

from config import PARENT_CONTEXT_RESPONDER
from utils import escape_xml


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
            f"version={escape_xml(meta.get('patch_version', 'unknown'))} "
            f"section={escape_xml(meta.get('section', 'unknown'))}"
        )

        parent_context = s.get("parent_context", "")
        child_text = s.get("child_text", "")
        if PARENT_CONTEXT_RESPONDER and parent_context and child_text:
            source_lines.append(f'    [matched] "{escape_xml(child_text)}"')
            source_lines.append(f'    [context] "{escape_xml(parent_context)}"')
        else:
            source_lines.append(f'    "{escape_xml(s.get("text", ""))}"')

    joined = "\n".join(source_lines)
    return f"<evidence_sources>\n{joined}\n</evidence_sources>"
