"""
Pydantic models for structured data passed between agent nodes.

These models are the handoff contract between Investigator, Responder,
and Critic. Each model validates LLM output or retrieval results at
trust boundaries, then gets serialized to a plain dict for AgentState.
"""

from typing import Literal
from pydantic import BaseModel, Field, field_validator


class EvidencePackage(BaseModel):
    """
    Structured evidence gathered by the Investigator node.

    Read by the Responder (to ground its draft) and the Critic
    (to verify claims against source_ids).
    """

    summary: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    relevant_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    known_unknowns: list[str] = Field(default_factory=list)
    retrieval_decision: Literal["retrieved", "skipped", "insufficient"] = Field(
        default="skipped"
    )
    retrieval_reasoning: str = Field(default="")
    query_used: str = Field(default="")

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        """Clamp confidence to [0.0, 1.0] instead of raising."""
        return max(0.0, min(1.0, v))

    def to_dict(self) -> dict:
        """Serialize to a plain dict for AgentState."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "EvidencePackage":
        """Reconstruct from an AgentState dict field."""
        return cls.model_validate(data)
