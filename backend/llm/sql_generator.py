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

log = logging.getLogger(__name__)

# Path to the SQLite DB for syntax validation
_DB_PATH = "o2c.db"

_SYSTEM = f"""\
You are a SQLite SQL generator for an Order-to-Cash (O2C) database.

Given a natural language question and a query plan, produce ONE valid SQLite
SELECT statement that answers the question.

Rules (strictly enforced):
  1. Output ONLY the SQL statement — no explanation, no markdown fences.
  2. Only use SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or ALTER.
  3. Always add LIMIT 200 unless the query is an aggregation returning a single row.
  4. Use only the exact table names and column names from the schema.
  5. Follow the CRITICAL JOIN PATHS exactly — do not invent join conditions.
  6. When joining products, always add: product_descriptions.language = 'EN'
  7. For "active" billing docs: billing_doc_is_cancelled = 0
  8. For "cancelled" billing docs: billing_doc_is_cancelled = 1
  9. Payments link to billing via:
       payments_ar.clearing_accounting_document = billing_document_headers.accounting_document
     Do NOT use invoice_reference or sales_document — they are NULL in all rows.
 10. Use LEFT JOIN when you need to show rows that may have no match (e.g. orders
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
    Run EXPLAIN on the SQL against the live DB.
    Raises sqlite3.Error or ValueError if invalid.
    """
    # Reject non-SELECT statements outright
    first_word = sql.strip().split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        raise ValueError(f"Non-SELECT statement rejected: {first_word}")

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