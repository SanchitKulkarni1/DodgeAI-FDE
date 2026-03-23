"""
graph/highlighter.py — Entity ID extraction for graph node/edge highlighting.

extract_highlights() scans SQL result rows for columns whose names match
known entity ID patterns and maps them to typed graph node descriptors.

FIX #3: Added _is_id_column() guard that rejects aggregate/metric columns
(total_revenue, billing_count, net_amount, etc.) even when their name
contains a substring that looks like an entity type. Without this, columns
like "total_revenue" or "billing_count" were being treated as entity IDs
and producing junk nodes in the graph.
"""

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name → (entity_type, label_prefix) mapping.
# ---------------------------------------------------------------------------
_EXACT_COL_MAP: dict[str, tuple[str, str]] = {
    "sales_order":                    ("sales_order",      "Sales Order"),
    "delivery_document":              ("delivery",         "Delivery"),
    "billing_document":               ("billing_document", "Billing Doc"),
    "accounting_document":            ("journal_entry",    "Journal Entry"),
    "payment_doc":                    ("payment",          "Payment"),
    "clearing_accounting_document":   ("payment",          "Payment"),
    "customer":                       ("customer",         "Customer"),
    "sold_to_party":                  ("customer",         "Customer"),
    "business_partner":               ("customer",         "Customer"),
    "material":                       ("product",          "Product"),
    "product":                        ("product",          "Product"),
    "plant":                          ("plant",            "Plant"),
}

# Substring patterns for alias detection
_SUFFIX_PATTERNS: list[tuple[str, str, str]] = [
    ("sales_order",       "sales_order",      "Sales Order"),
    ("delivery_document", "delivery",         "Delivery"),
    ("billing_document",  "billing_document", "Billing Doc"),
    ("accounting_doc",    "journal_entry",    "Journal Entry"),
    ("payment_doc",       "payment",          "Payment"),
    ("material",          "product",          "Product"),
]

# ---------------------------------------------------------------------------
# FIX #3: Words that disqualify a column from being an entity ID column.
# A column named "total_revenue" or "billing_count" is a metric, not an ID.
# ---------------------------------------------------------------------------
_METRIC_TOKENS = frozenset({
    "total", "count", "sum", "amount", "revenue", "quantity", "qty",
    "net", "gross", "avg", "average", "num", "number", "pct", "percent",
    "rate", "ratio", "value", "price", "cost", "balance",
})

def _is_id_column(col_name: str) -> bool:
    """
    Return True only if the column is likely to hold an entity ID value
    (not an aggregated metric like total_revenue or billing_count).
    """
    tokens = set(col_name.lower().split("_"))
    return not tokens.intersection(_METRIC_TOKENS)


# Valid O2C edge directions
_VALID_EDGE_PAIRS: set[tuple[str, str]] = {
    ("sales_order",      "delivery"),
    ("delivery",         "billing_document"),
    ("billing_document", "journal_entry"),
    ("billing_document", "payment"),
    ("customer",         "sales_order"),
    ("customer",         "billing_document"),
    ("product",          "sales_order"),
    ("product",          "billing_document"),
    ("plant",            "delivery"),
}


def _infer_type(col_name: str) -> tuple[str, str] | None:
    """
    Infer (entity_type, label_prefix) from a column name.

    FIX #3: Applies _is_id_column() guard before any pattern matching so
    aggregate columns are rejected at the door.

    Returns None if the column doesn't look like an entity ID column.
    """
    # FIX #3: reject metric columns immediately
    if not _is_id_column(col_name):
        return None

    if col_name in _EXACT_COL_MAP:
        return _EXACT_COL_MAP[col_name]

    col_lower = col_name.lower()
    for pattern, etype, prefix in _SUFFIX_PATTERNS:
        if pattern in col_lower:
            return etype, prefix

    return None


def extract_highlights(
    rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Extract highlight_nodes and highlight_edges from SQL result rows.

    Args:
        rows: List of dicts from execute_sql() — each dict is one result row.

    Returns:
        (highlight_nodes, highlight_edges) — both are lists of dicts,
        deduplicated. Empty lists if rows is empty.
    """
    if not rows:
        return [], []

    seen_nodes: dict[tuple[str, str], dict] = {}
    seen_edges: set[tuple[str, str, str, str]] = set()
    highlight_edges: list[dict] = []

    for row in rows:
        row_nodes: list[dict] = []

        for col, val in row.items():
            if val is None or val == "":
                continue

            # FIX #3: _infer_type now gates on _is_id_column internally
            type_info = _infer_type(col)
            if type_info is None:
                continue

            entity_type, label_prefix = type_info
            entity_id = str(val)

            # Extra sanity: entity IDs should not look like numbers > 1M
            # (which would be amounts/quantities, not document IDs)
            try:
                numeric_val = float(entity_id)
                if numeric_val > 1_000_000:
                    # Likely a monetary amount that slipped through
                    log.debug(
                        "[highlighter] skipping col=%r val=%r — looks like a large number",
                        col, entity_id,
                    )
                    continue
            except ValueError:
                pass  # non-numeric string — fine, it's an ID

            node_key = (entity_id, entity_type)
            if node_key not in seen_nodes:
                node = {
                    "id":    entity_id,
                    "type":  entity_type,
                    "label": f"{label_prefix} {entity_id}",
                }
                seen_nodes[node_key] = node

            row_nodes.append(seen_nodes[node_key])

        # Derive edges between nodes that appear in the same row
        for i, src_node in enumerate(row_nodes):
            for tgt_node in row_nodes[i + 1:]:
                src_type = src_node["type"]
                tgt_type = tgt_node["type"]

                if (src_type, tgt_type) in _VALID_EDGE_PAIRS:
                    edge_key = (src_node["id"], tgt_node["id"], src_type, tgt_type)
                elif (tgt_type, src_type) in _VALID_EDGE_PAIRS:
                    src_node, tgt_node = tgt_node, src_node
                    src_type, tgt_type = tgt_type, src_type
                    edge_key = (src_node["id"], tgt_node["id"], src_type, tgt_type)
                else:
                    continue

                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    highlight_edges.append({
                        "source":      src_node["id"],
                        "target":      tgt_node["id"],
                        "source_type": src_type,
                        "target_type": tgt_type,
                    })

    highlight_nodes = list(seen_nodes.values())

    log.info(
        "[highlighter] %d nodes, %d edges extracted from %d rows",
        len(highlight_nodes), len(highlight_edges), len(rows),
    )
    return highlight_nodes, highlight_edges


def nodes_from_semantic_results(results: list[dict]) -> list[dict]:
    """
    Convert semantic search result dicts into highlight_nodes format.
    """
    seen: dict[tuple[str, str], dict] = {}
    for r in results:
        key = (r["entity_id"], r["entity_type"])
        if key not in seen:
            seen[key] = {
                "id":    r["entity_id"],
                "type":  r["entity_type"],
                "label": r["label"],
            }
    return list(seen.values())