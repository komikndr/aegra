"""Tools for the analytic agent."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus

from langgraph.runtime import get_runtime
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from aegra_api.security.sql_readonly import UnsafeSqlError, execute_read_only_sql, validate_read_only_sql
from analytic_agent.context import Context


def _build_database_url(context: Context) -> str | None:
    database_url = os.environ.get("ANALYTIC_DB_URL", "").strip()
    if database_url:
        return database_url

    dialect = os.environ.get("ANALYTIC_DB_DIALECT", "sqlite").strip().lower()
    db_name = os.environ.get("ANALYTIC_DB_NAME", "").strip()
    if dialect == "sqlite":
        if not db_name:
            return None
        if db_name == ":memory:":
            return "sqlite:///:memory:"
        if db_name.startswith("/"):
            return f"sqlite:///{db_name}"
        return f"sqlite:///./{db_name}"

    host = os.environ.get("ANALYTIC_DB_HOST", "").strip()
    user = os.environ.get("ANALYTIC_DB_USER", "").strip()
    password = os.environ.get("ANALYTIC_DB_PASSWORD", "").strip()
    if not all([host, db_name, user]):
        return None

    driver = "postgresql+psycopg" if dialect in {"postgres", "postgresql"} else dialect
    port_value = os.environ.get("ANALYTIC_DB_PORT", "").strip()
    port = f":{port_value}" if port_value else ""
    auth = quote_plus(user)
    if password:
        auth = f"{auth}:{quote_plus(password)}"
    return f"{driver}://{auth}@{host}{port}/{db_name}"


@lru_cache(maxsize=8)
def _get_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _get_engine_from_runtime() -> Engine | None:
    runtime = get_runtime(Context)
    database_url = _build_database_url(runtime.context)
    if not database_url:
        return None
    return _get_engine(database_url)


def _database_unavailable_message() -> str:
    return "Analytic database is not configured. Set ANALYTIC_DB_URL or the ANALYTIC_DB_* connection env vars first."


def _database_error_message(error: Exception) -> str:
    return "Analytic database is unavailable."


def _is_read_only_query(query: str, dialect: str = "sqlite") -> bool:
    try:
        validate_read_only_sql(query, dialect)
    except UnsafeSqlError:
        return False
    return True


def analytic_list_tables() -> dict[str, Any]:
    """List available tables in the configured analytic database."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {
            "available": False,
            "message": _database_unavailable_message(),
            "tables": [],
        }

    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        view_names = inspector.get_view_names()
        return {
            "available": True,
            "dialect": engine.dialect.name,
            "tables": table_names,
            "views": view_names,
        }
    except SQLAlchemyError as error:
        return {
            "available": False,
            "message": _database_error_message(error),
            "tables": [],
        }


def analytic_describe_table(table_name: str) -> dict[str, Any]:
    """Describe a table in the configured analytic database, including column names and types."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {"available": False, "message": _database_unavailable_message()}

    try:
        inspector = inspect(engine)
        known_tables = set(inspector.get_table_names()) | set(inspector.get_view_names())
        if table_name not in known_tables:
            return {
                "available": True,
                "found": False,
                "message": f"Table or view '{table_name}' was not found.",
            }

        columns = inspector.get_columns(table_name)
        return {
            "available": True,
            "found": True,
            "table": table_name,
            "columns": [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column.get("nullable", True),
                    "default": column.get("default"),
                }
                for column in columns
            ],
        }
    except SQLAlchemyError as error:
        return {
            "available": False,
            "message": _database_error_message(error),
        }


def analytic_query_sql(query: str) -> dict[str, Any]:
    """Run a read-only SQL query against the configured analytic database."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {
            "available": False,
            "message": _database_unavailable_message(),
            "rows": [],
        }

    if not _is_read_only_query(query, engine.dialect.name):
        return {
            "available": True,
            "executed": False,
            "message": "Only read-only SQL statements are allowed (SELECT, WITH, PRAGMA, EXPLAIN).",
        }

    try:
        columns, rows = execute_read_only_sql(engine, query, max_rows=200)
        return {
            "available": True,
            "executed": True,
            "row_count": len(rows),
            "columns": columns,
            "rows": rows,
        }
    except (SQLAlchemyError, UnsafeSqlError) as error:
        return {
            "available": False,
            "executed": False,
            "message": _database_error_message(error),
            "rows": [],
        }


TOOLS: list[Callable[..., Any]] = [
    analytic_list_tables,
    analytic_describe_table,
    analytic_query_sql,
]
