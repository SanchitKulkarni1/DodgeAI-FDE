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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error": str(exc)})