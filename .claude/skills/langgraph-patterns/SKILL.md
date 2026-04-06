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
              ↓                             END    coordinator → responder
              ↓                                         ↑
              +————————————→ responder ————————————————→ critic
                    (revision — skip investigator)
```
- First pass (iteration 0): full pipeline through investigator
- Critic rejection: routes back to coordinator → responder (skips investigator)
- Human rejection: routes back to coordinator → responder (same revision loop)
- max_iterations is read from config.AGENT_MAX_ITERATIONS, not from state

## Side effects (not state updates)
Some nodes write to SQLite as fire-and-forget side effects — these are NOT state updates:
- **human_approval**: saves audit_log entry (always), cluster note of type `response_history` (on approve) or `human_feedback` (on reject with feedback). Uses dedup via `find_recent_similar_note`.
- **investigator**: saves cluster note of type `known_issue` when evidence confidence >= `CLUSTER_NOTE_AUTO_CONFIDENCE`. Uses dedup.
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