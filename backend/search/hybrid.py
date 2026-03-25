"""
search/hybrid.py — Hybrid search: semantic entity discovery + scoped SQL.

hybrid_search() executes a two-stage pipeline:

  Stage 1 — Semantic discovery
    Embeds the query and finds the most similar entities in the ChromaDB index.
    Returns candidate entity IDs grouped by type
    (e.g. product IDs, customer IDs).

  Stage 1.5 — Entity type filtering + relevance pruning (NEW)
    Infers which entity types are relevant to the query (e.g. "revenue from
    skincare products" → product, billing_document).  Drops irrelevant types
    (sales_order, delivery) before they can pollute the SQL prompt.

  Stage 2 — Scoped SQL with EXPLAIN validation (IMPROVED)
    Asks the LLM to generate a SQL query scoped to the discovered entity IDs.
    NOW validates the SQL via EXPLAIN before executing, with a retry loop that
    feeds the error back to the LLM for self-correction.

Example:
    Query:   "How much revenue came from face serum products?"
    Stage 1: semantic_search → [product S8907367008620 (FACESERUM 30ML VIT C), ...]
    Stage 2: SQL scoped to those product IDs →
             SELECT material, SUM(net_amount) FROM billing_document_items
             WHERE material IN ('S8907367008620', ...) ...
"""

import logging
import re
import sqlite3
from pathlib import Path

from search.semantic import semantic_search
from llm.client import gemini, MODEL, types
from llm.prompts import DB_SCHEMA

log = logging.getLogger(__name__)

_DB_PATH   = Path("o2c.db")
_ROW_LIMIT = 200

# ---------------------------------------------------------------------------
# Per-entity-type similarity thresholds (FIX #11).
# Primary entities need a lower bar; tangential types need high confidence.
# ChromaDB cosine score: 0 = unrelated, 1 = identical.
# ---------------------------------------------------------------------------
_SCORE_THRESHOLDS: dict[str, float] = {
    "product":          0.40,
    "customer":         0.50,
    "billing_document": 0.50,
    "sales_order":      0.60,
    "delivery":         0.60,
    "payment":          0.55,
    "plant":            0.55,
}
_DEFAULT_THRESHOLD = 0.40

# ---------------------------------------------------------------------------
# Cap IDs per type to prevent SQL truncation mid-token.
# ---------------------------------------------------------------------------
_MAX_IDS_PER_TYPE = 10

# ---------------------------------------------------------------------------
# Max retry attempts for SQL validation.
# ---------------------------------------------------------------------------
_MAX_SQL_RETRIES = 2

# ---------------------------------------------------------------------------
# Entity type relevance rules (FIX #5 + #6).
# Maps query signal keywords → entity types to retrieve + pass to SQL.
# ---------------------------------------------------------------------------
_TYPE_SIGNALS: dict[str, list[str]] = {
    "product":          ["product", "item", "sku", "skincare", "haircare",
                         "fragrance", "cream", "serum", "spray", "shampoo",
                         "deodorant", "lotion", "moistur", "perfume",
                         "body wash", "face wash", "hair oil", "beard"],
    "customer":         ["customer", "buyer", "client", "partner", "company",
                         "who bought", "who purchased"],
    "billing_document": ["invoice", "billing", "billed"],
    "sales_order":      ["order", "sales order", "ordered"],
    "delivery":         ["delivery", "shipment", "dispatch", "shipped",
                         "delivered", "goods movement"],
    "payment":          ["payment", "paid", "cleared", "outstanding"],
}

# For queries involving aggregation / revenue / amounts, always include these
_NUMERIC_QUERY_TYPES = {"product", "billing_document", "customer"}


# ───────────────────────────────────────────────────────────────────────────
# Improved scoped SQL system prompt
# ───────────────────────────────────────────────────────────────────────────
_SCOPED_SQL_SYSTEM = f"""\
You are a SQLite SQL generator for an Order-to-Cash (O2C) database.

You will be given:
  1. A natural language question
  2. Entity IDs grouped by type, identified by semantic search

Your job: write ONE valid SQLite SELECT statement that answers the question
using these entity IDs as the starting point (WHERE id IN (...) scope).

CRITICAL RULES (strictly enforced):
  1. Output ONLY the raw SQL — no markdown, no explanation, no comments.
  2. Only SELECT statements. Never INSERT / UPDATE / DELETE / DROP.
  3. Always add LIMIT {_ROW_LIMIT} unless the query returns a single aggregated row.
  4. Use ONLY the EXACT table and column names from the schema.
     NEVER abbreviate or truncate column names. Examples:
       CORRECT: billing_document_headers.billing_document
       WRONG:   billing_document_headers.billing   ← DOES NOT EXIST
       CORRECT: billing_document_items.billing_document
       WRONG:   billing_document_items.billing     ← DOES NOT EXIST
  5. Follow the CRITICAL JOIN PATHS exactly — do not invent join conditions.
  6. Always filter product_descriptions with: language = 'EN'
  7. For active billing docs: billing_doc_is_cancelled = FALSE
  8. Payment link: payments_ar.clearing_accounting_document = billing_document_headers.accounting_document
  9. IGNORE entity types that are irrelevant to the question.
     For revenue/amount queries, scope by PRODUCT IDs using:
       WHERE bdi.material IN (...product IDs...)
     Do NOT add unrelated sales_order or delivery IDs to revenue aggregation SQL.
 10. If the question is about a product category (e.g., "skincare"),
     scope by the product IDs provided — they are the semantic matches.

REQUIRED JOIN for product-to-revenue:
  billing_document_items bdi
  JOIN billing_document_headers bdh ON bdi.billing_document = bdh.billing_document
  WHERE bdh.billing_doc_is_cancelled = FALSE AND bdi.material IN (...)

{DB_SCHEMA}
"""


# ═══════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════

def _strip_sql_fences(text: str) -> str:
    """Strip markdown fences and extract the raw SQL statement."""
    text = text.strip()
    text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _is_numeric_query(query: str) -> bool:
    """Detect queries that need SQL-computed answers (totals, counts, etc.)."""
    signals = ["total", "sum", "count", "how many", "how much",
               "average", "revenue", "amount", "percentage", "ratio"]
    query_lower = query.lower()
    return any(s in query_lower for s in signals)


def _infer_relevant_entity_types(query: str) -> set[str]:
    """
    Determine which entity types are relevant to the query (FIX #5).
    Returns a set of type names to retrieve + pass to SQL generation.
    """
    query_lower = query.lower()
    relevant: set[str] = set()

    for etype, signals in _TYPE_SIGNALS.items():
        if any(s in query_lower for s in signals):
            relevant.add(etype)

    # Revenue / numeric queries always need products + billing
    if _is_numeric_query(query_lower):
        relevant |= _NUMERIC_QUERY_TYPES

    # Default: if nothing matched, return all types
    if not relevant:
        relevant = set(_TYPE_SIGNALS.keys())

    return relevant


def _filter_entity_groups(query: str,
                          entity_groups: dict[str, list[str]],
                          ) -> dict[str, list[str]]:
    """
    Drop entity types that are irrelevant to the query (FIX #6).
    For revenue queries, only keep product / customer / billing_document IDs.
    """
    relevant = _infer_relevant_entity_types(query)

    filtered = {k: v for k, v in entity_groups.items() if k in relevant}

    dropped = set(entity_groups) - set(filtered)
    if dropped:
        log.info(
            "[hybrid] dropped irrelevant entity types: %s (kept: %s)",
            dropped, set(filtered),
        )

    return filtered


def _validate_sql(sql: str) -> str | None:
    """
    Run EXPLAIN on the SQL against the live DB (FIX #2).
    Returns None on success, error string on failure.
    """
    if not sql:
        return "empty SQL"

    first_word = sql.strip().split()[0].upper() if sql.strip() else ""
    if first_word not in ("SELECT", "WITH"):
        return f"Rejected non-SELECT statement: {first_word}"

    uri = f"file:{_DB_PATH.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        con.execute(f"EXPLAIN {sql}")
        return None  # valid
    except sqlite3.Error as e:
        return str(e)
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════════
# Core pipeline stages
# ═══════════════════════════════════════════════════════════════════════════

def _build_scoped_sql(query: str,
                      entity_groups: dict[str, list[str]],
                      ) -> tuple[str | None, str | None]:
    """
    Ask the LLM to produce a SQL query scoped to the discovered entity IDs.
    Validates via EXPLAIN and retries on failure (FIX #2).

    Args:
        query:         The resolved user query.
        entity_groups: Dict mapping entity_type → list of entity IDs.

    Returns:
        (sql, error): sql is the validated SQL string (or None).
                      error is the last validation error (or None on success).
    """
    if not entity_groups:
        return None, "no entity groups to scope"

    # Determine which types are relevant and separate context-only types
    relevant_types = _infer_relevant_entity_types(query)

    # Build entity text — separate relevant from context-only
    relevant_text = ""
    context_text = ""
    for etype, ids in entity_groups.items():
        capped_ids = ids[:_MAX_IDS_PER_TYPE]
        if len(ids) > _MAX_IDS_PER_TYPE:
            log.warning(
                "[hybrid] capping %s IDs from %d to %d to prevent SQL truncation",
                etype, len(ids), _MAX_IDS_PER_TYPE,
            )
        id_list = ", ".join(f"'{eid}'" for eid in capped_ids)

        if etype in relevant_types:
            relevant_text += f"  {etype} IDs: {id_list}\n"
        else:
            context_text += f"  {etype}: {len(ids)} entities (context only — do NOT use in SQL)\n"

    entities_section = "RELEVANT entities (use in WHERE clauses):\n" + relevant_text
    if context_text:
        entities_section += "\nCONTEXT-ONLY entities (do NOT use in SQL):\n" + context_text

    base_prompt = (
        f"Question: {query}\n\n"
        f"Entities identified by semantic search:\n{entities_section}\n"
        f"Write a SQL query that answers the question using these entity IDs.\n"
        f"SQL:"
    )

    last_error: str | None = None

    for attempt in range(1, _MAX_SQL_RETRIES + 1):
        prompt = base_prompt
        if last_error and attempt > 1:
            prompt += (
                f"\n\nYour previous SQL attempt was INVALID.\n"
                f"Error: {last_error}\n"
                f"Fix the SQL using ONLY columns from the schema. "
                f"Double-check every column name matches the schema exactly.\n"
                f"SQL:"
            )

        try:
            response = gemini.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SCOPED_SQL_SYSTEM,
                    temperature=0.0,
                    max_output_tokens=1024,
                ),
            )
            sql = _strip_sql_fences(response.text)

            # EXPLAIN validation (FIX #2)
            validation_error = _validate_sql(sql)
            if validation_error is None:
                log.info("[hybrid] SQL validated on attempt %d: %r", attempt, sql[:120])
                return sql, None
            else:
                last_error = validation_error
                log.warning(
                    "[hybrid] SQL validation failed (attempt %d/%d): %s | sql=%r",
                    attempt, _MAX_SQL_RETRIES, last_error, sql[:120],
                )

        except Exception as e:
            last_error = str(e)
            log.error("[hybrid] SQL generation failed (attempt %d): %s", attempt, e)

    log.error("[hybrid] SQL generation failed after %d attempts: %s",
              _MAX_SQL_RETRIES, last_error)
    return None, last_error


def _execute_scoped_sql(sql: str) -> tuple[list[dict], str | None]:
    """
    Execute the scoped SQL and return (rows, error_message).

    Returns the error string instead of swallowing it silently.
    The caller (hybrid_search) sets hybrid_sql_failed in the result so
    answer_node can detect it and refuse to hallucinate a figure.
    """
    if not sql:
        return [], "No SQL was generated"

    first_word = sql.strip().split()[0].upper() if sql.strip() else ""
    if first_word not in ("SELECT", "WITH"):
        msg = f"Rejected non-SELECT scoped SQL: {sql[:80]!r}"
        log.warning("[hybrid] %s", msg)
        return [], msg

    uri = f"file:{_DB_PATH.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        cursor = con.execute(sql)
        rows   = cursor.fetchmany(_ROW_LIMIT)
        return [dict(row) for row in rows], None
    except sqlite3.Error as e:
        msg = str(e)
        log.error("[hybrid] SQL execution error: %s | sql=%r", msg, sql[:200])
        return [], msg
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def hybrid_search(query: str) -> dict:
    """
    Run hybrid search: semantic entity discovery → scoped SQL execution.

    Args:
        query: The resolved natural language query.

    Returns:
        dict with keys:
            "semantic_results" : list[dict] — raw semantic hits
            "sql_query"        : str | None — the scoped SQL generated
            "query_result"     : list[dict] — rows from executing the SQL
            "entity_groups"    : dict       — entity IDs by type (for debugging)
            "hybrid_sql_failed": bool       — True if SQL errored
            "sql_error"        : str | None — error message when failed
    """
    # ── Stage 1: targeted semantic discovery ────────────────────────────────
    # Infer which entity types are relevant and search per-type (FIX #5).
    relevant_types = _infer_relevant_entity_types(query)
    log.info("[hybrid] relevant entity types for query: %s", relevant_types)

    all_semantic_results: list[dict] = []
    for etype in relevant_types:
        hits = semantic_search(query, top_k=10, entity_type=etype)
        all_semantic_results.extend(hits)

    # De-duplicate (same entity may appear via different type searches)
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for hit in all_semantic_results:
        uid = f"{hit['entity_type']}_{hit['entity_id']}"
        if uid not in seen_ids:
            seen_ids.add(uid)
            deduped.append(hit)

    # Sort by score descending
    deduped.sort(key=lambda x: x["score"], reverse=True)
    semantic_results = deduped

    # ── Filter by per-type similarity thresholds (FIX #11) ──────────────────
    confident: list[dict] = []
    for r in semantic_results:
        threshold = _SCORE_THRESHOLDS.get(r["entity_type"], _DEFAULT_THRESHOLD)
        if r["score"] >= threshold:
            confident.append(r)
        else:
            log.debug(
                "[hybrid] filtered out %s %s (score=%.3f < threshold=%.3f)",
                r["entity_type"], r["entity_id"], r["score"], threshold,
            )

    if not confident:
        log.info("[hybrid] no confident semantic hits after per-type thresholds")
        return {
            "semantic_results":  semantic_results,
            "sql_query":         None,
            "query_result":      [],
            "entity_groups":     {},
            "hybrid_sql_failed": False,
            "sql_error":         None,
        }

    # Warn when top score is low (FIX #13)
    top_score = confident[0]["score"] if confident else 0.0
    if top_score < 0.70:
        log.warning(
            "[hybrid] top semantic score is LOW (%.3f for %s) — results may be inaccurate",
            top_score, confident[0].get("label", "?"),
        )

    # ── Group IDs by entity type ────────────────────────────────────────────
    entity_groups: dict[str, list[str]] = {}
    for hit in confident:
        etype = hit["entity_type"]
        eid   = hit["entity_id"]
        if etype not in entity_groups:
            entity_groups[etype] = []
        if eid not in entity_groups[etype]:
            entity_groups[etype].append(eid)

    # Drop irrelevant entity types for SQL generation (FIX #6)
    entity_groups = _filter_entity_groups(query, entity_groups)

    log.info(
        "[hybrid] entity groups (post-filter): %s",
        {k: len(v) for k, v in entity_groups.items()},
    )

    # ── Stage 2: scoped SQL with EXPLAIN validation (FIX #2) ────────────────
    scoped_sql, gen_error = _build_scoped_sql(query, entity_groups)

    if not scoped_sql:
        return {
            "semantic_results":  semantic_results,
            "sql_query":         None,
            "query_result":      [],
            "entity_groups":     entity_groups,
            "hybrid_sql_failed": True,
            "sql_error":         gen_error or "SQL generation returned nothing",
        }

    # Execute the validated SQL
    query_result, sql_error = _execute_scoped_sql(scoped_sql)
    hybrid_sql_failed = sql_error is not None

    if hybrid_sql_failed:
        log.warning("[hybrid] SQL execution failed — will NOT pass to answer_writer for fabrication")

    log.info(
        "[hybrid] scoped SQL returned %d rows  failed=%s  sql=%r",
        len(query_result),
        hybrid_sql_failed,
        scoped_sql[:100],
    )

    return {
        "semantic_results":  semantic_results,
        "sql_query":         scoped_sql,
        "query_result":      query_result,
        "entity_groups":     entity_groups,
        "hybrid_sql_failed": hybrid_sql_failed,
        "sql_error":         sql_error,
    }