"""
nodes.py — Node function stubs for the O2C LangGraph pipeline.

Each function here is a thin orchestration shell that:
  1. Reads from GraphState
  2. Calls the real implementation from its dedicated module
  3. Returns a partial GraphState dict (LangGraph merges it)

Implementation modules (to be created):
    llm/client.py          → shared google-genai client
    llm/memory.py          → resolve_query()
    llm/classifier.py      → classify_intent()
    llm/planner.py         → build_query_plan()
    llm/sql_generator.py   → generate_sql()
    llm/answer_writer.py   → write_answer()
    db/executor.py         → execute_sql()
    search/semantic.py     → semantic_search()
    search/hybrid.py       → hybrid_search()
    graph/highlighter.py   → extract_highlights()
"""

import logging
from typing import Any

from .state import GraphState

log = logging.getLogger(__name__)


# ===========================================================================
# 1. memory_node
#    Resolves pronouns and implicit references ("it", "that order", "the same
#    customer") using conversation history so downstream nodes always receive
#    a self-contained query.
# ===========================================================================

def memory_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["user_query"], state["conversation_history"]
    Output : state["resolved_query"]

    If there is no conversation history, resolved_query == user_query.
    Otherwise calls resolve_query() which asks the LLM to rewrite the query
    substituting any references back to explicit entity IDs or names.

    Implementation: llm/memory.py → resolve_query(user_query, history) -> str
    """
    from llm.memory import resolve_query

    user_query           = state["user_query"]
    conversation_history = state.get("conversation_history") or []

    resolved = resolve_query(
        user_query=user_query,
        conversation_history=conversation_history,
    )

    log.info("[memory_node] resolved_query=%r", resolved)
    return {"resolved_query": resolved}


# ===========================================================================
# 2. classify_node
#    Two responsibilities:
#      a) Domain guardrail  — is this query about the O2C dataset?
#      b) Retrieval routing — should we use SQL, semantic search, or hybrid?
#
#    Routing heuristics (implemented inside classify_intent):
#      • Exact entity IDs / numeric references   → sql
#      • Aggregation / "which", "how many", etc. → sql
#      • Vague / "find something similar to"     → semantic
#      • Exploratory + precise mix               → hybrid
# ===========================================================================

def classify_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["intent"], state["retrieval_mode"]

    intent values:
        "domain"    → query is about the O2C dataset, proceed
        "off_topic" → query is unrelated; answer_node will return guardrail msg

    retrieval_mode values:
        "sql"       → structured query against SQLite
        "semantic"  → vector similarity search over entity descriptions
        "hybrid"    → semantic search first, then SQL for exact figures

    Implementation: llm/classifier.py → classify_intent(query) -> (intent, mode)
    """
    from llm.classifier import classify_intent

    resolved_query = state.get("resolved_query") or state["user_query"]

    intent, retrieval_mode = classify_intent(resolved_query)

    log.info("[classify_node] intent=%r  retrieval_mode=%r", intent, retrieval_mode)
    return {
        "intent":         intent,
        "retrieval_mode": retrieval_mode,
    }


# ===========================================================================
# 3. route_node
#    Thin pass-through. The actual branching logic lives in graph.py (_route).
#    This node exists so the graph topology is explicit in the node list and
#    can carry any pre-routing state mutations if needed in future.
# ===========================================================================

def route_node(state: GraphState) -> dict[str, Any]:
    """
    No-op pass-through. Routing is handled by the conditional edge function
    _route() in graph.py which reads state["intent"] and state["retrieval_mode"].

    Nothing to implement here.
    """
    return {}


# ===========================================================================
# 4. planner_node  (SQL path, step 1 of 3)
#    Converts the natural language query into a structured query plan:
#    which tables are involved, which joins are needed, what the WHERE clause
#    should conceptually look like. This intermediate step improves SQL
#    generation quality by giving sql_gen_node a concrete plan to follow
#    rather than generating SQL from scratch.
# ===========================================================================

def planner_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["query_plan"]

    query_plan is a plain-text description such as:
        "Join sales_order_headers with outbound_delivery_items on
         sales_order = reference_sd_document. Filter where no matching
         delivery exists. Return sales_order, sold_to_party, total_net_amount."

    The plan is injected into the sql_gen_node prompt so the LLM doesn't have
    to infer the join strategy from scratch.

    Implementation: llm/planner.py → build_query_plan(query) -> str
    """
    from llm.planner import build_query_plan

    resolved_query = state.get("resolved_query") or state["user_query"]

    query_plan = build_query_plan(resolved_query)

    log.info("[planner_node] query_plan=%r", query_plan[:120])
    return {"query_plan": query_plan}


# ===========================================================================
# 5. sql_gen_node  (SQL path, step 2 of 3)
#    Generates executable SQLite SQL from the resolved query + query plan.
#    The prompt includes the full schema, all 11 join paths, and the plan.
#    Output is validated (syntax check) before returning.
# ===========================================================================

def sql_gen_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"], state["query_plan"]
    Output : state["sql_query"]  or  state["error"]

    If the LLM returns invalid SQL (detected by sqlite3 explain), error is set
    and sql_query is None — execute_node will handle this gracefully.

    Implementation: llm/sql_generator.py → generate_sql(query, plan) -> str
    """
    from llm.sql_generator import generate_sql

    resolved_query = state.get("resolved_query") or state["user_query"]
    query_plan     = state.get("query_plan") or ""

    try:
        sql = generate_sql(
            query=resolved_query,
            query_plan=query_plan,
        )
        log.info("[sql_gen_node] sql=%r", sql[:120])
        return {"sql_query": sql, "error": None}
    except Exception as e:
        log.error("[sql_gen_node] failed: %s", e)
        return {"sql_query": None, "error": str(e)}


# ===========================================================================
# 6. execute_node  (SQL path, step 3 of 3)
#    Executes the generated SQL against the SQLite database.
#    Row limit of 200 is enforced to keep the answer_node context manageable.
#    Also extracts entity IDs from results for graph highlighting.
# ===========================================================================

def execute_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["sql_query"], state["error"]
    Output : state["query_result"], state["highlight_nodes"], state["highlight_edges"]

    query_result  : list of dicts (column → value) for each row returned
    highlight_nodes / highlight_edges : derived by highlighter from the result
                    rows — any column whose name ends in known entity ID
                    patterns (sales_order, billing_document, delivery_document,
                    etc.) is extracted and mapped to a graph node type + id.

    If state["error"] is already set (sql_gen failed), skips execution and
    returns empty results so answer_node can report the error gracefully.

    Implementation:
        db_executor.py          → execute_sql(sql) -> list[dict]
        graph/highlighter.py    → extract_highlights(rows) -> (nodes, edges)
    """
    if state.get("error"):
        log.warning("[execute_node] skipping — upstream error: %s", state["error"])
        return {"query_result": [], "highlight_nodes": [], "highlight_edges": []}

    from db_executor import execute_sql
    from graph_highlighter import extract_highlights

    sql = state.get("sql_query") or ""

    try:
        rows = execute_sql(sql)
        highlight_nodes, highlight_edges = extract_highlights(rows)
        log.info(
            "[execute_node] %d rows returned, %d nodes highlighted",
            len(rows), len(highlight_nodes),
        )
        return {
            "query_result":    rows,
            "highlight_nodes": highlight_nodes,
            "highlight_edges": highlight_edges,
            "error":           None,
        }
    except Exception as e:
        log.error("[execute_node] SQL execution failed: %s", e)
        return {
            "query_result":    [],
            "highlight_nodes": [],
            "highlight_edges": [],
            "error":           str(e),
        }


# ===========================================================================
# 7. semantic_node  (semantic path)
#    Performs vector similarity search over a pre-built embedding index of
#    entity descriptions (product names, customer names, plant names, etc.).
#    Used when the query is vague or exploratory rather than exact.
# ===========================================================================

def semantic_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["semantic_results"], state["highlight_nodes"]

    semantic_results : list of dicts with keys:
        { "entity_type": str, "entity_id": str, "description": str, "score": float }

    The embedding index is built once at startup (search/index_builder.py) over
    a concatenation of searchable text fields per entity type:
        products          → product_description + product_old_id + product_group
        business_partners → business_partner_full_name + city_name + region
        plants            → plant_name + plant

    Implementation: search/semantic.py → semantic_search(query, top_k) -> list[dict]
    """
    from search.semantic import semantic_search
    from graph_highlighter import nodes_from_semantic_results

    resolved_query = state.get("resolved_query") or state["user_query"]

    results = semantic_search(query=resolved_query, top_k=10)
    highlight_nodes = nodes_from_semantic_results(results)

    log.info("[semantic_node] %d semantic results", len(results))
    return {
        "semantic_results": results,
        "highlight_nodes":  highlight_nodes,
    }


# ===========================================================================
# 8. hybrid_node  (hybrid path)
#    Runs semantic search first to identify candidate entities, then builds
#    and executes a SQL query scoped to those entity IDs for precise figures.
#    Best of both: fuzzy entity discovery + exact numerical answers.
# ===========================================================================

def hybrid_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["semantic_results"], state["query_result"],
             state["sql_query"], state["highlight_nodes"], state["highlight_edges"]

    Workflow inside hybrid_search():
        1. semantic_search(query) → candidate entity IDs
        2. build a scoped SQL WHERE id IN (...) query around those IDs
        3. execute_sql(scoped_sql) → precise rows
        4. return combined result

    Implementation: search/hybrid.py → hybrid_search(query) -> dict
        returns {
            "semantic_results": [...],
            "sql_query":        "SELECT ...",
            "query_result":     [...],
        }
    """
    from search.hybrid import hybrid_search
    from graph_highlighter import extract_highlights

    resolved_query = state.get("resolved_query") or state["user_query"]

    try:
        result = hybrid_search(query=resolved_query)
        rows   = result.get("query_result") or []
        highlight_nodes, highlight_edges = extract_highlights(rows)

        log.info(
            "[hybrid_node] %d semantic hits, %d SQL rows",
            len(result.get("semantic_results") or []),
            len(rows),
        )
        return {
            "semantic_results": result.get("semantic_results"),
            "sql_query":        result.get("sql_query"),
            "query_result":     rows,
            "highlight_nodes":  highlight_nodes,
            "highlight_edges":  highlight_edges,
            "error":            None,
        }
    except Exception as e:
        log.error("[hybrid_node] failed: %s", e)
        return {
            "semantic_results": [],
            "query_result":     [],
            "highlight_nodes":  [],
            "highlight_edges":  [],
            "error":            str(e),
        }


# ===========================================================================
# 9. answer_node  (all paths converge here)
#    Synthesises a natural language answer from whatever results exist in state.
#    Also handles three special cases:
#      a) off_topic  → returns the domain guardrail message
#      b) error      → returns a user-friendly error explanation
#      c) no results → returns "no data found" rather than hallucinating
#
#    Finally, appends the exchange to conversation_history for future turns.
# ===========================================================================

def answer_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : (all paths) state["resolved_query"], state["intent"],
             state["query_result"], state["semantic_results"],
             state["query_plan"], state["sql_query"],
             state["error"], state["user_query"]
    Output : state["final_answer"], state["conversation_history"]

    Guardrail path (intent == "off_topic"):
        final_answer = "This system is designed to answer questions related
                        to the Order-to-Cash dataset only. ..."

    Error path:
        final_answer = user-friendly explanation + suggestion

    Normal path:
        Calls write_answer() which receives the full reasoning chain:
          - resolved_query  : what the user actually asked
          - query_plan      : what the planner decided to retrieve
          - sql_query       : the exact SQL executed (columns, filters,
                              aggregations) — critical for grounded answers
          - query_result    : the actual rows returned by the SQL
          - semantic_results: fuzzy entity matches (hybrid/semantic paths)

        The SQL is the most important grounding input. Without it,
        write_answer can't know whether "billing_count" means active-only
        or all documents, or whether amounts are net or gross.

    History update:
        Appends {"role": "user", "content": user_query} and
                {"role": "assistant", "content": final_answer}
        to conversation_history for the memory_node in the next turn.

    Implementation: llm/answer_writer.py → write_answer(...)
    """
    from llm.answer_writer import write_answer

    intent           = state.get("intent")
    user_query       = state.get("user_query", "")
    resolved_query   = state.get("resolved_query") or user_query
    query_result     = state.get("query_result")     or []
    semantic_results = state.get("semantic_results") or []
    query_plan       = state.get("query_plan")        # None on semantic path
    sql_query        = state.get("sql_query")         # None on semantic path
    error            = state.get("error")
    history          = list(state.get("conversation_history") or [])

    # ── guardrail short-circuit ─────────────────────────────────────────────
    if intent == "off_topic":
        final_answer = (
            "This system is designed to answer questions related to the "
            "Order-to-Cash (O2C) dataset only. It can help you explore sales "
            "orders, deliveries, billing documents, payments, customers, and "
            "products. Please ask a question related to this domain."
        )
        history.append({"role": "user",      "content": user_query})
        history.append({"role": "assistant", "content": final_answer})
        return {
            "final_answer":          final_answer,
            "conversation_history":  history,
        }

    # ── error path ─────────────────────────────────────────────────────────
    if error and not query_result and not semantic_results:
        final_answer = (
            f"I wasn't able to retrieve data for your query. "
            f"Technical detail: {error}. "
            f"Please try rephrasing your question."
        )
        history.append({"role": "user",      "content": user_query})
        history.append({"role": "assistant", "content": final_answer})
        return {
            "final_answer":         final_answer,
            "conversation_history": history,
        }

    # ── normal path ─────────────────────────────────────────────────────────
    # Pass the full reasoning chain so write_answer can produce a grounded,
    # semantically accurate answer — not just a raw data dump.
    final_answer = write_answer(
        query=resolved_query,
        sql_results=query_result,
        semantic_results=semantic_results,
        query_plan=query_plan,     # ← what was planned
        sql_query=sql_query,       # ← what was actually executed
    )

    # ── update conversation memory ──────────────────────────────────────────
    history.append({"role": "user",      "content": user_query})
    history.append({"role": "assistant", "content": final_answer})

    log.info("[answer_node] answer length=%d chars", len(final_answer))
    return {
        "final_answer":         final_answer,
        "conversation_history": history,
    }