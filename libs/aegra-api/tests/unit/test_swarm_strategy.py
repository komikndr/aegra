"""Strategic search keeps scoped model opinions separate from deterministic evidence."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from swarm_strategy import graph as strategy_graph

from aegra_api.models.swarm_strategy import StrategyDecision, StrategySeed
from aegra_api.services.swarm_strategy import scoped_assessment, verify_investigations


def snapshot() -> dict[str, Any]:
    return {"snapshot_at": "2026-10-28T12:00:00+00:00", "evidence": [{
        "evidence_id": "stock", "source": "fixture", "simulation": True,
        "observed_at": "2026-10-27T00:00:00Z", "available_at": "2026-10-27T00:00:00Z",
        "content": json.dumps({"inventory_units": "700000", "daily_consumption_units": "70000", "horizon_days": "7"}),
    }]}


def assessment() -> dict[str, Any]:
    return {"summary": "10 days coverage.", "findings": [{"text": "Stock supports the horizon.", "evidence_ids": ["stock"]}],
            "uncertainties": ["Consumption can change."], "recommendation": "Monitor consumption.", "disposition": "wait"}


def test_strategy_contracts_reject_duplicate_and_unsupported_actions() -> None:
    candidate = {"branch_id": "branch_1", "approach": "Conserve stock", "falsification": "Consumption exceeds supply"}
    with pytest.raises(ValueError):
        StrategySeed.model_validate({"candidates": [candidate, candidate]})
    with pytest.raises(ValueError):
        StrategyDecision.model_validate({"assessment": assessment(), "actions": [{"branch_id": "branch_1", "action": "buy", "reason": "model says so"}]})
    with pytest.raises(ValueError):
        StrategyDecision.model_validate({"assessment": assessment(), "actions": [{"branch_id": "branch_1", "action": "revise", "reason": "try again", "next_question": " "}]})


def test_verification_is_calculated_not_a_model_validation_label() -> None:
    value = {"diagnosis": "Check stock math.", "investigations": [{"kind": "stock_calculation", "evidence_id": "stock", "question": "Coverage?"}]}
    receipt = verify_investigations(value, snapshot())[0]
    assert receipt["producer"] == "onyx:deterministic-verifier"
    assert receipt["status"] == "checked"
    assert receipt["result"]["coverage_days"] == "10"
    assert receipt["result"]["remaining_units"] == "210000"
    value["investigations"][0]["evidence_id"] = "foreign"
    with pytest.raises(ValueError):
        verify_investigations(value, snapshot())


def test_prose_is_not_silently_extracted_into_a_successful_stock_test() -> None:
    data = snapshot()
    data["evidence"][0]["content"] = "The stock is probably sufficient. CLASSIFICATION: VALIDATED"
    receipt = verify_investigations({"diagnosis": "Unknown stock", "investigations": [
        {"kind": "stock_calculation", "evidence_id": "stock", "question": "Check stock"}]}, data)[0]
    assert receipt["status"] == "unavailable"


def test_strategy_citations_and_expiry_are_server_validated() -> None:
    value = assessment()
    value["findings"][0]["evidence_ids"] = ["foreign"]
    with pytest.raises(ValueError):
        scoped_assessment(value, snapshot())
    data = snapshot()
    data["evidence"][0]["content"] = '{"expires_at":"2020-01-01T00:00:00Z"}'
    with pytest.raises(ValueError):
        scoped_assessment(assessment(), data)


@pytest.mark.asyncio
async def test_strategy_verification_never_loads_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = MagicMock(side_effect=AssertionError("Verification must not load a model"))
    monkeypatch.setattr(strategy_graph, "load_chat_model", loader)
    value = await strategy_graph.execute(strategy_graph.StrategyState(role="verify", snapshot=snapshot(), critique={
        "diagnosis": "Check numeric assumptions", "investigations": [{"kind": "stock_calculation", "evidence_id": "stock", "question": "Coverage?"}]}))
    assert value["verification"]["receipts"][0]["status"] == "checked"
    loader.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_worker_is_bounded_and_uses_only_assigned_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    model = MagicMock()
    model.model_copy.return_value = model
    call = AsyncMock(return_value=assessment())
    model.with_structured_output.return_value.ainvoke = call
    monkeypatch.setattr(strategy_graph, "resolve_agent_model", lambda name: SimpleNamespace(model="fixture", base_url=None, api_key=None))
    monkeypatch.setattr(strategy_graph, "load_chat_model", lambda *args, **kwargs: model)
    output = await strategy_graph.execute(strategy_graph.StrategyState(role="develop", snapshot=snapshot(), candidate={"branch_id": "branch_1"}))
    assert output["assessment"] == assessment()
    model.model_copy.assert_called_once_with(update={"max_tokens": 4096, "max_retries": 0})
    model.bind_tools.assert_not_called()
    payload = json.loads(call.call_args.args[0][1].content)
    assert payload["reports"] == [] and payload["candidate"] == {"branch_id": "branch_1"}
    with pytest.raises(ValueError):
        await strategy_graph.execute(strategy_graph.StrategyState(role="develop", snapshot={"oversized": "x" * 160001}))
