"""Airflow tool availability follows authenticated ownership and enablement."""

from unittest.mock import AsyncMock

import pytest

from analytic_agent import airflow_tools


@pytest.mark.parametrize("roles", [[], ["Staff"], ["Executive"], ["Senior Data Engineer"]])
def test_non_operators_expose_no_tools(monkeypatch: pytest.MonkeyPatch, roles: list[str]) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setattr(airflow_tools, "get_config", lambda: {"configurable": {"langgraph_auth_user": {"identity": "alice", "permissions": roles}}})
    assert airflow_tools.get_environment_airflow_tools() == []


def test_environment_tool_requires_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setattr(airflow_tools, "get_config", lambda: {"configurable": {"user_id": "untrusted"}})
    assert airflow_tools.get_environment_airflow_tools() == []


def test_environment_tool_requires_authenticated_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setattr(
        airflow_tools, "get_config", lambda: {"configurable": {"langgraph_auth_user": {"identity": "alice"}}}
    )
    assert airflow_tools.get_environment_airflow_tools() == []


@pytest.mark.asyncio
async def test_environment_tool_needs_no_saved_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setattr(
        airflow_tools,
        "get_config",
        lambda: {"configurable": {"langgraph_auth_user": {"identity": "alice", "permissions": ["SuperAdmin"]}}},
    )
    status = AsyncMock(return_value={"configured": True, "rows": []})
    monkeypatch.setattr(airflow_tools, "environment_airflow_status", status)
    tools = airflow_tools.get_environment_airflow_tools()
    assert len(tools) == 4
    assert await tools[0]() == {"configured": True, "rows": []}
    status.assert_awaited_once_with()
    monkeypatch.delenv("AIRFLOW_API_URL")
    assert airflow_tools.get_environment_airflow_tools() == []


@pytest.mark.asyncio
async def test_environment_tools_sanitize_upstream_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "http://airflow.example/api/v2")
    monkeypatch.setattr(
        airflow_tools,
        "get_config",
        lambda: {"configurable": {"langgraph_auth_user": {"identity": "alice", "permissions": ["SuperAdmin"]}}},
    )
    monkeypatch.setattr(airflow_tools, "environment_airflow_run_progress", AsyncMock(side_effect=ValueError("secret")))
    tools = airflow_tools.get_environment_airflow_tools()
    result = await tools[1]("dag", "run")
    assert result == {"available": False, "message": "Airflow run progress could not be retrieved."}
