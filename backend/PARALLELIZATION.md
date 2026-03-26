# LLM API Parallelization — Implementation Guide

## Overview

This document describes the parallelization optimizations implemented to reduce latency by executing independent LLM API calls concurrently.

## Problem

The original sequential graph flow had bottlenecks:

```
classify_node 
    ↓
route_node 
    ↓ (for SQL path)
planner_node (LLM call #1 — build query plan)
    ↓
sql_gen_node (LLM call #2 — generate SQL)
    ↓
execute_node (DB query)
```

Issue: `planner_node` and `sql_gen_node` calls to Gemini API were sequential, even though they could overlap.

## Solution: `parallel_prep_node`

A new intermediate node runs **both** query planning and semantic search in parallel before routing:

```
classify_node 
    ↓
parallel_prep_node (asyncio.gather: planner + semantic in parallel)
    ├─→ Gemini: build_query_plan (async)
    └─→ Gemini: semantic_search    (async)
    ↓
route_node 
    ↓
planner_node (reuses cached query_plan — no LLM call)
    ↓
sql_gen_node 
    ↓
execute_node
```

### Key Benefits

1. **Parallel API Calls**: `build_query_plan()` and `semantic_search()` execute concurrently
2. **Result Caching**: Both results cached in `GraphState`
3. **Downstream Reuse**: `planner_node` and `semantic_node` check cache first before recomputing
4. **Estimated Speedup**: 30-50% latency reduction (depending on individual API call times)

## Architecture

### New Files

#### `llm/async_helpers.py`

Provides async wrappers for blocking I/O operations:

```python
async def async_build_query_plan(query: str)
async def async_semantic_search(query: str, top_k=10)
async def parallel_planner_and_semantic(query: str, top_k=10)
    # Returns (query_plan, semantic_results) in parallel
```

### Modified Files

#### `graph/nodes.py`

**Added `parallel_prep_node()`**:
- Runs after `classify_node`
- Calls `parallel_planner_and_semantic()` via `asyncio.run()`
- Stores both results in `GraphState`
- Handles exceptions gracefully

**Modified `planner_node()`**:
```python
def planner_node(state):
    if state.get("query_plan"):  # Already cached?
        return {"query_plan": state["query_plan"]}  # Reuse
    # Otherwise, compute now
```

**Modified `semantic_node()`**:
```python
def semantic_node(state):
    if state.get("semantic_results"):  # Already cached?
        return {"semantic_results": state["semantic_results"]}  # Reuse
    # Otherwise, compute now
```

#### `graph/graph.py`

Updated graph topology:

```python
g.add_edge("classify_node",      "parallel_prep_node")
g.add_edge("parallel_prep_node", "route_node")
```

Graph now routes: memory → classify → **parallel_prep** → route → (sql | semantic | hybrid)

## Performance Impact

### Expected Latency Reduction

| Component | Sequential (ms) | Parallel (ms) | Speedup |
|-----------|-----------------|---------------|---------|
| SQL path: planner + semantic | 8000 (4s each) | 4000 (concurrent) | **2x** |
| First query cold-start overhead | Reduced | Eliminated | ✓ |
| Subsequent queries (cached) | 3000-4000 | 1500-2000 | **1.5-2x** |

### Best Case Scenario

Original latency: ~101 seconds
- ~50s: Gemini API calls
- ~40s: Database query + vector search

After parallelization:
- ~25s: Gemini API calls (50% reduction)
- ~40s: Database query + vector search
- **Expected total: ~65-70 seconds (30% improvement)**

With database indexing (next optimization):
- Expected: **15-25 seconds**

## Usage

No changes to client code. The parallelization is transparent:

```bash
# Same API as before
curl -X POST "http://127.0.0.1:8000/query/sync" \
  -H "Content-Type: application/json" \
  -d '{"query": "total revenue from skincare products"}'

# Latency should be ~30% faster
```

## Testing

To verify parallelization is working:

1. **Check logs** for `[parallel_prep_node]` entries:
```
[parallel_prep_node] completed — plan_intent=aggregation, semantic=5 results
[planner_node] using precomputed query_plan (via parallel_prep_node)
[semantic_node] using precomputed semantic_results (via parallel_prep_node)
```

2. **Monitor API call timing**:
```python
# In async_helpers.py, add timing
import time
t0 = time.time()
results = await asyncio.gather(...)
print(f"Parallel execution: {time.time() - t0:.2f}s")
```

3. **Compare latencies**:
   - Run same query 5 times
   - Average latency should show ~30% improvement

## Future Optimizations

1. **Connection Pooling**: Add psycopg2 connection pool to eliminate per-query connection overhead
2. **Database Indexing**: Add indexes on frequently-filtered columns (billing_doc_is_cancelled, product_group)
3. **Hybrid Search Parallelization**: Parallelize semantic + SQL generation within `hybrid_search()`
4. **Result Caching**: Add Redis to cache query_plan and semantic_results across requests
5. **Streaming**: Stream partial results as they become available (especially for large result sets)

## Troubleshooting

### Logs show "using precomputed" but query is still slow

- Likely bottleneck is **database query execution**, not LLM calls
- Run `EXPLAIN ANALYZE` on your SQL queries to identify missing indexes
- See root [OPTIMIZATION.md](./OPTIMIZATION.md) for database tuning

### asyncio.run() fails with "already running"

- LangGraph may already run in an event loop
- Switch from `asyncio.run()` to `asyncio.get_event_loop().create_task()`
- Or use `loop.run_until_complete()` if in sync context

### Memory usage increases

- Semantic search caches embeddings in ChromaDB
- Consider smaller `top_k` value or separate cache invalidation strategy

## References

- [LangGraph Async Documentation](https://python.langchain.com/docs/langgraph/concepts/low_level_conceptual_guide)
- [Python asyncio Documentation](https://docs.python.org/3/library/asyncio.html)
- [Gemini API Rate Limits](https://ai.google.dev/docs/quotas)
