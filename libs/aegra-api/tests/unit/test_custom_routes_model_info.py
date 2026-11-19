import json

import custom_routes_example as routes
import pytest
from aegra_api.models.auth import User

_TEST_USER = User(identity="tester")


def _prepare_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path, config: dict) -> None:
    """Point the agent config at an isolated file and keep model discovery local."""
    config_path = tmp_path / "agent-models.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(config_path))
    monkeypatch.setenv("MODEL", "openai/test-model")
    monkeypatch.setattr(routes, "get_active_model_name", lambda **_kwargs: None)


@pytest.mark.asyncio
async def test_model_info_reports_runtime_context_window_as_effective_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Without JSON limits the endpoint must mirror the graphs' Context values."""
    _prepare_endpoint(monkeypatch, tmp_path, {})
    monkeypatch.setenv("NUM_LIMIT_TOKEN", "65536")
    monkeypatch.setenv("NUM_LIMIT_RESPONSE_RESERVE", "4096")
    monkeypatch.setenv("NUM_LIMIT_SAFETY_BUFFER", "1024")
    monkeypatch.setenv("NUM_LIMIT_MIN_RECENT_MESSAGES", "6")

    response = await routes.model_info(agent_id="casual_chat", user=_TEST_USER)

    assert response["context_window"] == 65536
    assert response["response_reserve"] == 4096
    assert response["safety_buffer"] == 1024
    assert response["min_recent_messages"] == 6


@pytest.mark.asyncio
async def test_model_info_keeps_dataclass_default_without_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No env override: the shared Context default window is still reported."""
    _prepare_endpoint(monkeypatch, tmp_path, {})
    monkeypatch.delenv("NUM_LIMIT_TOKEN", raising=False)
    monkeypatch.delenv("NUM_LIMIT_RESPONSE_RESERVE", raising=False)
    monkeypatch.delenv("NUM_LIMIT_SAFETY_BUFFER", raising=False)
    monkeypatch.delenv("NUM_LIMIT_MIN_RECENT_MESSAGES", raising=False)

    response = await routes.model_info(agent_id="casual_chat", user=_TEST_USER)

    assert response["context_window"] == 65536
    assert response["response_reserve"] == 4096
    assert response["safety_buffer"] == 1024
    assert response["min_recent_messages"] == 6


@pytest.mark.asyncio
async def test_model_info_prefers_agent_config_over_runtime_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """JSON agent and default entries keep precedence over env-derived limits."""
    _prepare_endpoint(
        monkeypatch,
        tmp_path,
        {
            "default": {"context_window": 131072},
            "agents": {"casual_executive": {"context_window": 200000, "response_reserve": 2048}},
        },
    )
    monkeypatch.setenv("NUM_LIMIT_TOKEN", "65536")
    monkeypatch.setenv("NUM_LIMIT_RESPONSE_RESERVE", "4096")

    executive = await routes.model_info(agent_id="casual_executive", user=_TEST_USER)
    assert executive["context_window"] == 200000
    assert executive["response_reserve"] == 2048

    # Agents without their own entry fall back to the JSON default entry.
    other = await routes.model_info(agent_id="casual_chat", user=_TEST_USER)
    assert other["context_window"] == 131072
    assert other["response_reserve"] == 4096
