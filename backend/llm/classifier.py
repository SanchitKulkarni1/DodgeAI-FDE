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
  "sql"      — query involves:
               • counts, totals, sums, averages ("total revenue", "count orders")
               • specific IDs or references ("billing doc 9050", "customer 12345")
               • date ranges or status filters
               • flow traces ("trace payment", "trace delivery")
               • structured comparisons ("orders vs deliveries", "top products")
               • financial figures ("revenue", "amount", "payment")
               Even if products are fuzzy (e.g. "skincare"), if the query asks for
               aggregations like total/sum/count, use SQL.
               
  "semantic" — query is exploratory and vague without aggregation
               (e.g. "find skincare products", "show me customers in Maharashtra")
               
  "hybrid"   — query needs fuzzy entity discovery without aggregation
               (e.g. "what kind of products does customer X buy?")

Examples:
  "What products have the most billing documents?"  → domain, sql (aggregation)
  "Trace billing document 90504259"                → domain, sql (specific ID)
  "Total revenue from customers in Delhi"          → domain, sql (aggregation + vague region)
  "What is the total revenue from customers who bought skincare?" → domain, sql (total revenue)
  "Sum of all order amounts for product SKU-123"  → domain, sql (aggregation + specific ID)
  "Find products related to sunscreen"             → domain, semantic (exploratory, no aggregation)
  "How many customers bought haircare products?"   → domain, sql (count aggregation)
  "Show me orders from this month"                 → domain, sql (date filter)
  "What is the capital of France?"                → off_topic, sql
  "Write me a poem"                               → off_topic, sql
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