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
from llm.client import gemini, MODEL, types
from llm.prompts import DB_SCHEMA
from db.schema_validator import validate_sql_against_schema, report_sql_issues

log = logging.getLogger(__name__)

# Path to the SQLite DB for syntax validation
_DB_PATH = "o2c.db"

_SYSTEM = f"""\
You are a SQLite SQL generator for an Order-to-Cash (O2C) database.

Given a natural language question and a query plan, produce ONE valid SQLite
SELECT statement that answers the question.

CRITICAL RULES (strictly enforced by validator):
  1. Output ONLY the SQL statement — no explanation, no markdown fences.
  2. Only use SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or ALTER.
  3. Always add LIMIT 200 unless the query is an aggregation returning a single row.
  4. Use only the exact table names and column names from the schema.
  5. Follow the CRITICAL JOIN PATHS exactly — do not invent join conditions.

MANDATORY JOIN PATHS (memorise these):
  - Sales Order → Delivery: outbound_delivery_items.reference_sd_document = sales_order_headers.sales_order
  - Delivery → Billing: billing_document_items.reference_sd_document = outbound_delivery_headers.delivery_document
  - Billing → Payment: payments_ar.clearing_accounting_document = billing_document_headers.accounting_document
     (DO NOT use invoice_reference or sales_document — they are NULL in all rows)
  - Billing → Journal: journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document
  - Billing Item → Product: billing_document_items.material = products.product
  - Product → Description: products.product = product_descriptions.product AND product_descriptions.language = 'EN'
  - Billing → Customer: billing_document_headers.sold_to_party = business_partners.customer

FILTERS TO ALWAYS APPLY:
  - For "active" (non-cancelled) billing docs: WHERE billing_doc_is_cancelled = 0
  - For cash flow: Include payments_ar JOIN to show payment status
  - When filtering by customer: Use business_partners table

COLUMNS TO NEVER USE (always NULL in this dataset):
  - sales_order_headers.overall_billing_status
  - payments_ar.invoice_reference
  - payments_ar.sales_document

USE LEFT JOIN when you need to show rows that may have no match (e.g. orders
without deliveries). Use INNER JOIN when both sides must exist.

{DB_SCHEMA}
"""


def _extract_sql(text: str) -> str:
    """Strip markdown fences and extract the raw SQL statement."""
    text = text.strip()
    # Remove ```sql ... ``` or ``` ... ```
    text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


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


def generate_sql(query: str, query_plan: str) -> str:
    """
    Generate and validate a SQLite SELECT statement.

    Args:
        query:      The resolved natural language query.
        query_plan: The plain-English plan from build_query_plan().

    Returns:
        A validated SQLite SQL string.

    Raises:
        ValueError: If the generated SQL fails validation after retries.
    """
    prompt = (
        f"Question: {query}\n\n"
        f"Query Plan:\n{query_plan}\n\n"
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
                    max_output_tokens=3000,
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