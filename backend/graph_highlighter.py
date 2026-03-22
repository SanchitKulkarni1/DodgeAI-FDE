"""
graph/highlighter.py — Entity ID extraction for graph node/edge highlighting.

extract_highlights() scans SQL result rows for columns whose names match
known entity ID patterns (e.g. "sales_order", "delivery_document",
"billing_document") and maps them to typed graph node descriptors.

It also derives edges between nodes that appear together in the same row
— e.g. a row containing both a sales_order and a delivery_document implies
an edge between those two nodes.

Output format (matches state.py GraphState fields):

    highlight_nodes: [
        {"id": "740509", "type": "sales_order",       "label": "Sales Order 740509"},
        {"id": "80738040", "type": "delivery",         "label": "Delivery 80738040"},
        {"id": "90504204", "type": "billing_document", "label": "Billing Doc 90504204"},
        ...
    ]

    highlight_edges: [
        {"source": "740509",   "target": "80738040",  "source_type": "sales_order",       "target_type": "delivery"},
        {"source": "80738040", "target": "90504204",  "source_type": "delivery",          "target_type": "billing_document"},
        ...
    ]

The frontend uses these to light up the relevant subgraph in the visualisation.
"""

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name → (entity_type, label_prefix) mapping.
#
# Keys are exact column names as they appear in query results.
# When the LLM generates an alias (e.g. "so" for sales_order) we can't
# predict it, so we match on suffix patterns too (see _infer_type).
# ---------------------------------------------------------------------------
_EXACT_COL_MAP: dict[str, tuple[str, str]] = {
    "sales_order":           ("sales_order",       "Sales Order"),
    "delivery_document":     ("delivery",          "Delivery"),
    "billing_document":      ("billing_document",  "Billing Doc"),
    "accounting_document":   ("journal_entry",     "Journal Entry"),
    "payment_doc":           ("payment",           "Payment"),
    "clearing_accounting_document": ("payment",    "Payment"),
    "customer":              ("customer",          "Customer"),
    "sold_to_party":         ("customer",          "Customer"),
    "business_partner":      ("customer",          "Customer"),
    "material":              ("product",           "Product"),
    "product":               ("product",           "Product"),
    "plant":                 ("plant",             "Plant"),
}

# Substring patterns for alias detection (applied when exact key not found)
_SUFFIX_PATTERNS: list[tuple[str, str, str]] = [
    ("sales_order",       "sales_order",       "Sales Order"),
    ("delivery_document", "delivery",          "Delivery"),
    ("billing_document",  "billing_document",  "Billing Doc"),
    ("accounting_doc",    "journal_entry",     "Journal Entry"),
    ("payment_doc",       "payment",           "Payment"),
    ("material",          "product",           "Product"),
]

# Define valid O2C edge directions (source_type → target_type).
# Only edges that follow the real flow are emitted.
_VALID_EDGE_PAIRS: set[tuple[str, str]] = {
    ("sales_order",       "delivery"),
    ("delivery",          "billing_document"),
    ("billing_document",  "journal_entry"),
    ("billing_document",  "payment"),
    ("customer",          "sales_order"),
    ("customer",          "billing_document"),
    ("product",           "sales_order"),
    ("product",           "billing_document"),
    ("plant",             "delivery"),
}


def _infer_type(col_name: str) -> tuple[str, str] | None:
    """
    Infer (entity_type, label_prefix) from a column name.
    Returns None if the column doesn't look like an entity ID column.
    """
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

    seen_nodes: dict[tuple[str, str], dict] = {}  # (id, type) → node dict
    seen_edges: set[tuple[str, str, str, str]] = set()
    highlight_edges: list[dict] = []

    for row in rows:
        # Map each column to a typed node (if the column is an entity ID column)
        row_nodes: list[dict] = []

        for col, val in row.items():
            if val is None or val == "":
                continue
            type_info = _infer_type(col)
            if type_info is None:
                continue
            entity_type, label_prefix = type_info
            entity_id = str(val)

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

                # Check both directions — emit whichever matches the flow
                if (src_type, tgt_type) in _VALID_EDGE_PAIRS:
                    edge_key = (src_node["id"], tgt_node["id"], src_type, tgt_type)
                elif (tgt_type, src_type) in _VALID_EDGE_PAIRS:
                    # Swap so source → target follows the O2C flow direction
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

    Args:
        results: Output from semantic_search() — list of dicts with
                 keys: entity_type, entity_id, label, score, extra.

    Returns:
        List of {"id": str, "type": str, "label": str}
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