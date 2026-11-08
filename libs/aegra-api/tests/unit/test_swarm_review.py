"""Scope and model-boundary checks for the bounded review graph."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from swarm_review import graph as module

from aegra_api.models.swarm_coordination import Assessment, ObjectiveCreate
from aegra_api.services.swarm_coordination import validate_assessment


def test_assessment_rejects_fabricated_evidence_reference() -> None:
    value = {"summary": "Summary", "findings": [{"text": "Claim", "evidence_ids": ["invented"]}],
             "recommendation": "Wait", "disposition": "wait"}
    with pytest.raises(ValueError, match="outside"):
        validate_assessment(value, {"evidence": [{"evidence_id": "real"}]})


def test_objective_rejects_duplicate_evidence_ids_and_arbitrary_execution() -> None:
    now = datetime.now(UTC)
    evidence = {"evidence_id": "same", "source": "fixture", "observed_at": now, "available_at": now, "content": "input"}
    with pytest.raises(ValidationError, match="unique"):
        ObjectiveCreate(request_key="k", title="Title", goal="Goal", success_criteria="Criteria", evidence=(evidence, evidence))
    with pytest.raises(ValidationError, match="Extra inputs"):
        ObjectiveCreate.model_validate({"request_key": "k", "title": "Title", "goal": "Goal", "success_criteria": "Criteria", "graph_id": "arbitrary"})


@pytest.mark.asyncio
async def test_graph_uses_real_model_interface_without_d3_or_execution_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    result = Assessment(summary="Insufficient observations", recommendation="Request source data", disposition="needs_information")
    structured = AsyncMock()
    structured.ainvoke.return_value = result
    model = AsyncMock()
    model.model_copy = lambda **_kwargs: model
    model.with_structured_output = lambda *_args, **_kwargs: structured
    monkeypatch.setattr(module, "load_chat_model", lambda *_args, **_kwargs: model)
    output = await module.assess(module.ReviewState(role="skeptical_reviewer", snapshot={"evidence": []}))
    assert output["assessment"]["disposition"] == "needs_information"
    structured.ainvoke.assert_awaited_once()
    assert "no external tools" in structured.ainvoke.await_args.args[0][0].content


@pytest.mark.asyncio
async def test_graph_rejects_unapproved_roles_before_model_access(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = AsyncMock()
    monkeypatch.setattr(module, "load_chat_model", factory)
    with pytest.raises(ValueError, match="Unsupported"):
        await module.assess(module.ReviewState(role="trade_executor", snapshot={"evidence": []}))
    factory.assert_not_called()
