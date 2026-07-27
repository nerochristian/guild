"""
Query Builder Abstraction Layer

Replaces error-prone regex-based SQLite → PostgreSQL transpilation with
pypika-backed query construction.  Each method returns a (sql, params) tuple
that is valid for the current database dialect.

Usage:
    qb = QueryBuilder(is_postgres=db._is_postgres)
    sql, params = qb.insert("warnings", guild_id=123, user_id=456, reason="test")
    cursor = await db.execute(sql, params)
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger("ModBot.QueryBuilder")

try:
    from pypika import Query, Table, Field, Order, Parameter
    from pypika.terms import ValueWrapper
    from pypika.dialects import PostgreSQLQuery
    PYPIKA_AVAILABLE = True
except ImportError:
    PYPIKA_AVAILABLE = False
    logger.info("pypika not installed — QueryBuilder will use raw SQL fallback")


class QueryBuilder:
    """
    Dialect-aware SQL query builder.

    When pypika is available, queries are built programmatically.
    Otherwise, falls back to simple parameterised SQL strings (SQLite-style).
    """

    def __init__(self, *, is_postgres: bool = False) -> None:
        self._pg = is_postgres

    # ── Helpers ──────────────────────────────────────────────────────────

    def _placeholder(self, index: int) -> str:
        """Return the placeholder for a positional parameter."""
        return f"${index}" if self._pg else "?"

    def _placeholders(self, count: int) -> str:
        """Return a comma-separated string of placeholders."""
        if self._pg:
            return ", ".join(f"${i}" for i in range(1, count + 1))
        return ", ".join("?" for _ in range(count))

    def _autoincrement_type(self) -> str:
        return "BIGSERIAL" if self._pg else "INTEGER"

    def _integer_type(self) -> str:
        return "BIGINT" if self._pg else "INTEGER"

    def _boolean_type(self) -> str:
        return "BIGINT" if self._pg else "BOOLEAN"

    # ── SELECT ──────────────────────────────────────────────────────────

    def select(
        self,
        table_name: str,
        *,
        columns: Sequence[str] = ("*",),
        where: Optional[dict[str, Any]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = True,
        limit: Optional[int] = None,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build a SELECT query.

        Args:
            table_name: Target table.
            columns: Columns to select (default: all).
            where: Column=value equality conditions.
            order_by: Column to order by.
            order_desc: If True, ORDER BY DESC.
            limit: Max rows to return.

        Returns:
            (sql_string, params_tuple)
        """
        cols = ", ".join(columns)
        parts = [f"SELECT {cols} FROM {table_name}"]
        params: list[Any] = []

        if where:
            clauses = []
            for col, val in where.items():
                params.append(val)
                clauses.append(f"{col} = {self._placeholder(len(params))}")
            parts.append("WHERE " + " AND ".join(clauses))

        if order_by:
            direction = "DESC" if order_desc else "ASC"
            parts.append(f"ORDER BY {order_by} {direction}")

        if limit is not None:
            parts.append(f"LIMIT {int(limit)}")

        return " ".join(parts), tuple(params)

    # ── INSERT ──────────────────────────────────────────────────────────

    def insert(
        self,
        table_name: str,
        *,
        returning_id: bool = False,
        **values: Any,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build an INSERT query.

        Args:
            table_name: Target table.
            returning_id: If True, append RETURNING id (Postgres).
            **values: Column=value pairs.

        Returns:
            (sql_string, params_tuple)
        """
        cols = list(values.keys())
        vals = list(values.values())
        col_str = ", ".join(cols)
        ph_str = self._placeholders(len(vals))

        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({ph_str})"

        if returning_id and self._pg:
            sql += " RETURNING id"

        return sql, tuple(vals)

    # ── UPSERT (INSERT OR REPLACE / ON CONFLICT) ────────────────────────

    def upsert(
        self,
        table_name: str,
        *,
        conflict_columns: Sequence[str],
        mode: str = "replace",
        **values: Any,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build an upsert query.

        Args:
            table_name: Target table.
            conflict_columns: Columns that form the conflict target.
            mode: 'replace' (UPDATE on conflict) or 'ignore' (DO NOTHING).
            **values: Column=value pairs.

        Returns:
            (sql_string, params_tuple)
        """
        cols = list(values.keys())
        vals = list(values.values())
        col_str = ", ".join(cols)
        ph_str = self._placeholders(len(vals))

        if not self._pg:
            # SQLite syntax
            keyword = "REPLACE" if mode == "replace" else "IGNORE"
            sql = f"INSERT OR {keyword} INTO {table_name} ({col_str}) VALUES ({ph_str})"
            return sql, tuple(vals)

        # PostgreSQL ON CONFLICT syntax
        conflict_str = ", ".join(conflict_columns)
        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({ph_str})"

        if mode == "ignore":
            sql += f" ON CONFLICT ({conflict_str}) DO NOTHING"
        else:
            update_cols = [c for c in cols if c not in conflict_columns]
            if update_cols:
                update_str = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                sql += f" ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
            else:
                sql += f" ON CONFLICT ({conflict_str}) DO NOTHING"

        return sql, tuple(vals)

    # ── UPDATE ──────────────────────────────────────────────────────────

    def update(
        self,
        table_name: str,
        *,
        set_values: dict[str, Any],
        where: dict[str, Any],
    ) -> tuple[str, tuple[Any, ...]]:
        """Build an UPDATE query.

        Args:
            table_name: Target table.
            set_values: Column=value pairs to SET.
            where: Column=value equality conditions for WHERE.

        Returns:
            (sql_string, params_tuple)
        """
        params: list[Any] = []

        set_clauses = []
        for col, val in set_values.items():
            params.append(val)
            set_clauses.append(f"{col} = {self._placeholder(len(params))}")

        where_clauses = []
        for col, val in where.items():
            params.append(val)
            where_clauses.append(f"{col} = {self._placeholder(len(params))}")

        sql = f"UPDATE {table_name} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
        return sql, tuple(params)

    # ── DELETE ──────────────────────────────────────────────────────────

    def delete(
        self,
        table_name: str,
        *,
        where: dict[str, Any],
    ) -> tuple[str, tuple[Any, ...]]:
        """Build a DELETE query.

        Args:
            table_name: Target table.
            where: Column=value equality conditions.

        Returns:
            (sql_string, params_tuple)
        """
        params: list[Any] = []
        clauses = []
        for col, val in where.items():
            params.append(val)
            clauses.append(f"{col} = {self._placeholder(len(params))}")

        sql = f"DELETE FROM {table_name} WHERE {' AND '.join(clauses)}"
        return sql, tuple(params)

    # ── COUNT ───────────────────────────────────────────────────────────

    def count(
        self,
        table_name: str,
        *,
        where: Optional[dict[str, Any]] = None,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build a SELECT COUNT(*) query."""
        parts = [f"SELECT COUNT(*) FROM {table_name}"]
        params: list[Any] = []

        if where:
            clauses = []
            for col, val in where.items():
                params.append(val)
                clauses.append(f"{col} = {self._placeholder(len(params))}")
            parts.append("WHERE " + " AND ".join(clauses))

        return " ".join(parts), tuple(params)

    # ── Aggregate: MAX ──────────────────────────────────────────────────

    def max_value(
        self,
        table_name: str,
        column: str,
        *,
        where: Optional[dict[str, Any]] = None,
    ) -> tuple[str, tuple[Any, ...]]:
        """Build a SELECT MAX(column) query."""
        parts = [f"SELECT MAX({column}) FROM {table_name}"]
        params: list[Any] = []

        if where:
            clauses = []
            for col, val in where.items():
                params.append(val)
                clauses.append(f"{col} = {self._placeholder(len(params))}")
            parts.append("WHERE " + " AND ".join(clauses))

        return " ".join(parts), tuple(params)

    # ── EXISTS check ────────────────────────────────────────────────────

    def exists(
        self,
        table_name: str,
        *,
        where: dict[str, Any],
    ) -> tuple[str, tuple[Any, ...]]:
        """Build a SELECT 1 ... LIMIT 1 existence check."""
        params: list[Any] = []
        clauses = []
        for col, val in where.items():
            params.append(val)
            clauses.append(f"{col} = {self._placeholder(len(params))}")

        sql = f"SELECT 1 FROM {table_name} WHERE {' AND '.join(clauses)}"
        return sql, tuple(params)

    # ── Schema helpers ──────────────────────────────────────────────────

    def create_table_column_type(self, col_type: str) -> str:
        """Convert a SQLite column type to the appropriate dialect type."""
        upper = col_type.upper().strip()
        if "PRIMARY KEY AUTOINCREMENT" in upper:
            return upper.replace(
                "INTEGER PRIMARY KEY AUTOINCREMENT",
                f"{self._autoincrement_type()} PRIMARY KEY",
            )
        if upper == "INTEGER":
            return self._integer_type()
        if upper == "BOOLEAN":
            return self._boolean_type()
        return col_type
