"""Initial signal-routing contracts, independent of graph execution and board storage."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoutingDestination(StrEnum):
    LOCAL_ASSESSMENT = "local_assessment"
    STRATEGIC_REVIEW = "strategic_review"
    RECORD_ONLY = "record_only"


class Signal(Contract):
    """An observation supplied to the router, not an executable instruction."""

    signal_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    summary: str = Field(min_length=1, max_length=8_000)
    evidence_refs: tuple[Annotated[str, StringConstraints(min_length=1, max_length=512)], ...] = Field(
        default=(), max_length=20
    )


class RoutingDecision(Contract):
    signal_id: str = Field(min_length=1, max_length=128)
    destination: RoutingDestination
    reason: str = Field(min_length=1, max_length=2_000)
    probabilities: dict[RoutingDestination, Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class RoutingResult(Contract):
    status: Literal["routed", "unconfigured", "unavailable", "invalid_response"]
    mode: Literal["live", "fixture"]
    signal_id: str
    assessed_at: datetime
    model: str | None = None
    decision: RoutingDecision | None = None
    message: str


class OperatorReply(Contract):
    """Scenario input; a report is not proof of execution or human authorization."""

    kind: Literal["completed", "disagreement", "additional_information"]
    text: str = Field(min_length=1, max_length=4_000)


class RoutingScenario(Contract):
    scenario_id: str
    simulation: Literal[True] = True
    description: str
    signal: Signal
    routing: RoutingDecision
    operator_reply: OperatorReply | None = None
    duty_authority_reply: str | None = None
