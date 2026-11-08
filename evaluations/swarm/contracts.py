"""Bounded evaluation inputs and captured outputs, independent of a model provider."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_coordination import Evidence
from aegra_api.models.swarm_planning import Capability
from aegra_api.services.swarm_capabilities import bounded_json


class NumericFact(Contract):
    label: str
    value: str
    tolerance: str = "0"

    @model_validator(mode="after")
    def finite_fact(self) -> "NumericFact":
        value, tolerance = Decimal(self.value), Decimal(self.tolerance)
        if not value.is_finite() or not tolerance.is_finite() or tolerance < 0:
            raise ValueError("Numeric expectations require finite values and a nonnegative tolerance.")
        return self


class EvaluationCase(Contract):
    case_id: str
    description: str
    snapshot: dict[str, Any]
    stage: Literal["review", "planner"] = "review"
    decisions: tuple[Literal["tasks", "answer", "needs_information", "wait", "stop"], ...]
    required_capabilities: tuple[Capability, ...] = ()
    acquire_market: str | None = None
    numeric_facts: tuple[NumericFact, ...] = ()
    require_citations: bool = False
    require_uncertainty: bool = False

    @model_validator(mode="after")
    def valid_snapshot(self) -> "EvaluationCase":
        bounded_json(self.snapshot)
        stamp = datetime.fromisoformat(self.snapshot["snapshot_at"])
        records = [Evidence.model_validate(item) for item in self.snapshot["evidence"]]
        if stamp.tzinfo is None or len({item.evidence_id for item in records}) != len(records):
            raise ValueError("Cases require an aware snapshot time and unique evidence IDs.")
        return self


class CapturedCase(Contract):
    case_id: str
    snapshot_sha256: str
    plan: dict[str, Any] | None = None
    assessment: dict[str, Any] | None = None
    steps: tuple[dict[str, Any], ...] = Field(default=(), max_length=5)
    error: str | None = None
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


class Capture(Contract):
    suite_version: str
    origin: Literal["live", "imported"]
    cases: tuple[CapturedCase, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def unique_cases(self) -> "Capture":
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("Captured case IDs must be unique.")
        return self
