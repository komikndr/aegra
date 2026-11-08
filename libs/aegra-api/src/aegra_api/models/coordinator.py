"""Objective-scoped human conversation and bounded workflow requests."""

from typing import Literal

from pydantic import Field

from aegra_api.models.swarm import Contract


class CoordinatorMessage(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class CoordinatorAnswer(Contract):
    text: str = Field(min_length=1, max_length=6000)
    action: Literal["none", "review", "strategy_review", "feedback", "pause", "resume", "paper_start", "paper_stop"]
    feedback_kind: Literal["disagreement", "additional_information", "outcome"]
    account_id: str = Field(max_length=128)
    cycles: int = Field(ge=1, le=24)
    interval_seconds: int = Field(ge=60, le=86400)
    allow_market_acquisition: bool = False
