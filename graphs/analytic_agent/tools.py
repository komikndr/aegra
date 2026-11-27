"""Tools for the analytic agent."""

from __future__ import annotations

import os
import re
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


def _numbered_database_urls() -> dict[str, str]:
    entries = [(key.rsplit("_", 1)[1], value.strip()) for key, value in os.environ.items()
               if re.fullmatch(r"ANALYTIC_DB_URL_[0-9]+", key) and value.strip()]
    return dict(sorted(entries, key=lambda entry: (int(entry[0]), entry[0])))


def _build_database_url(context: Context, connection: str | None = None) -> str | None:
    numbered = _numbered_database_urls()
    if connection is not None:
        return numbered.get(connection)
    if numbered:
        return next(iter(numbered.values()))
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


def _get_engine_from_runtime(connection: str | None = None) -> Engine | None:
    runtime = get_runtime(Context)
    database_url = _build_database_url(runtime.context, connection)
    if not database_url:
        return None
    return _get_engine(database_url)


def _database_unavailable_message(connection: str | None = None) -> str:
    if connection is not None:
        return "Unknown Analytic connection ID. Use analytic_list_tables to see configured IDs."
    return "Analytic database is not configured. Set ANALYTIC_DB_URL or the ANALYTIC_DB_* connection env vars first."


def _database_error_message(error: Exception) -> str:
    return "Analytic database is unavailable."


def _is_read_only_query(query: str, dialect: str = "sqlite") -> bool:
    try:
        validate_read_only_sql(query, dialect)
    except UnsafeSqlError:
        return False
    return True


def analytic_list_tables(connection: str | None = None) -> dict[str, Any]:
    """List tables and available connection IDs. Omit connection for the default, or pass a listed ID such as '01'."""
    connections = list(_numbered_database_urls())
    selected = connection if connection is not None else (connections[0] if connections else None)
    routing = {"connection": selected, "connections": connections} if connections else {}
    engine = _get_engine_from_runtime(connection)
    if engine is None:
        return {
            "available": False,
            "message": _database_unavailable_message(connection),
            "tables": [],
            **routing,
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
            **routing,
            "_onyx_trace": {
                "format": "markdown",
                "body": f"**Database inventory**\n\n- Tables: {len(table_names)}\n- Views: {len(view_names)}",
            },
        }
    except SQLAlchemyError as error:
        return {
            "available": False,
            "message": _database_error_message(error),
            "tables": [],
            **routing,
        }


def analytic_describe_table(table_name: str, connection: str | None = None) -> dict[str, Any]:
    """Describe a table's columns. Pass the connection ID from analytic_list_tables when not using the default."""
    engine = _get_engine_from_runtime(connection)
    if engine is None:
        return {"available": False, "message": _database_unavailable_message(connection)}

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


def analytic_query_sql(query: str, connection: str | None = None) -> dict[str, Any]:
    """Run read-only SQL. Pass the connection ID from analytic_list_tables when not using the default."""
    engine = _get_engine_from_runtime(connection)
    if engine is None:
        return {
            "available": False,
            "message": _database_unavailable_message(connection),
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
