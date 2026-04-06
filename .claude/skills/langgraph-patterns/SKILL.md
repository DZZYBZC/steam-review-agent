---
name: langgraph-patterns
description: >
  LangGraph state schema, node update pattern, conditional-edge routing,
  coordinator behavior, checkpointing, and thread_id usage for this Steam
  review agent. Use when editing agent/state.py, agent/graph.py, agent/nodes/*,
  adding or changing nodes, wiring edges, or debugging checkpoint/resume behavior.
---

## State
- Defined in `agent/state.py` as a TypedDict (not Pydantic)
- Nodes receive the full state dict and return a dict of updates
- LangGraph merges returned updates into state after each node
- Append-only fields use `Annotated[list, operator.add]`

## Node pattern
Every node follows this structure:
```python
from agent.state import AgentState
from agent.utils import accumulate_tokens, format_evidence_sources

def my_node(state: AgentState) -> dict:
    # 1. Read only the fields this node needs
    review = state.get("review_text", "")
    
    # 2. Do work (LLM call, retrieval, plain Python)
    result = do_something(review)
    
    # 3. Return only the fields this node updates
    return {
        "my_output_field": result,
        "node_log": ["my_node: did something"],
    }
```

Important rules:
- Never mutate state directly — always return an update dict
- Use `state.get(key, default)` not `state[key]` for safety
- Each node writes to its own fields to avoid overwrites
- Always append to `node_log` (it uses operator.add)
- Use `accumulate_tokens()` from `agent/utils.py` for token tracking (adds to existing counts on revision cycles)
- Use `format_evidence_sources()` from `agent/utils.py` when building evidence XML for LLM prompts

## Routing
- Coordinator is plain Python — no LLM call
- Routing happens via conditional edges, not inside the node
- The node does bookkeeping, the edge function makes the decision
- Edge functions return a string that maps to a node name or END

```python
# In graph.py:
graph.add_conditional_edges(
    "coordinator",
    route_from_coordinator,    # function that returns a string
    {
        "investigate": "investigator",
        "done": END,
    },
)
```

## Graph construction
All graph building happens in `agent/graph.py`:
1. `StateGraph(AgentState)` — create with state type
2. `graph.add_node(name, function)` — register nodes
3. `graph.set_entry_point(name)` — set start node
4. `graph.add_edge(a, b)` — fixed transitions
5. `graph.add_conditional_edges(node, func, mapping)` — conditional transitions
6. `graph.compile(checkpointer=checkpointer)` — validate and build

## Checkpointing
- Configured in `config.py` (CHECKPOINT_BACKEND, CHECKPOINT_DB_PATH)
- Uses SqliteSaver with persistent storage at CHECKPOINT_DB_PATH
- Every invoke needs a thread_id: `config={"configurable": {"thread_id": "..."}}`
- State is saved automatically after every node execution

## Human-in-the-loop
- `interrupt_before=["human_approval"]` pauses the graph before the human gate
- Caller injects decision via `app.update_state(config, {"human_decision": "approved"})` then resumes with `app.invoke(None, config)`
- human_approval node is plain Python (no LLM) — reads human_decision/human_feedback, routes accordingly
- On rejection, coordinator clears human_decision/human_feedback before re-entering the loop

## Graph flow
```
START → coordinator → investigator → responder → critic ──┐
              ↓              (first pass)                  │
              ↓                                   critic approved?
              ↓                                    yes ↓      no ↓
              ↓                              [interrupt]    coordinator
              ↓                            human_approval      ↓
              ↓                          approved ↓  rejected ↓
              ↓                             END           (revision)
              ↓                                         ↓
              ↓                       reason_type == "evidence"?
              ↓                         yes ↓           no ↓
              +———→ investigator ←—————————+            responder
                    (uses retrieval_hint)                 ↓
                                                         critic
```
- First pass (iteration 0): full pipeline through investigator
- Critic rejection — **drafting type** (tone, hallucination, overconfidence, bad citation, action): coordinator → responder (re-draft only, evidence_package unchanged)
- Critic rejection — **evidence type** (insufficient or wrong coverage): coordinator → investigator → responder (re-investigate using Critic's `retrieval_hint` as query seed; investigator clears the hint on return)
- Human rejection: coordinator → responder (treated as drafting type)
- Terminal LLM/parse errors from responder or critic: routed directly to coordinator, which audit-logs and ends (`stop_reason="llm_error"` or `"parse_error"`, never reaches human_approval)
- max_iterations is read from config.AGENT_MAX_ITERATIONS, not from state

## Side effects (not state updates)
Some nodes write to SQLite as fire-and-forget side effects — these are NOT state updates:
- **human_approval**: saves audit_log entry (always), cluster note of type `response_history` (on approve) or `human_feedback` (on reject with feedback). Uses dedup via `find_recent_similar_note`.
- **coordinator**: saves audit_log entry on terminal exits (`max_iterations_reached`, `llm_error`, `parse_error`).
- **critic**: saves one row per pass to `audit_log_iterations` (draft, critique, approved, revision_reason, reason_type, retrieval_hint, tokens). This is the per-iteration observability surface for eval analysis.
- **investigator**: saves cluster note of type `known_issue` when `is_sufficient` and `len(relevant_ids) >= CLUSTER_NOTE_AUTO_MIN_SOURCES`. Deterministic gate — does not use LLM-self-reported confidence. Uses dedup.
- **responder**: reads feedback examples from audit_log (iteration 0 only). Reads cluster notes indirectly via investigator context.

All side-effect writes are wrapped in try/except — a DB error never crashes the node or changes the return value.

## Evidence chain of custody
```
Investigator: source_ids (all retrieved) → relevant_ids (LLM filtered)
Responder:    source_ids_cited (chunks referenced in draft) ⊆ relevant_ids
Critic:       verifies source_ids_cited ⊆ relevant_ids, rejects if violated
```
- `source_ids_cited` is a state field written by the Responder and read by the Critic
- The Critic receives both `relevant_ids` (from evidence_package) and `source_ids_cited` (from state)