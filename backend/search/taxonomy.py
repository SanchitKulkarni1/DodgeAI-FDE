"""
search/taxonomy.py — Product category taxonomy for the O2C dataset.

Maps abstract category names (e.g. "skincare", "haircare") to actual product
IDs from the database using keyword matching against product descriptions.

This is a static, rules-based taxonomy. For a production system, this could be
replaced with an LLM-based classifier or a maintained category table.

Usage:
    from search.taxonomy import resolve_category_to_products, infer_category

    # Get actual product IDs for a category
    product_ids = resolve_category_to_products("skincare", "o2c.db")

    # Infer category from a product description
    category = infer_category("DAILY MOISTURISING CREAM 50G SHEA BUTTER")
    # → "skincare"
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static taxonomy: category → list of keyword fragments to match against
# product_descriptions.product_description (case-insensitive LIKE).
# ---------------------------------------------------------------------------
PRODUCT_CATEGORIES: dict[str, list[str]] = {
    "skincare": [
        "cream", "moistur", "serum", "lotion", "face wash", "facewash",
        "glow", "sunscreen", "cleanser", "scrub", "toner", "mask",
        "bodywash", "body wash", "skin",
    ],
    "haircare": [
        "hair", "shampoo", "conditioner", "beard", "hair oil",
        "hair growth", "hair cream", "hair wax",
    ],
    "fragrance": [
        "perfume", "fragrance", "deodorant", "body spray",
        "cologne", "mist", "deo", "edp", "edt",
    ],
    "body care": [
        "body lotion", "bodylotion", "body cream", "soap", "shower",
        "hand wash", "sanitiz",
    ],
}

# Reverse index: keyword → category (built once at import time)
_KEYWORD_TO_CATEGORY: dict[str, str] = {}
for _cat, _keywords in PRODUCT_CATEGORIES.items():
    for _kw in _keywords:
        _KEYWORD_TO_CATEGORY[_kw.lower()] = _cat


def infer_category(product_description: str) -> str | None:
    """
    Infer the product category from a product description string.

    Args:
        product_description: e.g. "DAILY MOISTURISING CREAM 50G SHEA BUTTER"

    Returns:
        Category name (e.g. "skincare") or None if no match.
    """
    if not product_description:
        return None

    desc_lower = product_description.lower()

    # Check keywords longest-first for best specificity
    for keyword in sorted(_KEYWORD_TO_CATEGORY, key=len, reverse=True):
        if keyword in desc_lower:
            return _KEYWORD_TO_CATEGORY[keyword]

    return None


def resolve_category_to_products(category: str,
                                  db_path: str | Path = "o2c.db",
                                  ) -> list[str]:
    """
    Map a category name to actual product IDs from the database.

    Args:
        category: e.g. "skincare", "haircare", "fragrance"
        db_path:  Path to the SQLite database.

    Returns:
        List of product IDs matching the category.
        Empty list if the category is unknown or no products match.
    """
    keywords = PRODUCT_CATEGORIES.get(category.lower())
    if not keywords:
        log.info("[taxonomy] unknown category %r", category)
        return []

    # Build OR conditions for each keyword
    conditions = " OR ".join(
        f"LOWER(pd.product_description) LIKE '%{kw.lower()}%'"
        for kw in keywords
    )
    sql = f"""
        SELECT DISTINCT p.product
        FROM products p
        JOIN product_descriptions pd ON p.product = pd.product
        WHERE pd.language = 'EN' AND ({conditions})
    """

    try:
        con = sqlite3.connect(str(db_path))
        rows = con.execute(sql).fetchall()
        con.close()
        product_ids = [r[0] for r in rows]
        log.info(
            "[taxonomy] category %r → %d products: %s",
            category, len(product_ids),
            product_ids[:5],
        )
        return product_ids
    except Exception as e:
        log.error("[taxonomy] DB query failed: %s", e)
        return []


def detect_category_in_query(query: str) -> str | None:
    """
    Detect if the user query mentions a known product category.

    Args:
        query: Natural language query string.

    Returns:
        Category name if found, else None.
    """
    query_lower = query.lower()
    for category in PRODUCT_CATEGORIES:
        if category in query_lower:
            return category
    return None
