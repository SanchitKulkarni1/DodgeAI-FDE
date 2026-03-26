"""
graph.py — LangGraph pipeline for the O2C context graph system.

Graph topology:
    ENTRY
      ↓
    memory_node         (resolves pronouns/references using conversation history)
      ↓
    classify_node       (intent check: domain guard + retrieval mode decision)
      ↓
    parallel_prep_node  (NEW: parallelizes planner + semantic using asyncio)
      ↓
    route_node          (conditional edge → sql | semantic | hybrid)
     ↙        ↓        ↘
   sql      semantic   hybrid
    ↓          ↓         ↓
  planner   semantic   hybrid_node  (reuse cached results from parallel_prep_node)
    ↓          ↓         ↓
  sql_gen      ↓         ↓
    ↓          ↓         ↓
  execute      ↓         ↓
       ↘       ↓       ↙
           answer_node   (formats natural language response + highlight payloads)

Optimization:
  parallel_prep_node parallelizes build_query_plan + semantic_search calls
  using asyncio.gather(), eliminating sequential LLM API overhead.
  Both results are cached in state, so planner_node and semantic_node reuse
  them instead of recomputing.
"""

from langgraph.graph import StateGraph, END

from .state import GraphState
from .nodes import (
    memory_node,
    classify_node,
    parallel_prep_node,
    route_node,
    planner_node,
    sql_gen_node,
    execute_node,
    semantic_node,
    hybrid_node,
    answer_node,
)


# ---------------------------------------------------------------------------
# Routing logic — pure function, no LLM call, reads state set by classify_node
# ---------------------------------------------------------------------------

def _route(state: GraphState) -> str:
    """
    Called after classify_node. Returns the name of the next node.

    classify_node sets:
        state["intent"]         = "off_topic" | "domain"
        state["retrieval_mode"] = "sql" | "semantic" | "hybrid"

    Off-topic queries short-circuit directly to answer_node where a
    guardrail message is already written into final_answer.
    """
    if state.get("intent") == "off_topic":
        return "answer_node"

    mode = state.get("retrieval_mode", "sql")
    if mode == "semantic":
        return "semantic_node"
    if mode == "hybrid":
        return "hybrid_node"
    return "planner_node"          # default: sql path


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    g = StateGraph(GraphState)

    # ── nodes ──────────────────────────────────────────────────────────────
    g.add_node("memory_node",        memory_node)
    g.add_node("classify_node",      classify_node)
    g.add_node("parallel_prep_node", parallel_prep_node)  # NEW: parallelization
    g.add_node("route_node",         route_node)
    g.add_node("planner_node",       planner_node)
    g.add_node("sql_gen_node",       sql_gen_node)
    g.add_node("execute_node",       execute_node)
    g.add_node("semantic_node",      semantic_node)
    g.add_node("hybrid_node",        hybrid_node)
    g.add_node("answer_node",        answer_node)

    # ── entry ───────────────────────────────────────────────────────────────
    g.set_entry_point("memory_node")

    # ── linear edges ────────────────────────────────────────────────────────
    g.add_edge("memory_node",        "classify_node")
    g.add_edge("classify_node",      "parallel_prep_node")  # NEW: parallelize before routing
    g.add_edge("parallel_prep_node", "route_node")          # NEW

    # ── conditional fan-out from route_node ─────────────────────────────────
    g.add_conditional_edges(
        "route_node",
        _route,
        {
            "planner_node":  "planner_node",
            "semantic_node": "semantic_node",
            "hybrid_node":   "hybrid_node",
            "answer_node":   "answer_node",   # off-topic short-circuit
        },
    )

    # ── SQL path (3 sequential steps) ───────────────────────────────────────
    g.add_edge("planner_node", "sql_gen_node")
    g.add_edge("sql_gen_node", "execute_node")
    g.add_edge("execute_node", "answer_node")

    # ── semantic + hybrid paths converge at answer_node ─────────────────────
    g.add_edge("semantic_node", "answer_node")
    g.add_edge("hybrid_node",   "answer_node")

    # ── exit ────────────────────────────────────────────────────────────────
    g.add_edge("answer_node", END)

    return g.compile()