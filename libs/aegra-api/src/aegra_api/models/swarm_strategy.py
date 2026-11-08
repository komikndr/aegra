"""Bounded strategic search: model proposals remain distinct from verified receipts."""

from typing import Literal

from pydantic import Field, model_validator

from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_coordination import Assessment


class StrategyCandidate(Contract):
    branch_id: Literal["branch_1", "branch_2"]
    approach: str = Field(min_length=1, max_length=2000)
    falsification: str = Field(min_length=1, max_length=1000)


class StrategySeed(Contract):
    candidates: tuple[StrategyCandidate, ...] = Field(min_length=1, max_length=2)
    acquire_market: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def distinct_ids(self) -> "StrategySeed":
        if len({candidate.branch_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("Strategy IDs must be distinct.")
        return self


class VerificationRequest(Contract):
    kind: Literal["stock_calculation", "evidence_freshness"]
    evidence_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=1000)


class StrategyCritique(Contract):
    diagnosis: str = Field(min_length=1, max_length=3000)
    investigations: tuple[VerificationRequest, ...] = Field(default=(), max_length=2)


class BranchAction(Contract):
    branch_id: Literal["branch_1", "branch_2"]
    action: Literal["continue", "revise", "prune", "wait", "conclude"]
    reason: str = Field(min_length=1, max_length=2000)
    next_question: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def concrete_continuation(self) -> "BranchAction":
        if self.action in {"continue", "revise"} and not self.next_question.strip():
            raise ValueError("Continuing or revising a branch requires a concrete next question.")
        return self


class StrategyDecision(Contract):
    actions: tuple[BranchAction, ...] = Field(min_length=1, max_length=2)
    assessment: Assessment

    @model_validator(mode="after")
    def distinct_ids(self) -> "StrategyDecision":
        if len({action.branch_id for action in self.actions}) != len(self.actions):
            raise ValueError("Branch actions must be unique.")
        return self
