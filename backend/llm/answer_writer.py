"""
llm/answer_writer.py — Grounded natural language answer writer.

write_answer() synthesises a natural language response from the full
reasoning chain that produced the data:

  - The resolved user query
  - The query plan  (what the planner decided to do)
  - The SQL query   (exactly what was executed — anchors column meaning)
  - SQL result rows (the actual data returned)
  - Semantic search results (entity matches from vector search)

Why query_plan + sql_query matter for grounding
------------------------------------------------
Without them the LLM only sees raw rows like:
    {"material": "S8907367008620", "billing_count": 11, "total_revenue": 3100.17}

It has no idea whether "billing_count" means active invoices, all invoices,
or something else — because that was decided in the WHERE clause of the SQL.
With the SQL visible, the LLM knows exactly what filters were applied and
can phrase the answer accurately:
    "Among active (non-cancelled) billing documents, FACESERUM 30ML VIT C
     appeared in 11 invoices totalling INR 3,100.17."

The query plan gives higher-level intent context — useful when the SQL is
long and the business question needs to be re-anchored.
"""

import json
import logging
from llm.client import gemini, MODEL, types

log = logging.getLogger(__name__)

_SYSTEM = """\
You are a data analyst assistant for an Order-to-Cash (O2C) business system.

You will receive:
  1. The user's question
  2. The query plan that describes what data was retrieved and why
  3. The exact SQL query that was executed (use this to understand what
     filters, joins, and aggregations were applied to produce the results)
  4. The actual data returned by that query
  5. Any semantic search results (fuzzy entity matches)

Your job: write a clear, concise natural language answer grounded entirely
in the provided data and reasoning chain.

Rules (strictly enforced):
  1. ONLY use information present in the provided data. Never add facts,
     figures, or entity names that are not in the data.
  2. Use the SQL query to understand WHAT the numbers mean — e.g. if the
     SQL filters billing_doc_is_cancelled = 0, say "active billing documents"
     not just "billing documents". If it uses SUM(net_amount), say "total
     net revenue" not just "amount".
  3. Use the query plan to understand the INTENT — restate the business
     question in your own words before answering it.
  4. If the data is empty, say "No matching records were found" and suggest
     how the user might refine their query.
  5. For lists of results, use a structured format (numbered list or table
     described in prose). Keep it readable.
  6. For flow traces (SO → Delivery → Billing → Payment), present the chain
     step by step with the document IDs at each stage.
  7. For aggregations (counts, totals), state the exact figure from the data.
  8. For broken flow findings (undelivered SOs, unpaid invoices), describe
     the gap clearly and list the affected document IDs.
  9. Currency amounts are in INR (Indian Rupees). Format them with 2 decimal
     places and the INR symbol: e.g. "INR 17,108.25".
 10. Dates are in YYYY-MM-DD format — present them as-is.
 11. Keep the answer under 300 words unless the data genuinely requires more.
 12. Do not mention SQL syntax, table names, or column names in your answer.
     Speak in business language: "sales orders", "billing documents",
     "deliveries", "customers", "products" — not "rows", "records", or
     technical identifiers like "billing_doc_is_cancelled".
"""


def write_answer(
    query: str,
    sql_results: list[dict],
    semantic_results: list[dict],
    query_plan: str | None = None,
    sql_query: str | None = None,
) -> str:
    """
    Write a grounded natural language answer from the full reasoning chain.

    Args:
        query:            The resolved user query.
        sql_results:      List of row dicts from execute_node (may be empty).
        semantic_results: List of entity dicts from semantic/hybrid search
                          (may be empty).
        query_plan:       Plain-English plan from planner_node (may be None
                          for semantic-only paths).
        sql_query:        The exact SQL executed by execute_node (may be None
                          for semantic-only paths). This is the most important
                          grounding context — it tells the LLM exactly what
                          filters and aggregations produced the result rows.

    Returns:
        A natural language answer string grounded in the provided data.
    """
    # ── Reasoning chain section ─────────────────────────────────────────────
    # Include plan + SQL only when they exist (semantic-only path has neither)
    reasoning_parts = []

    if query_plan:
        reasoning_parts.append(
            f"QUERY PLAN (what was retrieved and why):\n{query_plan}"
        )

    if sql_query:
        reasoning_parts.append(
            f"SQL EXECUTED (exact filters and aggregations applied):\n{sql_query}"
        )

    reasoning_section = (
        "\n\n".join(reasoning_parts) + "\n\n"
        if reasoning_parts else ""
    )

    # ── Data section ────────────────────────────────────────────────────────
    # Cap at 50 rows / 20 semantic results to stay within token limits.
    sql_snippet      = json.dumps(sql_results[:50],      indent=2)
    semantic_snippet = json.dumps(semantic_results[:20], indent=2)

    has_sql      = bool(sql_results)
    has_semantic = bool(semantic_results)

    if has_sql and has_semantic:
        data_section = (
            f"QUERY RESULTS ({len(sql_results)} rows, showing first 50):\n"
            f"{sql_snippet}\n\n"
            f"SEMANTIC SEARCH RESULTS ({len(semantic_results)} results, showing first 20):\n"
            f"{semantic_snippet}"
        )
    elif has_sql:
        data_section = (
            f"QUERY RESULTS ({len(sql_results)} rows, showing first 50):\n"
            f"{sql_snippet}"
        )
    elif has_semantic:
        data_section = (
            f"SEMANTIC SEARCH RESULTS ({len(semantic_results)} results, showing first 20):\n"
            f"{semantic_snippet}"
        )
    else:
        data_section = "DATA: (no results returned)"

    # ── Assemble full prompt ─────────────────────────────────────────────────
    prompt = (
        f"USER QUESTION: {query}\n\n"
        f"{reasoning_section}"
        f"{data_section}\n\n"
        f"Write a natural language answer to the question based on the "
        f"reasoning chain and data above:"
    )

    try:
        response = gemini.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                temperature=0.3,
                max_output_tokens=1024,
            ),
        )
        answer = response.text.strip()
        log.info("[answer_writer] answer=%d chars", len(answer))
        return answer

    except Exception as e:
        log.error("[answer_writer] failed: %s", e)
        return (
            "I was able to retrieve the data but encountered an error while "
            f"formatting the answer. Technical detail: {e}"
        )