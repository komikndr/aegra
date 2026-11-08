"""Finite, explicit budgets for the fixed evidence-review supervisor."""

from typing import Literal

from pydantic import Field

from aegra_api.models.swarm import Contract


class SupervisorStart(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    workflow: Literal["evidence_review_v1", "planned_review_v1", "strategy_search_v1"] = "evidence_review_v1"
    allow_market_acquisition: bool = False
    max_reviews: int = Field(default=2, ge=1, le=24)
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    max_elapsed_seconds: int = Field(default=3600, ge=300, le=604800)
    max_model_calls: int = Field(default=6, ge=3, le=72)
    max_specialist_tasks: int = Field(default=4, ge=2, le=48)


class SupervisorCommand(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="Explicit human request.", min_length=1, max_length=1000)
