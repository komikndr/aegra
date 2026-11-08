"""HTTP authorization precedes shared database browsing and query execution."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from aegra_api.api.dbchat import create_dbchat_router
from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User


@pytest.mark.parametrize("method,path,body", [("GET", "", None), ("GET", "/main/catalog", None), ("GET", "/main/tree", None), ("GET", "/main/tables/ships/columns", None), ("GET", "/main/tables/ships/sample", None), ("POST", "/main/query", {"sql": "select 1"}), ("POST", "/main/chat", {"message": "hello"})])
@pytest.mark.parametrize("roles", [[], ["Executive"], ["Senior Data Engineer"]])
def test_non_operator_rejected_before_adapter_access(method: str, path: str, body: dict[str, str] | None, roles: list[str]) -> None:
    lookup, catalog = MagicMock(), MagicMock()
    app = FastAPI()
    app.include_router(create_dbchat_router(lookup, catalog))
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=roles)
    with TestClient(app) as client:
        assert client.request(method, "/dbchat/connections" + path, json=body).status_code == 403
    lookup.assert_not_called()
    catalog.assert_not_called()


def test_operator_query_and_retirement_without_legacy_execution() -> None:
    lookup = MagicMock()
    lookup.return_value.query.return_value = {"rows": [[1]], "rowCount": 1}
    app = FastAPI()
    app.include_router(create_dbchat_router(lookup, lambda: []))
    app.dependency_overrides[require_auth] = lambda: User(identity="admin", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        assert client.post("/dbchat/connections/main/chat", json={"message": "hello"}).status_code == 410
        lookup.assert_not_called()
        assert client.post("/dbchat/connections/main/query", json={"sql": "select 1", "limit": 20}).json()["rowCount"] == 1
        lookup.return_value.query.assert_called_once_with("select 1", limit=20)
        lookup.return_value.query.side_effect = SQLAlchemyError("secret connection details")
        error = client.post("/dbchat/connections/main/query", json={"sql": "select 1"})
        assert error.status_code == 502
        assert "secret" not in error.text
