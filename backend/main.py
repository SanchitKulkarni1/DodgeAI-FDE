"""
main.py — FastAPI Orchestration Layer
DodgeAI FDE: Graph-Based O2C Query System

Exposes the LangGraph pipeline via HTTP endpoints.
Run with: uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Internal modules (your architecture) ─────────────────────────────────────
from graph.graph import build_graph          # LangGraph compiled graph
from graph.state import GraphState           # TypedDict for shared state
from db_executor import get_executor         # Safe read-only SQL runner
from search.semantic import SemanticIndex    # ChromaDB wrapper
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


# ── App-level singletons ─────────────────────────────────────────────────────
_graph = None          # compiled LangGraph app
_semantic_index = None # ChromaDB semantic index
_db_executor = None    # SQLite read-only executor


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup / shutdown lifecycle.
    Heavy objects (graph, vector index, db connection) are built ONCE at boot.
    """
    global _graph, _semantic_index, _db_executor

    logger.info("🚀  Booting DodgeAI FDE backend …")

    # 1. Compile LangGraph orchestrator
    logger.info("  ↳ Compiling LangGraph pipeline …")
    _graph = build_graph()

    # 2. Initialise semantic search (ChromaDB + sentence-transformers)
    logger.info("  ↳ Loading ChromaDB semantic index …")
    _semantic_index = SemanticIndex()          # loads persisted ./chroma_store

    # 3. Open read-only SQLite connection
    logger.info("  ↳ Opening read-only SQLite connection …")
    _db_executor = get_executor()

    logger.info("✅  Startup complete — ready to accept requests.")
    yield

    # ── Shutdown ──
    logger.info("🛑  Shutting down — releasing resources …")
    if _db_executor:
        _db_executor.close()


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="DodgeAI FDE — O2C Query API",
    description=(
        "Natural-language query interface over SAP Order-to-Cash data. "
        "Powered by LangGraph, Gemini, ChromaDB, and SQLite."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response schemas ────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Natural language question")
    conversation_history: list[str] = Field(
        default_factory=list,
        description="Previous turns as alternating [user, assistant, user, …] strings (last 6 kept)",
    )


class HighlightNode(BaseModel):
    id: str
    type: str
    label: str


class HighlightEdge(BaseModel):
    source: str
    target: str
    source_type: str
    target_type: str


class QueryResponse(BaseModel):
    answer: str = Field(description="Natural language answer grounded in data")
    retrieval_mode: str = Field(description="sql | semantic | hybrid | off_topic")
    query_plan: dict | None = Field(None, description="Structured query plan (SQL path only) - JSON dict from QueryPlan Pydantic model")
    sql_query: str | None = Field(None, description="Executed SQL statement (SQL / hybrid path)")
    highlight_nodes: list[HighlightNode] = Field(default_factory=list)
    highlight_edges: list[HighlightEdge] = Field(default_factory=list)
    latency_ms: float = Field(description="End-to-end wall-clock time in milliseconds")
    error: str | None = Field(None, description="Set if a recoverable error occurred")


# ── Helper ────────────────────────────────────────────────────────────────────

def _build_initial_state(req: QueryRequest) -> GraphState:
    """Convert the HTTP request into an initial LangGraph state dict."""
    return GraphState(
        user_query=req.query,
        conversation_history=req.conversation_history[-12:],  # keep ≤ 6 turns (12 messages)
        resolved_query="",
        intent="",
        retrieval_mode="",
        query_plan=None,
        sql_query=None,
        query_result=[],
        semantic_results=[],
        final_answer="",
        highlight_nodes=[],
        highlight_edges=[],
        error=None,
    )


def _parse_response(state: GraphState, latency_ms: float) -> QueryResponse:
    """Map the final LangGraph state to the HTTP response schema."""
    nodes = [HighlightNode(**n) for n in (state.get("highlight_nodes") or [])]
    edges = [HighlightEdge(**e) for e in (state.get("highlight_edges") or [])]

    # Convert QueryPlan object to dict for JSON serialization
    query_plan = state.get("query_plan")
    query_plan_dict = None
    if query_plan:
        # If it's a Pydantic QueryPlan object, convert to dict
        if hasattr(query_plan, "model_dump"):
            query_plan_dict = query_plan.model_dump()
        else:
            # If it's already a dict or string, keep as is
            query_plan_dict = query_plan

    return QueryResponse(
        answer=state.get("final_answer") or "No answer generated.",
        retrieval_mode=state.get("retrieval_mode") or "unknown",
        query_plan=query_plan_dict,
        sql_query=state.get("sql_query"),
        highlight_nodes=nodes,
        highlight_edges=edges,
        latency_ms=round(latency_ms, 2),
        error=state.get("error"),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    """Health-check / welcome endpoint."""
    return {"status": "ok", "service": "DodgeAI FDE O2C Query API", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    """Detailed readiness probe — confirms graph + DB are loaded."""
    return {
        "status": "ok",
        "graph_loaded": _graph is not None,
        "semantic_index_loaded": _semantic_index is not None,
        "db_executor_ready": _db_executor is not None,
    }


@app.post("/query", response_model=QueryResponse, tags=["Query"])
async def query(req: QueryRequest):
    """
    Main entry-point.  Runs the full LangGraph orchestration pipeline:

      Memory → Classify → Route → (SQL | Semantic | Hybrid | Off-topic)
                                        → Answer Writer → Graph Highlighter

    Returns a grounded natural-language answer plus optional graph highlights.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialised — server still starting up.")

    logger.info("📥  query=%r  history_turns=%d", req.query, len(req.conversation_history) // 2)

    t0 = time.perf_counter()
    try:
        initial_state = _build_initial_state(req)
        final_state: GraphState = await _graph.ainvoke(initial_state)   # async invoke
    except Exception as exc:
        logger.exception("Pipeline error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc

    latency_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "📤  mode=%s  latency=%.0fms  nodes=%d  edges=%d",
        final_state.get("retrieval_mode", "?"),
        latency_ms,
        len(final_state.get("highlight_nodes") or []),
        len(final_state.get("highlight_edges") or []),
    )

    return _parse_response(final_state, latency_ms)


@app.post("/query/sync", response_model=QueryResponse, tags=["Query"])
async def query_sync(req: QueryRequest):
    """
    Synchronous-style wrapper (same pipeline, kept for clients that prefer it).
    Uses graph.invoke instead of ainvoke internally — useful for local testing.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialised.")

    t0 = time.perf_counter()
    try:
        initial_state = _build_initial_state(req)
        final_state: GraphState = _graph.invoke(initial_state)
    except Exception as exc:
        logger.exception("Sync pipeline error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc

    latency_ms = (time.perf_counter() - t0) * 1000
    return _parse_response(final_state, latency_ms)


@app.get("/schema", tags=["Meta"])
async def schema():
    """
    Returns the 19 O2C table names the system knows about.
    Useful for frontend schema exploration or debugging prompts.
    """
    tables = [
        "sales_order_headers", "sales_order_items", "sales_order_schedule_lines",
        "outbound_delivery_headers", "outbound_delivery_items",
        "billing_document_headers", "billing_document_items", "billing_document_cancellations",
        "journal_entry_items_accounts_receivable",
        "payments_ar",
        "business_partners", "business_partner_addresses",
        "customer_company_assignments", "customer_sales_area_assignments",
        "products", "product_descriptions", "product_plants", "product_storage_locations",
        "plants",
    ]
    return {"table_count": len(tables), "tables": tables}


@app.get("/graph/nodes", tags=["Graph"])
async def graph_nodes():
    """
    Returns the valid O2C entity types that can appear as graph nodes.
    The frontend uses this to seed the visualisation canvas.
    """
    entity_types = [
        {"type": "customer",          "label": "Customer",          "color": "#4A90E2"},
        {"type": "sales_order",       "label": "Sales Order",       "color": "#7ED321"},
        {"type": "delivery",          "label": "Delivery",          "color": "#F5A623"},
        {"type": "billing_document",  "label": "Billing Document",  "color": "#D0021B"},
        {"type": "journal_entry",     "label": "Journal Entry",     "color": "#9B59B6"},
        {"type": "payment",           "label": "Payment",           "color": "#1ABC9C"},
        {"type": "product",           "label": "Product",           "color": "#E67E22"},
        {"type": "plant",             "label": "Plant",             "color": "#95A5A6"},
    ]
    return {"entity_types": entity_types}


@app.get("/graph/edges", tags=["Graph"])
async def graph_edges():
    """
    Returns the valid O2C flow edge types.
    Used by the frontend to define allowed relationships in the graph visualisation.
    """
    valid_edges = [
        {"source_type": "customer",         "target_type": "sales_order",      "label": "placed"},
        {"source_type": "sales_order",      "target_type": "delivery",         "label": "fulfilled_by"},
        {"source_type": "delivery",         "target_type": "billing_document", "label": "billed_in"},
        {"source_type": "billing_document", "target_type": "journal_entry",    "label": "posted_to"},
        {"source_type": "billing_document", "target_type": "payment",          "label": "cleared_by"},
        {"source_type": "customer",         "target_type": "billing_document", "label": "billed"},
        {"source_type": "product",          "target_type": "sales_order",      "label": "ordered_in"},
        {"source_type": "product",          "target_type": "billing_document", "label": "invoiced_in"},
        {"source_type": "plant",            "target_type": "delivery",         "label": "ships_from"},
    ]
    return {"edge_count": len(valid_edges), "edges": valid_edges}


@app.post("/graph/expand", tags=["Graph"])
async def expand_node(node_id: str, node_type: str):
    """
    Expands a single node by finding related instances in the database.
    
    For example, expanding a 'customer' node will find related sales_orders,
    and expanding a 'sales_order' will find related deliveries, billings, etc.
    
    Returns new nodes (instances) and edges connecting them.
    """
    if not _db_executor:
        raise HTTPException(status_code=503, detail="Database executor not initialised.")
    
    # Mapping of entity type to table and ID column
    entity_table_map = {
        "customer": ("business_partners", "customer"),
        "sales_order": ("sales_order_headers", "sales_order"),
        "delivery": ("outbound_delivery_headers", "delivery_document"),
        "billing_document": ("billing_document_headers", "billing_document"),
        "journal_entry": ("journal_entry_items_ar", "accounting_document"),
        "payment": ("payments_ar", "accounting_document"),
        "product": ("products", "product"),
        "plant": ("plants", "plant"),
    }
    
    # Known relationships for expansion (table1 -> table2 with join condition)
    expansion_paths = {
        "customer": [  # From customer, expand to sales orders, billing docs
            {
                "target_type": "sales_order",
                "target_table": "sales_order_headers",
                "target_id_col": "sales_order",
                "join": "sales_order_headers.sold_to_party = business_partners.customer",
                "relationship": "placed"
            },
            {
                "target_type": "billing_document",
                "target_table": "billing_document_headers",
                "target_id_col": "billing_document",
                "join": "billing_document_headers.sold_to_party = business_partners.customer",
                "relationship": "billed"
            }
        ],
        "sales_order": [  # From sales order, expand to delivery & products
            {
                "target_type": "delivery",
                "target_table": "outbound_delivery_headers",
                "target_id_col": "delivery_document",
                "join": "outbound_delivery_headers.delivery_document IN (SELECT delivery_document FROM outbound_delivery_items WHERE reference_sd_document = sales_order_headers.sales_order)",
                "relationship": "fulfilled_by"
            },
            {
                "target_type": "product",
                "target_table": "products",
                "target_id_col": "product",
                "join": "products.product IN (SELECT material FROM sales_order_items WHERE sales_order = sales_order_headers.sales_order)",
                "relationship": "ordered_in"
            }
        ],
        "delivery": [  # From delivery, expand to billing & billing docs
            {
                "target_type": "billing_document",
                "target_table": "billing_document_headers",
                "target_id_col": "billing_document",
                "join": "billing_document_headers.billing_document IN (SELECT billing_document FROM billing_document_items WHERE reference_sd_document IN (SELECT delivery_document FROM outbound_delivery_items WHERE delivery_document = outbound_delivery_headers.delivery_document))",
                "relationship": "billed_in"
            }
        ],
        "billing_document": [  # From billing doc, expand to journal entries & payments
            {
                "target_type": "journal_entry",
                "target_table": "journal_entry_items_ar",
                "target_id_col": "journal_entry_item",
                "join": "journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document",
                "relationship": "posted_to"
            },
            {
                "target_type": "payment",
                "target_table": "payments_ar",
                "target_id_col": "accounting_document",
                "join": "payments_ar.clearing_accounting_document = billing_document_headers.accounting_document",
                "relationship": "cleared_by"
            }
        ]
    }
    
    if node_type not in entity_table_map:
        raise HTTPException(status_code=400, detail=f"Unknown node type: {node_type}")
    
    source_table, source_id_col = entity_table_map[node_type]
    expanded_nodes = []
    expanded_edges = []
    
    try:
        # Get expansion paths for this node type
        paths = expansion_paths.get(node_type, [])
        
        for path in paths:
            target_table = path["target_table"]
            target_id_col = path["target_id_col"]
            target_type = path["target_type"]
            relationship = path["relationship"]
            
            # Build simplified query to get related instances (limit to 5 to avoid explosion)
            try:
                # Simplified JOIN-based query
                if "IN (SELECT" in path["join"]:
                    # Handle complex subquery joins by selecting from target table
                    sql = f"""
                    SELECT DISTINCT {target_id_col} as id 
                    FROM {target_table} 
                    LIMIT 5
                    """
                else:
                    # Simple join
                    sql = f"""
                    SELECT DISTINCT {target_table}.{target_id_col} as id
                    FROM {source_table}
                    INNER JOIN {target_table} ON {path["join"]}
                    WHERE {source_table}.{source_id_col} = '{node_id}'
                    LIMIT 5
                    """
                
                results = _db_executor.execute(sql)
                
                # Add found nodes and edges
                for row in results:
                    target_id = row.get("id")
                    if target_id:
                        expanded_nodes.append({
                            "id": str(target_id),
                            "type": target_type,
                            "label": f"{target_type.replace('_', ' ').title()}",
                        })
                        expanded_edges.append({
                            "source": node_id,
                            "target": str(target_id),
                            "source_type": node_type,
                            "target_type": target_type,
                            "label": relationship
                        })
            except Exception as e:
                logger.warning("Failed to expand via path %s: %s", path, e)
                continue
        
        return {
            "nodes": expanded_nodes,
            "edges": expanded_edges,
            "source_node_id": node_id,
            "source_node_type": node_type,
        }
    
    except Exception as e:
        logger.exception("Error expanding node %s (type=%s): %s", node_id, node_type, e)
        raise HTTPException(status_code=500, detail=f"Error expanding node: {e}")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error": str(exc)})