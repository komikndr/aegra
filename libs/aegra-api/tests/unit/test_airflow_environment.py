"""Environment-only Airflow authentication, discovery, and failure handling."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from aegra_api.api import analytics_connections
from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from aegra_api.services import airflow_environment as service


@pytest.mark.asyncio
async def test_unconfigured_does_not_request_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIRFLOW_API_URL", raising=False)
    assert not service.environment_airflow_enabled()
    assert await service.environment_airflow_status() == {
        "configured": False,
        "rows": [],
        "message": "Airflow is not configured on the server.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("token_auth", [True, False])
async def test_discovery_credentials_and_partial_failure(monkeypatch: pytest.MonkeyPatch, token_auth: bool) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "private-token" if token_auth else "")
    monkeypatch.setenv("AIRFLOW_USERNAME", "admin")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "private-password")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/auth/token":
            assert not token_auth
            assert json.loads(request.content) == {"username": "admin", "password": "private-password"}
            return httpx.Response(201, json={"access_token": "private-token"})
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer private-token"
        if request.url.path == "/api/v2/dags":
            assert request.url.params["limit"] == "100"
            return httpx.Response(
                200, json={"dags": [{"dag_id": "healthy"}, {"dag_id": "broken"}], "total_entries": 101}
            )
        if "broken" in request.url.path:
            return httpx.Response(403, json={"detail": "private upstream details"})
        return httpx.Response(
            200, json={"dag_runs": []} if request.url.path.endswith("dagRuns") else {"is_paused": False}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport))
    result = await service.environment_airflow_status()
    assert result["configured"] and result["truncated"]
    assert result["rows"][0]["status"]["mock"] is False
    assert result["rows"][0]["status"]["latest_run"] is None
    assert result["rows"][1]["error"] == "DAG status could not be retrieved from Airflow."
    assert "private" not in json.dumps(result)
    assert sum(request.method == "POST" for request in calls) == (0 if token_auth else 1)


@pytest.mark.asyncio
async def test_dag_allowlist_skips_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "token")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "selected, selected")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/api/v2/dags/selected")
        return httpx.Response(
            200, json={"dag_runs": []} if request.url.path.endswith("dagRuns") else {"is_paused": True}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport))
    result = await service.environment_airflow_status()
    assert len(result["rows"]) == 1
    assert not result["truncated"]


@pytest.mark.asyncio
async def test_run_progress_enforces_allowlist_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "token")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "allowed")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/dags/allowed":
            return httpx.Response(200, json={"dag_id": "allowed"})
        if request.url.path.endswith("/dagRuns/run-1"):
            return httpx.Response(200, json={"dag_run_id": "run-1", "dag_id": "allowed", "state": "running"})
        if request.url.path.endswith("/taskInstances"):
            assert request.url.params["limit"] == "100"
            assert request.url.params["offset"] == "3"
            return httpx.Response(
                200,
                json={
                    "task_instances": [{"task_id": "task", "state": "success", "map_index": -1, "try_number": 1}],
                    "total_entries": 1,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport))
    result = await service.environment_airflow_run_progress(
        dag_id="allowed", dag_run_id="run-1", task_limit=500, task_offset=3
    )
    assert result["available"] is True
    assert result["pagination"]["limit"] == 100
    assert result["citation"]["dag_id"] == "allowed"
    assert result["citation"]["dag_run_id"] == "run-1"
    with pytest.raises(ValueError, match="DAG is not permitted"):
        await service.environment_airflow_run_progress(dag_id="blocked", dag_run_id="run-1")
    with pytest.raises(ValueError, match="task_offset"):
        await service.environment_airflow_run_progress(dag_id="allowed", dag_run_id="run-1", task_offset=-1)


@pytest.mark.asyncio
async def test_run_progress_verifies_access_and_escapes_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "token")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "/dags/dag%2Fid" in str(request.url) and request.url.path.endswith("dag/id"):
            return httpx.Response(200, json={"dag_id": "dag/id"})
        if request.url.path.endswith("/taskInstances"):
            return httpx.Response(200, json={"task_instances": [], "total_entries": 0})
        if "/dagRuns/run%2Fid" in str(request.url):
            return httpx.Response(200, json={"dag_run_id": "run/id", "dag_id": "dag/id", "state": "success"})
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport))
    result = await service.environment_airflow_run_progress(dag_id="dag/id", dag_run_id="run/id")
    assert result["available"] is True
    assert any("/dags/dag%2Fid" in url for url in urls)
    assert any("/dagRuns/run%2Fid" in url for url in urls)


@pytest.mark.asyncio
async def test_task_log_chunk_bounds_and_malformed_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "token")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "dag")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/dags/dag":
            return httpx.Response(200, json={"dag_id": "dag"})
        if request.url.path.endswith("/logs/1"):
            assert request.url.params["full_content"] == "false"
            assert request.url.params["map_index"] == "-1"
            return httpx.Response(200, json={"content": ["abcdef"], "continuation_token": "next"})
        if request.url.path.endswith("/logs/2"):
            return httpx.Response(200, json={"content": "bad", "continuation_token": None})
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport))
    result = await service.environment_airflow_task_log_chunk(
        dag_id="dag", dag_run_id="run", task_id="task", try_number=1, max_chars=3
    )
    assert result["content"] == "abc"
    assert result["truncated"] is True
    assert result["continuation_token"] == "next"
    assert "untrusted" in result["warning"]
    with pytest.raises(ValueError, match="log content"):
        await service.environment_airflow_task_log_chunk(dag_id="dag", dag_run_id="run", task_id="task", try_number=2)


@pytest.mark.asyncio
async def test_xcom_result_bounds_and_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "token")
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "dag")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/dags/dag":
            return httpx.Response(200, json={"dag_id": "dag"})
        if request.url.path.endswith("/xcomEntries/return_value"):
            assert request.url.params["deserialize"] == "false"
            assert request.url.params["stringify"] == "true"
            return httpx.Response(200, json={"key": "return_value", "value": "abcdef", "timestamp": "2026-01-01T00:00:00Z"})
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: client)
    result = await service.environment_airflow_xcom_result(
        dag_id="dag", dag_run_id="run", task_id="task", max_chars=4
    )
    assert result["value"] == "abcd"
    assert result["truncated"] is True
    assert result["citation"]["map_index"] == -1
    assert result["citation"]["retrieved_at_utc"].endswith("Z")
    assert "untrusted" in result["warning"]


def test_endpoint_sanitizes_environment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analytics_connections, "environment_airflow_status", AsyncMock(side_effect=ValueError("secret"))
    )
    app = FastAPI()
    app.include_router(analytics_connections.router)
    app.dependency_overrides[require_auth] = lambda: User(identity="test", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        response = client.get("/analytics/connections/environment")
    assert response.status_code == 502
    assert "secret" not in response.text
