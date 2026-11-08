"""Conversation is bounded model reasoning; actions execute in trusted reconciliation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from coordinator import graph as coordinator_graph


@pytest.mark.asyncio
async def test_coordinator_proposes_without_executing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {"text": "The recorded stock is uncertain.", "action": "none", "feedback_kind": "additional_information",
              "account_id": "", "cycles": 1, "interval_seconds": 300}
    model = MagicMock()
    model.model_copy.return_value = model
    inference = AsyncMock(return_value=answer)
    model.with_structured_output.return_value.ainvoke = inference
    monkeypatch.setattr(coordinator_graph, "resolve_agent_model", lambda name: SimpleNamespace(model="fixture-model", base_url=None, api_key=None))
    monkeypatch.setattr(coordinator_graph, "load_chat_model", lambda *args, **kwargs: model)
    message = "Start a new review now."
    snapshot = {"objective": {"goal": "Preserve capital"}, "conversation": [{"message": "Answer only. Do not start a review."}]}
    value = await coordinator_graph.respond(coordinator_graph.CoordinatorState(message=message, snapshot=snapshot))
    assert value["answer"] == {**answer, "allow_market_acquisition": False}
    assert value["model"] == "fixture-model"
    model.bind_tools.assert_not_called()
    model.model_copy.assert_called_once_with(update={"max_tokens": 4096, "max_retries": 0})
    assert "objective sub-coordinator" in inference.call_args.args[0][0].content
    assert "you are already the target" in inference.call_args.args[0][0].content
    messages = inference.call_args.args[0]
    assert len(messages) == 3
    assert "Answer only" in messages[1].content
    assert messages[-1].content == message


@pytest.mark.asyncio
async def test_coordinator_rejects_oversized_context_before_model(monkeypatch: pytest.MonkeyPatch) -> None:
    load = MagicMock()
    monkeypatch.setattr(coordinator_graph, "load_chat_model", load)
    with pytest.raises(ValueError, match="budget"):
        await coordinator_graph.respond(coordinator_graph.CoordinatorState(message="Explain", snapshot={"data": "x" * 160001}))
    load.assert_not_called()
