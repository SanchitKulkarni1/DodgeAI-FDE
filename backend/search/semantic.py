"""
search/semantic.py — ChromaDB-backed vector similarity search over O2C entities.


[docstring unchanged — omitted for brevity]


Dependencies:
    pip install chromadb google-genai pydantic-settings
"""


import logging
import sqlite3
from pathlib import Path
import os
from dotenv import load_dotenv


import chromadb
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings


from llm.client import gemini, MODEL, types


# Import Redis caching layer
import cache


# Load environment variables
load_dotenv()


log = logging.getLogger(__name__)


_DB_PATH     = Path("o2c.db")
_CHROMA_PATH = "./chroma_store"
_COLLECTION  = "o2c_entities"


# ChromaDB configuration: use cloud if enabled, otherwise local
_USE_CLOUD        = os.getenv("CHROMA_USE_CLOUD", "false").lower() == "true"
_CHROMA_API_KEY   = os.getenv("CHROMA_API_KEY")
_CHROMA_TENANT_ID = os.getenv("CHROMA_TENANT")        # ✅ FIX 1: was "CHROMA_TENANT_ID"
_CHROMA_DATABASE  = os.getenv("CHROMA_DATABASE", "dodgeai-o2c")

# Log configuration at module load
_config_msg = (
    f"[semantic] ChromaDB Configuration: "
    f"USE_CLOUD={_USE_CLOUD}, "
    f"API_KEY={'SET' if _CHROMA_API_KEY else 'MISSING'}, "
    f"TENANT={'SET' if _CHROMA_TENANT_ID else 'MISSING'}, "
    f"DATABASE={_CHROMA_DATABASE}"
)
# Will be logged after logger is configured


# Updated: old "models/embedding-001" was deprecated Jan 14 2026
_EMBED_MODEL = "gemini-embedding-001"



# ---------------------------------------------------------------------------
# Embedding function
# ---------------------------------------------------------------------------


class GeminiEmbeddingFunction(EmbeddingFunction):
    """
    ChromaDB-compatible embedding function backed by google-genai SDK.

    Uses the centralized gemini client from llm.client with automatic rate limit
    handling via GeminiRoundRobinClient.
    """

    def __init__(self):
        self._client = gemini

    def __call__(self, input: Documents) -> Embeddings:
        if not self._client:
            log.warning("[semantic] No Gemini client — returning zero vectors")
            return [[0.0] * 3072] * len(input)

        try:
            embeddings = []
            for text in input:
                response = self._client.models.embed_content(
                    model=_EMBED_MODEL,
                    contents=text,
                )
                if hasattr(response, 'embedding'):
                    embedding = response.embedding
                    if hasattr(embedding, 'values'):
                        embeddings.append(embedding.values)
                    else:
                        embeddings.append(embedding)
                elif hasattr(response, 'embeddings') and response.embeddings:
                    embedding_obj = response.embeddings[0]
                    if hasattr(embedding_obj, 'values'):
                        embeddings.append(embedding_obj.values)
                    else:
                        embeddings.append(embedding_obj)
                else:
                    log.warning("[semantic] Unexpected embedding response format")
                    embeddings.append([0.0] * 3072)

            return embeddings

        except Exception as e:
            log.error("[semantic] Gemini batch embed failed: %s", e)
            return [[0.0] * 3072] * len(input)



# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------


_client     = None
_collection = None



# ---------------------------------------------------------------------------
# Type coercion helpers
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

    # Log configuration on first initialization
    log.info(_config_msg)

    if _USE_CLOUD:
        if not _CHROMA_API_KEY or not _CHROMA_TENANT_ID:
            log.warning(
                "[semantic] CHROMA_USE_CLOUD=true but credentials missing. "
                "Falling back to local ChromaDB."
            )
            log.debug(
                "[semantic] Missing credentials: API_KEY=%s, TENANT_ID=%s",
                "SET" if _CHROMA_API_KEY else "MISSING",
                "SET" if _CHROMA_TENANT_ID else "MISSING"
            )
            _client = chromadb.PersistentClient(path=_CHROMA_PATH)
        else:
            try:
                # Log credentials before connection attempt
                log.info(
                    "[semantic] Attempting ChromaDB Cloud connection with: "
                    "api_key=%s..., tenant=%s, database=%s",
                    _CHROMA_API_KEY[:10] if _CHROMA_API_KEY else "NONE",
                    _CHROMA_TENANT_ID[:20] if _CHROMA_TENANT_ID else "NONE",
                    _CHROMA_DATABASE if _CHROMA_DATABASE else "NONE"
                )
                
                _client = chromadb.CloudClient(
                    api_key=_CHROMA_API_KEY,
                    tenant=_CHROMA_TENANT_ID,
                    database=_CHROMA_DATABASE,
                )
                log.info(
                    "[semantic] ✅ Connected to ChromaDB Cloud (tenant=%s..., db=%s)",
                    _CHROMA_TENANT_ID[:8], _CHROMA_DATABASE
                )
            except Exception as e:
                log.error(
                    "[semantic] ❌ CloudClient init failed: %s. "
                    "Details: api_key_set=%s, tenant_id_set=%s, database=%s. "
                    "Falling back to local.",
                    str(e),
                    "YES" if _CHROMA_API_KEY else "NO",
                    "YES" if _CHROMA_TENANT_ID else "NO",
                    _CHROMA_DATABASE
                )
                import traceback
                log.debug("[semantic] Full traceback: %s", traceback.format_exc())
                _client = chromadb.PersistentClient(path=_CHROMA_PATH)
    else:
        _client = chromadb.PersistentClient(path=_CHROMA_PATH)

    try:
        _collection = _client.get_collection(name=_COLLECTION)
        log.info(
            "[semantic] Loaded existing ChromaDB collection '%s' — %d docs",
            _COLLECTION, _collection.count(),
        )
    except ValueError:
        embed_fn = GeminiEmbeddingFunction()
        _collection = _client.create_collection(
            name=_COLLECTION,
            embedding_function=embed_fn,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "[semantic] Created new ChromaDB collection '%s'",
            _COLLECTION,
        )

    return _client, _collection



# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------


def build_index() -> None:
    """
    Build (or rebuild) the ChromaDB vector index from SQLite.
    Deletes the existing collection first so stale documents never accumulate.
    """

    embed_fn = GeminiEmbeddingFunction()

    if _USE_CLOUD:
        if not _CHROMA_API_KEY or not _CHROMA_TENANT_ID:
            log.warning(
                "[semantic] CHROMA_USE_CLOUD=true but credentials missing. "
                "Using local for build_index."
            )
            client = chromadb.PersistentClient(path=_CHROMA_PATH)
        else:
            try:
                client = chromadb.CloudClient(     # ✅ FIX 2: removed cloud_host param
                    api_key=_CHROMA_API_KEY,
                    tenant=_CHROMA_TENANT_ID,
                    database=_CHROMA_DATABASE,
                )
                log.info("[semantic] build_index using ChromaDB Cloud")
            except Exception as e:
                log.error("[semantic] CloudClient init failed: %s. Using local.", e)
                client = chromadb.PersistentClient(path=_CHROMA_PATH)
    else:
        client = chromadb.PersistentClient(path=_CHROMA_PATH)

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
        category = infer_category(desc) if desc else None
        text_parts = [desc, old_id, group, product]
        if category:
            text_parts.append(category)
        text = " ".join(filter(None, text_parts))

        ids.append(f"product_{product}")
        documents.append(text)
        metadatas.append({
            "type":             "product",
            "entity_id":        _s(product),
            "label":            _s(desc or old_id or product),
            "product_group":    _s(group),
            "product_type":     _s(ptype),
            "product_category": _s(category or ""),
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

    # ── 5. Billing Documents ─────────────────────────────────────────────────
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
    Uses Redis cache for fast lookups of identical queries (~90% latency reduction on cache hit).

    Args:
        query: Semantic search query
        top_k: Max results to return
        entity_type: Optional filter by entity type
        customer_id: Optional filter by customer ID
        where: Optional ChromaDB filter dict

    Returns:
        List of dicts with entity_type, entity_id, label, score, metadata
    """
    cache_key_parts = [query, str(top_k), str(entity_type), str(customer_id)]
    cache_key_query = "|".join(cache_key_parts)

    cached_result = cache.get_cached(cache_key_query, query_type="semantic", customer_id=customer_id)
    if cached_result is not None:
        log.info("[semantic] Cache HIT — %d results from cache", len(cached_result))
        return cached_result

    _, collection = _get_client_and_collection()

    if collection.count() == 0:
        log.warning("[semantic] empty collection — running build_index()")
        build_index()
        _, collection = _get_client_and_collection()

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
        if _USE_CLOUD:
            embed_fn = GeminiEmbeddingFunction()
            query_embedding = embed_fn([query])
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=min(top_k, collection.count()),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        else:
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
        score = max(0.0, 1.0 - (distance / 2.0))
        output.append({
            "entity_type": meta.get("type",      ""),
            "entity_id":   meta.get("entity_id", ""),
            "label":       meta.get("label",     ""),
            "score":       round(score, 4),
            "metadata":    meta,
        })

    output.sort(key=lambda x: x["score"], reverse=True)

    cache.set_cached(cache_key_query, output, query_type="semantic", customer_id=customer_id, ttl=600)

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
# SemanticIndex wrapper
# ---------------------------------------------------------------------------


class SemanticIndex:
    """Stateful wrapper for ChromaDB semantic search. Initialize once at startup."""

    def __init__(self):
        log.info("[SemanticIndex] Connecting to ChromaDB %s...",
                 "Cloud" if _USE_CLOUD else "Local")
        self._client, self._collection = _get_client_and_collection()
        doc_count = self._collection.count() if self._collection else 0
        log.info("[SemanticIndex] ✅ Connected — collection '%s' ready (%d docs)",
                 _COLLECTION, doc_count)

    def search(
        self,
        query: str,
        top_k: int = 10,
        entity_type: str | None = None,
        customer_id: str | None = None,
        where: dict | None = None,
    ) -> list[dict]:
        return semantic_search(
            query=query,
            top_k=top_k,
            entity_type=entity_type,
            customer_id=customer_id,
            where=where,
        )

    def build_or_rebuild_index(self) -> None:
        build_index()
        self._client, self._collection = _get_client_and_collection()
        log.info("[SemanticIndex] index rebuilt — collection count: %d", self._collection.count())

    def get_collection_count(self) -> int:
        if self._collection:
            return self._collection.count()
        return 0