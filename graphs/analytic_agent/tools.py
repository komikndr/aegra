"""Tools for the analytic agent."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any, cast
from urllib.parse import quote_plus

from langgraph.runtime import get_runtime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from analytic_agent.context import Context

READ_ONLY_PREFIXES = ("select", "with", "pragma", "explain")
FORBIDDEN_SQL_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "create",
    "truncate",
    "grant",
    "revoke",
    "replace",
    "merge",
    "attach",
    "vacuum",
)


def _build_database_url(context: Context) -> str | None:
    if context.analytic_db_url:
        return context.analytic_db_url

    dialect = context.analytic_db_dialect.strip().lower()
    if dialect == "sqlite":
        if not context.analytic_db_name:
            return None
        db_name = context.analytic_db_name
        if db_name == ":memory:":
            return "sqlite:///:memory:"
        if db_name.startswith("/"):
            return f"sqlite:///{db_name}"
        return f"sqlite:///./{db_name}"

    if not all(
        [
            context.analytic_db_host,
            context.analytic_db_name,
            context.analytic_db_user,
            context.analytic_db_password,
        ]
    ):
        return None

    driver = "postgresql+psycopg" if dialect in {"postgres", "postgresql"} else dialect
    port = f":{context.analytic_db_port}" if context.analytic_db_port else ""
    user = quote_plus(cast("str", context.analytic_db_user or ""))
    password = quote_plus(cast("str", context.analytic_db_password or ""))
    return f"{driver}://{user}:{password}@{context.analytic_db_host}{port}/{context.analytic_db_name}"


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
    return f"Analytic database is unavailable: {error}"


def _is_read_only_query(query: str) -> bool:
    normalized = query.strip().lower().strip(";")
    if not normalized.startswith(READ_ONLY_PREFIXES):
        return False
    return not any(
        f" {keyword} " in f" {normalized} " for keyword in FORBIDDEN_SQL_KEYWORDS
    )


async def analytic_list_tables() -> dict[str, Any]:
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


async def analytic_describe_table(table_name: str) -> dict[str, Any]:
    """Describe a table in the configured analytic database, including column names and types."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {"available": False, "message": _database_unavailable_message()}

    try:
        inspector = inspect(engine)
        known_tables = set(inspector.get_table_names()) | set(
            inspector.get_view_names()
        )
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


async def analytic_query_sql(query: str) -> dict[str, Any]:
    """Run a read-only SQL query against the configured analytic database."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {
            "available": False,
            "message": _database_unavailable_message(),
            "rows": [],
        }

    if not _is_read_only_query(query):
        return {
            "available": True,
            "executed": False,
            "message": "Only read-only SQL statements are allowed (SELECT, WITH, PRAGMA, EXPLAIN).",
        }

    try:
        with engine.connect() as connection:
            result = connection.execute(text(query))
            rows = result.mappings().fetchmany(200)
            return {
                "available": True,
                "executed": True,
                "row_count": len(rows),
                "columns": list(result.keys()),
                "rows": [dict(row) for row in rows],
            }
    except SQLAlchemyError as error:
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
