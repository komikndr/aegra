"""AST validation and bounded execution for model-generated read-only SQL."""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlglot import exp, parse
from sqlglot.errors import ParseError

SUPPORTED_DIALECTS = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "sqlite": "sqlite",
    "trino": "trino",
}
SIDE_EFFECT_FUNCTIONS = {
    "dblink",
    "lo_export",
    "lo_import",
    "nextval",
    "pg_advisory_lock",
    "pg_sleep",
    "set_config",
    "setval",
}


class UnsafeSqlError(ValueError):
    """Raised when SQL is not a single, read-only query."""


def _sqlglot_dialect(dialect: str) -> str:
    normalized = dialect.strip().lower().split("+")[0]
    try:
        return SUPPORTED_DIALECTS[normalized]
    except KeyError as exc:
        raise UnsafeSqlError(f"Unsupported SQL dialect: {dialect}") from exc


def validate_read_only_sql(query: str, dialect: str) -> None:
    """Reject anything except one parsed read query."""
    if not query.strip():
        raise UnsafeSqlError("SQL query is empty")

    try:
        statements = [statement for statement in parse(query, read=_sqlglot_dialect(dialect)) if statement is not None]
    except ParseError as exc:
        raise UnsafeSqlError("SQL query could not be parsed") from exc

    if len(statements) != 1:
        raise UnsafeSqlError("Exactly one SQL statement is required")

    statement = statements[0]
    if isinstance(statement, exp.Command) and str(statement.this).upper() == "EXPLAIN":
        expression = statement.args.get("expression")
        explained_sql = expression.this if isinstance(expression, exp.Literal) else None
        if not isinstance(explained_sql, str) or explained_sql.upper().startswith("ANALYZE "):
            raise UnsafeSqlError("Only non-executing EXPLAIN queries are allowed")
        validate_read_only_sql(explained_sql.removeprefix("QUERY PLAN "), dialect)
        return
    if not isinstance(statement, (exp.Query, exp.Describe)):
        raise UnsafeSqlError("Only SELECT queries are allowed")

    forbidden_types = tuple(
        expression_type
        for name in (
            "Alter",
            "Attach",
            "Command",
            "Copy",
            "Create",
            "Delete",
            "Detach",
            "Drop",
            "Grant",
            "Insert",
            "Into",
            "Lock",
            "Merge",
            "Pragma",
            "Transaction",
            "TruncateTable",
            "Update",
            "Use",
        )
        if (expression_type := getattr(exp, name, None)) is not None
    )
    if forbidden_types and any(statement.find_all(*forbidden_types)):
        raise UnsafeSqlError("SQL query contains a prohibited operation")

    for function in statement.find_all(exp.Func):
        function_name = function.name.lower() if isinstance(function, exp.Anonymous) else function.sql_name().lower()
        if function_name in SIDE_EFFECT_FUNCTIONS:
            raise UnsafeSqlError(f"SQL function is not allowed: {function_name}")


def _statement_timeout_ms() -> int:
    raw = os.environ.get("ANALYTIC_DB_STATEMENT_TIMEOUT_MS", "15000")
    try:
        return max(100, min(int(raw), 300_000))
    except ValueError:
        return 15_000


def _set_read_only_controls(connection: Connection, dialect: str, timeout_ms: int) -> Any:
    if dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
        return None
    if dialect == "sqlite":
        connection.exec_driver_sql("PRAGMA query_only = ON")
        driver_connection = connection.connection.driver_connection
        if driver_connection is None:
            raise RuntimeError("SQLite driver connection is unavailable")
        deadline = time.monotonic() + timeout_ms / 1000
        driver_connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
        return driver_connection
    return None


def execute_read_only_sql(
    engine: Engine,
    query: str,
    *,
    max_rows: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate and execute SQL with rollback-only transaction semantics."""
    dialect = engine.dialect.name.lower()
    validate_read_only_sql(query, dialect)
    timeout_ms = _statement_timeout_ms()

    with engine.connect() as connection:
        transaction = connection.begin()
        sqlite_connection = None
        try:
            sqlite_connection = _set_read_only_controls(connection, dialect, timeout_ms)
            result = connection.execute(text(query))
            rows = [dict(row) for row in result.mappings().fetchmany(max_rows)]
            return list(result.keys()), rows
        finally:
            if sqlite_connection is not None:
                sqlite_connection.set_progress_handler(None, 0)
            transaction.rollback()
