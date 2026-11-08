"""Authorized DBChat browsing/query routes. Conversations use the DBChat graph."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from aegra_api.core.operations_access import require_operations_access


class DbChatAdapter(Protocol):
    def catalog(self, *, include_system: bool) -> dict[str, Any]: ...
    def tree(self, *, include_system: bool) -> dict[str, Any]: ...
    def describe_table(self, *, table: str, schema: str | None, database: str | None) -> dict[str, Any]: ...
    def preview_table(self, *, table: str, schema: str | None, database: str | None, limit: int) -> dict[str, Any]: ...
    def query(self, sql: str, *, limit: int) -> dict[str, Any]: ...


class DbChatQuery(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    limit: int = Field(default=50, ge=1, le=200)


@contextmanager
def _database_errors() -> Iterator[None]:
    try:
        yield
    except SQLAlchemyError as exc:
        raise HTTPException(502, "The database operation failed.") from exc


def create_dbchat_router(
    adapter_lookup: Callable[[str], DbChatAdapter],
    connection_catalog: Callable[[], list[dict[str, Any]]],
) -> APIRouter:
    """Inject server-owned adapters; synchronous handlers run in FastAPI's thread pool."""
    router = APIRouter(prefix="/dbchat/connections", tags=["DBChat operations"], dependencies=[Depends(require_operations_access)])

    @router.get("")
    def connections() -> dict[str, Any]:
        with _database_errors():
            return {"connections": connection_catalog()}

    @router.get("/{connection_id}/catalog")
    def catalog(connection_id: str, include_system: bool = Query(False, alias="includeSystem")) -> dict[str, Any]:
        with _database_errors():
            return adapter_lookup(connection_id).catalog(include_system=include_system)

    @router.get("/{connection_id}/tree")
    def tree(connection_id: str, include_system: bool = Query(False, alias="includeSystem")) -> dict[str, Any]:
        with _database_errors():
            return adapter_lookup(connection_id).tree(include_system=include_system)

    @router.get("/{connection_id}/tables/{table}/columns")
    def columns(connection_id: str, table: str, schema: str | None = None, database: str | None = None) -> dict[str, Any]:
        with _database_errors():
            return adapter_lookup(connection_id).describe_table(table=table, schema=schema, database=database)

    @router.get("/{connection_id}/tables/{table}/sample")
    def sample(connection_id: str, table: str, schema: str | None = None, database: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        with _database_errors():
            return adapter_lookup(connection_id).preview_table(table=table, schema=schema, database=database, limit=limit)

    @router.post("/{connection_id}/query")
    def query(connection_id: str, payload: DbChatQuery) -> dict[str, Any]:
        with _database_errors():
            return adapter_lookup(connection_id).query(payload.sql, limit=payload.limit)

    @router.post("/{connection_id}/chat")
    def retired_chat(connection_id: str) -> None:
        raise HTTPException(410, "Use the analytic_dbchat graph for DBChat conversations.")

    return router
