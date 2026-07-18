"""Tools for the DBChat agent."""

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
from dbchat_agent.context import Context

SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "sys", "system"}


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

    db_host = os.environ.get("ANALYTIC_DB_HOST", "").strip()
    db_port = os.environ.get("ANALYTIC_DB_PORT", "").strip()
    db_user = os.environ.get("ANALYTIC_DB_USER", "").strip()
    db_password = os.environ.get("ANALYTIC_DB_PASSWORD", "").strip()
    if not all([db_host, db_name, db_user]):
        return None

    driver = "postgresql+psycopg" if dialect in {"postgres", "postgresql"} else dialect
    port = f":{db_port}" if db_port else ""
    user = quote_plus(db_user)
    password = quote_plus(db_password)
    auth = f"{user}:{password}" if password else user
    return f"{driver}://{auth}@{db_host}{port}/{db_name}"


@lru_cache(maxsize=8)
def _get_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _get_engine_from_runtime() -> Engine | None:
    runtime = get_runtime(Context)
    database_url = _build_database_url(runtime.context)
    if not database_url:
        return None
    return _get_engine(database_url)


def _is_system_schema(schema: str | None) -> bool:
    if schema is None:
        return False
    lowered = schema.lower()
    return lowered in SYSTEM_SCHEMAS or lowered.startswith("pg_temp_") or lowered.startswith("pg_toast_temp_")


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


def _normalize_identifier(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in re.split(r"\s*\.\s*", value.strip().strip(";,")):
        normalized = part.strip().strip('"`[]').lower()
        if normalized:
            parts.append(normalized)
    return tuple(parts)


def _referenced_tables(query: str) -> set[tuple[str, ...]]:
    references: set[tuple[str, ...]] = set()
    for match in re.finditer(r"\b(?:from|join)\s+([\w\"`\[\].]+)", query, flags=re.IGNORECASE):
        identifier = _normalize_identifier(match.group(1))
        if identifier:
            references.add(identifier)
    return references


def _selected_table_identifiers(context: Context) -> set[tuple[str, ...]]:
    allowed: set[tuple[str, ...]] = set()
    for item in context.dbchat_selected_tables:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip()
        if not table:
            continue
        parts = [str(part).strip() for part in [item.get("database"), item.get("schema"), table] if part]
        normalized = tuple(part.lower() for part in parts)
        if not normalized:
            continue
        allowed.add(normalized)
        allowed.add((normalized[-1],))
        if len(normalized) >= 2:
            allowed.add(normalized[-2:])
    return allowed


def _validate_lock_policy(query: str) -> str | None:
    runtime = get_runtime(Context)
    if runtime.context.dbchat_context_mode.strip().lower() != "lock":
        return None
    allowed = _selected_table_identifiers(runtime.context)
    if not allowed:
        return "Lock mode is active but no tables are selected. Select at least one table or switch to suggestion mode."
    blocked = sorted(".".join(reference) for reference in _referenced_tables(query) if reference not in allowed)
    if blocked:
        return f"Lock mode blocked this SQL because it referenced tables outside the locked selection: {', '.join(blocked)}"
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def dbchat_list_tables() -> dict[str, Any]:
    """List available tables and views in the configured analytic database."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {"available": False, "message": _database_unavailable_message(), "tables": [], "views": []}

    try:
        inspector = inspect(engine)
        schemas = []
        try:
            schemas = [schema for schema in inspector.get_schema_names() if not _is_system_schema(schema)]
        except SQLAlchemyError:
            schemas = [None]
        if not schemas:
            schemas = [None]

        tables: list[dict[str, Any]] = []
        views: list[dict[str, Any]] = []
        for schema in schemas:
            for table_name in inspector.get_table_names(schema=schema):
                tables.append({"schema": schema, "table": table_name})
            for view_name in inspector.get_view_names(schema=schema):
                views.append({"schema": schema, "table": view_name})
        return {"available": True, "dialect": engine.dialect.name, "tables": tables, "views": views}
    except SQLAlchemyError as error:
        return {"available": False, "message": _database_error_message(error), "tables": [], "views": []}


def dbchat_describe_table(table_name: str, schema: str | None = None) -> dict[str, Any]:
    """Describe a table or view, including column names and types."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {"available": False, "message": _database_unavailable_message()}

    try:
        inspector = inspect(engine)
        known_tables = set(inspector.get_table_names(schema=schema)) | set(inspector.get_view_names(schema=schema))
        if table_name not in known_tables:
            return {"available": True, "found": False, "message": f"Table or view '{table_name}' was not found."}
        columns = inspector.get_columns(table_name, schema=schema)
        return {
            "available": True,
            "found": True,
            "schema": schema,
            "table": table_name,
            "columns": [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column.get("nullable", True),
                    "default": _json_safe(column.get("default")),
                }
                for column in columns
            ],
        }
    except SQLAlchemyError as error:
        return {"available": False, "message": _database_error_message(error)}


def dbchat_query_sql(query: str) -> dict[str, Any]:
    """Run a read-only SQL query against the analytic database. Returns executed SQL and rows."""
    engine = _get_engine_from_runtime()
    if engine is None:
        return {"available": False, "message": _database_unavailable_message(), "rows": [], "sql": query}

    if not _is_read_only_query(query, engine.dialect.name):
        return {
            "available": True,
            "executed": False,
            "message": "Only read-only SQL statements are allowed.",
            "sql": query,
        }

    policy_error = _validate_lock_policy(query)
    if policy_error:
        return {"available": True, "executed": False, "message": policy_error, "sql": query}

    try:
        columns, rows = execute_read_only_sql(engine, query, max_rows=200)
        return {
            "available": True,
            "executed": True,
            "sql": query,
            "row_count": len(rows),
            "columns": columns,
            "rows": [{key: _json_safe(value) for key, value in row.items()} for row in rows],
        }
    except (SQLAlchemyError, UnsafeSqlError) as error:
        return {
            "available": False,
            "executed": False,
            "message": _database_error_message(error),
            "rows": [],
            "sql": query,
        }


TOOLS: list[Callable[..., Any]] = [dbchat_list_tables, dbchat_describe_table, dbchat_query_sql]
