"""
llm/sql_generator.py — Natural language + query plan → validated SQLite SQL.

generate_sql() uses the query plan from planner.py to produce a single
SQLite SELECT statement. The output is:
  1. Extracted from the LLM response (strips markdown fences)
  2. Syntax-validated against the live SQLite DB using EXPLAIN
  3. Returned as a clean SQL string, or raises ValueError on bad SQL

Key prompt constraints enforced:
  - SELECT only (no INSERT/UPDATE/DELETE/DROP)
  - LIMIT 200 always applied (prevents accidental full-table dumps)
  - Must use the confirmed join paths from DB_SCHEMA
  - No hallucinated column names
"""

import logging
import re
import sqlite3
import json
from llm.client import gemini, MODEL, types
from llm.prompts import DB_SCHEMA
from db.schema_validator import validate_sql_against_schema, report_sql_issues
from llm.query_plan import QueryPlan

log = logging.getLogger(__name__)

# Path to the SQLite DB for syntax validation
_DB_PATH = "o2c.db"

_SYSTEM = f"""\
You are a SQLite SQL generator for an Order-to-Cash (O2C) database.

Given a natural language question and a STRUCTURED QUERY PLAN (JSON), produce ONE valid SQLite
SELECT statement that answers the question.

The query plan specifies:
  - intent: aggregation, exploration, trace, or comparison
  - tables: list of tables needed
  - joins: list of JOIN conditions (exact strings from schema)
  - filters: list of WHERE conditions (field, operator, value)
  - aggregation: aggregation function if intent is aggregation
  - group_by: columns to GROUP BY
  - order_by: ORDER BY clause
  - limit: LIMIT value
  - reasoning: explanation of the plan

CRITICAL RULES (strictly enforced by validator):
  1. Output ONLY the SQL statement — no explanation, no markdown fences.
  2. Only use SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or ALTER.
  3. Always add LIMIT (default 200 unless overridden)
  4. Use only the exact table names and column names from the schema.
  5. For joins, use EXACT strings from the query plan JSON.
  6. Apply all filters from the query plan.
  7. If aggregation is specified, use it in SELECT and include GROUP BY.
  8. BOOLEAN columns (billing_doc_is_cancelled, is_blocked) use PostgreSQL syntax:
     Write  = FALSE  or  = TRUE  (never = 0, = 1, = 'FALSE', or = 'TRUE').

DON'T OVERTHINK: The query plan already has done the hard work (tables, joins, filters).
Your job is just to convert it to valid SQL.

{DB_SCHEMA}
"""


def _normalize_boolean_literals(sql: str) -> str:
    """
    Normalize SQLite-style boolean integer comparisons to PostgreSQL boolean literals.

    PostgreSQL stores BOOLEAN columns as true/false, not 0/1. The LLM (trained
    on the SQLite schema) often generates:
        billing_doc_is_cancelled = 0   → should be: billing_doc_is_cancelled = FALSE
        billing_doc_is_cancelled = 1   → should be: billing_doc_is_cancelled = TRUE
        billing_doc_is_cancelled = 'FALSE' → should be: billing_doc_is_cancelled = FALSE

    This function rewrites those patterns so the SQL runs correctly on PostgreSQL.
    """
    # Boolean columns in the schema that are stored as BOOLEAN in PostgreSQL
    BOOL_COLUMNS = [
        "billing_doc_is_cancelled",
        "is_blocked",
    ]

    for col in BOOL_COLUMNS:
        # col = 0  →  col = FALSE
        sql = re.sub(
            rf"({re.escape(col)}\s*=\s*)0\b",
            r"\g<1>FALSE",
            sql,
            flags=re.IGNORECASE,
        )
        # col = 1  →  col = TRUE
        sql = re.sub(
            rf"({re.escape(col)}\s*=\s*)1\b",
            r"\g<1>TRUE",
            sql,
            flags=re.IGNORECASE,
        )
        # col = 'FALSE'  →  col = FALSE  (strip quotes)
        sql = re.sub(
            rf"({re.escape(col)}\s*=\s*)'(TRUE|FALSE)'",
            r"\g<1>\2",
            sql,
            flags=re.IGNORECASE,
        )

    return sql


def _extract_sql(text: str) -> str:
    """Strip markdown fences and extract the raw SQL statement."""
    text = text.strip()
    # Remove ```sql ... ``` or ``` ... ```
    text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    sql = text.strip()

    # Normalize boolean literals for PostgreSQL compatibility
    sql = _normalize_boolean_literals(sql)

    # Detect truncation — SQL ending mid-keyword (incomplete clause)
    if sql and len(sql) > 50:
        # If query ends with incomplete clause indicators
        ends_with = sql.rstrip()[-3:].upper()
        if any(sql.rstrip().endswith(c) for c in ('=', ' AND', ' OR', ' ON', ' WHERE', ' b')):
            # Likely truncated (ends with single letter or incomplete clause)
            raise ValueError(f"Truncated SQL detected: query ends incomplete at: ...{sql[-30:]}")
    
    return sql


def _validate_sql(sql: str) -> None:
    """
    Validate SQL against schema constraints AND EXPLAIN syntax.
    
    PATH 1: Enhanced validation approach
    1. Check schema (tables, columns, join paths, NULL columns)
    2. Check syntax with EXPLAIN
    
    Raises ValueError with detailed error messages for LLM retry.
    """
    # Reject non-SELECT statements outright
    first_word = sql.strip().split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        raise ValueError(f"Non-SELECT statement rejected: {first_word}")

    # ─────────────────────────────────────────────────────────────────────
    # NEW: Schema validation (before EXPLAIN to catch logical errors)
    # ─────────────────────────────────────────────────────────────────────
    
    is_valid, errors = validate_sql_against_schema(sql)
    if not is_valid:
        error_msg = "\n".join(errors)
        raise ValueError(f"Schema validation failed:\n{error_msg}")
    
    # ─────────────────────────────────────────────────────────────────────
    # EXISTING: Syntax validation with EXPLAIN
    # ─────────────────────────────────────────────────────────────────────

    con = sqlite3.connect(_DB_PATH)
    try:
        con.execute(f"EXPLAIN {sql}")
    finally:
        con.close()


def generate_sql(query: str, query_plan: QueryPlan) -> str:
    """
    Generate and validate a SQLite SELECT statement.

    Args:
        query:       The resolved natural language query.
        query_plan:  QueryPlan (Pydantic) from build_query_plan().

    Returns:
        A validated SQLite SQL string.

    Raises:
        ValueError: If the generated SQL fails validation after retries.
    """
    # Convert QueryPlan to structured format for LLM
    plan_json = json.dumps(query_plan.model_dump(), indent=2)
    
    prompt = (
        f"Question: {query}\n\n"
        f"Query Plan (JSON):\n{plan_json}\n\n"
        f"SQL:"
    )

    last_error = None

    # Up to 2 attempts — on failure, include the error in the retry prompt
    for attempt in range(1, 3):
        try:
            contents = prompt
            if last_error and attempt > 1:
                contents = (
                    f"{prompt}\n\n"
                    f"The previous attempt produced invalid SQL.\n"
                    f"Error: {last_error}\n"
                    f"Please fix the SQL and try again.\n"
                    f"SQL:"
                )

            response = gemini.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    temperature=0.0,
                    max_output_tokens=4000,
                ),
            )

            sql = _extract_sql(response.text)
            _validate_sql(sql)

            log.info("[sql_gen] attempt %d succeeded  sql=%r", attempt, sql[:120])
            return sql

        except (sqlite3.Error, ValueError) as e:
            last_error = str(e)
            log.warning("[sql_gen] attempt %d failed: %s", attempt, last_error)

        except Exception as e:
            last_error = str(e)
            log.error("[sql_gen] unexpected error on attempt %d: %s", attempt, e)

    raise ValueError(
        f"SQL generation failed after 2 attempts. Last error: {last_error}"
    )