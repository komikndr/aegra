"""Exercise the real adapter without an available GPU or model endpoint."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aegra_api.api.swarm import router
from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User
from aegra_api.models.swarm import RoutingDestination, Signal
from aegra_api.services.swarm_scenarios import routing_scenarios
from aegra_api.services.systemone import SystemOneSettings, route_signal


def settings() -> SystemOneSettings:
    return SystemOneSettings.model_validate(
        {"base_url": "http://vllm.example/v1", "model": "served-d3", "api_key": "private-token"}
    )


def signal() -> Signal:
    return routing_scenarios()[1].signal


def choice_response(*, model: str = "served-d3", destination: str = "strategic_review") -> dict[str, Any]:
    return {
        "model": model,
        "answers": {
            "route": {
                "type": "choice",
                "choice": destination,
                "probabilities": {"local_assessment": 0.2, "strategic_review": 0.7, "record_only": 0.1},
                "confidence": 0.5,
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 0},
    }


@pytest.mark.asyncio
async def test_unconfigured_does_not_contact_network() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        pytest.fail("An unconfigured adapter must not contact the network.")

    result = await route_signal(signal(), settings=SystemOneSettings(), transport=httpx.MockTransport(fail))
    assert result.status == "unconfigured"
    assert result.mode == "live"
    assert result.decision is None


@pytest.mark.asyncio
async def test_native_systemone_request_and_validated_routing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://vllm.example/v1/systemone"
        assert request.headers["authorization"] == "Bearer private-token"
        payload = json.loads(request.content)
        assert payload["model"] == "served-d3"
        assert set(payload) == {"model", "state", "questions"}
        assert payload["questions"]["route"]["type"] == "choice"
        assert set(payload["questions"]["route"]["criteria"]) == set(RoutingDestination)
        assert payload["state"]["signal_id"] == signal().signal_id
        return httpx.Response(200, json=choice_response())

    result = await route_signal(signal(), settings=settings(), transport=httpx.MockTransport(handle))
    assert result.status == "routed"
    assert result.mode == "live"
    assert result.decision is not None
    assert result.decision.destination == RoutingDestination.STRATEGIC_REVIEW
    assert result.decision.signal_id == signal().signal_id
    assert result.decision.confidence == 0.5
    assert result.decision.probabilities == {"local_assessment": 0.2, "strategic_review": 0.7, "record_only": 0.1}
    assert "private-token" not in result.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        choice_response(model="wrong"),
        choice_response(destination="execute_shell"),
        choice_response(destination="record_only"),
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_response_is_not_a_default_route(body: dict[str, object]) -> None:
    result = await route_signal(
        signal(), settings=settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    assert result.status == "invalid_response"
    assert result.decision is None
    assert result.mode == "live"


@pytest.mark.parametrize(
    "answer_update",
    [
        {"type": "score"},
        {"confidence": None},
        {"confidence": 1.1},
        {"probabilities": {"strategic_review": 1}},
        {"probabilities": {"local_assessment": 0.1, "strategic_review": 0.1, "record_only": 0.1}},
        {"probabilities": {"local_assessment": -0.1, "strategic_review": 1, "record_only": 0.1}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_native_choice_scores_are_rejected(answer_update: dict[str, object]) -> None:
    body = choice_response()
    body["answers"]["route"].update(answer_update)
    result = await route_signal(
        signal(), settings=settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    assert result.status == "invalid_response"
    assert result.decision is None


@pytest.mark.parametrize("status", [401, 429, 503, 307])
@pytest.mark.asyncio
async def test_upstream_failures_are_sanitized_without_fixture_fallback(status: int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private upstream response", headers={"Location": "http://other.example"})

    result = await route_signal(signal(), settings=settings(), transport=httpx.MockTransport(handle))
    assert calls == 1
    assert result.status == "unavailable"
    assert result.decision is None
    assert "private" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_gpu_connection_failure_is_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint is down", request=request)

    result = await route_signal(signal(), settings=settings(), transport=httpx.MockTransport(handle))
    assert result.status == "unavailable"
    assert result.decision is None


@pytest.mark.asyncio
async def test_total_deadline_bounds_response_wait() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=choice_response())

    config = settings().model_copy(update={"timeout_seconds": 0.01})
    result = await route_signal(signal(), settings=config, transport=httpx.MockTransport(handle))
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_stream_limit_closes_before_reading_entire_body() -> None:
    class LargeBody(httpx.AsyncByteStream):
        reads: int = 0
        closed: bool = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                self.reads += 1
                yield b"x" * 16_384

        async def aclose(self) -> None:
            self.closed = True

    body = LargeBody()
    result = await route_signal(
        signal(), settings=settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    )
    assert result.status == "invalid_response"
    assert body.reads == 17
    assert body.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@model.example/v1",
        "http://model.example/v1?token=secret",
        "http://model.example/v1#secret",
        "http://model.example/other",
    ],
)
def test_invalid_configuration_never_echoes_credentials(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SYSTEMONE_BASE_URL", url)
    with pytest.raises(ValueError, match="^Invalid server SystemOne configuration.$") as exc:
        SystemOneSettings.from_environment()
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("roles", [[], ["Staff"], ["Executive"]])
def test_routes_require_admin_before_upstream(monkeypatch: pytest.MonkeyPatch, roles: list[str]) -> None:
    upstream = AsyncMock()
    monkeypatch.setattr("aegra_api.api.swarm.route_signal", upstream)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=roles)
    with TestClient(app) as client:
        assert client.get("/swarm/v1/capabilities").status_code == 403
        assert client.get("/swarm/v1/simulations/routing").status_code == 403
        assert client.post("/swarm/v1/routing/assess", json=signal().model_dump(mode="json")).status_code == 403
    upstream.assert_not_awaited()


def test_fixture_catalog_is_explicit_and_never_contacts_model(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = AsyncMock()
    monkeypatch.setattr("aegra_api.api.swarm.route_signal", upstream)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        response = client.get("/swarm/v1/simulations/routing")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 4
        assert all(row["simulation"] for row in rows)
        assert rows[1]["operator_reply"]["kind"] == "disagreement"
        assert rows[2]["duty_authority_reply"]
        assert rows[3]["scenario_id"] == "eve-regional-supply"
        assert rows[3]["signal"]["source"] == "fixture:eve-market"
    upstream.assert_not_awaited()


def test_signal_contract_rejects_request_supplied_endpoint() -> None:
    with pytest.raises(ValidationError):
        Signal.model_validate({**signal().model_dump(), "base_url": "http://attacker.example/v1"})
    with pytest.raises(ValidationError):
        Signal.model_validate({**signal().model_dump(), "evidence_refs": ["x" * 513]})


def test_admin_api_reports_unconfigured_without_synthetic_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYSTEMONE_BASE_URL", raising=False)
    monkeypatch.delenv("SYSTEMONE_MODEL", raising=False)
    monkeypatch.delenv("SYSTEMONE_TIMEOUT_SECONDS", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        capabilities = client.get("/swarm/v1/capabilities").json()
        assert capabilities["systemone"]["configured"] is False
        assert capabilities["systemone"]["model"] is None
        assert capabilities["systemone"]["transport"] == "typesafe_systemone"
        assert capabilities["durable_coordination"] is True
        assert capabilities["external_execution"] is False
        assert capabilities["scenario_runner"] is False
        response = client.post("/swarm/v1/routing/assess", json=signal().model_dump(mode="json"))
        assert response.status_code == 200
        assert response.json()["status"] == "unconfigured"
        assert response.json()["mode"] == "live"
        assert response.json()["decision"] is None


def test_invalid_server_config_returns_sanitized_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMONE_BASE_URL", "http://user:private-secret@vllm.example/v1")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        for response in (
            client.get("/swarm/v1/capabilities"),
            client.post("/swarm/v1/routing/assess", json=signal().model_dump(mode="json")),
        ):
            assert response.status_code == 503
            assert "private-secret" not in response.text


def test_configured_capabilities_report_served_model_without_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMONE_BASE_URL", "http://desktop-14p3oeb:8001/v1")
    monkeypatch.setenv("SYSTEMONE_MODEL", "d3-flash")
    monkeypatch.delenv("SYSTEMONE_TIMEOUT_SECONDS", raising=False)
    inference = AsyncMock(side_effect=AssertionError("Discovery must not invoke inference"))
    monkeypatch.setattr("aegra_api.api.swarm.route_signal", inference)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["SuperAdmin"])
    with TestClient(app) as client:
        response = client.get("/swarm/v1/capabilities")
    assert response.status_code == 200
    assert response.json()["systemone"] == {
        "role": "signal_router",
        "model": "d3-flash",
        "transport": "typesafe_systemone",
        "configured": True,
    }
    inference.assert_not_called()


def test_eve_fixture_references_match_catalog_and_preserve_operator_correction() -> None:
    path = Path(__file__).resolve().parents[4] / "examples/swarm/eve-regional-supply.json"
    dataset = json.loads(path.read_text())
    scenario = next(row for row in routing_scenarios() if row.scenario_id == dataset["scenario_id"])
    assert dataset["simulation"] is True
    assert set(scenario.signal.evidence_refs) == {dataset["market_history"]["reference"], dataset["news"]["reference"]}
    assert dataset["operator_feedback"]["supersedes"] == dataset["prior_board_finding"]["id"]
    assert dataset["operator_feedback"]["observed_at"] > dataset["prior_board_finding"]["observed_at"]
    assert scenario.operator_reply is not None
