"""
search/hybrid.py — Hybrid search: semantic entity discovery + scoped SQL.

hybrid_search() executes a two-stage pipeline:

  Stage 1 — Semantic discovery
    Embeds the query and finds the most similar entities in the FAISS index.
    Returns candidate entity IDs grouped by type
    (e.g. product IDs, customer IDs).

  Stage 2 — Scoped SQL
    Asks the LLM to generate a SQL query scoped to the discovered entity IDs
    using WHERE id IN (...) clauses. This produces precise, grounded figures
    (totals, counts, dates) for the fuzzy entities the user described.

Example:
    Query:   "How much revenue came from face serum products?"
    Stage 1: semantic_search → [product S8907367008620 (FACESERUM 30ML VIT C), ...]
    Stage 2: SQL scoped to those product IDs →
             SELECT material, SUM(net_amount) FROM billing_document_items
             WHERE material IN ('S8907367008620', ...) ...
"""

import json
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
# Minimum similarity score to include an entity in the scoped SQL.
# Below this threshold the semantic hit is likely noise.
# ---------------------------------------------------------------------------
# Minimum similarity score to include a hit in the scoped SQL.
# ChromaDB returns cosine distance (0=identical, 2=opposite).
# We convert: score = 1 - (distance / 2). So 0.35 ~ distance 1.3 = loose match.
_SCORE_THRESHOLD = 0.35

# ---------------------------------------------------------------------------
# How many top semantic results to pass into the scoped SQL per entity type.
# Keeps the IN (...) clause from becoming huge.
# ---------------------------------------------------------------------------
_MAX_IDS_PER_TYPE = 10

_SCOPED_SQL_SYSTEM = f"""\
You are a SQLite SQL generator for an Order-to-Cash (O2C) database.

You will be given:
  1. A natural language question
  2. A set of entity IDs already identified by semantic search (grouped by type)

Your job: write ONE valid SQLite SELECT statement that answers the question
using these entity IDs as the starting point (WHERE id IN (...) scope).

Rules:
  1. Output ONLY the raw SQL — no markdown, no explanation.
  2. Only SELECT statements. Never INSERT / UPDATE / DELETE / DROP.
  3. Always add LIMIT {_ROW_LIMIT} unless the query returns a single aggregated row.
  4. Use ONLY the exact table and column names from the schema.
  5. Follow the CRITICAL JOIN PATHS exactly.
  6. Always filter product_descriptions with: language = 'EN'
  7. For active billing docs: billing_doc_is_cancelled = 0
  8. Payment link: payments_ar.clearing_accounting_document = billing_document_headers.accounting_document

{DB_SCHEMA}
"""


def _build_scoped_sql(query: str, entity_groups: dict[str, list[str]]) -> str | None:
    """
    Ask the LLM to produce a SQL query scoped to the discovered entity IDs.

    Args:
        query:         The resolved user query.
        entity_groups: Dict mapping entity_type → list of entity IDs.
                       e.g. {"product": ["S890...", "B890..."], "customer": ["32000..."]}

    Returns:
        A raw SQL string, or None if generation fails.
    """
    if not entity_groups:
        return None

    # Describe the discovered entities to the LLM
    entities_text = ""
    for etype, ids in entity_groups.items():
        id_list = ", ".join(f"'{eid}'" for eid in ids[:_MAX_IDS_PER_TYPE])
        entities_text += f"  {etype} IDs: {id_list}\n"

    prompt = (
        f"Question: {query}\n\n"
        f"Entities identified by semantic search:\n{entities_text}\n"
        f"Write a SQL query that answers the question using these entity IDs.\n"
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
        sql = response.text.strip()
        # Strip markdown fences
        sql = re.sub(r"^```(?:sql)?\s*\n?", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\n?```\s*$", "", sql)
        return sql.strip()

    except Exception as e:
        log.error("[hybrid] scoped SQL generation failed: %s", e)
        return None


def _execute_scoped_sql(sql: str) -> list[dict]:
    """Execute the scoped SQL and return rows as list of dicts."""
    if not sql:
        return []

    first_word = sql.strip().split()[0].upper() if sql.strip() else ""
    if first_word not in ("SELECT", "WITH"):
        log.warning("[hybrid] rejected non-SELECT scoped SQL: %r", sql[:80])
        return []

    uri = f"file:{_DB_PATH.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        cursor = con.execute(sql)
        rows   = cursor.fetchmany(_ROW_LIMIT)
        return [dict(row) for row in rows]
    except sqlite3.Error as e:
        log.error("[hybrid] SQL execution error: %s | sql=%r", e, sql[:200])
        return []
    finally:
        con.close()


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
    """
    # ── Stage 1: semantic discovery ─────────────────────────────────────────
    # semantic_search now supports ChromaDB metadata pre-filtering via
    # entity_type= or where= kwargs. For hybrid we search all types so
    # the LLM can identify which entity types the query is about.
    semantic_results = semantic_search(query, top_k=20)

    # Filter by confidence threshold
    confident = [r for r in semantic_results if r["score"] >= _SCORE_THRESHOLD]

    if not confident:
        log.info("[hybrid] no confident semantic hits (threshold=%.2f)", _SCORE_THRESHOLD)
        return {
            "semantic_results": semantic_results,
            "sql_query":        None,
            "query_result":     [],
            "entity_groups":    {},
        }

    # Group IDs by entity type
    entity_groups: dict[str, list[str]] = {}
    for hit in confident:
        etype = hit["entity_type"]
        eid   = hit["entity_id"]
        if etype not in entity_groups:
            entity_groups[etype] = []
        if eid not in entity_groups[etype]:
            entity_groups[etype].append(eid)

    log.info(
        "[hybrid] entity groups: %s",
        {k: len(v) for k, v in entity_groups.items()},
    )

    # ── Stage 2: scoped SQL ──────────────────────────────────────────────────
    scoped_sql  = _build_scoped_sql(query, entity_groups)
    query_result = _execute_scoped_sql(scoped_sql) if scoped_sql else []

    log.info(
        "[hybrid] scoped SQL returned %d rows  sql=%r",
        len(query_result),
        (scoped_sql or "")[:100],
    )

    return {
        "semantic_results": semantic_results,
        "sql_query":        scoped_sql,
        "query_result":     query_result,
        "entity_groups":    entity_groups,
    }