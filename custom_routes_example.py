"""Example custom routes file for Aegra.

This demonstrates how to add custom FastAPI endpoints to your Aegra server,
including examples of authentication integration.

Configuration:
Add this to your aegra.json or langgraph.json:

{
  "graphs": {
    "agent": "./graphs/react_agent/graph.py:graph"
  },
  "auth": {
    "path": "./jwt_mock_auth_example.py:auth"
  },
  "http": {
    "app": "./custom_routes_example.py:app",
    "enable_custom_route_auth": false
  }
}

You can also configure CORS:

{
  "http": {
    "app": "./custom_routes_example.py:app",
    "enable_custom_route_auth": true,
    "cors": {
      "allow_origins": ["https://example.com"],
      "allow_credentials": true
    }
  }
}
"""

# ruff: noqa: E402

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sys
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

_GRAPHS_DIR = Path(__file__).resolve().parent / "graphs"
if _GRAPHS_DIR.exists() and str(_GRAPHS_DIR) not in sys.path:
    sys.path.insert(0, str(_GRAPHS_DIR))

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import OpenAIEmbeddings
from opensearchpy import OpenSearch as OpenSearchClient
from opensearchpy import TransportError
from pydantic import BaseModel, Field
from react_agent.model_config import resolve_agent_model
from react_agent.utils import (
    apply_reasoning_effort,
    get_active_model_name,
    get_message_text,
    is_media_not_supported_error,
    load_chat_model,
    resolve_model_name,
)
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.agent_access import ensure_graph_access
from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import ExecutiveArtifact, PipelineApiKey, PipelineRun, PipelineWorkflow, Thread, get_session
from aegra_api.models.auth import User

# Create your FastAPI app instance
# This will be merged with Aegra's core routes
app = FastAPI(
    title="Custom Routes Example",
    description="Example custom endpoints for Aegra with authentication",
)

logger = logging.getLogger(__name__)


class ExecutiveArtifactPayload(BaseModel):
    id: str
    title: str | None = None
    content: str | None = None
    titleBase64: str | None = None
    contentBase64: str | None = None
    timestamp: datetime
    threadId: str | None = None
    agentId: str | None = None
    artifactKind: str = "executive_report"
    sourceMessageId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplaceExecutiveArtifactsRequest(BaseModel):
    artifacts: list[ExecutiveArtifactPayload] = Field(default_factory=list)
    artifactsBase64: str | None = None


class PipelineNodePayload(BaseModel):
    id: str
    type: str
    label: str
    position: dict[str, float] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class PipelineEdgePayload(BaseModel):
    id: str
    source: str
    target: str
    label: str | None = None


class PipelineBlueprintPayload(BaseModel):
    workflowId: str
    description: str
    nodes: list[PipelineNodePayload]
    edges: list[PipelineEdgePayload]


class PipelineWorkflowRequest(BaseModel):
    workflowId: str | None = None
    name: str
    description: str = ""
    graph: PipelineBlueprintPayload


class PipelineWorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    graph: PipelineBlueprintPayload | None = None


class PipelineApiKeyRequest(BaseModel):
    name: str = "External push key"
    expiresAt: datetime | None = None


class PipelineExecuteRequest(BaseModel):
    workflowId: str
    data: Any = None
    rows: list[Any] | None = None
    dryRun: bool = False


class DbChatQueryRequest(BaseModel):
    sql: str
    limit: int = Field(default=200, ge=1, le=1000)


class DbChatSelectedTable(BaseModel):
    database: str | None = None
    schema: str | None = None
    table: str


class DbChatMessage(BaseModel):
    role: str
    content: str


class DbChatChatRequest(BaseModel):
    message: str
    history: list[DbChatMessage] = Field(default_factory=list)
    selectedTable: DbChatSelectedTable | None = None
    selectedTables: list[DbChatSelectedTable] = Field(default_factory=list)
    contextMode: str | None = Field(default="suggest")
    reasoningEffort: str | None = Field(default="low")


class DbChatAdapter(ABC):
    connection_id: str
    title: str

    @abstractmethod
    def connection_info(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def catalog(self, include_system: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def tree(self, include_system: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def describe_table(self, table: str, schema: str | None = None, database: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def preview_table(
        self,
        table: str,
        schema: str | None = None,
        database: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def query(self, sql: str, limit: int = 200) -> dict[str, Any]:
        raise NotImplementedError


READ_ONLY_SQL_PREFIXES = ("select", "with", "show", "describe", "explain")
FORBIDDEN_DBCHAT_SQL_KEYWORDS = (
    "alter",
    "attach",
    "call",
    "copy",
    "create",
    "delete",
    "drop",
    "grant",
    "insert",
    "merge",
    "replace",
    "revoke",
    "truncate",
    "update",
    "vacuum",
)
SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "sys",
    "system",
}


def _build_analytic_database_url() -> str | None:
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
    port = os.environ.get("ANALYTIC_DB_PORT", "").strip()
    port_part = f":{port}" if port else ""
    auth_part = quote_plus(user)
    if password:
        auth_part = f"{auth_part}:{quote_plus(password)}"
    return f"{driver}://{auth_part}@{host}{port_part}/{db_name}"


@lru_cache(maxsize=8)
def _get_dbchat_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _is_system_schema(schema: str | None) -> bool:
    if schema is None:
        return False
    lowered = schema.lower()
    return lowered in SYSTEM_SCHEMAS or lowered.startswith("pg_temp_") or lowered.startswith("pg_toast_temp_")


def _is_read_only_dbchat_sql(sql: str) -> bool:
    normalized = " ".join(sql.strip().strip(";").lower().split())
    if not normalized.startswith(READ_ONLY_SQL_PREFIXES):
        return False
    guarded = f" {normalized} "
    return not any(f" {keyword} " in guarded for keyword in FORBIDDEN_DBCHAT_SQL_KEYWORDS)


def _json_safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return jsonable_encoder(rows)


def _first_row_value(row: Any) -> Any:
    if hasattr(row, "_mapping"):
        values = list(row._mapping.values())
        return values[0] if values else None
    if isinstance(row, tuple):
        return row[0] if row else None
    return row


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SqlAlchemyDbChatAdapter(DbChatAdapter):
    def __init__(self, connection_id: str, title: str, engine: Engine) -> None:
        self.connection_id = connection_id
        self.title = title
        self.engine = engine

    def connection_info(self) -> dict[str, Any]:
        return {
            "id": self.connection_id,
            "title": self.title,
            "engineId": self.engine.dialect.name,
            "dialect": self.engine.dialect.name,
            "configured": True,
            "capabilities": {
                "catalog": True,
                "query": True,
                "tablePreview": True,
                "sqlroomsCatalog": True,
            },
        }

    def _database_name(self) -> str:
        return self.engine.url.database or self.connection_id

    def _schemas(self, include_system: bool) -> list[str | None]:
        inspector = inspect(self.engine)
        try:
            schemas = inspector.get_schema_names()
        except SQLAlchemyError:
            schemas = [None]
        filtered = [schema for schema in schemas if include_system or not _is_system_schema(schema)]
        return filtered or [None]

    def catalog(self, include_system: bool = False) -> dict[str, Any]:
        inspector = inspect(self.engine)
        database = self._database_name()
        schemas: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []

        for schema in self._schemas(include_system):
            if schema is not None:
                schemas.append({"database": database, "schema": schema})
            for table_name in inspector.get_table_names(schema=schema):
                tables.append({"database": database, "schema": schema, "table": table_name, "isView": False})
            for view_name in inspector.get_view_names(schema=schema):
                tables.append({"database": database, "schema": schema, "table": view_name, "isView": True})

        return {
            "connectionId": self.connection_id,
            "engineId": self.engine.dialect.name,
            "databases": [{"database": database}],
            "schemas": schemas,
            "tables": tables,
        }

    def tree(self, include_system: bool = False) -> dict[str, Any]:
        catalog = self.catalog(include_system=include_system)
        database_name = catalog["databases"][0]["database"] if catalog["databases"] else self._database_name()
        schema_nodes: list[dict[str, Any]] = []

        for schema_record in catalog["schemas"] or [{"schema": None}]:
            schema_name = schema_record.get("schema")
            schema_tables = [table for table in catalog["tables"] if table.get("schema") == schema_name]
            table_nodes = [
                {
                    "id": f"{self.connection_id}:table:{schema_name or ''}:{table['table']}",
                    "kind": "view" if table.get("isView") else "table",
                    "name": table["table"],
                    "schema": schema_name,
                    "database": database_name,
                    "isView": table.get("isView", False),
                }
                for table in schema_tables
            ]
            schema_nodes.append(
                {
                    "id": f"{self.connection_id}:schema:{schema_name or 'default'}",
                    "kind": "schema",
                    "name": schema_name or "default",
                    "database": database_name,
                    "children": [
                        {
                            "id": f"{self.connection_id}:schema:{schema_name or 'default'}:tables",
                            "kind": "group",
                            "name": "Tables",
                            "children": [node for node in table_nodes if not node["isView"]],
                        },
                        {
                            "id": f"{self.connection_id}:schema:{schema_name or 'default'}:views",
                            "kind": "group",
                            "name": "Views",
                            "children": [node for node in table_nodes if node["isView"]],
                        },
                    ],
                }
            )

        return {
            "connection": self.connection_info(),
            "tree": {
                "id": self.connection_id,
                "kind": "connection",
                "name": self.title,
                "children": [
                    {
                        "id": f"{self.connection_id}:database:{database_name}",
                        "kind": "database",
                        "name": database_name,
                        "children": schema_nodes,
                    }
                ],
            },
            "catalog": catalog,
        }

    def describe_table(self, table: str, schema: str | None = None, database: str | None = None) -> dict[str, Any]:
        inspector = inspect(self.engine)
        columns = inspector.get_columns(table, schema=schema)
        indexes = inspector.get_indexes(table, schema=schema)
        return {
            "connectionId": self.connection_id,
            "database": database or self._database_name(),
            "schema": schema,
            "table": table,
            "columns": [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column.get("nullable", True),
                    "default": column.get("default"),
                }
                for column in columns
            ],
            "indexes": jsonable_encoder(indexes),
        }

    def _qualified_table_name(self, table: str, schema: str | None = None, database: str | None = None) -> str:
        preparer = self.engine.dialect.identifier_preparer
        parts = [part for part in [schema, table] if part]
        if self.engine.dialect.name == "trino" and database:
            parts = [database, *parts]
        return ".".join(preparer.quote(part) for part in parts)

    def preview_table(
        self,
        table: str,
        schema: str | None = None,
        database: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 200))
        qualified_table = self._qualified_table_name(table, schema=schema, database=database)
        sql = f"SELECT * FROM {qualified_table} LIMIT {safe_limit}"
        return self.query(sql, limit=safe_limit)

    def query(self, sql: str, limit: int = 200) -> dict[str, Any]:
        if not _is_read_only_dbchat_sql(sql):
            raise HTTPException(status_code=400, detail="Only read-only SQL is allowed for DBChat.")
        safe_limit = max(1, min(limit, 1000))
        with self.engine.connect() as connection:
            result = connection.execute(text(sql))
            rows = [dict(row) for row in result.mappings().fetchmany(safe_limit)]
            return {
                "connectionId": self.connection_id,
                "engineId": self.engine.dialect.name,
                "columns": list(result.keys()),
                "rows": _json_safe_rows(rows),
                "rowCount": len(rows),
                "limit": safe_limit,
            }


class TrinoDbChatAdapter(SqlAlchemyDbChatAdapter):
    def _fetch_first_column(self, sql: str) -> list[str]:
        with self.engine.connect() as connection:
            result = connection.execute(text(sql))
            return [str(_first_row_value(row)) for row in result.fetchall() if _first_row_value(row) is not None]

    def catalog(self, include_system: bool = False) -> dict[str, Any]:
        catalogs = self._fetch_first_column("SHOW CATALOGS")
        databases = [{"database": catalog} for catalog in catalogs]
        schemas: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []

        for catalog_name in catalogs:
            try:
                catalog_schemas = self._fetch_first_column(f"SHOW SCHEMAS FROM {_quote_identifier(catalog_name)}")
            except SQLAlchemyError:
                continue
            for schema_name in catalog_schemas:
                if not include_system and _is_system_schema(schema_name):
                    continue
                schemas.append({"database": catalog_name, "schema": schema_name})
                try:
                    table_names = self._fetch_first_column(
                        f"SHOW TABLES FROM {_quote_identifier(catalog_name)}.{_quote_identifier(schema_name)}"
                    )
                except SQLAlchemyError:
                    continue
                for table_name in table_names:
                    tables.append(
                        {"database": catalog_name, "schema": schema_name, "table": table_name, "isView": False}
                    )

        return {
            "connectionId": self.connection_id,
            "engineId": self.engine.dialect.name,
            "databases": databases,
            "schemas": schemas,
            "tables": tables,
        }

    def describe_table(self, table: str, schema: str | None = None, database: str | None = None) -> dict[str, Any]:
        if not database or not schema:
            return super().describe_table(table, schema=schema, database=database)
        sql = f"DESCRIBE {_quote_identifier(database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"
        with self.engine.connect() as connection:
            result = connection.execute(text(sql))
            columns = []
            for row in result.fetchall():
                values = list(row._mapping.values()) if hasattr(row, "_mapping") else list(row)
                if len(values) < 2:
                    continue
                columns.append({"name": values[0], "type": values[1], "nullable": None, "default": None})
        return {
            "connectionId": self.connection_id,
            "database": database,
            "schema": schema,
            "table": table,
            "columns": columns,
            "indexes": [],
        }


def _get_dbchat_adapter(connection_id: str) -> DbChatAdapter:
    if connection_id != "analytic":
        raise HTTPException(status_code=404, detail=f"DBChat connection '{connection_id}' was not found")

    database_url = _build_analytic_database_url()
    if not database_url:
        raise HTTPException(
            status_code=400,
            detail="Analytic database is not configured. Set ANALYTIC_DB_URL or ANALYTIC_DB_* env vars first.",
        )

    try:
        engine = _get_dbchat_engine(database_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not create analytic database engine: {exc}") from exc

    if engine.dialect.name == "trino":
        return TrinoDbChatAdapter(connection_id="analytic", title="Analytic Trino", engine=engine)
    return SqlAlchemyDbChatAdapter(connection_id="analytic", title="Analytic Database", engine=engine)


def _available_dbchat_connections() -> list[dict[str, Any]]:
    database_url = _build_analytic_database_url()
    if not database_url:
        return [
            {
                "id": "analytic",
                "title": "Analytic Database",
                "engineId": os.environ.get("ANALYTIC_DB_DIALECT", "unknown"),
                "dialect": os.environ.get("ANALYTIC_DB_DIALECT", "unknown"),
                "configured": False,
                "capabilities": {"catalog": False, "query": False},
            }
        ]

    try:
        adapter = _get_dbchat_adapter("analytic")
        return [adapter.connection_info()]
    except HTTPException:
        raise
    except Exception as exc:
        return [
            {
                "id": "analytic",
                "title": "Analytic Database",
                "engineId": os.environ.get("ANALYTIC_DB_DIALECT", "unknown"),
                "dialect": os.environ.get("ANALYTIC_DB_DIALECT", "unknown"),
                "configured": True,
                "error": str(exc),
                "capabilities": {"catalog": False, "query": False},
            }
        ]


def _dbchat_qualified_table_label(table: DbChatSelectedTable) -> str:
    return ".".join(part for part in [table.database, table.schema, table.table] if part)


def _dbchat_table_key(table: DbChatSelectedTable) -> tuple[str, str, str]:
    return (table.database or "", table.schema or "", table.table)


def _normalize_dbchat_selected_tables(payload: DbChatChatRequest) -> list[DbChatSelectedTable]:
    selected_tables = list(payload.selectedTables)
    if payload.selectedTable is not None:
        selected_tables.append(payload.selectedTable)

    deduped: dict[tuple[str, str, str], DbChatSelectedTable] = {}
    for selected_table in selected_tables:
        table_name = selected_table.table.strip()
        if not table_name:
            continue
        normalized = DbChatSelectedTable(
            database=selected_table.database,
            schema=selected_table.schema,
            table=table_name,
        )
        deduped[_dbchat_table_key(normalized)] = normalized
    return list(deduped.values())


def _normalize_dbchat_context_mode(value: str | None) -> str:
    return "lock" if (value or "").strip().lower() == "lock" else "suggest"


def _format_dbchat_table_details(adapter: DbChatAdapter, selected_table: DbChatSelectedTable) -> str:
    details = adapter.describe_table(
        table=selected_table.table,
        schema=selected_table.schema,
        database=selected_table.database,
    )
    columns = details.get("columns") if isinstance(details, dict) else []
    column_lines = []
    if isinstance(columns, list):
        for column in columns[:80]:
            if not isinstance(column, dict):
                continue
            name = column.get("name")
            column_type = column.get("type")
            nullable = column.get("nullable")
            nullability = " nullable" if nullable is not False else " not null"
            column_lines.append(f"- {name}: {column_type}{nullability}")
    return _dbchat_qualified_table_label(selected_table) + "\nColumns:\n" + ("\n".join(column_lines) or "No columns returned.")


def _format_dbchat_catalog_scope(adapter: DbChatAdapter, limit: int = 140) -> str:
    catalog = adapter.catalog(include_system=False)
    tables = catalog.get("tables") if isinstance(catalog, dict) else []
    table_lines = []
    if isinstance(tables, list):
        for table_record in tables[:limit]:
            if not isinstance(table_record, dict):
                continue
            qualified = ".".join(
                str(part)
                for part in [table_record.get("database"), table_record.get("schema"), table_record.get("table")]
                if part
            )
            if qualified:
                suffix = " view" if table_record.get("isView") else " table"
                table_lines.append(f"- {qualified} ({suffix})")
    return "Available tables/views:\n" + ("\n".join(table_lines) or "No tables returned.")


def _format_dbchat_scope(adapter: DbChatAdapter, selected_tables: list[DbChatSelectedTable], context_mode: str) -> str:
    selected_tables = selected_tables[:12]
    if context_mode == "lock" and selected_tables:
        sections = ["Locked tables only:"]
        sections.extend(_format_dbchat_table_details(adapter, table) for table in selected_tables)
        return "\n\n".join(sections)

    sections = [_format_dbchat_catalog_scope(adapter)]
    if selected_tables:
        sections.append("Suggested tables to consider first:")
        sections.extend(_format_dbchat_table_details(adapter, table) for table in selected_tables)
    return "\n\n".join(sections)


def _dbchat_history_messages(history: list[DbChatMessage]) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for item in history[-8:]:
        content = item.content.strip()
        if not content:
            continue
        if item.role.strip().lower() == "assistant":
            messages.append(AIMessage(content=content[:4000]))
        else:
            messages.append(HumanMessage(content=content[:4000]))
    return messages


def _split_dbchat_inline_thinking(text: str) -> tuple[str, str]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    cursor = 0

    while cursor < len(text):
        next_open = text.find("<think>", cursor)
        if next_open == -1:
            content_parts.append(text[cursor:])
            break

        content_parts.append(text[cursor:next_open])
        reasoning_start = next_open + len("<think>")
        next_close = text.find("</think>", reasoning_start)
        if next_close == -1:
            reasoning_parts.append(text[reasoning_start:])
            break
        reasoning_parts.append(text[reasoning_start:next_close])
        cursor = next_close + len("</think>")

    return "".join(content_parts), "".join(reasoning_parts)


def _extract_dbchat_reasoning_from_value(value: Any) -> str:
    if isinstance(value, str):
        return _split_dbchat_inline_thinking(value)[1].strip()

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                reasoning = _split_dbchat_inline_thinking(item)[1].strip()
                if reasoning:
                    parts.append(reasoning)
                continue
            if isinstance(item, dict):
                item_type = str(item.get("type", "")).lower()
                if item_type in {"thinking", "reasoning", "reasoning_content"}:
                    direct = item.get("text") or item.get("content")
                    if isinstance(direct, str) and direct.strip():
                        parts.append(direct.strip())
                    continue
                nested = _extract_dbchat_reasoning_from_value(item)
                if nested:
                    parts.append(nested)
        return "\n".join(parts).strip()

    if not isinstance(value, dict):
        return ""

    for key in ("reasoning_content", "reasoning", "thinking"):
        direct = value.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

    content_reasoning = _extract_dbchat_reasoning_from_value(value.get("content"))
    if content_reasoning:
        return content_reasoning

    for key in ("additional_kwargs", "response_metadata", "kwargs", "message", "delta", "raw", "chunk", "data"):
        nested = value.get(key)
        if isinstance(nested, (dict, list)):
            nested_reasoning = _extract_dbchat_reasoning_from_value(nested)
            if nested_reasoning:
                return nested_reasoning

    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            reasoning = _extract_dbchat_reasoning_from_value(choice)
            if reasoning:
                return reasoning

    return ""


def _extract_dbchat_reasoning(message: Any) -> str:
    sources: list[Any] = [message]
    for attr in ("additional_kwargs", "response_metadata", "content"):
        if hasattr(message, attr):
            sources.append(getattr(message, attr))

    for source in sources:
        reasoning = _extract_dbchat_reasoning_from_value(source)
        if reasoning:
            return reasoning[:16000]
    return ""


def _get_dbchat_visible_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return _split_dbchat_inline_thinking(content)[0].strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(_split_dbchat_inline_thinking(item)[0])
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).lower()
            if item_type in {"thinking", "reasoning", "reasoning_content"}:
                continue
            for key in ("text", "content"):
                value = item.get(key)
                if isinstance(value, str):
                    parts.append(_split_dbchat_inline_thinking(value)[0])
                    break
        return "".join(parts).strip()
    return _split_dbchat_inline_thinking(get_message_text(message))[0].strip()


def _normalize_dbchat_reasoning_effort(value: str | None) -> str | None:
    if not value:
        return "low"
    normalized = value.strip().lower()
    if normalized in {"none", "low", "medium", "high", "ultra"}:
        return normalized
    return "low"


def _dbchat_user_wants_rows(message: str) -> bool:
    lowered = message.lower()
    return any(
        keyword in lowered
        for keyword in (
            "show",
            "count",
            "total",
            "average",
            "sum",
            "preview",
            "sample",
            "records",
            "rows",
            "data",
            "lihat",
            "tampil",
        )
    )


def _dbchat_user_wants_preview(message: str) -> bool:
    lowered = message.lower()
    return any(keyword in lowered for keyword in ("show", "preview", "sample", "records", "rows", "data", "lihat", "tampil"))


def _dbchat_user_wants_chart(message: str) -> bool:
    lowered = message.lower()
    return any(keyword in lowered for keyword in ("chart", "graph", "plot", "visual", "pie", "bar", "line"))


def _dbchat_selected_table_sql(
    adapter: DbChatAdapter,
    selected_tables: list[DbChatSelectedTable],
    limit: int = 10,
) -> str | None:
    if not selected_tables:
        return None
    selected_table = selected_tables[0]
    if isinstance(adapter, SqlAlchemyDbChatAdapter):
        qualified_table = adapter._qualified_table_name(  # noqa: SLF001 - adapter-owned SQL quoting helper
            selected_table.table,
            schema=selected_table.schema,
            database=selected_table.database,
        )
    else:
        parts = [part for part in [selected_table.database, selected_table.schema, selected_table.table] if part]
        qualified_table = ".".join(_quote_identifier(part) for part in parts)
    return f"SELECT * FROM {qualified_table} LIMIT {max(1, min(limit, 100))}"


def _extract_dbchat_sql(text: str) -> str | None:
    fenced_match = re.search(r"```sql\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced_match:
        return fenced_match.group(1).strip().rstrip(";")

    statement_match = re.search(r"\b(select|with|show|describe|explain)\b[\s\S]*?(?:;|$)", text, flags=re.IGNORECASE)
    if not statement_match:
        return None
    return statement_match.group(0).strip().rstrip(";")


def _strip_dbchat_sql_blocks(text: str) -> str:
    without_blocks = re.sub(r"```sql\s*.*?\s*```", "", text, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", without_blocks).strip()


def _normalize_dbchat_sql_identifier(value: str) -> tuple[str, ...]:
    stripped = value.strip().strip(";,")
    if not stripped:
        return ()
    parts: list[str] = []
    for part in re.split(r"\s*\.\s*", stripped):
        normalized = part.strip().strip('"`[]').lower()
        if normalized:
            parts.append(normalized)
    return tuple(parts)


def _dbchat_sql_referenced_tables(sql: str) -> set[tuple[str, ...]]:
    references: set[tuple[str, ...]] = set()
    pattern = r"\b(?:from|join)\s+([\w\"`\[\].]+)"
    for match in re.finditer(pattern, sql, flags=re.IGNORECASE):
        identifier = _normalize_dbchat_sql_identifier(match.group(1))
        if identifier:
            references.add(identifier)
    return references


def _dbchat_allowed_table_identifiers(selected_tables: list[DbChatSelectedTable]) -> set[tuple[str, ...]]:
    allowed: set[tuple[str, ...]] = set()
    for selected_table in selected_tables:
        parts = [part for part in [selected_table.database, selected_table.schema, selected_table.table] if part]
        normalized = tuple(part.lower() for part in parts)
        if normalized:
            allowed.add(normalized)
            allowed.add((normalized[-1],))
            if len(normalized) >= 2:
                allowed.add(normalized[-2:])
    return allowed


def _validate_dbchat_lock_sql(sql: str, selected_tables: list[DbChatSelectedTable]) -> None:
    if not selected_tables:
        raise HTTPException(status_code=400, detail="Lock mode needs at least one selected table.")
    references = _dbchat_sql_referenced_tables(sql)
    if not references:
        return
    allowed = _dbchat_allowed_table_identifiers(selected_tables)
    blocked = sorted(".".join(reference) for reference in references if reference not in allowed)
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=f"Lock mode blocked SQL because it referenced tables outside the locked selection: {', '.join(blocked)}",
        )


def _dbchat_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _build_dbchat_chart_option(message: str, query_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not query_result:
        return None
    rows = query_result.get("rows")
    columns = query_result.get("columns")
    if not isinstance(rows, list) or not rows or not isinstance(columns, list) or len(columns) < 2:
        return None

    sample_rows = [row for row in rows if isinstance(row, dict)]
    if not sample_rows:
        return None

    label_column = next((column for column in columns if isinstance(column, str)), None)
    value_column = next(
        (
            column
            for column in columns
            if isinstance(column, str) and any(_dbchat_float(row.get(column)) is not None for row in sample_rows)
        ),
        None,
    )
    if not label_column or not value_column:
        return None
    if label_column == value_column:
        label_column = next((column for column in columns if isinstance(column, str) and column != value_column), label_column)

    labels = [str(row.get(label_column, "")) for row in sample_rows[:50]]
    values = [_dbchat_float(row.get(value_column)) or 0 for row in sample_rows[:50]]
    lowered = message.lower()
    title = "Query chart"

    if "pie" in lowered:
        return {
            "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "item"},
            "legend": {"bottom": 0},
            "series": [
                {
                    "type": "pie",
                    "radius": "60%",
                    "data": [{"name": label, "value": value} for label, value in zip(labels, values, strict=False)],
                }
            ],
        }

    chart_type = "line" if "line" in lowered else "bar"
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": labels},
        "yAxis": {"type": "value"},
        "series": [{"type": chart_type, "data": values, "name": value_column}],
    }


def _normalize_base64(encoded_value: str) -> str:
    normalized = encoded_value.strip().replace("-", "+").replace("_", "/")
    padding = len(normalized) % 4
    if padding:
        normalized += "=" * (4 - padding)
    return normalized


def _decode_transport_string(encoded_value: str | None, raw_value: str | None, field_name: str) -> str:
    if encoded_value:
        try:
            normalized = _normalize_base64(encoded_value)
            return base64.b64decode(normalized).decode("utf-8")
        except Exception as exc:  # pragma: no cover - defensive validation path
            logger.warning(
                "Failed to decode artifact %s payload length=%s",
                field_name,
                len(encoded_value),
            )
            raise HTTPException(
                status_code=422,
                detail=f"Invalid base64 payload for artifact {field_name}",
            ) from exc
    if raw_value is not None:
        return raw_value
    raise HTTPException(status_code=422, detail=f"Missing artifact {field_name}")


def _decode_transport_json(encoded_value: str | None, field_name: str) -> list[dict[str, Any]] | None:
    if not encoded_value:
        return None
    try:
        normalized = _normalize_base64(encoded_value)
        decoded = base64.b64decode(normalized).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:  # pragma: no cover - defensive validation path
        logger.warning(
            "Failed to decode %s payload length=%s",
            field_name,
            len(encoded_value),
        )
        raise HTTPException(
            status_code=422,
            detail=f"Invalid base64 payload for {field_name}",
        ) from exc

    if not isinstance(payload, list):
        raise HTTPException(status_code=422, detail=f"Invalid payload for {field_name}")
    return payload


def _get_configured_model() -> str:
    return os.environ.get("MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"


PIPELINE_KEY_PREFIX = "aegra_pipeline_"


def _slugify_workflow_id(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:64] or f"workflow-{uuid.uuid4().hex[:8]}"


def _hash_pipeline_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _resolve_pipeline_base_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    resolved = os.path.expandvars(value).strip()
    if "$" in resolved:
        return None
    return resolved or None


def _default_pipeline_blueprint(workflow_id: str) -> PipelineBlueprintPayload:
    return PipelineBlueprintPayload(
        workflowId=workflow_id,
        description=(
            "HTTP push workflow. External systems POST rows to /pipeline with workflowId, "
            "processors transform each row, then output nodes return the result."
        ),
        nodes=[
            PipelineNodePayload(
                id="source-1",
                type="source",
                label="Source",
                position={"x": 80, "y": 180},
                config={
                    "method": "POST",
                    "path": "/pipeline",
                    "workflowId": workflow_id,
                    "auth": "Bearer API key",
                    "description": "Accepts a pushed JSON row or rows array.",
                },
            ),
            PipelineNodePayload(
                id="formatter-1",
                type="formatter",
                label="JSON Formatter",
                position={"x": 390, "y": 90},
                config={
                    "model": _get_configured_model(),
                    "systemMessage": "Normalize each inbound row into a clean JSON object suitable for downstream analytics.",
                    "structuredOutput": {
                        "name": "normalized_row",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": True,
                        },
                    },
                },
            ),
            PipelineNodePayload(
                id="output-1",
                type="output",
                label="Output",
                position={"x": 720, "y": 180},
                config={"description": "Returns processed rows in the pipeline response."},
            ),
        ],
        edges=[
            PipelineEdgePayload(id="source-1-to-formatter-1", source="source-1", target="formatter-1", label="rows"),
            PipelineEdgePayload(id="formatter-1-to-output-1", source="formatter-1", target="output-1", label="json"),
        ],
    )


def _serialize_workflow(workflow: PipelineWorkflow) -> dict[str, Any]:
    return {
        "workflowId": workflow.workflow_id,
        "name": workflow.name,
        "description": workflow.description,
        "graph": workflow.graph,
        "createdAt": workflow.created_at.isoformat() if workflow.created_at else None,
        "updatedAt": workflow.updated_at.isoformat() if workflow.updated_at else None,
    }


def _serialize_api_key(api_key: PipelineApiKey) -> dict[str, Any]:
    return {
        "keyId": api_key.key_id,
        "workflowId": api_key.workflow_id,
        "name": api_key.name,
        "createdAt": api_key.created_at.isoformat() if api_key.created_at else None,
        "lastUsedAt": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
        "revokedAt": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
        "expiresAt": api_key.expires_at.isoformat() if api_key.expires_at else None,
    }


def _validate_pipeline_graph(graph: PipelineBlueprintPayload) -> None:
    node_ids = [node.id for node in graph.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise HTTPException(status_code=422, detail="Workflow node ids must be unique")
    if not any(node.type == "source" for node in graph.nodes):
        raise HTTPException(status_code=422, detail="Workflow requires a source node")
    known = set(node_ids)
    for edge in graph.edges:
        if edge.source not in known or edge.target not in known:
            raise HTTPException(status_code=422, detail=f"Invalid edge {edge.id}")


def _pipeline_rows(request: PipelineExecuteRequest) -> list[Any]:
    if request.rows is not None:
        return request.rows
    if isinstance(request.data, list):
        return request.data
    return [request.data if request.data is not None else {}]


def _parse_model_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"text": text}


async def _run_pipeline_processor(row: Any, node: PipelineNodePayload) -> Any:
    return await _run_pipeline_formatter(row, node)


def _structured_schema(config: dict[str, Any]) -> dict[str, Any]:
    structured = config.get("structuredOutput")
    if isinstance(structured, dict) and isinstance(structured.get("schema"), dict):
        return structured["schema"]
    schema = config.get("jsonSchema")
    return schema if isinstance(schema, dict) else {"type": "object"}


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str = "output") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if isinstance(key, str) and key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise ValueError(f"{path} has unexpected keys: {', '.join(sorted(extra))}")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                _validate_schema_value(value[key], child_schema, f"{path}.{key}")
    elif expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else None
        if item_schema:
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{index}]")
    elif expected_type == "string" and not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    elif expected_type == "number" and not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    elif expected_type == "integer" and not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    elif expected_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")


def _csv_from_value(value: Any, config: dict[str, Any]) -> str:
    separator = config.get("separator") if isinstance(config.get("separator"), str) else ","
    delimiter = separator[:1] or ","
    include_header = bool(config.get("includeHeader", True))
    columns = config.get("columns") if isinstance(config.get("columns"), list) else []
    rows = value if isinstance(value, list) else [value]
    dict_rows = [row if isinstance(row, dict) else {"value": row} for row in rows]
    fieldnames = [column for column in columns if isinstance(column, str) and column]
    if not fieldnames:
        fieldnames = list(dict.fromkeys(key for row in dict_rows for key in row)) or ["value"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore")
    if include_header:
        writer.writeheader()
    writer.writerows(dict_rows)
    return output.getvalue().strip("\r\n")


async def _run_pipeline_formatter(row: Any, node: PipelineNodePayload) -> Any:
    config = node.config
    selected_model = config.get("model") if isinstance(config.get("model"), str) else _get_configured_model()
    base_url = _resolve_pipeline_base_url(config.get("baseUrl"))
    model = load_chat_model(selected_model, base_url=base_url)
    model = apply_reasoning_effort(model, selected_model, "none", base_url=base_url)
    schema = _structured_schema(config)
    structured = config.get("structuredOutput") if isinstance(config.get("structuredOutput"), dict) else {}
    strict = bool(structured.get("strict", True))
    system_message = config.get("systemMessage") if isinstance(config.get("systemMessage"), str) else "Process one JSON row."
    prompt = (
        f"{system_message}\n\n"
        "Return only valid JSON. Do not include prose or markdown fences.\n"
        f"Structured output schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=json.dumps(row, ensure_ascii=False, default=str)),
        ]
    )
    parsed = _parse_model_json(get_message_text(response))
    if strict:
        try:
            _validate_schema_value(parsed, schema)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{node.label} output failed schema validation: {exc}") from exc
    if node.type == "csv_formatter":
        return _csv_from_value(parsed, config)
    return parsed


async def _post_pipeline_egress(url: str, payload: Any, *, method: str = "POST", headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}

    def send() -> dict[str, Any]:
        request = urllib.request.Request(  # nosec B310 - user-configured pipeline sink
            url,
            data=body,
            method=method.upper(),
            headers=request_headers,
        )
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310 - user-configured pipeline sink
            response_body = response.read().decode("utf-8")
            return {"status": response.status, "body": response_body[:2000]}

    return await asyncio.to_thread(send)


def _topological_pipeline_nodes(graph: PipelineBlueprintPayload) -> list[PipelineNodePayload]:
    nodes_by_id = {node.id: node for node in graph.nodes}
    indegree = {node.id: 0 for node in graph.nodes}
    outgoing: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
    queue = [node.id for node in graph.nodes if indegree.get(node.id, 0) == 0]
    ordered: list[PipelineNodePayload] = []
    while queue:
        node_id = queue.pop(0)
        ordered.append(nodes_by_id[node_id])
        for target in outgoing.get(node_id, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(ordered) != len(graph.nodes):
        raise HTTPException(status_code=422, detail="Workflow graph cannot contain cycles")
    return ordered


async def _execute_pipeline_graph(rows: list[Any], graph: PipelineBlueprintPayload, dry_run: bool) -> dict[str, Any]:
    ordered = _topological_pipeline_nodes(graph)
    incoming: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        incoming.setdefault(edge.target, []).append(edge.source)

    output_rows: list[Any] = []
    sink_results: list[dict[str, Any]] = []
    for source_row in rows:
        node_values: dict[str, Any] = {}
        for node in ordered:
            parents = incoming.get(node.id, [])
            if node.type == "source":
                node_values[node.id] = source_row
                continue
            if len(parents) > 1 and node.type in {"formatter", "csv_formatter", "sink", "output"}:
                node_input: Any = {parent: node_values.get(parent) for parent in parents}
            elif parents:
                node_input = node_values.get(parents[0])
            else:
                node_input = source_row

            if node.type in {"formatter", "csv_formatter"}:
                node_values[node.id] = node_input if dry_run else await _run_pipeline_formatter(node_input, node)
            elif node.type == "sink":
                node_values[node.id] = node_input
                url = node.config.get("url") if isinstance(node.config.get("url"), str) else ""
                enabled = bool(node.config.get("enabled", True))
                if url and enabled and not dry_run:
                    method = node.config.get("method") if isinstance(node.config.get("method"), str) else "POST"
                    headers = node.config.get("headers") if isinstance(node.config.get("headers"), dict) else {}
                    safe_headers = {str(key): str(value) for key, value in headers.items()}
                    sink_results.append({"nodeId": node.id, **await _post_pipeline_egress(url, node_input, method=method, headers=safe_headers)})
            elif node.type == "output":
                node_values[node.id] = node_input
            else:
                node_values[node.id] = node_input

        outputs = [node_values[node.id] for node in graph.nodes if node.type == "output" and node.id in node_values]
        output_rows.append(outputs[0] if len(outputs) == 1 else outputs if outputs else node_values.get(ordered[-1].id, source_row))
    return {"processed": output_rows, "sinks": sink_results}


def _load_pipeline_model_choices() -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = [{"id": _get_configured_model(), "label": "Default", "model": _get_configured_model()}]
    path = Path(os.environ.get("AGENT_MODELS_CONFIG", "agent-models.json"))
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = {}
    if isinstance(parsed, dict):
        entries: list[tuple[str, dict[str, Any]]] = []
        default = parsed.get("default")
        if isinstance(default, dict):
            entries.append(("Configured default", default))
        agents = parsed.get("agents")
        if isinstance(agents, dict):
            entries.extend((str(name), value) for name, value in agents.items() if isinstance(value, dict))
        for label, entry in entries:
            model = entry.get("model")
            if isinstance(model, str) and model:
                base_url = _resolve_pipeline_base_url(entry.get("base_url"))
                choices.append({"id": f"{label}:{model}", "label": label, "model": model, "baseUrl": base_url})
    deduped: dict[tuple[str, str | None], dict[str, Any]] = {}
    for choice in choices:
        key = (str(choice.get("model")), choice.get("baseUrl") if isinstance(choice.get("baseUrl"), str) else None)
        deduped[key] = choice
    return list(deduped.values())


async def _get_user_workflow(session: AsyncSession, workflow_id: str, user_id: str) -> PipelineWorkflow:
    workflow = await session.scalar(
        select(PipelineWorkflow).where(
            PipelineWorkflow.workflow_id == workflow_id,
            PipelineWorkflow.user_id == user_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


def _pipeline_key_from_request(request: Request) -> str | None:
    explicit_key = request.headers.get("x-pipeline-key")
    if explicit_key:
        return explicit_key.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token.startswith(PIPELINE_KEY_PREFIX):
            return token
    return None


async def _resolve_pipeline_actor(
    request: Request,
    request_payload: PipelineExecuteRequest,
    session: AsyncSession,
) -> tuple[str, PipelineWorkflow]:
    raw_key = _pipeline_key_from_request(request)
    if raw_key:
        api_key = await session.scalar(
            select(PipelineApiKey).where(
                PipelineApiKey.key_hash == _hash_pipeline_key(raw_key),
                PipelineApiKey.workflow_id == request_payload.workflowId,
                PipelineApiKey.revoked_at.is_(None),
            )
        )
        if api_key is None:
            raise HTTPException(status_code=401, detail="Invalid pipeline API key")
        now = datetime.now(UTC)
        expires_at = api_key.expires_at
        if expires_at and expires_at.replace(tzinfo=expires_at.tzinfo or UTC) < now:
            raise HTTPException(status_code=401, detail="Pipeline API key expired")
        workflow = await _get_user_workflow(session, request_payload.workflowId, api_key.user_id)
        api_key.last_used_at = now
        return api_key.user_id, workflow

    user = await require_auth(request)
    workflow = await _get_user_workflow(session, request_payload.workflowId, user.identity)
    return user.identity, workflow


def _serialize_artifact(artifact: ExecutiveArtifact) -> dict[str, Any]:
    timestamp = artifact.updated_at or artifact.created_at
    return {
        "id": artifact.artifact_id,
        "title": artifact.title,
        "content": artifact.content,
        "threadId": artifact.thread_id,
        "timestamp": timestamp.isoformat() if timestamp else None,
        "agentId": artifact.agent_id,
        "artifactKind": artifact.artifact_kind,
        "sourceMessageId": artifact.source_message_id,
        "metadata": artifact.metadata_dict or {},
    }


async def _get_user_thread(session: AsyncSession, thread_id: str, user: User) -> Thread:
    thread = await session.scalar(select(Thread).where(Thread.thread_id == thread_id, Thread.user_id == user.identity))
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    metadata = thread.metadata_json or {}
    graph_id = metadata.get("graph_id") if isinstance(metadata, dict) else None
    ensure_graph_access(user, graph_id if isinstance(graph_id, str) else None)
    return thread


@app.get("/custom/whoami")
async def whoami(user: User = Depends(require_auth)):
    """Return current user info - demonstrates authentication integration.

    This endpoint shows how to access authenticated user data in custom routes.
    Custom fields from your auth handler (e.g., role, team_id) are accessible.
    """
    return {
        "identity": user.identity,
        "display_name": user.display_name,
        "is_authenticated": user.is_authenticated,
        "permissions": user.permissions,
        # Custom fields from auth handler are accessible
        "role": getattr(user, "role", None),
        "subscription_tier": getattr(user, "subscription_tier", None),
        "team_id": getattr(user, "team_id", None),
        "email": getattr(user, "email", None),
    }


@app.get("/custom/public")
async def public_endpoint(user: User = Depends(require_auth)):
    """Example endpoint protected by the active auth configuration."""
    return {
        "message": "This endpoint requires authentication",
        "user": user.identity,
    }


@app.get("/custom/protected")
async def protected_endpoint(user: User = Depends(require_auth)):
    """Protected endpoint - explicitly requires authentication.

    This endpoint always requires authentication regardless of
    enable_custom_route_auth configuration.
    """
    return {
        "message": "This endpoint is always protected",
        "user": user.identity,
        "role": getattr(user, "role", None),
    }


@app.get("/custom/model-info")
async def model_info(agent_id: str | None = Query(default=None, alias="agentId"), user: User = Depends(require_auth)):
    """Return configured and resolved active model info for the UI."""
    model_config = resolve_agent_model(agent_id, context_model=_get_configured_model())
    resolved_provider, resolved_model = resolve_model_name(
        model_config.model,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    resolved_base_url = (
        model_config.base_url
        or os.environ.get("VLLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or None
    )
    return {
        "user": user.identity,
        "agent_id": agent_id,
        "configured_model": model_config.model,
        "active_model": f"{resolved_provider}/{resolved_model}",
        "active_model_name": resolved_model,
        "display_model_name": resolved_model,
        "provider": resolved_provider,
        "base_url": resolved_base_url,
        "vllm_base_url": os.environ.get("VLLM_BASE_URL", "").strip() or None,
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "").strip() or None,
        "endpoint_active_model": get_active_model_name(base_url=model_config.base_url, api_key=model_config.api_key),
        "context_window": model_config.context_window,
        "response_reserve": model_config.response_reserve,
        "safety_buffer": model_config.safety_buffer,
        "min_recent_messages": model_config.min_recent_messages,
    }


@app.get("/dbchat/connections")
async def list_dbchat_connections(user: User = Depends(require_auth)):
    """List DBChat connections. Currently exposes the configured analytic DB connection."""
    return {"connections": _available_dbchat_connections(), "user": user.identity}


@app.get("/dbchat/connections/{connection_id}/catalog")
async def get_dbchat_catalog(
    connection_id: str,
    include_system: bool = Query(default=False, alias="includeSystem"),
    user: User = Depends(require_auth),
):
    """Return SQLRooms-compatible catalog arrays: databases, schemas, tables."""
    try:
        adapter = _get_dbchat_adapter(connection_id)
        return adapter.catalog(include_system=include_system)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning("DBChat catalog failed connection=%s user=%s error=%s", connection_id, user.identity, exc)
        raise HTTPException(status_code=502, detail=f"Could not read database catalog: {exc}") from exc


@app.get("/dbchat/connections/{connection_id}/tree")
async def get_dbchat_tree(
    connection_id: str,
    include_system: bool = Query(default=False, alias="includeSystem"),
    user: User = Depends(require_auth),
):
    """Return a normalized side-panel tree for DBChat schema browsing."""
    try:
        adapter = _get_dbchat_adapter(connection_id)
        return adapter.tree(include_system=include_system)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning("DBChat tree failed connection=%s user=%s error=%s", connection_id, user.identity, exc)
        raise HTTPException(status_code=502, detail=f"Could not read database tree: {exc}") from exc


@app.get("/dbchat/connections/{connection_id}/tables/{table}/columns")
async def describe_dbchat_table(
    connection_id: str,
    table: str,
    schema: str | None = Query(default=None),
    database: str | None = Query(default=None),
    user: User = Depends(require_auth),
):
    """Describe a table or view, including columns and adapter-provided index metadata."""
    try:
        adapter = _get_dbchat_adapter(connection_id)
        return adapter.describe_table(table=table, schema=schema, database=database)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning(
            "DBChat describe failed connection=%s table=%s schema=%s user=%s error=%s",
            connection_id,
            table,
            schema,
            user.identity,
            exc,
        )
        raise HTTPException(status_code=502, detail=f"Could not describe table: {exc}") from exc


@app.get("/dbchat/connections/{connection_id}/tables/{table}/sample")
async def preview_dbchat_table(
    connection_id: str,
    table: str,
    schema: str | None = Query(default=None),
    database: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_auth),
):
    """Return a limited read-only preview of a table or view."""
    try:
        adapter = _get_dbchat_adapter(connection_id)
        return adapter.preview_table(table=table, schema=schema, database=database, limit=limit)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning(
            "DBChat preview failed connection=%s table=%s schema=%s user=%s error=%s",
            connection_id,
            table,
            schema,
            user.identity,
            exc,
        )
        raise HTTPException(status_code=502, detail=f"Could not preview table: {exc}") from exc


@app.post("/dbchat/connections/{connection_id}/query")
async def query_dbchat_connection(
    connection_id: str,
    payload: DbChatQueryRequest,
    user: User = Depends(require_auth),
):
    """Run a read-only SQL query and return JSON rows for the DBChat workbench."""
    try:
        adapter = _get_dbchat_adapter(connection_id)
        result = adapter.query(payload.sql, limit=payload.limit)
        logger.info(
            "DBChat query completed connection=%s user=%s rows=%s",
            connection_id,
            user.identity,
            result.get("rowCount"),
        )
        return result
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning("DBChat query failed connection=%s user=%s error=%s", connection_id, user.identity, exc)
        raise HTTPException(status_code=502, detail=f"Could not execute query: {exc}") from exc


@app.post("/dbchat/connections/{connection_id}/chat")
async def chat_dbchat_connection(
    connection_id: str,
    payload: DbChatChatRequest,
    user: User = Depends(require_auth),
):
    """Answer DBChat questions and return executed DB artifacts separately from prose."""
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    try:
        adapter = _get_dbchat_adapter(connection_id)
        selected_tables = _normalize_dbchat_selected_tables(payload)
        context_mode = _normalize_dbchat_context_mode(payload.contextMode)
        scope = _format_dbchat_scope(adapter, selected_tables, context_mode)
        model_name = _get_configured_model()
        model = load_chat_model(model_name)
        model = apply_reasoning_effort(model, model_name, _normalize_dbchat_reasoning_effort(payload.reasoningEffort))
        wants_rows = _dbchat_user_wants_rows(message)
        wants_chart = _dbchat_user_wants_chart(message)
        system_prompt = (
            "You are DBChat, a natural data analyst assistant embedded in a SQL workbench. "
            "Answer conversationally like a normal chat assistant. Do not make the response formulaic. "
            "Use the provided schema context to answer database questions and explain table relationships when useful. "
            "If data must be queried or a chart must be built, append exactly one fenced sql block at the end; "
            "do not put SQL in the prose and do not include chart JSON. The application will execute safe read-only SQL "
            "and render query/chart artifacts separately. If the user asks about SQL itself, explain it normally. "
            "If the schema context is insufficient, say what table or column is needed. "
            f"Context mode: {context_mode}."
            + (" In lock mode, use only the locked tables. " if context_mode == "lock" else " Suggested tables are hints only; all listed tables are available. ")
            + "\n\n"
            f"Connection: {adapter.title} ({adapter.connection_info().get('engineId', 'database')})\n"
            f"Schema context:\n{scope}"
        )
        response = await model.ainvoke(
            [
                SystemMessage(content=system_prompt),
                *_dbchat_history_messages(payload.history),
                HumanMessage(content=message[:8000]),
            ]
        )
        visible_answer = _get_dbchat_visible_message_text(response)
        answer = _strip_dbchat_sql_blocks(visible_answer)
        reasoning = _extract_dbchat_reasoning(response)
        generated_sql = (
            _dbchat_selected_table_sql(adapter, selected_tables)
            if _dbchat_user_wants_preview(message) and not wants_chart and selected_tables
            else None
        )
        generated_sql = generated_sql or _extract_dbchat_sql(visible_answer)
        query_result: dict[str, Any] | None = None
        executed_sql: str | None = None
        if generated_sql and (wants_rows or wants_chart) and _is_read_only_dbchat_sql(generated_sql):
            if context_mode == "lock":
                _validate_dbchat_lock_sql(generated_sql, selected_tables)
            query_result = adapter.query(generated_sql, limit=50)
            executed_sql = generated_sql
        chart_option = _build_dbchat_chart_option(message, query_result) if wants_chart else None
        tool_events: list[dict[str, Any]] = []
        if executed_sql and query_result is not None:
            tool_events.append(
                {
                    "type": "sql",
                    "sql": executed_sql,
                    "rowCount": query_result.get("rowCount", 0),
                    "limit": query_result.get("limit", 50),
                }
            )
        if chart_option is not None:
            tool_events.append({"type": "chart", "chartOption": chart_option})
        if query_result is not None and wants_rows and not answer:
            answer = "Here are the rows I found."
        if chart_option is not None and not answer:
            answer = "Here is the chart."
        logger.info("DBChat chat completed connection=%s user=%s", connection_id, user.identity)
        return {
            "connectionId": connection_id,
            "response": answer or "I could not produce a DBChat response.",
            "reasoning": reasoning,
            "generatedSql": executed_sql,
            "queryResult": query_result,
            "chartOption": chart_option,
            "toolEvents": tool_events,
            "contextMode": context_mode,
            "selectedTables": [table.model_dump() for table in selected_tables],
            "selectedTable": selected_tables[0].model_dump() if selected_tables else None,
        }
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.warning("DBChat chat schema failed connection=%s user=%s error=%s", connection_id, user.identity, exc)
        raise HTTPException(status_code=502, detail=f"Could not read database schema for chat: {exc}") from exc


@app.get("/pipeline/models")
async def list_pipeline_models(user: User = Depends(require_auth)):
    """Return model choices usable by side-chain pipeline formatter nodes."""
    return {"models": _load_pipeline_model_choices(), "user": user.identity}


@app.get("/pipeline/workflows")
async def list_pipeline_workflows(
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    result = await session.scalars(
        select(PipelineWorkflow)
        .where(PipelineWorkflow.user_id == user.identity)
        .order_by(PipelineWorkflow.updated_at.desc())
    )
    return {"workflows": [_serialize_workflow(workflow) for workflow in result.all()]}


@app.post("/pipeline/workflows")
async def create_pipeline_workflow(
    payload: PipelineWorkflowRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow_id = _slugify_workflow_id(payload.workflowId or payload.name)
    existing = await session.scalar(select(PipelineWorkflow).where(PipelineWorkflow.workflow_id == workflow_id))
    if existing is not None:
        workflow_id = f"{workflow_id}-{uuid.uuid4().hex[:6]}"
    graph = payload.graph.model_copy(update={"workflowId": workflow_id})
    _validate_pipeline_graph(graph)
    workflow = PipelineWorkflow(
        workflow_id=workflow_id,
        user_id=user.identity,
        name=payload.name,
        description=payload.description,
        graph=graph.model_dump(),
    )
    session.add(workflow)
    await session.commit()
    await session.refresh(workflow)
    return {"workflow": _serialize_workflow(workflow)}


@app.get("/pipeline/workflows/{workflow_id}")
async def get_pipeline_workflow(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    return {"workflow": _serialize_workflow(workflow)}


@app.put("/pipeline/workflows/{workflow_id}")
async def update_pipeline_workflow(
    workflow_id: str,
    payload: PipelineWorkflowUpdateRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    if payload.name is not None:
        workflow.name = payload.name
    if payload.description is not None:
        workflow.description = payload.description
    if payload.graph is not None:
        graph = payload.graph.model_copy(update={"workflowId": workflow_id})
        _validate_pipeline_graph(graph)
        workflow.graph = graph.model_dump()
    workflow.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(workflow)
    return {"workflow": _serialize_workflow(workflow)}


@app.delete("/pipeline/workflows/{workflow_id}")
async def delete_pipeline_workflow(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    await session.delete(workflow)
    await session.commit()
    return {"ok": True, "workflowId": workflow_id}


@app.get("/pipeline/workflows/{workflow_id}/keys")
async def list_pipeline_api_keys(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    result = await session.scalars(
        select(PipelineApiKey)
        .where(PipelineApiKey.workflow_id == workflow_id, PipelineApiKey.user_id == user.identity)
        .order_by(PipelineApiKey.created_at.desc())
    )
    return {"keys": [_serialize_api_key(api_key) for api_key in result.all()]}


@app.post("/pipeline/workflows/{workflow_id}/keys")
async def create_pipeline_api_key(
    workflow_id: str,
    payload: PipelineApiKeyRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    raw_key = f"{PIPELINE_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    api_key = PipelineApiKey(
        workflow_id=workflow_id,
        user_id=user.identity,
        name=payload.name,
        key_hash=_hash_pipeline_key(raw_key),
        expires_at=payload.expiresAt,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return {"key": _serialize_api_key(api_key), "secret": raw_key}


@app.delete("/pipeline/workflows/{workflow_id}/keys/{key_id}")
async def revoke_pipeline_api_key(
    workflow_id: str,
    key_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    api_key = await session.scalar(
        select(PipelineApiKey).where(
            PipelineApiKey.key_id == key_id,
            PipelineApiKey.workflow_id == workflow_id,
            PipelineApiKey.user_id == user.identity,
        )
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    api_key.revoked_at = datetime.now(UTC)
    await session.commit()
    return {"ok": True, "keyId": key_id}


@app.post("/pipeline")
async def execute_pipeline(
    request_payload: PipelineExecuteRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Accept pushed JSON, run a persisted workflow, then return or sink the result."""
    user_id, workflow = await _resolve_pipeline_actor(request, request_payload, session)
    graph = PipelineBlueprintPayload.model_validate(workflow.graph)
    _validate_pipeline_graph(graph)
    rows = _pipeline_rows(request_payload)
    output = await _execute_pipeline_graph(rows, graph, request_payload.dryRun)
    result: dict[str, Any] = {
        "ok": True,
        "workflowId": workflow.workflow_id,
        "user": user_id,
        "dryRun": request_payload.dryRun,
        "processed": output["processed"],
        "rowCount": len(output["processed"]),
        "sinks": output["sinks"],
    }
    session.add(
        PipelineRun(
            workflow_id=workflow.workflow_id,
            user_id=user_id,
            status="completed",
            input=request_payload.model_dump(),
            output=result,
        )
    )
    await session.commit()
    return result


@app.post("/custom/analyze-images")
async def analyze_images(
    prompt: str = Form(""),
    files: list[UploadFile] = File(...),
    user: User = Depends(require_auth),
):
    """Analyze uploaded images and return a text description for text-only agent flows."""
    if not files:
        raise HTTPException(status_code=400, detail="No image files were provided")

    content_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Analyze the uploaded image(s) for an agent workflow. "
                "Describe the important visible facts, charts, tables, text, and context that would help answer the user's request. "
                "Keep it concise but specific.\n\n"
                f"User request: {prompt or 'Please analyze this image.'}"
            ),
        }
    ]

    valid_image_count = 0
    for uploaded_file in files:
        content_type = (uploaded_file.content_type or "").strip().lower()
        if not content_type.startswith("image/"):
            continue

        file_bytes = await uploaded_file.read()
        if not file_bytes:
            continue

        valid_image_count += 1
        data_url = f"data:{content_type};base64,{base64.b64encode(file_bytes).decode('ascii')}"
        content_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            }
        )

    if valid_image_count == 0:
        raise HTTPException(status_code=400, detail="No valid image files were provided")

    try:
        model = load_chat_model(_get_configured_model())
        response = await model.ainvoke([HumanMessage(content=content_blocks)])
    except Exception as error:
        if is_media_not_supported_error(error):
            return {
                "supported": False,
                "message": "Sorry, the model do not have image capability.",
                "user": user.identity,
            }
        raise

    return {
        "supported": True,
        "analysis": get_message_text(response).strip(),
        "user": user.identity,
    }


@app.get("/custom/executive-artifacts")
async def list_executive_artifacts(
    thread_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_thread(session, thread_id, user)
    result = await session.scalars(
        select(ExecutiveArtifact)
        .where(
            ExecutiveArtifact.thread_id == thread_id,
            ExecutiveArtifact.user_id == user.identity,
        )
        .order_by(ExecutiveArtifact.created_at.desc())
    )
    artifacts = result.all()
    return {"artifacts": [_serialize_artifact(artifact) for artifact in artifacts]}


@app.get("/custom/executive-artifacts/{artifact_id}")
async def get_executive_artifact(
    artifact_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    artifact = await session.scalar(
        select(ExecutiveArtifact).where(
            ExecutiveArtifact.artifact_id == artifact_id,
            ExecutiveArtifact.user_id == user.identity,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    await _get_user_thread(session, artifact.thread_id, user)
    return {"artifact": _serialize_artifact(artifact)}


@app.put("/custom/executive-artifacts/thread/{thread_id}")
async def replace_executive_artifacts(
    thread_id: str,
    request: Request,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_thread(session, thread_id, user)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:thread_key))"),
        {
            "scope": f"executive_artifacts:{user.identity}",
            "thread_key": thread_id,
        },
    )

    body = await request.body()
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            raw_payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid JSON request body for executive artifacts content_type=%s length=%s",
                content_type,
                len(body),
            )
            raise HTTPException(status_code=422, detail="Invalid JSON request body") from exc

        parsed_request = ReplaceExecutiveArtifactsRequest.model_validate(raw_payload)
        encoded_artifacts = _decode_transport_json(parsed_request.artifactsBase64, "artifacts")
        artifact_payloads = (
            [ExecutiveArtifactPayload.model_validate(artifact) for artifact in encoded_artifacts]
            if encoded_artifacts is not None
            else parsed_request.artifacts
        )
    else:
        encoded_artifacts = body.decode("utf-8").strip()
        decoded_artifacts = _decode_transport_json(encoded_artifacts, "artifacts") or []
        artifact_payloads = [ExecutiveArtifactPayload.model_validate(artifact) for artifact in decoded_artifacts]

    logger.info(
        "Replacing executive artifacts thread_id=%s count=%s content_type=%s",
        thread_id,
        len(artifact_payloads),
        content_type or "<missing>",
    )

    await session.execute(
        delete(ExecutiveArtifact).where(
            ExecutiveArtifact.thread_id == thread_id,
            ExecutiveArtifact.user_id == user.identity,
        )
    )

    artifacts = []
    for artifact in artifact_payloads:
        title = _decode_transport_string(artifact.titleBase64, artifact.title, "title")
        content = _decode_transport_string(artifact.contentBase64, artifact.content, "content")
        artifacts.append(
            ExecutiveArtifact(
                artifact_id=artifact.id,
                thread_id=thread_id,
                user_id=user.identity,
                title=title,
                content=content,
                agent_id=artifact.agentId,
                artifact_kind=artifact.artifactKind,
                source_message_id=artifact.sourceMessageId,
                metadata_dict=artifact.metadata,
                created_at=artifact.timestamp,
                updated_at=artifact.timestamp,
            )
        )

    session.add_all(artifacts)
    await session.commit()

    return {
        "ok": True,
        "artifacts": [_serialize_artifact(artifact) for artifact in artifacts],
    }


# -- Articles / OpenSearch document ingestion endpoints --

_ARTICLES_CONFIG_PATH = Path(__file__).resolve().parent / ".aegra-articles.json"


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ArticlesConfigRequest(BaseModel):
    opensearch_url: str
    opensearch_user: str
    opensearch_password: str
    embedding_model: str
    use_ssl: bool = False
    verify_certs: bool = True
    ssl_assert_hostname: bool = True


class ArticlesConfigResponse(BaseModel):
    opensearch_url: str
    opensearch_user: str
    embedding_model: str
    use_ssl: bool
    verify_certs: bool
    ssl_assert_hostname: bool


class IngestDocumentRequest(BaseModel):
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentsRequest(BaseModel):
    index: str
    documents: list[IngestDocumentRequest]
    force: bool = False


def _load_articles_config() -> dict[str, Any] | None:
    articles_url = os.environ.get("ARTICLES_OPENSEARCH_URL")
    articles_user = os.environ.get("ARTICLES_OPENSEARCH_USER")
    articles_password = os.environ.get("ARTICLES_OPENSEARCH_PASSWORD")
    articles_model = os.environ.get("ARTICLES_EMBEDDING_MODEL")
    if articles_url and articles_user and articles_password and articles_model:
        return {
            "opensearch_url": articles_url,
            "opensearch_user": articles_user,
            "opensearch_password": articles_password,
            "embedding_model": articles_model,
            "use_ssl": _read_bool_env("ARTICLES_OPENSEARCH_USE_SSL", False),
            "verify_certs": _read_bool_env("ARTICLES_OPENSEARCH_VERIFY_CERTS", True),
            "ssl_assert_hostname": _read_bool_env("ARTICLES_OPENSEARCH_SSL_ASSERT_HOSTNAME", True),
        }

    kms_host = os.environ.get("KMS_OPENSEARCH_HOST")
    kms_user = os.environ.get("KMS_OPENSEARCH_USER")
    kms_password = os.environ.get("KMS_OPENSEARCH_PASSWORD")
    kms_model = os.environ.get("KMS_EMBEDDING_MODEL")
    if kms_host and kms_user and kms_password and kms_model:
        kms_port = os.environ.get("KMS_OPENSEARCH_PORT", "9200")
        use_ssl = _read_bool_env("KMS_OPENSEARCH_USE_SSL", False)
        scheme = "https" if use_ssl else "http"
        return {
            "opensearch_url": kms_host
            if kms_host.startswith(("http://", "https://"))
            else f"{scheme}://{kms_host}:{kms_port}",
            "opensearch_user": kms_user,
            "opensearch_password": kms_password,
            "embedding_model": kms_model,
            "use_ssl": use_ssl,
            "verify_certs": _read_bool_env("KMS_OPENSEARCH_VERIFY_CERTS", True),
            "ssl_assert_hostname": _read_bool_env("KMS_OPENSEARCH_SSL_ASSERT_HOSTNAME", True),
        }

    if not _ARTICLES_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(_ARTICLES_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_articles_config(data: dict[str, Any]) -> None:
    _ARTICLES_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@lru_cache(maxsize=8)
def _get_articles_embeddings(model_name: str) -> OpenAIEmbeddings:
    init_kwargs: dict[str, str] = {"model": model_name}
    base_url = os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        init_kwargs["base_url"] = base_url
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        init_kwargs["api_key"] = api_key
    return OpenAIEmbeddings(**init_kwargs)


def _opensearch_client(config: dict[str, Any]) -> OpenSearchClient:
    return OpenSearchClient(
        hosts=[config["opensearch_url"]],
        http_auth=(config["opensearch_user"], config["opensearch_password"]),
        use_ssl=config.get("use_ssl", False),
        verify_certs=config.get("verify_certs", True),
        ssl_assert_hostname=config.get("ssl_assert_hostname", True),
    )


def _vector_store_for_index(index_name: str, config: dict[str, Any]) -> OpenSearchVectorSearch:
    return OpenSearchVectorSearch(
        opensearch_url=config["opensearch_url"],
        index_name=index_name,
        embedding_function=_get_articles_embeddings(config["embedding_model"]),
        http_auth=(config["opensearch_user"], config["opensearch_password"]),
        use_ssl=config.get("use_ssl", False),
        verify_certs=config.get("verify_certs", True),
        ssl_assert_hostname=config.get("ssl_assert_hostname", True),
    )


def _articles_index_metadata(mapping: dict[str, Any], index_name: str) -> dict[str, Any]:
    mappings = mapping.get(index_name, {}).get("mappings", {})
    meta = mappings.get("_meta", {}).get("aegra_metadata", {})
    if meta:
        return meta

    # Older local test mappings stored metadata under a field mapping.
    props = mappings.get("properties", {})
    field_meta = props.get("aegra_metadata", {})
    if field_meta.get("value"):
        return field_meta["value"]
    return field_meta


@app.get("/custom/articles/config")
async def get_articles_config(user: User = Depends(require_auth)):
    """Return the persisted articles OpenSearch connection config (without password)."""
    data = _load_articles_config()
    if not data:
        raise HTTPException(status_code=404, detail="No articles config saved yet")
    return ArticlesConfigResponse(
        opensearch_url=data["opensearch_url"],
        opensearch_user=data["opensearch_user"],
        embedding_model=data["embedding_model"],
        use_ssl=data.get("use_ssl", False),
        verify_certs=data.get("verify_certs", True),
        ssl_assert_hostname=data.get("ssl_assert_hostname", True),
    ).model_dump()


@app.post("/custom/articles/config")
async def save_articles_config(
    request: ArticlesConfigRequest,
    user: User = Depends(require_auth),
):
    """Persist the articles OpenSearch connection config for future use."""
    data = request.model_dump()
    _save_articles_config(data)
    logger.info(
        "Articles config saved by user=%s url=%s model=%s",
        user.identity,
        request.opensearch_url,
        request.embedding_model,
    )
    return ArticlesConfigResponse(
        opensearch_url=data["opensearch_url"],
        opensearch_user=data["opensearch_user"],
        embedding_model=data["embedding_model"],
        use_ssl=data.get("use_ssl", False),
        verify_certs=data.get("verify_certs", True),
        ssl_assert_hostname=data.get("ssl_assert_hostname", True),
    ).model_dump()


@app.get("/custom/articles/indexes")
async def list_articles_indexes(user: User = Depends(require_auth)):
    """List all OpenSearch indexes with document counts and embedding model metadata."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    client = _opensearch_client(config)
    try:
        all_indexes = client.indices.get_alias(index="*")
    except TransportError as exc:
        logger.error("Failed to list OpenSearch indexes: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    indexes = []
    for index_name in sorted(all_indexes.keys()):
        info: dict[str, Any] = {"name": index_name}
        try:
            stats = client.count(index=index_name)
            info["documentCount"] = stats.get("count", 0)
        except TransportError:
            info["documentCount"] = 0

        try:
            mapping = client.indices.get_mapping(index=index_name)
            info["embeddingModel"] = _articles_index_metadata(mapping, index_name).get("embedding_model")
        except TransportError:
            pass

        indexes.append(info)

    return {"indexes": indexes}


@app.post("/custom/articles/indexes")
async def create_articles_index(
    index_name: str = Query(..., description="Name for the new index"),
    user: User = Depends(require_auth),
):
    """Create a new OpenSearch index with embedding model metadata."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    client = _opensearch_client(config)

    try:
        if client.indices.exists(index=index_name):
            raise HTTPException(status_code=409, detail=f"Index '{index_name}' already exists")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    try:
        client.indices.create(
            index=index_name,
            body={
                "mappings": {
                    "_meta": {
                        "aegra_metadata": {
                            "embedding_model": config["embedding_model"],
                            "created_by": user.identity,
                        }
                    }
                }
            },
        )
    except TransportError as exc:
        logger.error("Failed to create index %s: %s", index_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to create index: {exc.error or str(exc)}",
        ) from exc

    logger.info("Created articles index=%s by user=%s model=%s", index_name, user.identity, config["embedding_model"])
    return {"ok": True, "index": index_name, "embeddingModel": config["embedding_model"]}


@app.post("/custom/articles/ingest")
async def ingest_articles(
    request: IngestDocumentsRequest,
    user: User = Depends(require_auth),
):
    """Ingest documents into an OpenSearch index with optional embedding model validation."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    if not request.documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    client = _opensearch_client(config)

    try:
        if not client.indices.exists(index=request.index):
            raise HTTPException(status_code=404, detail=f"Index '{request.index}' does not exist")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    # Check embedding model mismatch
    if not request.force:
        try:
            mapping = client.indices.get_mapping(index=request.index)
            stored_model = _articles_index_metadata(mapping, request.index).get("embedding_model")
            if stored_model and stored_model != config["embedding_model"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Embedding model mismatch: index uses '{stored_model}', "
                        f"config has '{config['embedding_model']}'. "
                        f"Set force=true to override."
                    ),
                )
        except HTTPException:
            raise
        except TransportError:
            pass

    try:
        vector_store = _vector_store_for_index(request.index, config)
        documents = [Document(page_content=doc.content, metadata=doc.metadata) for doc in request.documents]
        vector_store.add_documents(documents)
    except Exception as exc:
        logger.error("Failed to ingest documents into %s: %s", request.index, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to ingest documents: {exc}",
        ) from exc

    logger.info(
        "Ingested %d documents into index=%s by user=%s",
        len(request.documents),
        request.index,
        user.identity,
    )
    return {"ok": True, "index": request.index, "ingested": len(request.documents)}


class ArticleChatRequest(BaseModel):
    message: str
    index_name: str
    force: bool = False


@app.post("/custom/articles/chat")
async def article_chat(
    request: ArticleChatRequest,
    user: User = Depends(require_auth),
):
    """Chat with AI over a selected OpenSearch index. Retrieval runs automatically."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. Configure indexes first.")

    index_name = request.index_name.strip()
    if not index_name:
        raise HTTPException(status_code=400, detail="No index selected. Choose an index before chatting.")

    client = _opensearch_client(config)

    try:
        if not client.indices.exists(index=index_name):
            raise HTTPException(status_code=404, detail=f"Index '{index_name}' does not exist")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    # Check embedding model mismatch
    if not request.force:
        try:
            mapping = client.indices.get_mapping(index=index_name)
            stored_model = _articles_index_metadata(mapping, index_name).get("embedding_model")
            if stored_model and stored_model != config["embedding_model"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Embedding model mismatch: index uses '{stored_model}', "
                        f"config has '{config['embedding_model']}'. Contact an administrator."
                    ),
                )
        except HTTPException:
            raise
        except TransportError:
            pass

    try:
        vector_store = _vector_store_for_index(index_name, config)
        query = request.message.strip()
        results = vector_store.similarity_search(query, k=5)
    except Exception as exc:
        logger.error("Failed to query index %s: %s", index_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not query index '{index_name}': {exc}",
        ) from exc

    if not results:
        context_text = ""
    else:
        context_parts = []
        for i, doc in enumerate(results):
            source = doc.metadata.get("source", doc.metadata.get("file_name", "unknown"))
            context_parts.append(f"[Document {i + 1}] Source: {source}\n{doc.page_content}")
        context_text = "\n\n---\n\n".join(context_parts)

    prompt = f"Use the following retrieved context to answer the question. If the context does not contain relevant information, say so clearly.\n\n{context_text}\n\n---\n\nQuestion: {query}"

    try:
        model = load_chat_model(_get_configured_model())
        response = await model.ainvoke([HumanMessage(content=prompt)])
        answer = get_message_text(response).strip()
    except Exception as exc:
        logger.error("Model generation failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Model generation failed: {exc}",
        ) from exc

    logger.info(
        "Article chat completed index=%s user=%s",
        index_name,
        user.identity,
    )
    return {
        "answer": answer,
        "index": index_name,
        "retrieved_count": len(results),
    }
