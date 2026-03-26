"""
nodes.py — Node function stubs for the O2C LangGraph pipeline.

Each function here is a thin orchestration shell that:
  1. Reads from GraphState
  2. Calls the real implementation from its dedicated module
  3. Returns a partial GraphState dict (LangGraph merges it)

Parallelization:
  - parallel_prep_node runs build_query_plan and semantic_search in parallel
    using ThreadPoolExecutor for non-blocking concurrent execution.
  - This node runs after classify_node and before routing, ensuring both
    are precomputed and reused by downstream nodes.
"""

import logging
from typing import Any

from .state import GraphState

log = logging.getLogger(__name__)


# ===========================================================================
# 1. memory_node
# ===========================================================================

def memory_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["user_query"], state["conversation_history"]
    Output : state["resolved_query"]

    FIX #8 (latency): resolve_query() now fast-paths when there is no history
    OR no pronouns in the query — skipping the LLM call entirely (~2s saved).
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
# ===========================================================================

def classify_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["intent"], state["retrieval_mode"]
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
# ===========================================================================

def route_node(state: GraphState) -> dict[str, Any]:
    """No-op pass-through. Routing is handled by _route() in graph.py."""
    return {}


# ===========================================================================
# 3.5. parallel_prep_node  (NEW: parallelization optimization)
# ===========================================================================

def parallel_prep_node(state: GraphState) -> dict[str, Any]:
    """
    NEW: Parallelizes build_query_plan and semantic_search using threading.
    
    Runs AFTER classify_node and BEFORE routing. Precomputes both query_plan
    and semantic_results using ThreadPoolExecutor for parallelization,
    eliminating sequential LLM call overhead.
    
    Both planner_node and semantic_node will use these precomputed results,
    avoiding redundant API calls.
    
    Input  : state["resolved_query"]
    Output : state["query_plan"], state["semantic_results"]
    """
    from concurrent.futures import ThreadPoolExecutor
    from llm.planner import build_query_plan
    from search.semantic import semantic_search
    
    resolved_query = state.get("resolved_query") or state["user_query"]
    
    try:
        # Use ThreadPoolExecutor to parallelize blocking I/O operations
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both tasks to run concurrently
            planner_future = executor.submit(build_query_plan, resolved_query)
            semantic_future = executor.submit(semantic_search, resolved_query, 10)
            
            # Wait for both to complete
            query_plan = planner_future.result()
            semantic_results = semantic_future.result()
        
        log.info(
            "[parallel_prep_node] completed — plan_intent=%s, semantic=%d results",
            query_plan.intent if hasattr(query_plan, 'intent') else '?',
            len(semantic_results),
        )
        return {
            "query_plan":        query_plan,
            "semantic_results":  semantic_results,
        }
    except Exception as e:
        log.error("[parallel_prep_node] parallelization failed: %s", e)
        # Return empty results — downstream nodes will handle gracefully
        return {
            "query_plan":        None,
            "semantic_results":  [],
            "error":             f"parallel prep failed: {e}",
        }


# ===========================================================================
# 4. planner_node  (SQL path, step 1 of 3)
# ===========================================================================

def planner_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"], state["query_plan"] (may be cached)
    Output : state["query_plan"] (QueryPlan Pydantic object)
    
    OPTIMIZATION: If query_plan is already in state (from parallel_prep_node),
    reuse it. Otherwise, compute it now.
    """
    # Check if already precomputed by parallel_prep_node
    if state.get("query_plan"):
        log.info("[planner_node] using precomputed query_plan (via parallel_prep_node)")
        return {"query_plan": state["query_plan"]}
    
    from llm.planner import build_query_plan

    resolved_query = state.get("resolved_query") or state["user_query"]

    query_plan = build_query_plan(resolved_query)

    log.info(
        "[planner_node] computed query_plan — intent=%s tables=%s joins=%d filters=%d",
        query_plan.intent,
        query_plan.tables,
        len(query_plan.joins),
        len(query_plan.filters)
    )
    return {"query_plan": query_plan}


# ===========================================================================
# 5. sql_gen_node  (SQL path, step 2 of 3)
# ===========================================================================

def sql_gen_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"], state["query_plan"] (QueryPlan object)
    Output : state["sql_query"]  or  state["error"]
    """
    from llm.sql_generator import generate_sql

    resolved_query = state.get("resolved_query") or state["user_query"]
    query_plan = state.get("query_plan")
    
    if not query_plan:
        log.error("[sql_gen_node] query_plan is missing from state")
        return {"sql_query": None, "error": "No query plan provided"}

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
# ===========================================================================

def execute_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["sql_query"], state["error"]
    Output : state["query_result"], state["highlight_nodes"], state["highlight_edges"]
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
# ===========================================================================

def semantic_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"], state["semantic_results"] (may be cached)
    Output : state["semantic_results"], state["highlight_nodes"]
    
    OPTIMIZATION: If semantic_results are already in state (from parallel_prep_node),
    reuse them. Otherwise, compute them now.
    """
    from search.semantic import semantic_search
    from graph_highlighter import nodes_from_semantic_results

    # Check if already precomputed by parallel_prep_node
    if state.get("semantic_results"):
        log.info("[semantic_node] using precomputed semantic_results (via parallel_prep_node)")
        highlight_nodes = nodes_from_semantic_results(state["semantic_results"])
        return {
            "semantic_results": state["semantic_results"],
            "highlight_nodes":  highlight_nodes,
        }

    resolved_query = state.get("resolved_query") or state["user_query"]

    results = semantic_search(query=resolved_query, top_k=10)
    highlight_nodes = nodes_from_semantic_results(results)

    log.info("[semantic_node] computed semantic results — %d results", len(results))
    return {
        "semantic_results": results,
        "highlight_nodes":  highlight_nodes,
    }


# ===========================================================================
# 8. hybrid_node  (hybrid path)
# ===========================================================================

def hybrid_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : state["resolved_query"]
    Output : state["semantic_results"], state["query_result"],
             state["sql_query"], state["highlight_nodes"], state["highlight_edges"]

    FIX #1: reads hybrid_sql_failed from hybrid_search() result and stores it
    in state so answer_node can detect a fabrication risk and refuse to
    produce a specific INR figure from an empty query_result.
    """
    from search.hybrid import hybrid_search
    from graph_highlighter import extract_highlights

    resolved_query = state.get("resolved_query") or state["user_query"]

    try:
        result = hybrid_search(query=resolved_query)
        rows   = result.get("query_result") or []
        highlight_nodes, highlight_edges = extract_highlights(rows)

        log.info(
            "[hybrid_node] %d semantic hits, %d SQL rows, sql_failed=%s",
            len(result.get("semantic_results") or []),
            len(rows),
            result.get("hybrid_sql_failed", False),
        )
        return {
            "semantic_results":  result.get("semantic_results"),
            "sql_query":         result.get("sql_query"),
            "query_result":      rows,
            "highlight_nodes":   highlight_nodes,
            "highlight_edges":   highlight_edges,
            # FIX #1: propagate failure flag so answer_node blocks hallucination
            "hybrid_sql_failed": result.get("hybrid_sql_failed", False),
            "sql_error":         result.get("sql_error"),
            "error":             None,
        }
    except Exception as e:
        log.error("[hybrid_node] failed: %s", e)
        return {
            "semantic_results":  [],
            "query_result":      [],
            "highlight_nodes":   [],
            "highlight_edges":   [],
            "hybrid_sql_failed": True,
            "sql_error":         str(e),
            "error":             str(e),
        }


# ===========================================================================
# 9. answer_node  (all paths converge here)
# ===========================================================================

def _is_numeric_query(query: str) -> bool:
    """Detect queries that require SQL-computed answers (totals, counts, etc.)."""
    signals = ["total", "sum", "count", "how many", "how much",
               "average", "revenue", "amount", "percentage", "ratio"]
    return any(s in query.lower() for s in signals)


def _sanity_check_answer(answer: str, sql_results: list[dict],
                          sql_failed: bool) -> str | None:
    """
    Post-answer validation (FIX #4).
    Returns error message if answer contains fabricated data, else None.
    """
    import re

    if sql_failed or not sql_results:
        # Check if the answer contains INR amounts — these MUST come from SQL
        amounts = re.findall(r'INR\s*[\d,]+(?:\.\d{2})?', answer, re.IGNORECASE)
        if amounts:
            log.warning(
                "[answer_node] BLOCKED fabricated answer containing %d INR amount(s): %s",
                len(amounts), amounts[:3],
            )
            return (
                "I found relevant entities but could not compute the exact "
                "financial figures — the data query encountered an error. "
                "Please try rephrasing your question or ask for a specific "
                "product or customer by name."
            )

        # Check for raw numbers that look like fabricated totals
        large_numbers = re.findall(r'\b\d{4,}(?:,\d{3})*(?:\.\d+)?\b', answer)
        if large_numbers and sql_failed:
            log.warning(
                "[answer_node] BLOCKED answer containing large numbers without SQL data: %s",
                large_numbers[:3],
            )
            return (
                "I found relevant entities but could not compute the exact "
                "figures — the data query encountered an error. "
                "Please try rephrasing your question."
            )

    return None  # OK


def answer_node(state: GraphState) -> dict[str, Any]:
    """
    Input  : (all paths) state["resolved_query"], state["intent"],
             state["query_result"], state["semantic_results"],
             state["query_plan"], state["sql_query"],
             state["error"], state["user_query"],
             state["hybrid_sql_failed"], state["sql_error"]
    Output : state["final_answer"], state["conversation_history"]

    Guardrails:
      1. Off-topic → canned refusal
      2. hybrid_sql_failed + no data → safe error (no hallucination)
      3. Numeric query + no SQL data → "insufficient data" (FIX #12)
      4. Post-answer sanity check blocks fabricated INR figures (FIX #4)
    """
    from llm.answer_writer import write_answer

    intent             = state.get("intent")
    retrieval_mode     = state.get("retrieval_mode")
    user_query         = state.get("user_query", "")
    resolved_query     = state.get("resolved_query") or user_query
    query_result       = state.get("query_result")     or []
    semantic_results   = state.get("semantic_results") or []
    query_plan         = state.get("query_plan")        # None on semantic path
    sql_query          = state.get("sql_query")         # None on semantic path
    error              = state.get("error")
    hybrid_sql_failed  = state.get("hybrid_sql_failed", False)
    sql_error          = state.get("sql_error")
    history            = list(state.get("conversation_history") or [])

    def _return_answer(answer: str, **extra) -> dict[str, Any]:
        """Helper to build return dict and append to history."""
        history.append({"role": "user",      "content": user_query})
        history.append({"role": "assistant", "content": answer})
        result = {
            "final_answer":         answer,
            "conversation_history": history,
        }
        result.update(extra)
        return result

    # ── Guard 1: off-topic short-circuit ───────────────────────────────────
    if intent == "off_topic":
        return _return_answer(
            "This system is designed to answer questions related to the "
            "Order-to-Cash (O2C) dataset only. It can help you explore sales "
            "orders, deliveries, billing documents, payments, customers, and "
            "products. Please ask a question related to this domain.",
            retrieval_mode="off_topic",
        )

    # ── Guard 2: hybrid SQL failed — refuse to hallucinate ─────────────────
    if hybrid_sql_failed and not query_result:
        semantic_labels = [r.get("label", "") for r in semantic_results[:5]]
        return _return_answer(
            "I found relevant entities via semantic search "
            f"({', '.join(semantic_labels) if semantic_labels else 'none'}) "
            "but could not compute the exact figure — the data query encountered "
            f"an error ({sql_error or 'unknown'}). "
            "Please try rephrasing your question or ask for a specific product "
            "or customer by name."
        )

    # ── Guard 3: numeric query with no SQL data (FIX #12) ──────────────────
    if (_is_numeric_query(resolved_query)
            and not query_result
            and retrieval_mode in ("hybrid", "sql")):
        semantic_labels = [r.get("label", "") for r in semantic_results[:5]]
        log.warning(
            "[answer_node] numeric query with no SQL data — blocking fabrication"
        )
        if semantic_labels:
            return _return_answer(
                "This question requires computed data from the database, but "
                "the query did not return any results. I did find related "
                f"entities: {', '.join(semantic_labels)}. "
                "Try asking about a specific product or customer by name, "
                "or rephrase your question."
            )
        else:
            return _return_answer(
                "This question requires computed data from the database, but "
                "no matching records were found. Please try rephrasing your "
                "question or check that the entities you refer to exist in "
                "the dataset."
            )

    # ── error path ─────────────────────────────────────────────────────────
    if error and not query_result and not semantic_results:
        return _return_answer(
            f"I wasn't able to retrieve data for your query. "
            f"Technical detail: {error}. "
            f"Please try rephrasing your question."
        )

    # ── normal path ─────────────────────────────────────────────────────────
    final_answer = write_answer(
        query=resolved_query,
        sql_results=query_result,
        semantic_results=semantic_results,
        query_plan=query_plan,
        sql_query=sql_query,
        sql_failed=hybrid_sql_failed,
        sql_error=sql_error,
    )

    # ── Guard 4: post-answer sanity check (FIX #4) ─────────────────────────
    blocked_msg = _sanity_check_answer(
        answer=final_answer,
        sql_results=query_result,
        sql_failed=hybrid_sql_failed,
    )
    if blocked_msg:
        log.warning("[answer_node] post-answer sanity check BLOCKED the answer")
        return _return_answer(blocked_msg)

    log.info("[answer_node] answer length=%d chars", len(final_answer))
    return _return_answer(final_answer)