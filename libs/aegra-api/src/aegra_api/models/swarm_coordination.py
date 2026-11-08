"""Bounded contracts for persistent objectives and evidence-review workflows."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aegra_api.models.swarm import Contract


class Evidence(Contract):
    evidence_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=512)
    observed_at: AwareDatetime
    available_at: AwareDatetime
    content: str = Field(min_length=1, max_length=8_000)
    simulation: bool = False


class ObjectiveCreate(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4_000)
    success_criteria: str = Field(min_length=1, max_length=2_000)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def unique_evidence(self) -> "ObjectiveCreate":
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique.")
        return self


class ReviewRequest(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    workflow: Literal["evidence_review_v1", "planned_review_v1", "strategy_search_v1"] = "evidence_review_v1"
    allow_market_acquisition: bool = False


class Feedback(ReviewRequest):
    kind: Literal["disagreement", "additional_information", "outcome"]
    content: str = Field(min_length=1, max_length=4_000)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=10)


class Finding(Contract):
    text: str = Field(min_length=1, max_length=2_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class Assessment(Contract):
    """A model assessment, never a receipt of execution or a private reasoning trace."""

    summary: str = Field(min_length=1, max_length=4_000)
    findings: tuple[Finding, ...] = Field(default=(), max_length=12)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=12)
    recommendation: str = Field(min_length=1, max_length=2_000)
    disposition: Literal["wait", "needs_information"]

    @model_validator(mode="after")
    def bound_uncertainties(self) -> "Assessment":
        if any(not item or len(item) > 1_000 for item in self.uncertainties):
            raise ValueError("Each uncertainty must contain 1–1000 characters.")
        return self


class ObjectiveView(Contract):
    objective_id: str
    title: str
    goal: str
    success_criteria: str
    status: str
    revision: int
    created_at: datetime
    latest_assessment: dict[str, Any] | None = None
    latest_review_status: str | None = None
    supervisor: dict[str, Any] | None = None


class EntryView(Contract):
    sequence: int
    review_id: str | None
    kind: str
    author: str
    content: dict[str, Any]
    created_at: datetime
