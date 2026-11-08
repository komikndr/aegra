"""Approved sources, immutable receipts, cache identity and failure boundaries."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from swarm_planner import graph as planner

from aegra_api.models.paper_trading import PaperQuoteInput
from aegra_api.services import swarm_acquisition as acquisition
from aegra_api.services.swarm_capabilities import validate_plan

SCOPE = {"region_id": 10000002, "location_id": 60003760, "type_id": 34, "fee_bps": 100, "slippage_bps": 10}


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setenv("SWARM_EVE_MARKETS", json.dumps({"jita": SCOPE}))
    quote = AsyncMock(return_value=(PaperQuoteInput(type_id=34, bid="3.8", ask="3.93", bid_quantity=10000, ask_quantity=10000,
                                                  observed_at=datetime.now(UTC)), "cached-version", datetime.now(UTC) + timedelta(minutes=4)))
    history = AsyncMock(return_value=[{"date": "2026-01-01", "average": 4, "highest": 5, "lowest": 3,
                                      "volume": 100000, "order_count": 100}])
    monkeypatch.setattr(acquisition, "fetch_eve_quote", quote)
    monkeypatch.setattr(acquisition, "fetch_eve_history", history)
    return quote, history


@pytest.mark.asyncio
async def test_receipt_identity_does_not_refresh_cached_data(sources: tuple[AsyncMock, AsyncMock]) -> None:
    first = await acquisition.acquire_market("jita", SCOPE)
    second = await acquisition.acquire_market("jita", SCOPE)
    assert first["receipt_id"] == second["receipt_id"]
    assert first["retrieved_at"] != second["retrieved_at"]
    record = json.loads(first["evidence"][0]["content"])
    assert record["upstream_observation_time"] is None
    assert "cached" in record["cache_note"]
    assert record["scope"] == SCOPE and record["kind"] == "current_top_of_book"
    assert record["cost_check"]["net_per_unit_isk"] == "-0.2150313"
    sources[0].assert_awaited_with(region_id=10000002, location_id=60003760, type_id=34)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["unknown", "config_changed"])
async def test_unapproved_scope_rejected_before_network(sources: tuple[AsyncMock, AsyncMock], changed: str) -> None:
    with pytest.raises(ValueError, match="approved"):
        await acquisition.acquire_market("unknown" if changed == "unknown" else "jita", {**SCOPE, "type_id": 99})
    sources[0].assert_not_awaited()


@pytest.mark.parametrize("config", ["[]", '{"jita":{"url":"http://internal/"}}', '{"../host":{}}', "malformed"])
def test_config_has_no_endpoint_escape(monkeypatch: pytest.MonkeyPatch, config: str) -> None:
    monkeypatch.setenv("SWARM_EVE_MARKETS", config)
    with pytest.raises(ValueError, match="Invalid server EVE"):
        acquisition.approved_markets()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["upstream", "expiry", "tamper"])
async def test_failed_or_invalid_sources_never_become_evidence(sources: tuple[AsyncMock, AsyncMock], failure: str) -> None:
    if failure == "upstream":
        sources[1].side_effect = httpx.ConnectError("sensitive upstream details")
        with pytest.raises(ValueError, match="collection failed") as error:
            await acquisition.acquire_market("jita", SCOPE)
        assert "sensitive" not in str(error.value)
        return
    receipt = await acquisition.acquire_market("jita", SCOPE)
    if failure == "expiry":
        receipt["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    else:
        value = json.loads(receipt["evidence"][0]["content"])
        value["quote"]["bid"] = "1"
        receipt["evidence"][0]["content"] = json.dumps(value)
    with pytest.raises(ValueError):
        acquisition.validate_receipt(receipt, "jita", {"acquisition_scopes": {"jita": SCOPE}})


@pytest.mark.asyncio
async def test_connector_worker_has_no_model_call(sources: tuple[AsyncMock, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    model = MagicMock(side_effect=AssertionError("Acquisition must not load an LLM"))
    monkeypatch.setattr(planner, "load_chat_model", model)
    output = await planner.execute(planner.PlanningState(role="market_acquisition", market_scope="jita",
                                                        snapshot={"evidence": [], "acquisition_scopes": {"jita": SCOPE}}))
    assert output["acquisition"]["scope"] == SCOPE
    model.assert_not_called()


def market_plan() -> dict[str, Any]:
    return {"decision": "tasks", "rationale": "Need current market evidence.", "acquire_market": "jita", "tasks": [
        {"task_id": "task_1", "capability": "market_analysis", "question": "Assess the supplied market.",
         "evidence_ids": ["eve:jita:quote", "eve:jita:history"], "depends_on": []}]}


def test_prospective_evidence_requires_explicit_scope() -> None:
    with pytest.raises(ValueError, match="approved"):
        validate_plan(market_plan(), {"evidence": []})
    plan = validate_plan(market_plan(), {"evidence": [], "acquisition_scopes": {"jita": SCOPE}})
    assert plan.acquire_market == "jita"
