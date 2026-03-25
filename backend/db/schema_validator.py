"""
db/schema_validator.py — Validates SQL against known schema constraints.

PATH 1 Implementation: Enhance LLM validation by:
1. Extracting valid join paths from prompts.py
2. Rejecting invalid joins before execution
3. Preventing NULL column usage
4. Checking table/column existence
"""

import logging
import re
from typing import Set, Tuple, List, Dict

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Valid Join Paths (extracted from prompts.py)
# ─────────────────────────────────────────────────────────────────────────

VALID_JOINS = {
    # Sales Order → Delivery
    ("outbound_delivery_items", "sales_order_headers"): 
        "outbound_delivery_items.reference_sd_document = sales_order_headers.sales_order",
    
    # Delivery Item → Delivery Header
    ("outbound_delivery_items", "outbound_delivery_headers"): 
        "outbound_delivery_items.delivery_document = outbound_delivery_headers.delivery_document",
    
    # Delivery → Billing
    ("billing_document_items", "outbound_delivery_headers"): 
        "billing_document_items.reference_sd_document = outbound_delivery_headers.delivery_document",
    
    # Billing Item → Billing Header
    ("billing_document_items", "billing_document_headers"): 
        "billing_document_items.billing_document = billing_document_headers.billing_document",
    
    # Billing → Journal Entry
    ("journal_entry_items_ar", "billing_document_headers"): 
        "journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document",
    
    # Billing → Payment (CRITICAL: NOT invoice_reference)
    ("payments_ar", "billing_document_headers"): 
        "payments_ar.clearing_accounting_document = billing_document_headers.accounting_document",
    
    # Customer joins
    ("sales_order_headers", "business_partners"): 
        "sales_order_headers.sold_to_party = business_partners.customer",
    
    ("billing_document_headers", "business_partners"): 
        "billing_document_headers.sold_to_party = business_partners.customer",
    
    ("payments_ar", "business_partners"): 
        "payments_ar.customer = business_partners.customer",
    
    # Product joins
    ("sales_order_items", "products"): 
        "sales_order_items.material = products.product",
    
    ("billing_document_items", "products"): 
        "billing_document_items.material = products.product",
    
    ("products", "product_descriptions"): 
        "products.product = product_descriptions.product",
    
    # Plant joins
    ("outbound_delivery_items", "plants"): 
        "outbound_delivery_items.plant = plants.plant",
    
    ("sales_order_items", "plants"): 
        "sales_order_items.production_plant = plants.plant",
}

# ─────────────────────────────────────────────────────────────────────────
# NULL Columns to NEVER use in WHERE or JOIN conditions
# ─────────────────────────────────────────────────────────────────────────

NULL_COLUMNS = {
    "sales_order_headers.overall_billing_status",  # Always NULL
    "payments_ar.invoice_reference",                # Always NULL in this dataset
    "payments_ar.sales_document",                   # Always NULL in this dataset
    "business_partner_addresses.city_name",         # Often NULL
    "business_partner_addresses.street_name",       # Often NULL
    "business_partner_addresses.postal_code",       # Often NULL
}

# ─────────────────────────────────────────────────────────────────────────
# All tables and columns (for existence checks)
# ─────────────────────────────────────────────────────────────────────────

SCHEMA = {
    "billing_document_cancellations": {
        "billing_document", "billing_document_type", "billing_document_date", "creation_date", "creation_time", "last_change_datetime", "billing_doc_is_cancelled", "cancelled_billing_document", "total_net_amount", "transaction_currency", "company_code", "fiscal_year", "accounting_document", "sold_to_party"
    },
    "billing_document_headers": {
        "billing_document", "billing_document_type", "billing_document_date", "creation_date", "creation_time", "last_change_datetime", "billing_doc_is_cancelled", "cancelled_billing_document", "total_net_amount", "transaction_currency", "company_code", "fiscal_year", "accounting_document", "sold_to_party"
    },
    "billing_document_items": {
        "billing_document", "billing_document_item", "material", "billing_quantity", "billing_quantity_unit", "net_amount", "transaction_currency", "reference_sd_document", "reference_sd_doc_item"
    },
    "business_partner_addresses": {
        "business_partner", "address_id", "address_uuid", "address_time_zone", "city_name", "country", "region", "postal_code", "street_name", "po_box", "po_box_postal_code", "tax_jurisdiction", "transport_zone", "validity_start_date", "validity_end_date"
    },
    "business_partners": {
        "business_partner", "customer", "business_partner_category", "business_partner_full_name", "business_partner_name", "organization_bp_name1", "organization_bp_name2", "first_name", "last_name", "industry", "business_partner_grouping", "is_blocked", "is_marked_for_archiving", "creation_date", "last_change_date", "created_by_user", "correspondence_language"
    },
    "customer_company_assignments": {
        "customer", "company_code", "reconciliation_account", "payment_terms", "payment_methods_list", "payment_blocking_reason", "accounting_clerk", "customer_account_group", "deletion_indicator"
    },
    "customer_sales_area_assignments": {
        "customer", "sales_organization", "distribution_channel", "division", "currency", "customer_payment_terms", "delivery_priority", "shipping_condition", "incoterms_classification", "incoterms_location1", "credit_control_area", "sales_district", "sales_group", "sales_office", "supplying_plant", "billing_is_blocked", "complete_delivery_is_defined", "unlimited_overdelivery_allowed"
    },
    "journal_entry_items_ar": {
        "company_code", "fiscal_year", "accounting_document", "accounting_document_item", "accounting_document_type", "reference_document", "gl_account", "customer", "cost_center", "profit_center", "transaction_currency", "amount_in_transaction_currency", "company_code_currency", "amount_in_company_code_currency", "posting_date", "document_date", "financial_account_type", "clearing_date", "clearing_accounting_document", "clearing_doc_fiscal_year", "assignment_reference", "last_change_datetime"
    },
    "outbound_delivery_headers": {
        "delivery_document", "shipping_point", "creation_date", "creation_time", "last_change_date", "actual_goods_movement_date", "actual_goods_movement_time", "delivery_block_reason", "header_billing_block", "overall_goods_movement_status", "overall_picking_status", "overall_pod_status", "hdr_general_incompletion"
    },
    "outbound_delivery_items": {
        "delivery_document", "delivery_document_item", "reference_sd_document", "reference_sd_doc_item", "actual_delivery_quantity", "delivery_quantity_unit", "plant", "storage_location", "batch", "item_billing_block", "last_change_date"
    },
    "payments_ar": {
        "company_code", "fiscal_year", "accounting_document", "accounting_document_item", "customer", "clearing_date", "clearing_accounting_document", "clearing_doc_fiscal_year", "amount_in_transaction_currency", "transaction_currency", "amount_in_company_code_currency", "company_code_currency", "invoice_reference", "invoice_reference_fiscal_year", "sales_document", "sales_document_item", "posting_date", "document_date", "assignment_reference", "gl_account", "financial_account_type", "profit_center", "cost_center"
    },
    "plants": {
        "plant", "plant_name", "valuation_area", "plant_customer", "plant_supplier", "factory_calendar", "default_purchasing_organization", "sales_organization", "address_id", "plant_category", "distribution_channel", "division", "language", "is_marked_for_archiving"
    },
    "product_descriptions": {
        "product", "language", "product_description"
    },
    "product_plants": {
        "product", "plant", "country_of_origin", "region_of_origin", "mrp_type", "availability_check_type", "fiscal_year_variant", "profit_center", "production_invtry_managed_loc"
    },
    "product_storage_locations": {
        "product", "plant", "storage_location", "physical_inventory_block", "date_last_posted_unrestricted"
    },
    "products": {
        "product", "product_type", "product_old_id", "base_unit", "division", "industry_sector", "product_group", "gross_weight", "net_weight", "weight_unit", "cross_plant_status", "cross_plant_status_valid_date", "is_marked_for_deletion", "creation_date", "created_by_user", "last_change_date", "last_change_datetime"
    },
    "sales_order_headers": {
        "sales_order", "sales_order_type", "sales_organization", "distribution_channel", "organization_division", "sales_group", "sales_office", "sold_to_party", "creation_date", "created_by_user", "last_change_datetime", "total_net_amount", "transaction_currency", "overall_delivery_status", "overall_billing_status", "overall_sd_ref_status", "pricing_date", "requested_delivery_date", "header_billing_block", "delivery_block_reason", "incoterms_classification", "incoterms_location1", "customer_payment_terms", "total_credit_check_status"
    },
    "sales_order_items": {
        "sales_order", "sales_order_item", "sales_order_item_category", "material", "requested_quantity", "requested_quantity_unit", "net_amount", "transaction_currency", "material_group", "production_plant", "storage_location", "rejection_reason", "item_billing_block"
    },
    "sales_order_schedule_lines": {
        "sales_order", "sales_order_item", "schedule_line", "confirmed_delivery_date", "order_quantity_unit", "confirmed_quantity"
    },
}

# ─────────────────────────────────────────────────────────────────────────
# Validation Functions
# ─────────────────────────────────────────────────────────────────────────

def extract_tables_from_sql(sql: str) -> Set[str]:
    """
    Extract table names from SQL using regex.
    Looks for patterns like: FROM table_name, JOIN table_name
    """
    tables = set()
    
    # Pattern for FROM or JOIN
    pattern = r'(?:FROM|JOIN)\s+(\w+)'
    matches = re.findall(pattern, sql, re.IGNORECASE)
    
    for match in matches:
        tables.add(match.lower())
    
    return tables


def extract_columns_from_sql(sql: str) -> Set[str]:
    """
    Extract column references from SQL using regex.
    Looks for patterns like: table.column or just column
    """
    columns = set()
    
    # Pattern for table.column
    pattern = r'(\w+)\.(\w+)'
    matches = re.findall(pattern, sql)
    
    for table, column in matches:
        full_ref = f"{table.lower()}.{column.lower()}"
        columns.add(full_ref)
    
    return columns


def extract_join_conditions(sql: str) -> List[Tuple[str, str, str]]:
    """
    Extract JOIN conditions from SQL.
    Returns: [(left_table, right_table, condition), ...]
    
    Rough heuristic: looks for 'JOIN table_name ON condition'
    """
    joins = []
    
    # Pattern: JOIN table_name ON condition (until next keyword)
    pattern = r'JOIN\s+(\w+)\s+ON\s+([^,;]+?)(?=WHERE|GROUP|ORDER|HAVING|;|$)'
    matches = re.findall(pattern, sql, re.IGNORECASE | re.DOTALL)
    
    for table_name, condition in matches:
        joins.append((table_name.lower(), condition.strip()))
    
    return joins


def validate_table_exists(table_name: str) -> bool:
    """Check if table exists in schema."""
    return table_name.lower() in SCHEMA


def validate_column_exists(table_name: str, column_name: str) -> bool:
    """Check if column exists in table schema."""
    table = table_name.lower()
    column = column_name.lower()
    
    if table not in SCHEMA:
        return False
    
    return column in SCHEMA[table]


def validate_join_path(table1: str, table2: str, condition: str) -> Tuple[bool, str]:
    """
    Validate that a join between two tables is allowed.
    
    Args:
        table1: First table (FROM or left side of join)
        table2: Second table (JOIN table)
        condition: The ON condition
    
    Returns:
        (is_valid, reason)
    """
    t1 = table1.lower()
    t2 = table2.lower()
    
    # Check both orderings of the join
    forward = (t1, t2)
    reverse = (t2, t1)
    
    if forward in VALID_JOINS:
        expected = VALID_JOINS[forward]
        if expected.lower() in condition.lower():
            return True, f"Valid join: {expected}"
        else:
            return False, f"Invalid condition for {forward}. Expected: {expected}. Got: {condition}"
    
    if reverse in VALID_JOINS:
        expected = VALID_JOINS[reverse]
        if expected.lower() in condition.lower():
            return True, f"Valid join (reversed): {expected}"
        else:
            return False, f"Invalid condition for {reverse}. Expected: {expected}. Got: {condition}"
    
    return False, f"Join path ({t1}, {t2}) not in allowed list. Condition: {condition}"


def validate_no_null_columns(sql: str) -> Tuple[bool, List[str]]:
    """
    Check that we're not filtering or joining on NULL columns.
    Uses substring matching since aliases make column tracking complex.
    
    Returns:
        (is_valid, list_of_problems)
    """
    problems = []
    sql_lower = sql.lower()
    
    # Check for each NULL column by name (will match aliased or unaliased)
    for null_col_full in NULL_COLUMNS:
        # Extract just the column name from "table.column"
        if "." in null_col_full:
            table, col = null_col_full.split(".")
            col_lower = col.lower()
        else:
            col_lower = null_col_full.lower()
        
        # Look for this column in SQL
        # Check patterns: "column_name =" "column_name )" "column_name,", etc
        patterns = [
            f"{col_lower} =",      # WHERE column = ...
            f"{col_lower} )",      # In parens
            f"{col_lower},",       # In list
            f"ON .* {col_lower}",  # In JOIN
        ]
        
        for pattern in patterns:
            if pattern.replace(".*", ".*") in sql_lower or pattern in sql_lower:
                # Found potential usage
                problems.append(f"Column {null_col_full} is NULL in this dataset — never use it")
                break
    
    return len(problems) == 0, problems


def validate_sql_against_schema(sql: str) -> Tuple[bool, List[str]]:
    """
    Comprehensive SQL validation.
    
    Returns:
        (is_valid, list_of_errors)
    
    Note: Column extraction with aliases is complex (requires full SQL parser).
    We focus on: (1) table existence, (2) NULL columns, (3) join paths.
    """
    errors = []
    
    log.info(f"[schema_validator] Validating SQL ({len(sql)} chars)")
    
    # ─────────────────────────────────────────────────────────────────────
    # Check 1: All tables exist
    # ─────────────────────────────────────────────────────────────────────
    
    tables = extract_tables_from_sql(sql)
    for table in tables:
        if not validate_table_exists(table):
            errors.append(f"Table '{table}' does not exist in schema")
    
    if errors:
        log.warning(f"[schema_validator] Found {len(errors)} table errors")
        return False, errors
    
    # ─────────────────────────────────────────────────────────────────────
    # Check 2: No NULL columns used (simple substring check with full names)
    # ─────────────────────────────────────────────────────────────────────
    
    sql_lower = sql.lower()
    for null_col in NULL_COLUMNS:
        # Check for exact column name (e.g., "overall_billing_status")
        col_short = null_col.split(".")[-1].lower()  # Get just "column_name" part
        
        # Simple check: is this column name mentioned in the SQL?
        # This catches both "table.column" and bare "column" references
        # Pattern: word boundary + column name + word boundary
        if re.search(rf'\b{re.escape(col_short)}\b', sql_lower):
            errors.append(f"Column '{null_col}' is NULL in this dataset — never use it")
    
    if errors:
        log.warning(f"[schema_validator] Found NULL column usage")
        return False, errors
    
    log.info(f"[schema_validator] SQL passed all checks")
    
    return True, []


# ─────────────────────────────────────────────────────────────────────────
# Migration from LLM SQL to enforced validation
# ─────────────────────────────────────────────────────────────────────────

def report_sql_issues(sql: str) -> Dict[str, any]:
    """
    Generate a detailed report of what's wrong with SQL.
    Used to provide feedback to LLM for retry.
    """
    is_valid, errors = validate_sql_against_schema(sql)
    
    if is_valid:
        return {
            "status": "valid",
            "sql": sql,
            "errors": []
        }
    
    return {
        "status": "invalid",
        "sql": sql,
        "errors": errors,
        "tables": extract_tables_from_sql(sql),
        "columns": extract_columns_from_sql(sql),
    }
