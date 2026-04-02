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
- MemorySaver for development, SqliteSaver for persistence
- Every invoke needs a thread_id: `config={"configurable": {"thread_id": "..."}}`
- State is saved automatically after every node execution

## Graph flow
```
START → coordinator → investigator → responder → critic → coordinator → ...
                                                              ↓
                                                    (approved or max_iterations)
                                                              ↓
                                                             END
```