---
name: project-conventions
description: >
  Steam review agent file layout, import rules, naming conventions, config usage,
  and Anthropic API call pattern. Use when creating new Python files, moving code
  between pipeline/ and agent/, adding imports, writing config constants, or
  implementing new LLM calls with load_skill().
---

## File organization
- Pipeline code (ingest_reviews, ingest_patch_notes, clean, classify, cluster, retrieve, storage, stats) lives in `pipeline/`
- Agent code (state, graph, nodes) lives in `agent/`
- Shared agent helpers (accumulate_tokens, format_evidence_sources) live in `agent/utils.py`
- All configuration lives in `config.py` at the project root
- Shared utilities live in `utils.py` at the project root

## Skills distinction
- Agent skills live in `skills/{name}/SKILL.md` — loaded by `utils.load_skill()`, sent to Anthropic API
- Claude Code skills live in `.claude/skills/{name}/SKILL.md` — read by Claude Code for project context
- These are different systems — do not confuse them

## Import patterns
```python
# Config — always import specific values
from config import CLASSIFIER_MODEL, CLASSIFIER_TEMPERATURE

# Skills — always use the load_skill utility
from utils import load_skill
SYSTEM_PROMPT = load_skill("classify-review")

# Pipeline modules — use the pipeline package
from pipeline.storage import get_connection, save_reviews
from pipeline.classify import run_classification
from pipeline.retrieve import retrieve
from pipeline.ingest_reviews import fetch_all_reviews

# Agent modules
from agent.state import AgentState
from agent.graph import build_graph
from agent.nodes.coordinator import coordinator_node
from agent.utils import accumulate_tokens, format_evidence_sources
```

## Naming conventions
- Files: lowercase with underscores (`cluster.py`, `state.py`)
- Agent skill directories: lowercase with hyphens (`classify-review/`, `draft-response/`)
- Config constants: UPPER_SNAKE_CASE (`CLASSIFIER_MODEL`)
- Functions: lower_snake_case (`build_graph()`)
- Pydantic models: PascalCase (`ClassificationResult`)

## Error handling pattern
```python
try:
    response = client.messages.create(...)
except anthropic.APIError as e:
    logger.error(f"API call failed: {e}")
    raise
```

## LLM call pattern
Every LLM call follows this structure:
1. Load skill with `load_skill()`
2. Build the user message
3. Call `client.messages.create()` with model/temperature/max_tokens from config
4. Check `response.stop_reason` for "max_tokens" and "refusal"
5. Extract text from `response.content[0]`
6. Parse/validate the output (JSON parse + Pydantic for structured output)