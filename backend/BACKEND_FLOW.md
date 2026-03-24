# DodgeAI FDE Backend — Complete Query Flow

## Overview
This document describes the entire journey of a query from HTTP request to final answer in the DodgeAI FDE backend.

---

## 1. ENTRY POINT: HTTP Request → FastAPI

### Request Endpoint
```
POST /query
or
POST /query/sync
```

### Request Schema
```python
class QueryRequest(BaseModel):
    query: str                          # Natural language question
    conversation_history: list[str]     # Previous turns (last 6 kept)
```

### Example Request
```json
{
    "query": "What is the total revenue from customers who bought skincare products?",
    "conversation_history": []
}
```

### Initial State Creation
The `_build_initial_state()` function converts the HTTP request into a `GraphState`:

```python
GraphState = {
    "user_query": "What is the total revenue from customers who bought skincare products?",
    "conversation_history": [],
    "resolved_query": "",           # ← Will be filled by memory_node
    "intent": "",                   # ← Will be filled by classify_node
    "retrieval_mode": "",           # ← Will be filled by classify_node
    "query_plan": None,             # ← Will be filled by planner_node (SQL path)
    "sql_query": None,              # ← Will be filled by sql_gen_node (SQL path)
    "query_result": [],             # ← Will be filled by execute_node (SQL path)
    "semantic_results": [],         # ← Will be filled by semantic_node
    "final_answer": "",             # ← Will be filled by answer_node
    "highlight_nodes": [],
    "highlight_edges": [],
    "error": None,
}
```

### FastAPI Orchestration (main.py)
```
HTTP Request
    ↓
query() or query_sync()
    ↓
_build_initial_state(req) → GraphState
    ↓
_graph.invoke(initial_state) or ainvoke(initial_state)  [LangGraph execution]
    ↓
_parse_response(final_state, latency_ms) → QueryResponse
    ↓
HTTP Response (JSON)
```

---

## 2. LANGGRAPH PIPELINE: The 8-Node Orchestra

All query execution happens inside the compiled LangGraph graph. The pipeline follows this topology:

```
                    ┌─────────────────┐
                    │   ENTRY POINT   │
                    │  (memory_node)  │
                    └────────┬────────┘
                             ↓
                    ┌─────────────────┐
                    │ classify_node   │ ← Intent check + routing decision
                    │ (intent, mode)  │
                    └────────┬────────┘
                             ↓
                    ┌─────────────────┐
                    │  route_node     │ ← Conditional: sql/semantic/hybrid/off-topic
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            ↓                ↓                ↓     (off-topic → answer_node)
      ┌──────────┐    ┌────────────┐    ┌─────────┐
      │ SQL Path │    │ Semantic   │    │ Hybrid  │
      └──────────┘    │   Path     │    │  Path   │
            ↓         └───────┬────┘    └────┬────┘
            ↓                 ↓              ↓
    ┌──────────────┐         │              │
    │ planner_node │         │              │
    └───────┬──────┘         │              │
            ↓                 │              │
    ┌──────────────┐         │              │
    │ sql_gen_node │         │              │
    └───────┬──────┘         │              │
            ↓                 │              │
    ┌──────────────┐         │              │
    │execute_node  │         │              │
    └───────┬──────┴─────────┴──────────────┘
            └─────────────────┬─────────────────┘
                              ↓
                    ┌─────────────────┐
                    │  answer_node    │ ← All paths converge
                    │(guardrails, LLM)│   Formats final answer
                    └────────┬────────┘
                             ↓
                    ┌─────────────────┐
                    │      END        │
                    └─────────────────┘
```

---

## 3. NODE-BY-NODE BREAKDOWN

### Node 1: memory_node
**Purpose:** Resolve pronouns and contextual references using conversation history

| Property | Value |
|----------|-------|
| **Input** | `user_query`, `conversation_history` |
| **Output** | `resolved_query` |
| **LLM Call** | Yes (but fast-pathed when no pronouns) |
| **Module** | `llm/memory.py::resolve_query()` |
| **Latency** | ~0-2s (skipped if no pronouns) |

**Flow:**
```
"What is the total revenue from them?" + history
    ↓
[LLM resolves "them" → "skincare products"]
    ↓
resolved_query = "What is the total revenue from skincare products?"
```

---

### Node 2: classify_node
**Purpose:** Determine query intent (domain vs off-topic) and routing mode

| Property | Value |
|----------|-------|
| **Input** | `resolved_query` |
| **Output** | `intent`, `retrieval_mode` |
| **LLM Call** | Yes |
| **Module** | `llm/classifier.py::classify_intent()` |
| **Latency** | ~1-2s |

**Possible Outputs:**
```
resolve_query = "What is the total revenue from customers who bought skincare products?"

intent = "domain"           (vs "off_topic")
retrieval_mode = "sql"      (vs "semantic" or "hybrid")
```

**Classification Logic:**
```
IF query mentions aggregation (sum, total, count, revenue)
    → intent = "domain", retrieval_mode = "sql"
ELSE IF query is semantic (brand, description, similarity)
    → intent = "domain", retrieval_mode = "semantic"
ELSE IF query is complex
    → intent = "domain", retrieval_mode = "hybrid"
ELSE
    → intent = "off_topic"
```

---

### Node 3: route_node
**Purpose:** No-op pass-through; routing logic lives in `_route()` function

| Property | Value |
|----------|-------|
| **Input** | `intent`, `retrieval_mode` |
| **Output** | (none) |
| **LLM Call** | No |
| **Module** | `graph/graph.py::_route()` |
| **Latency** | ~0ms |

**Routing Decision:**
```python
if intent == "off_topic":
    next_node = "answer_node"  # Short-circuit with error message
elif retrieval_mode == "semantic":
    next_node = "semantic_node"
elif retrieval_mode == "hybrid":
    next_node = "hybrid_node"
else:  # "sql"
    next_node = "planner_node"
```

---

### Node 4: planner_node (SQL Path)
**Purpose:** Convert natural language to structured query plan (JSON)

| Property | Value |
|----------|-------|
| **Input** | `resolved_query` |
| **Output** | `query_plan` (Pydantic QueryPlan object) |
| **LLM Call** | Yes (up to 2 retries) |
| **Module** | `llm/planner.py::build_query_plan()` |
| **Latency** | ~15-20s (2 LLM calls + JSON cleanup) |

**QueryPlan Structure:**
```python
class QueryPlan(BaseModel):
    intent: str                    # "aggregation", "exploration", "trace", "comparison"
    tables: List[str]              # ["sales_order_headers", "products", ...]
    joins: List[JoinCondition]     # Exact join paths from schema
    filters: List[FilterCondition] # WHERE conditions
    aggregation: Optional[str]     # "SUM(bdi.net_amount)", "COUNT(*)", ...
    group_by: List[str]           # Columns to GROUP BY
    order_by: Optional[str]       # ORDER BY clause
    limit: int                     # Default 200
    reasoning: str                # Explanation for debugging
```

**Example Plan:**
```json
{
    "intent": "aggregation",
    "tables": ["sales_order_headers", "billing_document_items", "products"],
    "joins": [
        {
            "left_table": "sales_order_headers",
            "right_table": "billing_document_items",
            "join_type": "INNER",
            "on": "billing_document_items.reference_sd_document = sales_order_headers.sales_order"
        }
    ],
    "filters": [
        {"field": "billing_doc_is_cancelled", "operator": "=", "value": "0"}
    ],
    "aggregation": "SUM(bdi.net_amount)",
    "group_by": [],
    "limit": 1,
    "reasoning": "This aggregates total revenue across billing items for skincare products."
}
```

---

### Node 5: sql_gen_node (SQL Path)
**Purpose:** Convert query plan to executable SQL

| Property | Value |
|----------|-------|
| **Input** | `resolved_query`, `query_plan` |
| **Output** | `sql_query` |
| **LLM Call** | No (uses query plan directly) |
| **Module** | `llm/sql_generator.py::generate_sql()` |
| **Latency** | ~100-500ms (deterministic SQL generation) |

**Generated SQL Example:**
```sql
SELECT
    SUM(bdi.net_amount) AS total_revenue
FROM
    sales_order_headers so
    INNER JOIN billing_document_items bdi
        ON bdi.reference_sd_document = so.sales_order
    INNER JOIN products p
        ON so.material = p.product
WHERE
    bdi.billing_doc_is_cancelled = 0
    AND p.category = 'Skincare'
LIMIT 1;
```

---

### Node 6: execute_node (SQL Path)
**Purpose:** Execute SQL against SQLite database and extract graph highlights

| Property | Value |
|----------|-------|
| **Input** | `sql_query` |
| **Output** | `query_result`, `highlight_nodes`, `highlight_edges` |
| **Database** | SQLite (read-only) |
| **Module** | `db_executor.py::execute_sql()` + `graph_highlighter.py` |
| **Latency** | ~100-2000ms (depends on query complexity) |

**Query Result:**
```python
query_result = [
    {
        "total_revenue": 1250000.00,
    }
]
```

**Graph Extraction:**
```python
highlight_nodes = [
    {"id": "skincare", "type": "product", "label": "Skincare Products"},
    {"id": "cust_123", "type": "customer", "label": "Customer X"},
]

highlight_edges = [
    {
        "source": "cust_123",
        "target": "skincare",
        "source_type": "customer",
        "target_type": "product",
        "label": "purchased"
    }
]
```

---

### Node 7: semantic_node (Semantic Path)
**Purpose:** Search ChromaDB vector store for similar entities

| Property | Value |
|----------|-------|
| **Input** | `resolved_query` |
| **Output** | `semantic_results`, `highlight_nodes` |
| **Vector Store** | ChromaDB (persisted in ./chroma_store) |
| **Module** | `search/semantic.py::semantic_search()` |
| **Latency** | ~500-1000ms |

**Semantic Results:**
```python
semantic_results = [
    {"id": "prod_456", "type": "product", "label": "Facewash", "similarity": 0.92},
    {"id": "prod_789", "type": "product", "label": "Moisturizer", "similarity": 0.88},
]
```

---

### Node 8: hybrid_node (Hybrid Path)
**Purpose:** Combine semantic search with scoped SQL query

| Property | Value |
|----------|-------|
| **Input** | `resolved_query` |
| **Output** | `semantic_results`, `sql_query`, `query_result`, `highlight_nodes` |
| **Modules** | `search/hybrid.py::hybrid_search()` |
| **Latency** | ~2-5s (semantic + SQL path combined) |

**Hybrid Process:**
```
resolved_query
    ↓
[Semantic search] → semantic_results (entities)
    ↓
[Scoped SQL on entities] → query_result (data)
    ↓
[Extract highlights] → highlight_nodes, highlight_edges
```

---

### Node 9: answer_node (Convergence Point)
**Purpose:** Format final natural-language answer with guardrails

| Property | Value |
|----------|-------|
| **Input** | All paths: `resolved_query`, `intent`, `query_result`, `semantic_results`, `query_plan`, `sql_query`, `error`, etc. |
| **Output** | `final_answer`, `conversation_history` |
| **LLM Call** | Yes (answer_writer) |
| **Module** | `llm/answer_writer.py::write_answer()` |
| **Latency** | ~3-5s |

**Guardrails Applied:**

1. **Off-topic guard:**
   ```
   IF intent == "off_topic" → return canned guardrail message
   ```

2. **Hybrid SQL failure guard:**
   ```
   IF hybrid_sql_failed AND no query_result
       → refuse to hallucinate, suggest rephrasing
   ```

3. **Numeric query guard:**
   ```
   IF query asks for a number AND no SQL data
       → block fabricated figures, suggest alternative phrasing
   ```

4. **Post-answer sanity check:**
   ```
   IF answer contains "INR" amounts BUT SQL failed
       → BLOCK and request rephrasing
   ```

**Answer Writing:**
```python
final_answer = write_answer(
    query=resolved_query,
    sql_results=query_result,
    semantic_results=semantic_results,
    query_plan=query_plan,
    sql_query=sql_query,
    sql_failed=hybrid_sql_failed,
)
```

**Example Answer:**
```
"The total revenue from customers who bought skincare products is INR 1,250,000.00. 
This was calculated across 847 skincare product units sold over the past 12 months, 
primarily to corporate and individual customers in the IT and finance sectors."
```

---

## 4. COMPLETE REQUEST-RESPONSE CYCLE

### Input State → State Updates → Output
```
┌─────────────────────────────────────────────────────────────────┐
│ INITIAL STATE (from HTTP request)                               │
│                                                                   │
│ user_query: "What is the total revenue from customers..."      │
│ conversation_history: []                                         │
│ resolved_query: ""                                              │
│ intent: ""                                                      │
│ retrieval_mode: ""                                              │
│ query_plan: None                                                │
│ sql_query: None                                                 │
│ query_result: []                                                │
│ semantic_results: []                                            │
│ final_answer: ""                                                │
│ highlight_nodes: []                                             │
│ highlight_edges: []                                             │
│ error: None                                                     │
└─────────────────────────────────────────────────────────────────┘
                          ↓ memory_node
┌─────────────────────────────────────────────────────────────────┐
│ resolved_query: "What is the total revenue from customers..."  │
└─────────────────────────────────────────────────────────────────┘
                          ↓ classify_node
┌─────────────────────────────────────────────────────────────────┐
│ intent: "domain"                                                 │
│ retrieval_mode: "sql"                                            │
└─────────────────────────────────────────────────────────────────┘
                          ↓ route_node
                          ↓ → planner_node (SQL path)
┌─────────────────────────────────────────────────────────────────┐
│ query_plan: QueryPlan(                                           │
│   intent="aggregation",                                          │
│   tables=["sales_order_headers", "billing_document_items", ...] │
│   joins=[...],                                                   │
│   aggregation="SUM(bdi.net_amount)",                             │
│   ...                                                            │
│ )                                                                │
└─────────────────────────────────────────────────────────────────┘
                          ↓ sql_gen_node
┌─────────────────────────────────────────────────────────────────┐
│ sql_query: "SELECT SUM(bdi.net_amount) FROM ... WHERE ..."      │
└─────────────────────────────────────────────────────────────────┘
                          ↓ execute_node
┌─────────────────────────────────────────────────────────────────┐
│ query_result: [{"total_revenue": 1250000.00}]                   │
│ highlight_nodes: [{"id": "skincare", "type": "product", ...}]  │
│ highlight_edges: [{"source": "cust_123", "target": ...}]        │
└─────────────────────────────────────────────────────────────────┘
                          ↓ answer_node
┌─────────────────────────────────────────────────────────────────┐
│ final_answer: "The total revenue from customers who bought..."  │
│ conversation_history: [                                          │
│   {"role": "user", "content": "What is the total revenue..."}   │
│   {"role": "assistant", "content": "The total revenue..."}      │
│ ]                                                                │
└─────────────────────────────────────────────────────────────────┘
                          ↓ END
┌─────────────────────────────────────────────────────────────────┐
│ FINAL HTTP RESPONSE                                              │
│                                                                   │
│ {                                                                │
│   "answer": "The total revenue from customers...",              │
│   "retrieval_mode": "sql",                                       │
│   "query_plan": {...},                                           │
│   "sql_query": "SELECT SUM(...)",                                │
│   "highlight_nodes": [...],                                      │
│   "highlight_edges": [...],                                      │
│   "latency_ms": 24500,                                           │
│   "error": null                                                  │
│ }                                                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. DATA FLOW DIAGRAM

```
HTTP Request (Raw Query)
        ↓
┌──────────────────────────────┐
│ main.py :: query() / query_sync() │
└────────────┬─────────────────┘
             ↓
┌──────────────────────────────────────────┐
│ _build_initial_state()                    │
│ Convert HTTP req → GraphState dict        │
└────────────┬─────────────────────────────┘
             ↓
        ┌────────────────────────────────┐
        │  _graph.invoke()               │
        │  (or ainvoke for async)        │
        │  ↓ Executes LangGraph          │
        │                                │
        │  memory_node ───→ Update state │
        │      ↓                         │
        │  classify_node ──→ Update state │
        │      ↓                         │
        │  route_node ────→ Conditional edge │
        │      ↓                         │
        │  [SQL|Semantic|Hybrid] ──→ Update state │
        │      ↓                         │
        │  answer_node ────→ Final state │
        │      ↓                         │
        │  END                           │
        │                                │
        │  Returns: GraphState (final)   │
        └────────────┬────────────────────┘
                     ↓
        ┌──────────────────────────────────┐
        │ _parse_response()                 │
        │ Convert GraphState → QueryResponse │
        └────────────┬─────────────────────┘
                     ↓
        HTTP Response (JSON)
             ↓
        Client receives answer
```

---

## 6. ERROR HANDLING & RECOVERY

### Error Scenarios

**Scenario 1: Off-Topic Query**
```
Query: "What is the capital of France?"
    ↓ classify_node
intent = "off_topic"
    ↓ route_node
next_node = "answer_node" (short-circuit)
    ↓ answer_node
final_answer = "This system is designed to answer questions related to the O2C dataset only..."
```

**Scenario 2: Malformed Query Plan JSON**
```
Query: "What is total revenue?"
    ↓ planner_node
Attempt 1: LLM generates invalid JSON
    → json.JSONDecodeError
    → Retry with error feedback
Attempt 2: LLM generates valid JSON
    → QueryPlan created successfully
    ↓ (continues to sql_gen_node)
```

**Scenario 3: No SQL Results + Numeric Query**
```
Query: "What is the total revenue from non-existent products?"
    ↓ planner_node, sql_gen_node, execute_node
query_result = []
    ↓ answer_node (Guard 3)
IF _is_numeric_query() AND not query_result AND retrieval_mode in ("sql", "hybrid")
    → final_answer = "This question requires computed data... no matching records found."
```

**Scenario 4: Hybrid SQL Failure**
```
Query: "Aggregate some complex metric"
    ↓ hybrid_node
semantic_results = [entities found]
query_result = []  (SQL failed)
hybrid_sql_failed = True
    ↓ answer_node (Guard 2)
IF hybrid_sql_failed AND not query_result
    → final_answer = "I found relevant entities but could not compute..."
```

---

## 7. LATENCY BREAKDOWN

| Component | Latency | Notes |
|-----------|---------|-------|
| **memory_node** | 0-2s | Fast-pathed if no pronouns |
| **classify_node** | 1-2s | LLM classification |
| **route_node** | 0ms | No-op |
| **planner_node** | 15-20s | 1-2 LLM calls + JSON cleanup |
| **sql_gen_node** | 100-500ms | Deterministic |
| **execute_node** | 100-2000ms | SQLite query execution |
| **semantic_node** | 500-1000ms | ChromaDB vector search |
| **hybrid_node** | 2-5s | Semantic + SQL combined |
| **answer_node** | 3-5s | LLM answer generation |
| **FastAPI overhead** | ~100ms | Request/response parsing |
| **TOTAL (SQL path)** | ~20-30s | Dominated by LLM calls |
| **TOTAL (Semantic path)** | ~5-10s | Mostly ChromaDB + answer |
| **TOTAL (Hybrid path)** | ~10-20s | Both paths combined |

---

## 8. KEY COMPONENTS

### GraphState (Shared Memory)
Located in `graph/state.py` — TypedDict that flows through all nodes:
- **Read-only inputs** come from HTTP request
- **Outputs** are set by LangGraph nodes
- **Accumulated state** is merged by LangGraph on each edge

### Singletons (Initialized at Startup)
```python
_graph: StateGraph             # Compiled LangGraph
_semantic_index: SemanticIndex # ChromaDB vector store
_db_executor: SQLiteExecutor   # Read-only DB connection
```

### LLM Integration
- **Model:** Google Gemini (Flash)
- **Calls:**
  - 1× memory_node (resolve pronouns)
  - 1× classify_node (routing decision)
  - 1-2× planner_node (query plan generation, with retry)
  - 1× answer_node (answer formatting)
- **Temperature:** 0.0 (deterministic)
- **Max tokens:** 3000 (per call)

### Database
- **Type:** SQLite (read-only)
- **Tables:** 19 O2C tables (sales orders, deliveries, billing, payments, etc.)
- **Access:** db_executor.py wraps all queries with permission checks

### Vector Store
- **Type:** ChromaDB
- **Persistence:** ./chroma_store (persisted at boot)
- **Embeddings:** SentenceTransformers
- **Indexes:** Product descriptions, customer info, etc.

---

## 9. RESPONSE SCHEMA

```python
class QueryResponse(BaseModel):
    answer: str                      # Natural language answer
    retrieval_mode: str              # "sql" | "semantic" | "hybrid" | "off_topic"
    query_plan: dict | None          # Structured query plan (SQL path only)
    sql_query: str | None            # Executed SQL statement
    highlight_nodes: list            # Graph nodes to highlight in UI
    highlight_edges: list            # Graph edges to highlight in UI
    latency_ms: float                # End-to-end latency
    error: str | None                # Error message (if any)
```

### Example Response
```json
{
    "answer": "The total revenue from customers who bought skincare products is INR 1,250,000.00.",
    "retrieval_mode": "sql",
    "query_plan": {
        "intent": "aggregation",
        "tables": ["sales_order_headers", "billing_document_items", "products"],
        "joins": [...],
        "filters": [...],
        "aggregation": "SUM(bdi.net_amount)",
        "group_by": [],
        "limit": 1,
        "reasoning": "Aggregating revenue across billing items for skincare products."
    },
    "sql_query": "SELECT SUM(bdi.net_amount) FROM billing_document_items bdi ...",
    "highlight_nodes": [
        {
            "id": "skincare",
            "type": "product",
            "label": "Skincare Products"
        }
    ],
    "highlight_edges": [],
    "latency_ms": 22500.50,
    "error": null
}
```

---

## 10. SUMMARY: End-to-End Query Journey

```
1. User asks     → HTTP POST /query
2. FastAPI       → Validates, builds GraphState
3. memory_node   → Resolve pronouns via LLM
4. classify_node → Determine intent + routing mode via LLM
5. route_node    → Select SQL/Semantic/Hybrid/Off-topic path
6. [PATH-SPECIFIC]:
   - SQL:      planner_node → sql_gen_node → execute_node
   - Semantic: semantic_node
   - Hybrid:   hybrid_node (combines both)
7. answer_node   → Format final answer via LLM, apply guardrails
8. FastAPI       → Parse state, build QueryResponse
9. Client        → Receives JSON with answer + metadata
```

Total time: **~20-30 seconds** (mostly LLM latency)

---

## 11. DEBUGGING TIPS

### Enable Debug Logging
```python
logging.basicConfig(level=logging.DEBUG)
```

### Test Individual Nodes
```python
from graph.state import GraphState
from graph.nodes import memory_node

state = GraphState(
    user_query="Test query",
    conversation_history=[],
)
result = memory_node(state)
print(result)
```

### Inspect Query Plan
```python
from llm.planner import build_query_plan

plan = build_query_plan("What is the total revenue?")
print(plan.model_dump(indent=2))
```

### Trace SQL Execution
```python
from db_executor import execute_sql

sql = "SELECT SUM(net_amount) FROM billing_document_items"
rows = execute_sql(sql)
print(rows)
```

### Simulate Full Pipeline
```python
from main import _build_initial_state
from graph.graph import build_graph

graph = build_graph()
state = _build_initial_state(QueryRequest(
    query="What is the total revenue?",
    conversation_history=[]
))
final = graph.invoke(state)
print(final)
```

---

Generated: March 24, 2026
