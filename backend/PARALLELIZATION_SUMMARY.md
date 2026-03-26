# LLM API Parallelization — Quick Summary

## What Was Implemented

✅ **Async Wrappers** (`llm/async_helpers.py`)
- `async_build_query_plan()` — Non-blocking query plan generation
- `async_semantic_search()` — Non-blocking semantic search
- `parallel_planner_and_semantic()` — Runs both in parallel using `asyncio.gather()`

✅ **New Graph Node** (`graph/nodes.py`)
- `parallel_prep_node` — Executes after `classify_node` and parallelizes LLM API calls

✅ **Result Caching** (`graph/nodes.py`)
- `planner_node` — Checks for cached query_plan before recomputing
- `semantic_node` — Checks for cached semantic_results before recomputing

✅ **Updated Graph Topology** (`graph/graph.py`)
```
classify_node → parallel_prep_node → route_node → (sql | semantic | hybrid)
                    ├─ planner (concurrent)
                    └─ semantic (concurrent)
```

## Expected Performance Improvement

**Before**: 101 seconds latency
- ~50s Gemini API calls (sequential)
- ~40s Database + vector search

**After Parallelization**: 65-70 seconds (~30% faster)
- ~25s Gemini API calls (parallel)
- ~40s Database + vector search

**With Database Indexing**: 15-25 seconds (~75% faster)

## How It Works

1. **After classification**, `parallel_prep_node` runs:
   ```python
   query_plan, semantic_results = await asyncio.gather(
       async_build_query_plan(query),      # LLM call #1
       async_semantic_search(query),       # LLM call #2
   )
   ```
   Both API calls happen **concurrently**, not sequentially.

2. **Results cached** in GraphState for downstream nodes to reuse

3. **Nodes skip recomputation**:
   ```python
   if state.get("query_plan"):
       return {"query_plan": state["query_plan"]}  # Use cached, skip LLM
   ```

## Files Modified

| File | Changes |
|------|---------|
| `llm/async_helpers.py` | ✨ NEW — Async wrappers + parallelization function |
| `graph/nodes.py` | Added `parallel_prep_node`, modified `planner_node` & `semantic_node` for cache reuse |
| `graph/graph.py` | Added `parallel_prep_node` to execution flow |

## Testing The Implementation

### 1. Check logs for parallelization in action
```bash
# Run a query and look for these log messages:
grep "parallel_prep_node\|using precomputed" app.log
```

Expected output:
```
[parallel_prep_node] completed — plan_intent=aggregation, semantic=5 results
[planner_node] using precomputed query_plan (via parallel_prep_node)
[semantic_node] using precomputed semantic_results (via parallel_prep_node)
```

### 2. Compare latencies
```bash
# Run same query 3 times and compare latency_ms field
curl -s -X POST "http://127.0.0.1:8000/query/sync" \
  -H "Content-Type: application/json" \
  -d '{"query": "total skincare revenue"}' | jq .latency_ms

# Expected: ~30% reduction from baseline
```

### 3. Monitor concurrent API calls
Add timing to `parallel_planner_and_semantic()`:
```python
import time
t_start = time.time()
results = await asyncio.gather(...)
print(f"Parallel time: {time.time() - t_start:.2f}s")
```

If truly parallel, should be ~5-10s (max of two calls) instead of 10-20s (sum of two calls).

## Next Optimization Steps

1. **Database Indexing** (highest priority)
   ```sql
   CREATE INDEX idx_billing_cancelled ON billing_document_headers(billing_doc_is_cancelled);
   CREATE INDEX idx_product_group ON products(product_group);
   ```

2. **Connection Pooling** (psycopg2)
   - Replace single connection with pool in `db_executor.py`
   - Expected: 5-10% faster

3. **Caching** (Redis)
   - Cache `query_plan` and `semantic_results` for identical queries
   - Expected: 90% faster for repeated queries

## Backward Compatibility

✅ **No breaking changes** — API remains identical
- Same endpoints
- Same response format
- Same error handling

Parallelization is transparent to clients.

## References

- Implementation: [PARALLELIZATION.md](./PARALLELIZATION.md)
- Async helpers: [llm/async_helpers.py](./llm/async_helpers.py)
- Graph nodes: [graph/nodes.py](./graph/nodes.py)
- Graph topology: [graph/graph.py](./graph/graph.py)
