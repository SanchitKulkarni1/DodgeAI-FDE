"""
search/semantic.py — ChromaDB-backed vector similarity search over O2C entities.

Why ChromaDB over FAISS
-----------------------
FAISS is a vector array + a parallel Python list. It has no concept of
metadata — you maintain a separate list of dicts and pray the indices stay
in sync. It can't filter by entity type, customer ID, or any other field
before doing similarity search, and it has no persistence.

ChromaDB stores each vector WITH its structured metadata as a native document:

    {
        "id":        "product_S8907367008620",
        "embedding": [...384 floats...],
        "document":  "FACESERUM 30ML VIT C ABC-WEB-...",   <- searchable text
        "metadata":  {
            "type":          "product",
            "entity_id":     "S8907367008620",
            "label":         "FACESERUM 30ML VIT C",
            "product_group": "ZFG1001",
            ...
        }
    }

This lets the hybrid search layer do typed pre-filtering:
    collection.query(
        query_texts=["face serum"],
        where={"type": "product"},
        n_results=10
    )

Or scoped to a specific customer:
    collection.query(
        query_texts=["unpaid invoice"],
        where={"$and": [{"type": "billing_document"}, {"customer_id": "320000083"}]},
        n_results=5
    )

ChromaDB persists the index to disk (./chroma_store/) so build_index() only
needs to run once per data load, not on every app start.

Entity types and their metadata keys
-------------------------------------
  product          -> type, entity_id, label, product_group, product_type,
                      division, industry_sector, gross_weight, net_weight
  customer         -> type, entity_id, label, customer_id, region, city,
                      country, payment_terms, is_blocked
  plant            -> type, entity_id, label, sales_organization, distribution_channel
  sales_order      -> type, entity_id, label, customer_id, customer_name,
                      delivery_status, total_amount, currency, creation_date,
                      billing_blocked, delivery_blocked
  billing_document -> type, entity_id, label, customer_id, customer_name,
                      total_amount, billing_date, fiscal_year, is_cancelled
  delivery         -> type, entity_id, label, customer_id, customer_name,
                      sales_order_id, goods_movement_status, picking_status
  payment          -> type, entity_id, label, customer_id, customer_name,
                      amount, currency, clearing_date, billing_document_id

Dependencies:
    pip install chromadb sentence-transformers
"""

import logging
import sqlite3
from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

log = logging.getLogger(__name__)

_DB_PATH     = Path("o2c.db")
_CHROMA_PATH = "./chroma_store"
_COLLECTION  = "o2c_entities"
_EMBED_MODEL = "all-MiniLM-L6-v2"

# Module-level cache
_client     = None
_collection = None


# ---------------------------------------------------------------------------
# Type coercion helpers — ChromaDB metadata accepts str, int, float, bool only
# ---------------------------------------------------------------------------

def _s(v) -> str:
    return str(v) if v is not None else ""

def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0

def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    return False


# ---------------------------------------------------------------------------
# Client initialisation
# ---------------------------------------------------------------------------

def _get_client_and_collection():
    """Return cached ChromaDB client + collection, initialising if needed."""
    global _client, _collection

    if _client is not None and _collection is not None:
        return _client, _collection

    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    embed_fn = SentenceTransformerEmbeddingFunction(model_name=_EMBED_MODEL)

    _client = chromadb.PersistentClient(path=_CHROMA_PATH)
    _collection = _client.get_or_create_collection(
        name=_COLLECTION,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    log.info(
        "[semantic] ChromaDB collection '%s' ready — %d docs",
        _COLLECTION, _collection.count(),
    )
    return _client, _collection


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def build_index() -> None:
    """
    Build (or rebuild) the ChromaDB vector index from SQLite.

    Deletes the existing collection first so stale documents never accumulate.
    Call once explicitly at app startup:
        from search.semantic import build_index
        build_index()
    """
    

    embed_fn = SentenceTransformerEmbeddingFunction(model_name=_EMBED_MODEL)
    client   = chromadb.PersistentClient(path=_CHROMA_PATH)

    # Clean rebuild
    try:
        client.delete_collection(_COLLECTION)
        log.info("[semantic] old collection deleted")
    except Exception:
        pass

    collection = client.create_collection(
        name=_COLLECTION,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    con = sqlite3.connect(_DB_PATH)
    ids, documents, metadatas = [], [], []

    def _flush():
        if not ids:
            return
        collection.add(ids=list(ids), documents=list(documents), metadatas=list(metadatas))
        log.info("[semantic] flushed %d docs to ChromaDB", len(ids))
        ids.clear(); documents.clear(); metadatas.clear()

    # ── 1. Products ──────────────────────────────────────────────────────────
    # FIX #10: Import category inference to enrich product embeddings
    from search.taxonomy import infer_category

    rows = con.execute("""
        SELECT p.product, pd.product_description, p.product_old_id,
               p.product_group, p.product_type, p.division,
               p.industry_sector, p.base_unit, p.gross_weight, p.net_weight
        FROM products p
        LEFT JOIN product_descriptions pd
               ON p.product = pd.product AND pd.language = 'EN'
    """).fetchall()

    for product, desc, old_id, group, ptype, division, sector, unit, gw, nw in rows:
        # FIX #10: Enrich embedding text with inferred category for better
        # semantic matching on queries like "skincare products"
        category = infer_category(desc) if desc else None
        text_parts = [desc, old_id, group, product]
        if category:
            text_parts.append(category)  # e.g. "skincare", "haircare"
        text = " ".join(filter(None, text_parts))

        ids.append(f"product_{product}")
        documents.append(text)
        metadatas.append({
            "type":             "product",
            "entity_id":        _s(product),
            "label":            _s(desc or old_id or product),
            "product_group":    _s(group),
            "product_type":     _s(ptype),
            "product_category": _s(category or ""),  # FIX #10: category metadata
            "division":         _s(division),
            "industry_sector":  _s(sector),
            "base_unit":        _s(unit),
            "gross_weight":     _f(gw),
            "net_weight":       _f(nw),
        })
    _flush()

    # ── 2. Customers ─────────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT bp.customer, bp.business_partner_full_name,
               bp.is_blocked, bp.business_partner_grouping,
               bpa.city_name, bpa.region, bpa.country, bpa.postal_code,
               csa.customer_payment_terms, csa.shipping_condition,
               csa.incoterms_classification
        FROM business_partners bp
        LEFT JOIN business_partner_addresses bpa
               ON bp.business_partner = bpa.business_partner
        LEFT JOIN customer_sales_area_assignments csa
               ON bp.customer = csa.customer
        GROUP BY bp.customer
    """).fetchall()

    for customer, name, blocked, grouping, city, region, country, postal, \
        payment_terms, shipping, incoterms in rows:
        text = " ".join(filter(None, [name, city, region, country, customer]))
        ids.append(f"customer_{customer}")
        documents.append(text)
        metadatas.append({
            "type":          "customer",
            "entity_id":     _s(customer),
            "label":         _s(name or customer),
            "customer_id":   _s(customer),
            "region":        _s(region),
            "city":          _s(city),
            "country":       _s(country),
            "postal_code":   _s(postal),
            "payment_terms": _s(payment_terms),
            "shipping":      _s(shipping),
            "incoterms":     _s(incoterms),
            "is_blocked":    _b(blocked),
            "grouping":      _s(grouping),
        })
    _flush()

    # ── 3. Plants ────────────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT plant, plant_name, sales_organization,
               distribution_channel, division, factory_calendar
        FROM plants
    """).fetchall()

    for plant, name, sales_org, dist_ch, division, calendar in rows:
        text = " ".join(filter(None, [name, plant, sales_org]))
        ids.append(f"plant_{plant}")
        documents.append(text)
        metadatas.append({
            "type":                 "plant",
            "entity_id":            _s(plant),
            "label":                _s(name or plant),
            "sales_organization":   _s(sales_org),
            "distribution_channel": _s(dist_ch),
            "division":             _s(division),
            "factory_calendar":     _s(calendar),
        })
    _flush()

    # ── 4. Sales Orders ──────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT soh.sales_order, soh.sales_order_type,
               soh.sold_to_party, bp.business_partner_full_name,
               soh.creation_date, soh.total_net_amount, soh.transaction_currency,
               soh.overall_delivery_status, soh.requested_delivery_date,
               soh.sales_organization, soh.header_billing_block,
               soh.delivery_block_reason
        FROM sales_order_headers soh
        LEFT JOIN business_partners bp ON soh.sold_to_party = bp.customer
    """).fetchall()

    for (so, so_type, cust_id, cust_name, created, amount, currency,
         del_status, req_date, sales_org, bill_block, del_block) in rows:
        text = (
            f"Sales Order {so} customer {cust_name or cust_id} "
            f"amount {amount or ''} {currency or ''} "
            f"created {created or ''} delivery status {del_status or ''}"
        )
        ids.append(f"sales_order_{so}")
        documents.append(text)
        metadatas.append({
            "type":             "sales_order",
            "entity_id":        _s(so),
            "label":            f"Sales Order {so}",
            "customer_id":      _s(cust_id),
            "customer_name":    _s(cust_name),
            "sales_order_type": _s(so_type),
            "delivery_status":  _s(del_status),
            "total_amount":     _f(amount),
            "currency":         _s(currency),
            "creation_date":    _s(created),
            "requested_date":   _s(req_date),
            "sales_org":        _s(sales_org),
            "billing_blocked":  _b(bill_block),
            "delivery_blocked": _b(del_block),
        })
    _flush()

    # ── 5. Billing Documents (active only) ───────────────────────────────────
    rows = con.execute("""
        SELECT bdh.billing_document, bdh.billing_document_type,
               bdh.sold_to_party, bp.business_partner_full_name,
               bdh.total_net_amount, bdh.billing_document_date,
               bdh.billing_doc_is_cancelled, bdh.fiscal_year,
               bdh.company_code, bdh.accounting_document
        FROM billing_document_headers bdh
        LEFT JOIN business_partners bp ON bdh.sold_to_party = bp.customer
        WHERE bdh.billing_doc_is_cancelled = FALSE
    """).fetchall()

    for (bd, bd_type, cust_id, cust_name, amount, bd_date,
         cancelled, fiscal_year, company_code, acct_doc) in rows:
        text = (
            f"Billing Document Invoice {bd} {bd_type or ''} "
            f"customer {cust_name or cust_id} "
            f"amount {amount or ''} date {bd_date or ''} "
            f"fiscal year {fiscal_year or ''}"
        )
        ids.append(f"billing_document_{bd}")
        documents.append(text)
        metadatas.append({
            "type":                  "billing_document",
            "entity_id":             _s(bd),
            "label":                 f"Billing Document {bd}",
            "customer_id":           _s(cust_id),
            "customer_name":         _s(cust_name),
            "billing_document_type": _s(bd_type),
            "total_amount":          _f(amount),
            "billing_date":          _s(bd_date),
            "fiscal_year":           _s(fiscal_year),
            "company_code":          _s(company_code),
            "accounting_document":   _s(acct_doc),
            "is_cancelled":          False,
        })
    _flush()

    # ── 6. Deliveries ────────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT odh.delivery_document, odh.shipping_point,
               odh.creation_date, odh.overall_goods_movement_status,
               odh.overall_picking_status, odh.actual_goods_movement_date,
               odi.reference_sd_document,
               bp.business_partner_full_name,
               soh.sold_to_party
        FROM outbound_delivery_headers odh
        LEFT JOIN outbound_delivery_items odi
               ON odh.delivery_document = odi.delivery_document
        LEFT JOIN sales_order_headers soh
               ON odi.reference_sd_document = soh.sales_order
        LEFT JOIN business_partners bp
               ON soh.sold_to_party = bp.customer
        GROUP BY odh.delivery_document
    """).fetchall()

    for (dd, shipping_pt, created, gm_status, pick_status,
         gm_date, so_ref, cust_name, cust_id) in rows:
        text = (
            f"Delivery {dd} for Sales Order {so_ref or ''} "
            f"customer {cust_name or ''} "
            f"goods movement {gm_status or ''} picking {pick_status or ''} "
            f"created {created or ''}"
        )
        ids.append(f"delivery_{dd}")
        documents.append(text)
        metadatas.append({
            "type":                  "delivery",
            "entity_id":             _s(dd),
            "label":                 f"Delivery {dd}",
            "customer_id":           _s(cust_id),
            "customer_name":         _s(cust_name),
            "sales_order_id":        _s(so_ref),
            "goods_movement_status": _s(gm_status),
            "picking_status":        _s(pick_status),
            "creation_date":         _s(created),
            "goods_movement_date":   _s(gm_date),
            "shipping_point":        _s(shipping_pt),
        })
    _flush()

    # ── 7. Payments ──────────────────────────────────────────────────────────
    rows = con.execute("""
        SELECT p.accounting_document, p.customer,
               bp.business_partner_full_name,
               p.clearing_date, p.amount_in_transaction_currency,
               p.transaction_currency, p.clearing_accounting_document,
               bdh.billing_document, p.posting_date
        FROM payments_ar p
        LEFT JOIN business_partners bp ON p.customer = bp.customer
        LEFT JOIN billing_document_headers bdh
               ON p.clearing_accounting_document = bdh.accounting_document
    """).fetchall()

    for (acct_doc, cust_id, cust_name, clearing_date, amount,
         currency, clearing_acct, billing_doc, posting_date) in rows:
        text = (
            f"Payment {acct_doc} customer {cust_name or cust_id} "
            f"amount {amount or ''} {currency or ''} "
            f"cleared {clearing_date or ''} for invoice {billing_doc or ''}"
        )
        ids.append(f"payment_{acct_doc}")
        documents.append(text)
        metadatas.append({
            "type":                "payment",
            "entity_id":           _s(acct_doc),
            "label":               f"Payment {acct_doc}",
            "customer_id":         _s(cust_id),
            "customer_name":       _s(cust_name),
            "amount":              _f(amount),
            "currency":            _s(currency),
            "clearing_date":       _s(clearing_date),
            "posting_date":        _s(posting_date),
            "billing_document_id": _s(billing_doc),
        })
    _flush()

    con.close()

    # Update module-level cache
    global _client, _collection
    _client     = client
    _collection = collection

    log.info(
        "[semantic] build complete — %d documents in '%s'",
        collection.count(), _COLLECTION,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def semantic_search(
    query: str,
    top_k: int = 10,
    entity_type: str | None = None,
    customer_id: str | None = None,
    where: dict | None = None,
) -> list[dict]:
    """
    Find the top_k most semantically similar entities to the query.

    Args:
        query:       Natural language search string.
        top_k:       Max results to return.
        entity_type: Pre-filter to one entity type before ANN search.
                     e.g. "product", "customer", "billing_document", "delivery"
        customer_id: Scope search to a specific customer's documents.
        where:       Raw ChromaDB where filter — overrides entity_type and
                     customer_id when provided. Supports ChromaDB operators:
                         {"$and": [{"type": "billing_document"}, {"customer_id": "320000083"}]}
                         {"total_amount": {"$gte": 1000}}
                         {"delivery_status": {"$eq": "A"}}

    Returns:
        List of dicts sorted by similarity score descending:
            {
                "entity_type": str,
                "entity_id":   str,
                "label":       str,
                "score":       float,   # 0.0 (unrelated) to 1.0 (identical)
                "metadata":    dict,    # full ChromaDB metadata doc
            }
    """
    _, collection = _get_client_and_collection()

    if collection.count() == 0:
        log.warning("[semantic] empty collection — running build_index()")
        build_index()
        _, collection = _get_client_and_collection()

    # Build where clause from convenience args
    if where is None:
        filters = []
        if entity_type:
            filters.append({"type": {"$eq": entity_type}})
        if customer_id:
            filters.append({"customer_id": {"$eq": customer_id}})

        if len(filters) == 1:
            where = filters[0]
        elif len(filters) > 1:
            where = {"$and": filters}

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(top_k, collection.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        log.error("[semantic] query failed: %s", e)
        return []

    ids_list       = results.get("ids",       [[]])[0]
    metadatas_list = results.get("metadatas", [[]])[0]
    distances_list = results.get("distances", [[]])[0]

    output = []
    for chroma_id, meta, distance in zip(ids_list, metadatas_list, distances_list):
        # ChromaDB cosine distance: 0 = identical, 2 = opposite
        # Convert to similarity: 1 = identical, 0 = unrelated
        score = max(0.0, 1.0 - (distance / 2.0))
        output.append({
            "entity_type": meta.get("type",      ""),
            "entity_id":   meta.get("entity_id", ""),
            "label":       meta.get("label",      ""),
            "score":       round(score, 4),
            "metadata":    meta,
        })

    output.sort(key=lambda x: x["score"], reverse=True)

    log.info(
        "[semantic] query=%r  filter=%r  top=%s (score=%.3f)",
        query[:60],
        where,
        output[0]["label"] if output else "none",
        output[0]["score"] if output else 0.0,
    )
    return output


# ---------------------------------------------------------------------------
# UI helper
# ---------------------------------------------------------------------------

def nodes_from_semantic_results(results: list[dict]) -> list[dict]:
    """
    Convert semantic_search() output to highlight_nodes format for the UI.

    Returns:
        List of {"id": str, "type": str, "label": str}
    """
    seen: dict[tuple, dict] = {}
    for r in results:
        key = (r["entity_id"], r["entity_type"])
        if key not in seen:
            seen[key] = {
                "id":    r["entity_id"],
                "type":  r["entity_type"],
                "label": r["label"],
            }
    return list(seen.values())


# ---------------------------------------------------------------------------
# SemanticIndex wrapper class
# ---------------------------------------------------------------------------

class SemanticIndex:
    """
    Stateful wrapper for ChromaDB semantic search functionality.
    Initialize once at app startup (in lifespan) and reuse across requests.
    """

    def __init__(self):
        """Initialize the semantic index, loading persisted ChromaDB."""
        self._client, self._collection = _get_client_and_collection()
        log.info("[SemanticIndex] initialized — collection '%s' ready", _COLLECTION)

    def search(
        self,
        query: str,
        top_k: int = 10,
        entity_type: str | None = None,
        customer_id: str | None = None,
        where: dict | None = None,
    ) -> list[dict]:
        """
        Wrapper around semantic_search().

        Args:
            query: Natural language search string.
            top_k: Max results to return.
            entity_type: Pre-filter to one entity type.
            customer_id: Scope search to a specific customer's documents.
            where: Raw ChromaDB where filter.

        Returns:
            List of dicts sorted by similarity score (descending).
        """
        return semantic_search(
            query=query,
            top_k=top_k,
            entity_type=entity_type,
            customer_id=customer_id,
            where=where,
        )

    def build_or_rebuild_index(self) -> None:
        """
        Rebuild the entire ChromaDB vector index from SQLite.
        Call this if you've ingested new data and need to refresh.
        """
        build_index()
        self._client, self._collection = _get_client_and_collection()
        log.info("[SemanticIndex] index rebuilt — collection count: %d", self._collection.count())

    def get_collection_count(self) -> int:
        """Return the current document count in the ChromaDB collection."""
        if self._collection:
            return self._collection.count()
        return 0