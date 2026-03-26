"""
async_helpers.py — Helpers for parallel execution of LLM API calls.

NOTE: We use ThreadPoolExecutor instead of pure asyncio because LangGraph
manages its own event loop. ThreadPoolExecutor provides thread-based
parallelization that works well with LangGraph's sync node functions.

This module is kept for reference and potential future async optimization.
Currently, parallel_prep_node in graph/nodes.py uses ThreadPoolExecutor directly.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


def parallel_planner_and_semantic_threaded(
    query: str,
    top_k: int = 10,
) -> tuple:
    """
    Parallelizes build_query_plan and semantic_search using ThreadPoolExecutor.
    
    Args:
        query: The resolved user query
        top_k: Number of semantic results to retrieve
    
    Returns:
        (query_plan, semantic_results)
    
    Useful for hybrid path where both are needed, or for precomputing
    both before we know the retrieval mode.
    """
    from llm.planner import build_query_plan
    from search.semantic import semantic_search
    
    log.info("[threading] parallelizing planner + semantic for query: %r", query[:50])
    
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both tasks to run concurrently
            planner_future = executor.submit(build_query_plan, query)
            semantic_future = executor.submit(semantic_search, query, top_k)
            
            # Wait for both to complete
            query_plan = planner_future.result()
            semantic_results = semantic_future.result()
        
        log.info(
            "[threading] parallel execution complete — plan_intent=%s, semantic_results=%d",
            query_plan.intent if hasattr(query_plan, 'intent') else '?',
            len(semantic_results),
        )
        return query_plan, semantic_results
    except Exception as e:
        log.error("[threading] parallel execution failed: %s", e)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Legacy async functions (kept for reference, not actively used)
# ─────────────────────────────────────────────────────────────────────────────

async def async_build_query_plan(query: str):
    """
    Async wrapper for build_query_plan (legacy — for reference only).
    We use ThreadPoolExecutor in graph/nodes.py instead.
    """
    from llm.planner import build_query_plan
    
    loop = __import__('asyncio').get_event_loop()
    return await loop.run_in_executor(None, build_query_plan, query)


async def async_semantic_search(
    query: str,
    top_k: int = 10,
) -> list:
    """
    Async wrapper for semantic_search (legacy — for reference only).
    We use ThreadPoolExecutor in graph/nodes.py instead.
    """
    from search.semantic import semantic_search
    
    loop = __import__('asyncio').get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: semantic_search(query=query, top_k=top_k),
    )
