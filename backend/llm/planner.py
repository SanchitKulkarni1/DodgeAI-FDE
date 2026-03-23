"""
llm/planner.py — Natural language → structured query plan.

build_query_plan() produces a plain-English plan that tells sql_generator.py:
  - Which tables are involved
  - Which join paths to use (referencing the critical joins by name)
  - What filter conditions apply
  - What columns to SELECT and how to aggregate

This two-step approach (plan → SQL) significantly improves SQL quality
compared to generating SQL directly from a natural language query, because
the planner can reason about the schema before committing to syntax.
"""

import logging
from llm.client import gemini, MODEL, types
from llm.prompts import DB_SCHEMA

log = logging.getLogger(__name__)

_SYSTEM = f"""\
You are a query planner for an Order-to-Cash (O2C) SQLite database.

Given a natural language question, produce a concise query plan in plain English.
The plan should state:
  1. Which tables to use (from the schema below)
  2. Which join paths to follow (use the CRITICAL JOIN PATHS section)
  3. What WHERE conditions to apply
  4. What to SELECT and any GROUP BY / ORDER BY / LIMIT needed

Do NOT write SQL. Write a numbered plan in plain English only.

{DB_SCHEMA}
"""


def build_query_plan(query: str) -> str:
    """
    Generate a structured query plan for the given natural language query.

    Args:
        query: The resolved user query.

    Returns:
        A plain-English query plan string.
        Falls back to a generic passthrough plan on error.
    """
    try:
        response = gemini.models.generate_content(
            model=MODEL,
            contents=f"Question: {query}",
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                temperature=0.0,
                max_output_tokens=2000,
            ),
        )
        plan = response.text.strip()
        log.info("[planner] plan generated (%d chars)", len(plan))
        return plan

    except Exception as e:
        log.warning("[planner] failed (%s) — using passthrough plan", e)
        return f"Answer the question directly: {query}"