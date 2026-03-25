"""
llm/planner.py — Natural language → structured query plan (Pydantic JSON).

build_query_plan() produces a structured QueryPlan JSON that tells sql_generator.py:
  - Intent (aggregation, exploration, trace, comparison)
  - Which tables are involved
  - Which join paths to use (exact strings from CRITICAL JOIN PATHS)
  - What filter conditions apply
  - What columns to SELECT and how to aggregate

This two-step approach (plan → SQL) significantly improves SQL quality.
The structured output eliminates "English text re-interpretation" problems.
"""

import logging
import json
import re
from llm.client import gemini, MODEL, types
from llm.prompts import DB_SCHEMA
from llm.query_plan import QueryPlan, validate_join_against_known_paths

log = logging.getLogger(__name__)


def extract_and_clean_json(text: str) -> str:
    """
    Extract valid JSON from potentially malformed text response.
    
    Handles:
    - Markdown code blocks
    - Extra text before/after JSON
    - Trailing commas
    - Truncated responses (raises ValueError early with a clear message)
    
    Returns cleaned JSON string ready for parsing.
    """
    text = text.strip()
    log.debug(f"[planner] raw response: {text[:300]}")
    
    # Remove markdown code blocks
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    text = text.strip()
    
    # Find JSON start
    if not (text.startswith('{') or text.startswith('[')):
        match = re.search(r'[{\[]', text)
        if match:
            text = text[match.start():]
    
    # Extract balanced JSON (find matching closing bracket)
    if text and text[0] in '{[':
        bracket_count = 0
        in_string = False
        escape_next = False
        json_end = -1
        
        for i, char in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if not in_string:
                if char in '{[':
                    bracket_count += 1
                elif char in '}]':
                    bracket_count -= 1
                    if bracket_count == 0:
                        json_end = i + 1
                        break
        
        if json_end < 0:
            # The JSON was truncated — brackets never closed.
            # Raise immediately so the retry loop gets a clear signal.
            raise ValueError(
                f"LLM response was truncated (JSON never closed). "
                f"Response length: {len(text)} chars. "
                f"First 200 chars: {text[:200]!r}"
            )
        
        text = text[:json_end]
    
    # Remove trailing commas (common JSON error)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    
    return text

_SYSTEM = f"""\
You are a query planner for an Order-to-Cash (O2C) SQLite database.

Your job: Convert a natural language question into a STRUCTURED JSON query plan.

STRICT RULES (mandatory):
1. Output ONLY valid JSON — no explanation, no markdown code blocks, no extra text
2. Use ONLY tables, columns, and joins from the schema below
3. DO NOT invent fields, columns, or joins
4. Always include "reasoning" explaining the plan (for debugging)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REQUIRED OUTPUT SHAPE (copy this structure exactly):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "intent": "<aggregation|exploration|trace|comparison>",
  "tables": ["table1", "table2"],
  "joins": [
    {{
      "left_table": "source_table",
      "right_table": "joined_table",
      "join_type": "INNER",
      "on": "joined_table.foreign_key = source_table.primary_key"
    }}
  ],
  "filters": [
    {{"field": "column_name", "operator": "=", "value": "some_value"}},
    {{"field": "other_col", "operator": "IS NULL", "value": null}}
  ],
  "aggregation": "SUM(alias.column)",
  "group_by": ["alias.column1"],
  "order_by": null,
  "limit": 200,
  "reasoning": "Explain why these tables/joins/filters were chosen."
}}

FIELD RULES:
- "joins" entries have EXACTLY 4 keys: "left_table", "right_table", "join_type", "on"
  - "left_table": the FROM/driving table name (string, no column)
  - "right_table": the JOIN target table name (string, no column)
  - "join_type": always "INNER" unless you need "LEFT"
  - "on": the full join condition string e.g. "billing_document_items.billing_document = billing_document_headers.billing_document"
- "filters" operator must be one of: =, IN, >, <, >=, <=, BETWEEN, IS NULL
- "aggregation" and "group_by" are required when intent is "aggregation"; otherwise null and []
- "limit": use 1 for single-row aggregations, 200 otherwise

INTENT TYPES:
  - "aggregation": SUM/COUNT/AVG queries — always set aggregation + group_by
  - "exploration": Browse/list records — no aggregation
  - "trace": Follow O2C flow (order → delivery → billing → payment)
  - "comparison": Before/after analysis

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL JOIN PATHS — use these exact "on" strings and table names:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sales Order → Delivery Items:
  left_table="sales_order_headers", right_table="outbound_delivery_items"
  on="outbound_delivery_items.reference_sd_document = sales_order_headers.sales_order"

Delivery Items → Delivery Header:
  left_table="outbound_delivery_items", right_table="outbound_delivery_headers"
  on="outbound_delivery_items.delivery_document = outbound_delivery_headers.delivery_document"

Delivery Header → Billing Items:
  left_table="outbound_delivery_headers", right_table="billing_document_items"
  on="billing_document_items.reference_sd_document = outbound_delivery_headers.delivery_document"

Billing Items → Billing Header:
  left_table="billing_document_items", right_table="billing_document_headers"
  on="billing_document_items.billing_document = billing_document_headers.billing_document"

Billing Header → Journal Entry:
  left_table="billing_document_headers", right_table="journal_entry_items_ar"
  on="journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document"

Billing Header → Payment (use clearing_accounting_document — NOT invoice_reference or sales_document):
  left_table="billing_document_headers", right_table="payments_ar"
  on="payments_ar.clearing_accounting_document = billing_document_headers.accounting_document"

Billing Header → Customer:
  left_table="billing_document_headers", right_table="business_partners"
  on="billing_document_headers.sold_to_party = business_partners.customer"

Sales Order → Customer:
  left_table="sales_order_headers", right_table="business_partners"
  on="sales_order_headers.sold_to_party = business_partners.customer"

Payment → Customer:
  left_table="payments_ar", right_table="business_partners"
  on="payments_ar.customer = business_partners.customer"

Sales Order Items → Product:
  left_table="sales_order_items", right_table="products"
  on="sales_order_items.material = products.product"

Billing Items → Product:
  left_table="billing_document_items", right_table="products"
  on="billing_document_items.material = products.product"

Products → Description:
  left_table="products", right_table="product_descriptions"
  on="products.product = product_descriptions.product"

Sales Order Items → Plant:
  left_table="sales_order_items", right_table="plants"
  on="sales_order_items.production_plant = plants.plant"

Outbound Delivery Items → Plant:
  left_table="outbound_delivery_items", right_table="plants"
  on="outbound_delivery_items.plant = plants.plant"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILTER RULES (apply these whenever the relevant table is in use):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - Active billing docs: {{"field": "billing_doc_is_cancelled", "operator": "=", "value": false}}
  - Active orders: {{"field": "header_billing_block", "operator": "IS NULL", "value": null}}
  - Product description language: {{"field": "language", "operator": "=", "value": "EN"}}
  - Product group (use IN, NOT LIKE): {{"field": "product_group", "operator": "IN", "value": ["ZFG1001", "ZPKG004"]}}

AGGREGATION EXAMPLES:
  - Total revenue: "SUM(bdi.net_amount)"
  - Count of orders: "COUNT(DISTINCT so.sales_order)"
  - Average payment: "AVG(p.amount_in_transaction_currency)"
  - For a single scalar result (no group_by): set limit=1

{DB_SCHEMA}

Return ONLY the JSON object. No preamble, no explanation, no markdown fences.
"""


def build_query_plan(query: str) -> QueryPlan:
    """
    Generate a structured query plan for the given natural language query.

    Args:
        query: The resolved user query.

    Returns:
        A validated QueryPlan object (Pydantic model).

    Raises:
        ValueError: If JSON parsing or validation fails after retries.
    """
    last_error = None

    # Up to 2 attempts — on failure, include a structured correction prompt
    for attempt in range(1, 3):
        try:
            if attempt == 1:
                contents = f"Question: {query}"
            else:
                contents = (
                    f"Question: {query}\n\n"
                    f"Your previous response was rejected with this validation error:\n"
                    f"  {last_error}\n\n"
                    f"CRITICAL CORRECTIONS REQUIRED:\n"
                    f"1. Each join entry must have EXACTLY these 4 keys:\n"
                    f'   {{"left_table": "source_table_name", "right_table": "joined_table_name", "join_type": "INNER", "on": "joined_table.fk = source_table.pk"}}\n'
                    f"   - 'left_table' and 'right_table' are table NAMES only (no dots, no columns)\n"
                    f"   - 'on' is the full join condition string e.g. 'billing_document_items.billing_document = billing_document_headers.billing_document'\n"
                    f"2. 'tables', 'intent', and 'reasoning' are ALL required top-level fields\n"
                    f"3. 'filters' operator must be one of: =, IN, >, <, >=, <=, BETWEEN, IS NULL (NOT 'LIKE')\n"
                    f"4. Return ONLY the complete JSON object — no truncation, no markdown.\n"
                )

            response = gemini.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    temperature=0.0,
                    max_output_tokens=8000,  # Increased from 6000 to prevent truncation on first attempt
                ),
            )

            response_text = response.text.strip()
            
            # Clean and extract JSON from response
            response_text = extract_and_clean_json(response_text)
            
            # Parse JSON
            try:
                plan_dict = json.loads(response_text)
            except json.JSONDecodeError as e:
                log.error(f"[planner] JSON parsing failed on attempt {attempt}: {e}")
                log.error(f"[planner] cleaned response: {response_text[:300]}")
                raise
            
            # Validate with Pydantic
            plan = QueryPlan(**plan_dict)
            
            # ─────────────────────────────────────────────────────────────────────
            # Validate all joins against known paths
            # ─────────────────────────────────────────────────────────────────────
            
            for join in plan.joins:
                is_valid, reason = validate_join_against_known_paths(join)
                if not is_valid:
                    raise ValueError(f"Invalid join: {reason}")
            
            log.info(
                f"[planner] attempt {attempt} succeeded — "
                f"intent={plan.intent}, tables={plan.tables}, joins={len(plan.joins)}"
            )
            return plan

        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            log.warning("[planner] attempt %d failed: %s", attempt, last_error)

        except Exception as e:
            last_error = str(e)
            log.error("[planner] unexpected error on attempt %d: %s", attempt, e)

    raise ValueError(
        f"Query plan generation failed after 2 attempts. Last error: {last_error}"
    )