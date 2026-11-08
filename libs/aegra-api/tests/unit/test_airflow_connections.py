"""Operator authorization and retirement of saved-connection endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegra_api.api.analytics_connections import router
from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User


@pytest.mark.parametrize("roles,expected", [([], 403), (["Staff"], 403), (["Executive"], 403), (["Senior Data Engineer"], 403), (["SuperAdmin"], 200)])
def test_environment_requires_operations_access(monkeypatch: pytest.MonkeyPatch, roles: list[str], expected: int) -> None:
    status = AsyncMock(return_value={"configured": False, "rows": []})
    monkeypatch.setattr("aegra_api.api.analytics_connections.environment_airflow_status", status)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=roles)
    with TestClient(app) as client:
        assert client.get("/analytics/connections/environment").status_code == expected
    assert status.await_count == (1 if expected == 200 else 0)


@pytest.mark.parametrize("method,path", [("get", ""), ("post", ""), ("post", "/test"), ("put", "/other"), ("delete", "/other")])
@pytest.mark.parametrize("roles,expected", [(["Executive"], 403), (["SuperAdmin"], 410)])
def test_saved_connections_are_retired(method: str, path: str, roles: list[str], expected: int) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=roles)
    with TestClient(app) as client:
        assert client.request(method, "/analytics/connections" + path).status_code == expected
