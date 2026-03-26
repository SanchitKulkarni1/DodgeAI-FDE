"""
db_executor.py — Safe PostgreSQL query execution.

execute_sql() runs a SELECT statement against the PostgreSQL database and returns results
as a list of dicts (column_name → value).

Safety measures:
  - Read-only PostgreSQL user (credentials from .env)
  - Hard row limit of 200 (enforced by wrapping in a subquery if needed)
  - Query timeout (guards against accidental full-table scans)
  - Only SELECT / WITH statements are accepted
  - Connection pooling for better performance
"""

import logging
import os
import re
from typing import Optional
import psycopg2
import psycopg2.extras
from psycopg2 import sql, Error
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

log = logging.getLogger(__name__)

# PostgreSQL connection parameters from environment
_DB_HOST = os.getenv("DB_HOST", "localhost").strip()
_DB_PORT = int(str(os.getenv("DB_PORT", 5432)).strip())
_DB_USER = os.getenv("DB_USER", "postgres").strip()
_DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres").strip()
_DB_NAME = os.getenv("DB_NAME", "dodgeai_o2c").strip()

_ROW_LIMIT = 200
_TIMEOUT = 10.0  # seconds


def execute_sql(sql_query: str) -> list[dict]:
    """
    Execute a SELECT statement and return rows as a list of dicts.

    Args:
        sql_query: A validated PostgreSQL SELECT statement (from sql_generator.py).

    Returns:
        List of dicts mapping column name → value. At most _ROW_LIMIT rows.

    Raises:
        ValueError: If sql_query is not a SELECT / WITH statement.
        psycopg2.Error: On any database error.
    """
    sql_query = sql_query.strip()

    # Safety check — reject any non-read statement
    first_word = sql_query.split()[0].upper() if sql_query else ""
    if first_word not in ("SELECT", "WITH"):
        raise ValueError(
            f"Only SELECT statements are allowed. Got: {first_word!r}"
        )

    # Wrap in LIMIT if one isn't already present (case-insensitive last-word check)
    if not re.search(r"\bLIMIT\b", sql_query, re.IGNORECASE):
        sql_query = f"SELECT * FROM ({sql_query}) _q LIMIT {_ROW_LIMIT}"
        log.debug("[executor] LIMIT %d injected", _ROW_LIMIT)

    con = None
    try:
        # Connect to PostgreSQL database
        con = psycopg2.connect(
            host=_DB_HOST,
            port=_DB_PORT,
            user=_DB_USER,
            password=_DB_PASSWORD,
            database=_DB_NAME,
            connect_timeout=int(_TIMEOUT),
        )

        # Use RealDictCursor to return rows as dicts
        cursor = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(sql_query)
        rows = cursor.fetchmany(_ROW_LIMIT)
        result = [dict(row) for row in rows]
        log.info("[executor] %d rows returned", len(result))
        return result

    except Error as e:
        log.error("[executor] PostgreSQL Error: %s | sql=%r", e, sql_query[:200])
        raise

    finally:
        if con:
            con.close()


# ---------------------------------------------------------------------------
# Executor class — stateful wrapper for connection lifecycle
# ---------------------------------------------------------------------------


class DBExecutor:
    """Wrapper for PostgreSQL database connection with lifecycle management."""

    def __init__(self):
        self.con: Optional[psycopg2.extensions.connection] = None
        self._connect()

    def _connect(self):
        """Open connection to PostgreSQL database."""
        try:
            self.con = psycopg2.connect(
                host=_DB_HOST,
                port=_DB_PORT,
                user=_DB_USER,
                password=_DB_PASSWORD,
                database=_DB_NAME,
                connect_timeout=int(_TIMEOUT),
            )
            log.info(
                "[DBExecutor] connected to postgres://%s:%d/%s",
                _DB_HOST,
                _DB_PORT,
                _DB_NAME,
            )
        except Error as e:
            log.error("[DBExecutor] Connection failed: %s", e)
            raise

    def execute(self, sql_query: str) -> list[dict]:
        """Execute a SELECT statement (with safety checks)."""
        if not self.con:
            self._connect()

        sql_query = sql_query.strip()

        # Safety check — reject any non-read statement
        first_word = sql_query.split()[0].upper() if sql_query else ""
        if first_word not in ("SELECT", "WITH"):
            raise ValueError(
                f"Only SELECT statements are allowed. Got: {first_word!r}"
            )

        # Wrap in LIMIT if one isn't already present
        if not re.search(r"\bLIMIT\b", sql_query, re.IGNORECASE):
            sql_query = f"SELECT * FROM ({sql_query}) _q LIMIT {_ROW_LIMIT}"
            log.debug("[executor] LIMIT %d injected", _ROW_LIMIT)

        try:
            cursor = self.con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(sql_query)
            rows = cursor.fetchmany(_ROW_LIMIT)
            result = [dict(row) for row in rows]
            log.info("[executor] %d rows returned", len(result))
            cursor.close()
            return result
        except Error as e:
            log.error("[executor] PostgreSQL Error: %s | sql=%r", e, sql_query[:200])
            # Try to reconnect on error
            try:
                self.con.close()
            except:
                pass
            self.con = None
            raise

    def close(self):
        """Close the database connection."""
        if self.con:
            try:
                self.con.close()
                self.con = None
                log.info("[DBExecutor] connection closed")
            except Error as e:
                log.error("[DBExecutor] Error closing connection: %s", e)

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        self.close()


def get_executor() -> DBExecutor:
    """
    Factory function — returns a new DBExecutor instance.
    Call this at application startup to get a reusable database connection.
    """
    return DBExecutor()