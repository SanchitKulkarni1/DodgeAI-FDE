"""
llm/query_plan.py — Structured query plan schema using Pydantic.

Replaces plain-English plans with validated JSON structure.
Eliminates the "English text re-interpretation" problem in sql_generator.py
"""

from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, validator


class FilterCondition(BaseModel):
    """Represents a single WHERE condition."""
    
    field: str = Field(..., description="Column name (e.g., 'billing_doc_is_cancelled')")
    operator: Literal["=", "IN", ">", "<", ">=", "<=", "BETWEEN", "IS NULL"] = Field(
        default="=", 
        description="Comparison operator"
    )
    value: Any = Field(..., description="Filter value (or list for IN)")


class JoinCondition(BaseModel):
    """Represents a single JOIN between two tables."""
    
    left_table: str = Field(..., description="Source table (from FROM clause)")
    right_table: str = Field(..., description="Joined table")
    join_type: Literal["INNER", "LEFT", "RIGHT"] = Field(
        default="INNER",
        description="Type of join"
    )
    on: str = Field(..., description="JOIN condition (exact string from CRITICAL JOIN PATHS)")
    
    @validator("on")
    def validate_join_string(cls, v):
        """Ensure join string matches known critical path format."""
        # Will be validated against VALID_JOINS dict at execution time
        if not ("=" in v):
            raise ValueError(f"Invalid join string (must contain '='): {v}")
        return v


class QueryPlan(BaseModel):
    """Structured query plan output from planner."""
    
    intent: Literal["aggregation", "exploration", "trace", "comparison"] = Field(
        ...,
        description="Type of query: aggregation (revenue/count), exploration (browse), trace (flow), comparison (before/after)"
    )
    
    tables: List[str] = Field(
        ..., 
        description="Tables required for query (from schema)"
    )
    
    joins: List[JoinCondition] = Field(
        default_factory=list,
        description="JOIN paths between tables"
    )
    
    filters: Optional[List[FilterCondition]] = Field(
        default_factory=list,
        description="WHERE conditions"
    )
    
    aggregation: Optional[str] = Field(
        default=None,
        description="Aggregation function (e.g., 'SUM(bdi.net_amount)', 'COUNT(bd.billing_document)')"
    )
    
    group_by: Optional[List[str]] = Field(
        default_factory=list,
        description="GROUP BY columns"
    )
    
    order_by: Optional[str] = Field(
        default=None,
        description="ORDER BY clause (e.g., 'total_revenue DESC')"
    )
    
    limit: Optional[int] = Field(
        default=200,
        description="LIMIT clause (default 200)"
    )
    
    reasoning: str = Field(
        ...,
        description="Brief explanation of the query plan (for debugging)"
    )
    
    @validator("aggregation")
    def validate_aggregation(cls, v, values):
        """
        If aggregation is present, intent should be 'aggregation'.
        """
        if v and values.get("intent") not in ("aggregation", "comparison"):
            raise ValueError(
                f"Aggregation '{v}' provided but intent is '{values.get('intent')}' "
                "(should be 'aggregation' or 'comparison')"
            )
        return v
    
    @validator("group_by")
    def validate_group_by(cls, v, values):
        """
        If group_by is present, aggregation should also be present.
        """
        if v and not values.get("aggregation"):
            raise ValueError(
                "GROUP BY provided without aggregation function. "
                "Set both 'aggregation' and 'group_by'."
            )
        return v
    
    @validator("tables")
    def validate_tables(cls, v):
        """Tables list should not be empty."""
        if not v:
            raise ValueError("At least one table must be specified")
        return v
    
    class Config:
        """Pydantic config."""
        use_enum_values = False
        arbitrary_types_allowed = True


# ─────────────────────────────────────────────────────────────────────────
# Known Valid Join Paths (from schema)
# ─────────────────────────────────────────────────────────────────────────

KNOWN_JOIN_PATHS = {
    ("sales_order_headers", "outbound_delivery_items"): 
        "outbound_delivery_items.reference_sd_document = sales_order_headers.sales_order",
    
    ("outbound_delivery_headers", "outbound_delivery_items"): 
        "outbound_delivery_items.delivery_document = outbound_delivery_headers.delivery_document",
    
    ("billing_document_headers", "billing_document_items"): 
        "billing_document_items.billing_document = billing_document_headers.billing_document",
    
    ("outbound_delivery_headers", "billing_document_items"): 
        "billing_document_items.reference_sd_document = outbound_delivery_headers.delivery_document",
    
    ("billing_document_headers", "journal_entry_items_ar"): 
        "journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document",
    
    ("billing_document_headers", "payments_ar"): 
        "payments_ar.clearing_accounting_document = billing_document_headers.accounting_document",
    
    ("business_partners", "sales_order_headers"): 
        "sales_order_headers.sold_to_party = business_partners.customer",
    
    ("business_partners", "billing_document_headers"): 
        "billing_document_headers.sold_to_party = business_partners.customer",
    
    ("business_partners", "payments_ar"): 
        "payments_ar.customer = business_partners.customer",
    
    ("products", "sales_order_items"): 
        "sales_order_items.material = products.product",
    
    ("products", "billing_document_items"): 
        "billing_document_items.material = products.product",
    
    ("product_descriptions", "products"): 
        "products.product = product_descriptions.product",
    
    ("plants", "sales_order_items"): 
        "sales_order_items.production_plant = plants.plant",
    
    ("plants", "outbound_delivery_items"): 
        "outbound_delivery_items.plant = plants.plant",
}


def validate_join_against_known_paths(join: JoinCondition) -> tuple[bool, str]:
    """
    Validate that a join path is in KNOWN_JOIN_PATHS.
    
    Returns:
        (is_valid, reason)
    """
    left = join.left_table.lower()
    right = join.right_table.lower()
    
    # Check both orderings
    forward = (left, right)
    reverse = (right, left)
    
    if forward in KNOWN_JOIN_PATHS:
        expected = KNOWN_JOIN_PATHS[forward]
        if expected.lower() == join.on.lower():
            return True, f"Valid join: {expected}"
        else:
            return False, f"Invalid join condition. Expected: {expected}. Got: {join.on}"
    
    if reverse in KNOWN_JOIN_PATHS:
        expected = KNOWN_JOIN_PATHS[reverse]
        if expected.lower() == join.on.lower():
            return True, f"Valid join (reversed): {expected}"
        else:
            return False, f"Invalid join condition. Expected: {expected}. Got: {join.on}"
    
    return False, f"Join ({left} ↔ {right}) not in known paths. On: {join.on}"
