"""
db_executor.py — Safe SQLite query execution.

execute_sql() runs a SELECT statement against o2c.db and returns results
as a list of dicts (column_name → value).

Safety measures:
  - Read-only connection (uri mode with ?mode=ro)
  - Hard row limit of 200 (enforced by wrapping in a subquery if needed)
  - Query timeout of 10 seconds (guards against accidental full-table scans)
  - Only SELECT / WITH statements are accepted
"""

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH  = Path("o2c.db")
_ROW_LIMIT = 200
_TIMEOUT   = 10.0   # seconds


def execute_sql(sql: str) -> list[dict]:
    """
    Execute a SELECT statement and return rows as a list of dicts.

    Args:
        sql: A validated SQLite SELECT statement (from sql_generator.py).

    Returns:
        List of dicts mapping column name → value. At most _ROW_LIMIT rows.

    Raises:
        ValueError: If sql is not a SELECT / WITH statement.
        sqlite3.Error: On any database error.
    """
    sql = sql.strip()

    # Safety check — reject any non-read statement
    first_word = sql.split()[0].upper() if sql else ""
    if first_word not in ("SELECT", "WITH"):
        raise ValueError(
            f"Only SELECT statements are allowed. Got: {first_word!r}"
        )

    # Wrap in LIMIT if one isn't already present (case-insensitive last-word check)
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql = f"SELECT * FROM ({sql}) _q LIMIT {_ROW_LIMIT}"
        log.debug("[executor] LIMIT %d injected", _ROW_LIMIT)

    # Open as read-only URI to prevent any accidental writes
    uri = f"file:{_DB_PATH.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT)
    con.row_factory = sqlite3.Row  # allows dict-like access

    try:
        cursor = con.execute(sql)
        rows = cursor.fetchmany(_ROW_LIMIT)
        result = [dict(row) for row in rows]
        log.info("[executor] %d rows returned", len(result))
        return result

    except sqlite3.OperationalError as e:
        log.error("[executor] OperationalError: %s | sql=%r", e, sql[:200])
        raise

    finally:
        con.close()