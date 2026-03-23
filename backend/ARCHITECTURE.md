# DodgeAI FDE Backend - Architecture & Flow

## Overview

DodgeAI FDE (Financial Data Extraction) is an intelligent Order-to-Cash (O2C) analysis platform. It processes natural language questions about SAP Order-to-Cash data and returns answers grounded in actual SQL queries, with interactive graph visualization of the underlying business flows.

**Core capability**: Convert natural language questions like *"Show me the top customers by billing amount in Q4 2024"* into SQL queries, execute them safely against a SQLite database, and return human-readable answers with highlighted entity relationships.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          USER QUERY INPUT                              │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────▼───────────────┐
                    │   graph/graph.py           │
                    │  (LangGraph Orchestrator)  │
                    └────────────┬───────────────┘
                                 │
         ┌───────────────────────┴───────────────────────┐
         │                                               │
    ┌────▼──────────┐  ┌─────────────────────────────────────┐
    │ MEMORY LAYER  │  │ INTENT CLASSIFICATION & ROUTING     │
    │               │  │  ├─ Domain guard (on-topic?)        │
    │ memory.py:    │  │  ├─ Retrieval mode selector         │
    │ ├─ Resolves   │  │  │  (sql|semantic|hybrid)           │
    │ │ pronouns    │  │  └─ classifier.py                   │
    │ ├─ Augments   │  └─────────────────────────────────────┘
    │ │ follow-ups  │             │
    │ └─ Using      │             │ ROUTE
    │   history     │    ┌────────┴────────┬────────┐
    │               │    │                 │        │
    └───────────────┘    ▼                 ▼        ▼
         │           SQL PATH         SEMANTIC   HYBRID
         │              │              PATH       PATH
         │           ┌──────┐        ┌────────┐ ┌─────┐
         │           │Plan  │        │Semantic│ │Hybrid
         │           │      │        │Search  │ │Search
         │           └──────┘        └────────┘ └─────┘
         │              │                  │          │
         │           ┌──────┐              │          │
         │           │Gen   │              │          │
         │           │SQL   │              │          │
         │           └──────┘              │          │
         │              │                  │          │
         │           ┌──────┐              │          │
         │           │Exec  │              │          │
         │           │Query │              │          │
         │           └──────┘              │          │
         │              │                  │          │
         │              └──────────────┬───┴──────────┘
         │                             │
         │                   ┌─────────▼──────────┐
         │                   │  Answer Writer     │
         │                   │  answer_writer.py  │
         │                   │                    │
         │                   │ Synthesizes LLM   │
         │                   │ response from:     │
         │                   │ ├─ Query plan      │
         │                   │ ├─ Resolved query  │
         │                   │ ├─ SQL statement   │
         │                   │ ├─ Data results    │
         │                   │ └─ Semantic hits   │
         │                   └─────────┬──────────┘
         │                             │
         │                   ┌─────────▼──────────┐
         │                   │ Graph Highlighting │
         │                   │ graph_highlighter  │
         │                   │                    │
         │                   │ Extracts entity    │
         │                   │ IDs & relationships│
         │                   │ from results       │
         │                   └─────────┬──────────┘
         │                             │
         └─────────────────────────────┼─────────────────┐
                                       │                 │
                            ┌──────────▼────────┐    ┌────▼──────────┐
                            │ NATURAL LANGUAGE  │    │ VISUALIZATION │
                            │ ANSWER            │    │ PAYLOAD       │
                            │ (grounded in SQL) │    │ ├─ Nodes      │
                            └───────────────────┘    │ └─ Edges      │
                                                     └───────────────┘
```

---

## Data Flow & Major Components

### 1. **Data Ingestion Pipeline** (`ingest.py`)

Prepares raw SAP data for querying.

**What it does:**
- Reads JSONL part-files from `sap-order-to-cash-dataset/sap-o2c-data/`
- Normalizes nested JSON structures into a flat SQLite schema
- Creates 19 O2C tables with proper types and relationships
- Applies business logic transformations (e.g., nested `creationTime` dicts → `HH:MM:SS` strings)
- Builds indexes for common join paths and searches
- Deduplicates rows based on primary keys

**Key transformations:**
```
Raw JSONL:  {"salesOrder": "740509", "creationTime": {"hours": 14, "minutes": 30, ...}}
SQLite row: sales_order='740509', creation_time='14:30:00'
```

**19 tables created:**
- `sales_order_headers`, `sales_order_items`, `sales_order_schedule_lines`
- `outbound_delivery_headers`, `outbound_delivery_items`
- `billing_document_headers`, `billing_document_items`, `billing_document_cancellations`
- `journal_entry_items_accounts_receivable`
- `payments_ar` (accounts receivable)
- `business_partners`, `business_partner_addresses`
- `customer_company_assignments`, `customer_sales_area_assignments`
- `products`, `product_descriptions`, `product_plants`, `product_storage_locations`
- `plants`

---

### 2. **Semantic Search Index** (`search/semantic.py`)

Maintains a ChromaDB vector index for fuzzy entity matching.

**What it does:**
- Embeds entity text (product descriptions, customer names, order details) using `all-MiniLM-L6-v2` model
- Stores vectors WITH structured metadata (entity type, IDs, attributes) in ChromaDB
- Enables pre-filtered searches: "Find products that sound like 'face serum' AND belong to product group 'ZFG1001'"
- Persists index to disk (`./chroma_store`) for fast startup on subsequent runs

**Why ChromaDB over FAISS:**
- FAISS is just vectors + separate Python lists (no metadata filtering)
- ChromaDB links each vector to metadata as a single document, enabling typed filtered queries
- No separate indexing required—metadata stays perfectly in-sync

**Index structure:**
```python
{
    "id": "product_S8907367008620",
    "embedding": [...384 floats...],
    "document": "FACESERUM 30ML VIT C ABC-WEB-...",  # searchable text
    "metadata": {
        "type": "product",
        "entity_id": "S8907367008620",
        "label": "FACESERUM 30ML VIT C",
        "product_group": "ZFG1001",
        ...
    }
}
```

**Indexed entity types:**
- `product`, `customer`, `plant`, `sales_order`, `billing_document`, `delivery`, `payment`

---

### 3. **Query Orchestration** (`graph/graph.py`, `graph/state.py`)

LangGraph-based state machine that coordinates the entire request.

**GraphState (shared context):**
```python
{
    "user_query": str,                    # raw input
    "conversation_history": List[str],    # for pronoun resolution
    "resolved_query": str,                # after memory context
    
    "intent": "domain" | "off_topic",     # domain guard result
    "retrieval_mode": "sql" | "semantic" | "hybrid",  # routing decision
    
    # SQL path outputs
    "query_plan": str,                    # plain English plan
    "sql_query": str,                     # final SQL statement
    "query_result": List[dict],           # rows from database
    
    # Semantic path outputs
    "semantic_results": List[dict],       # ChromaDB matches
    
    # Final answer & UI
    "final_answer": str,                  # natural language response
    "highlight_nodes": List[dict],        # O2C entities to highlight
    "highlight_edges": List[dict],        # relationships to draw
    
    "error": str | None,                  # error state
}
```

**Graph topology (nodes & edges):**
```
ENTRY
  ↓
[memory_node]        Resolve pronouns ("it", "that customer") using history
  ↓
[classify_node]      Decide: on_topic? + which retrieval mode?
  ↓
[route_node]         Route based on intent & retrieval_mode
  ├─→ [planner_node]      ──→ [sql_gen_node]     ──→ [execute_node]
  ├─→ [semantic_node]     (direct semantic search)
  ├─→ [hybrid_node]       (semantic search + SQL query)
  └─→ [off_topic]         (set guardrail message)
  ↓
[answer_node]        Synthesize LLM response + extract highlights
  ↓
FINAL OUTPUT
```

---

### 4. **Memory & Context Resolution** (`llm/memory.py`)

Resolves pronouns and implicit references using conversation history.

**Problem solved:**
```
User (turn 1): "Show me sales order 740509"
Assistant: "Sales Order 740509 was placed by customer ABC..."
User (turn 2): "Who is the customer for it?"
              ↑ "it" is ambiguous without history
```

**Solution:**
- Keeps last 6 turns of conversation
- Uses LLM to rewrite follow-up with explicit entity IDs/names
- Output: *"Who is the customer for sales order 740509?"*

**Temperature set to 0.0** for deterministic rewriting.

---

### 5. **Intent Classification & Routing** (`llm/classifier.py`)

Makes two decisions in one LLM call:

1. **Domain Guard**: Is this question about the O2C dataset?
   - ✅ Returns `intent="domain"` → proceed to retrieval
   - ❌ Returns `intent="off_topic"` → skip to answer_node (guardrail response)

2. **Retrieval Mode Selection**: Which strategy fits best?
   - **`sql`**: Exact IDs, aggregations, date ranges, status filters
     - Example: *"Total revenue by customer in December?"*
   - **`semantic`**: Fuzzy product/customer discovery, browsing
     - Example: *"Products similar to vitamin C serum?"*
   - **`hybrid`**: Mix of vague entity discovery + precise figures
     - Example: *"Total paid by customers who bought anti-aging products?"*

**Decision logic (encoded in prompt):**
```
→ SQL: Key indicators = specific order/invoice IDs, "how many", "which", date ranges, status checks
→ Semantic: Key indicators = "find", "similar to", "like", exploratory questions
→ Hybrid: Mix of "find products matching..." + "what did they pay in total"
```

---

### 6. **Query Planning & SQL Generation** (`llm/planner.py`, `llm/sql_generator.py`)

Two-step approach for robust SQL generation.

**Step 1: Query Planning (`planner.py`)**
- Input: Resolved natural language query
- Output: Plain-English plan (not SQL)
- Purpose: Reason about schema before committing to syntax

Example:
```
User Query: "Total revenue by customer for Q4 2024?"

Query Plan:
1. Join sales_order_headers → billing_document_headers on customer
2. Filter dates: billing_document_date BETWEEN '2024-10-01' AND '2024-12-31'
3. Only active (non-cancelled) billing docs: billing_doc_is_cancelled = 0
4. GROUP BY customer
5. SELECT customer, SUM(total_net_amount) as revenue
6. ORDER BY revenue DESC
```

**Step 2: SQL Generation (`sql_generator.py`)**
- Input: Query plan
- Output: Syntactically valid SQLite SELECT statement
- Validation: Tested with EXPLAIN against live database

**Critical join paths** (hardcoded to prevent hallucinated joins):
```
sales_order_headers → delivery_headers via:
  sales_order_headers.sales_order = delivery_items.reference_sd_document

billing_documents → payments via:
  billing_document_headers.accounting_document = payments_ar.clearing_accounting_document

products → product_descriptions via:
  products.product = product_descriptions.product AND language='EN'
```

**Safety constraints:**
- LIMIT 200 always applied (prevents accidental full-table dumps)
- Read-only connection to database
- 10-second query timeout
- Session layer wraps LIMIT in subquery if not present

---

### 7. **Database Execution** (`db_executor.py`)

Safe, read-only SQL execution layer.

**Safety measures:**
1. **Read-only mode**: SQLite opened with `?mode=ro` URI
2. **Hard row limit**: LIMIT 200 enforced via subquery wrapper if missing
3. **Query timeout**: 10 seconds max (guards against accidental full-table scans)
4. **Statement validation**: Only SELECT/WITH statements accepted

```python
execute_sql("SELECT ...")  # ✅ OK
execute_sql("INSERT ...")  # ❌ Rejected
execute_sql("DROP TABLE")  # ❌ Rejected
```

---

### 8. **Answer Writing** (`llm/answer_writer.py`)

Synthesizes grounded natural language response from the full reasoning chain.

**Inputs:**
- Resolved query (original intent)
- Query plan (what the system decided to do)
- **SQL query** (exactly what was executed—grounds the answer)
- SQL result rows (actual data)
- Semantic search results (entity matches for context)

**Why SQL visibility matters:**
Without SQL, the LLM only sees:
```python
{"material": "S8907367008620", "billing_count": 11, "total_revenue": 3100.17}
```

With SQL, it understands:
```sql
SELECT p.product, COUNT(bdh.billing_document) AS billing_count, 
       SUM(bdh.total_net_amount) AS total_revenue
FROM products p
LEFT JOIN billing_document_items bdi ON p.product = bdi.material
LEFT JOIN billing_document_headers bdh ON bdi.billing_document = bdh.billing_document
WHERE bdh.billing_doc_is_cancelled = 0  -- only active invoices
GROUP BY p.product
```

**Output example:**
```
Product S8907367008620 (FACESERUM 30ML VIT C) appeared in 11 active 
billing documents with a total net revenue of INR 3,100.17.
```

**Formatting rules:**
- Currency amounts in INR with 2 decimals: `INR 17,108.25`
- Dates in `YYYY-MM-DD` format as-is
- Business language (no SQL jargon)
- Keep under 300 words (unless data requires more)
- For lists: numbered or table format
- For flows: step-by-step chain visualization

---

### 9. **Graph Highlighting** (`graph_highlighter.py`)

Extracts entity IDs from query results and derives relationships.

**What it does:**
1. **Scans result rows** for columns matching entity ID patterns
   - `sales_order` → `sales_order` (entity type)
   - `delivery_document` → `delivery`
   - `billing_document` → `billing_document`
   - `customer` / `sold_to_party` → `customer`
   - Etc.

2. **Derives edges** between entities appearing together
   - If a row contains both `sales_order_id` and `delivery_document_id`, edge = (sales_order → delivery)
   - Only valid O2C flow edges are emitted

3. **Output format:**
```python
highlight_nodes: [
    {"id": "740509", "type": "sales_order", "label": "Sales Order 740509"},
    {"id": "80738040", "type": "delivery", "label": "Delivery 80738040"},
    {"id": "90504204", "type": "billing_document", "label": "Billing Doc 90504204"},
]

highlight_edges: [
    {"source": "740509", "target": "80738040", 
     "source_type": "sales_order", "target_type": "delivery"},
    {"source": "80738040", "target": "90504204", 
     "source_type": "delivery", "target_type": "billing_document"},
]
```

**Valid O2C flow edges:**
```
customer → sales_order
sales_order → delivery
delivery → billing_document
billing_document → journal_entry
billing_document → payment
customer → billing_document
product → sales_order
product → billing_document
plant → delivery
```

---

## Complete Request Flow Example

**User asks:** *"Show me the top 5 customers by total billing amount in 2024"*

### Step 1: Memory Resolution
- No pronouns/references → query passes through unchanged
- `resolved_query = "Show me the top 5 customers by total billing amount in 2024"`

### Step 2: Classification
```
🔍 Classifier LLM decides:
   ✅ intent = "domain" (clearly about O2C data)
   ✅ retrieval_mode = "sql" (specific aggregation, no fuzzy entity discovery)
```

### Step 3: Routing
- Classifier sets `retrieval_mode = "sql"`
- Router directs to `planner_node` (not semantic or hybrid)

### Step 4: Query Planning
```
Planner LLM generates:

1. Join tables: billing_document_headers with business_partners
2. Filter active billing docs: billing_doc_is_cancelled = 0
3. Filter 2024: billing_document_date BETWEEN '2024-01-01' AND '2024-12-31'
4. GROUP BY customer (sold_to_party)
5. SELECT customer_name, SUM(total_net_amount)
6. ORDER BY total_net_amount DESC
7. LIMIT 5
```

### Step 5: SQL Generation
```
Generator LLM produces:

SELECT 
    bp.business_partner_full_name AS customer_name,
    SUM(bdh.total_net_amount) AS total_amount
FROM billing_document_headers bdh
LEFT JOIN business_partners bp ON bdh.sold_to_party = bp.customer
WHERE bdh.billing_doc_is_cancelled = 0
  AND SUBSTR(bdh.billing_document_date, 1, 4) = '2024'
GROUP BY bdh.sold_to_party
ORDER BY total_amount DESC
LIMIT 5
```

### Step 6: Execution
```
db_executor.execute_sql(sql_query) returns:

[
  {"customer_name": "Acme Corp", "total_amount": 1205300.50},
  {"customer_name": "GlobalTech Inc", "total_amount": 980200.75},
  {"customer_name": "Summit Industries", "total_amount": 875400.25},
  {"customer_name": "Apex Solutions", "total_amount": 725100.00},
  {"customer_name": "Zenith Enterprises", "total_amount": 650300.10},
]
```

### Step 7: Answer Writing
```
LLM synthesizes grounded response using:
- Original query: "Show me top 5 customers by total billing amount in 2024"
- SQL query: (shows filters, joins, aggregation)
- Results: (actual rows)
- Semantic context: (entity metadata if helpful)

Output:
"In 2024, the top 5 customers by total billing amount were:

1. Acme Corp — INR 1,205,300.50
2. GlobalTech Inc — INR 980,200.75
3. Summit Industries — INR 875,400.25
4. Apex Solutions — INR 725,100.00
5. Zenith Enterprises — INR 650,300.10

These figures represent the total net amount of active (non-cancelled) 
billing documents issued to each customer."
```

### Step 8: Graph Highlighting
```
Highlighter scans results. No entity IDs in the result rows 
(only aggregated customer names), so:

highlight_nodes = []
highlight_edges = []
```

(Note: If results included detailed order/delivery/payment IDs, 
those would be extracted and edges derived.)

---

## Request Types & Routing

### Type 1: SQL (Structured Queries)
**Triggers:** Specific IDs, aggregations, status filters, date ranges, comparisons
**Example:** 
- "Total revenue by customer for Q4?"
- "How many orders are pending delivery for customer 320000083?"
- "Which products were delivered but not invoiced?"

**Path:** Memory → Classify(sql) → Planner → SQL Gen → Executor → Answer

---

### Type 2: Semantic (Fuzzy Discovery)
**Triggers:** Vague product/customer descriptions, exploratory browsing
**Example:**
- "Products similar to vitamin C serum"
- "Find customers in the cosmetics industry"

**Path:** Memory → Classify(semantic) → Semantic Search → Answer

**Process:**
1. ChromaDB query with entity type filter
2. Similarity scoring (0.0 = unrelated, 1.0 = identical)
3. Surface top matches with metadata

---

### Type 3: Hybrid (Discovery + Precision)
**Triggers:** Mix of fuzzy entity discovery + aggregation
**Example:**
- "Total revenue from customers who bought sunscreen products?"
- "How many orders for luxury brands are still pending delivery?"

**Path:** Memory → Classify(hybrid) → Semantic Search + SQL Query → Combine Results → Answer

**Process:**
1. Semantic search finds matching entities (products/customers)
2. Construct WHERE clause with discovered IDs
3. Run SQL query with those constraints
4. Combine semantic context + precise results

---

### Type 4: Off-Topic
**Triggers:** Questions not about O2C data
**Example:**
- "What's the square root of 144?"
- "Tell me about the stock market"

**Path:** Memory → Classify(off_topic) → Answer Node (guardrail response)

**Output:** 
```
"I'm specialized for Order-to-Cash data queries. Please ask me about 
sales orders, deliveries, billing, or payments in the SAP O2C system."
```

---

## Key Design Principles

### 1. **Grounding in SQL**
The final answer is never just data—it's **data + the exact SQL that produced it**.
This allows the LLM to accurately explain what filters, joins, and aggregations were applied.

### 2. **Two-Step Query Generation**
Plan → SQL (not direct NL → SQL) significantly improves quality by letting the planner 
reason about the schema before committing to syntax.

### 3. **ChromaDB for Metadata-Aware Search**
Unlike FAISS (vectors + separate list), ChromaDB stores metadata WITH each vector, 
enabling typed filters like "find products by type AND customer scope".

### 4. **Safety-First Database Access**
- Read-only connections
- Hard row limits
- Query timeouts
- Statement validation
Prevents accidental data loss or runaway queries.

### 5. **Hybrid Retrieval as First-Class Citizen**
SQL handles precision (aggregations, exact IDs). Semantic handles discovery (descriptions, fuzzy matching). 
Hybrid combines both for complex real-world questions.

### 6. **Conversation Memory for Context**
Last 6 turns kept in state. LLM resolves pronouns on each new query so follow-ups are 
always fully self-contained.

### 7. **Entity Relationship Graphs for UI**
Extract entity IDs from results + derive edges. Frontend lights up relevant subgraph 
to show how documents flow through the business process.

---

## Code Structure Summary

```
backend/
├── ingest.py                    # Data pipeline: JSONL → SQLite
├── db_executor.py               # Safe read-only query execution
├── graph_highlighter.py          # Extract entities & edges for UI
│
├── graph/
│   ├── graph.py                 # LangGraph orchestrator
│   ├── nodes.py                 # Individual node functions
│   └── state.py                 # GraphState TypedDict
│
├── llm/
│   ├── client.py                # Google Gemini API wrapper
│   ├── classifier.py            # Intent classification & mode routing
│   ├── memory.py                # Pronoun resolution
│   ├── planner.py               # NL → query plan
│   ├── sql_generator.py         # Query plan → SQL
│   ├── answer_writer.py         # SQL results → natural language
│   └── prompts.py               # DB schema definitions
│
├── search/
│   ├── semantic.py              # ChromaDB vector index & search
│   └── hybrid.py                # Combines semantic + SQL
│
├── sap-order-to-cash-dataset/   # Raw JSONL data (19 entity types)
└── o2c.db                       # SQLite database (built by ingest.py)
```

---

## Dependencies

- **LangGraph**: Graph-based state machine orchestration
- **Google GenAI SDK**: LLM calls (Gemini)
- **ChromaDB**: Vector search with metadata filtering
- **sentence-transformers**: Embedding model (`all-MiniLM-L6-v2`)
- **SQLite3**: Database (built-in)
- **Pydantic**: Response validation

---

## Future Enhancements

1. **Caching**: Memoize semantic searches and common SQL patterns
2. **Explainability**: Deeper reasoning traces with intermediate steps shown to user
3. **Multi-turn clarifications**: Ask user for disambiguation before committing to query
4. **Custom metrics**: Define business KPIs as reusable query templates
5. **Data lineage**: Track which raw JSONL records fed into final answer
6. **Schema evolution**: Handle incremental data updates without rebuild
