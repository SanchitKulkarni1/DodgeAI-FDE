"""
llm/classifier.py — Intent classification and retrieval mode routing.

classify_intent() makes two decisions in a single LLM call:

  1. Domain guard   → is this question about the O2C dataset?
                      Returns intent = "domain" | "off_topic"

  2. Retrieval mode → which retrieval strategy fits best?
                      Returns mode = "sql" | "semantic" | "hybrid"

Routing heuristics baked into the prompt:
  sql      — exact entity IDs, aggregations, comparisons, "how many", "total",
             "which orders", date ranges, status filters
  semantic — vague product/customer descriptions, "find something like",
             exploratory browsing without specific IDs
  hybrid   — mix of vague entity discovery + need for precise figures
             ("how much did customers buying sunscreen pay in total?")
"""

import logging
from typing import Literal
from pydantic import BaseModel, ValidationError
from llm.client import gemini, MODEL, types

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schema for LLM response validation
# ---------------------------------------------------------------------------
class ClassificationResponse(BaseModel):
    """Schema for the LLM's classification response."""
    intent: Literal["domain", "off_topic"]
    retrieval_mode: Literal["sql", "semantic", "hybrid"]
    reason: str

# ---------------------------------------------------------------------------
# System prompt — enforces JSON output so we can parse reliably
# ---------------------------------------------------------------------------
_SYSTEM = """\
You are a routing classifier for an Order-to-Cash (O2C) data system.

The system contains data about:
  - Sales Orders and their items
  - Outbound Deliveries (headers and items)
  - Billing Documents (invoices, cancellations)
  - Journal Entries (accounts receivable)
  - Payments
  - Customers (Business Partners) and their addresses
  - Products and their descriptions
  - Plants (warehouses / distribution centres)

Your job: classify the user query and return ONLY a JSON object.

JSON schema (return exactly this, no markdown, no explanation):
{
  "intent": "domain" | "off_topic",
  "retrieval_mode": "sql" | "semantic" | "hybrid",
  "reason": "<one sentence>"
}

Rules for intent:
  "domain"    — the query is about the O2C data described above
  "off_topic" — the query is unrelated (general knowledge, creative writing,
                coding help, personal questions, etc.)
  When intent is "off_topic", set retrieval_mode to "sql" (ignored by system).

Rules for retrieval_mode:
  "sql"      — query involves specific IDs, counts, totals, date filters,
               status checks, flow traces, or structured comparisons
  "semantic" — query is vague or descriptive without specific IDs
               (e.g. "find skincare products", "show me customers in Maharashtra")
  "hybrid"   — query needs fuzzy entity discovery AND precise figures
               (e.g. "how much revenue came from face serum products?")

Examples:
  "Which products have the most billing documents?"  → domain, sql
  "Trace billing document 90504259"                 → domain, sql
  "Sales orders with no delivery"                   → domain, sql
  "Find products related to sunscreen"               → domain, semantic
  "Total revenue from haircare products"             → domain, hybrid
  "What is the capital of France?"                  → off_topic, sql
  "Write me a poem"                                 → off_topic, sql
"""


def classify_intent(query: str) -> tuple[str, str]:
    """
    Classify the query's intent and select a retrieval mode.

    Args:
        query: The resolved (self-contained) user query.

    Returns:
        (intent, retrieval_mode) — both are strings.
        Falls back to ("domain", "sql") on any error so the pipeline
        never stalls due to a classification failure.
    """
    try:
        response = gemini.models.generate_content(
            model=MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                temperature=0.0,
                max_output_tokens=1000,
                response_mime_type="application/json",
                response_schema=ClassificationResponse
            ),
        )
        res= response.parsed
        
        if not res:
                    raise ValueError("LLM returned an empty response")
        
        log.info("[classifier] intent=%r mode=%r reason=%r", res.intent, res.retrieval_mode, res.reason)
        return res.intent, res.retrieval_mode
        
    except Exception as e:
        # Catches API errors, ValidationErrors, and empty responses in one block
        log.warning("[classifier] failed (%s) — defaulting to domain/sql", e)
        return "domain", "sql"