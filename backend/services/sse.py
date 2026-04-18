"""
SSE event helpers for the demo backend.

Both replay_player and live_runner produce events conforming to the canonical
schema declared in the project plan (5 event types: node_start, state_update,
human_gate_open, run_complete, error).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


EventType = str  # one of: node_start, state_update, human_gate_open, run_complete, error


def envelope(
    type: EventType,
    run_id: str,
    iteration: int,
    payload: dict[str, Any],
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a canonical event envelope."""
    return {
        "type": type,
        "run_id": run_id,
        "iteration": iteration,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def format_sse(event: dict[str, Any]) -> str:
    """Serialize an event envelope into a single SSE `data:` frame."""
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


SPEED_DELAYS_MS = {
    "instant": 0,
    "normal": 400,
    "slow": 800,
}
