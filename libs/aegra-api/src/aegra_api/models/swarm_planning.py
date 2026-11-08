"""Approved, bounded task plans; no executable code or caller-defined tools."""

from typing import Literal

from pydantic import Field, model_validator

from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_coordination import Assessment

Capability = Literal["evidence_analysis", "market_analysis", "stock_calculation", "skeptical_review"]


class PlannedTask(Contract):
    task_id: Literal["task_1", "task_2"]
    capability: Capability
    question: str = Field(min_length=1, max_length=1000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    depends_on: tuple[Literal["task_1", "task_2"], ...] = Field(default=(), max_length=1)


class ReviewPlan(Contract):
    decision: Literal["tasks", "answer", "needs_information", "wait", "stop"]
    rationale: str = Field(min_length=1, max_length=2000)
    tasks: tuple[PlannedTask, ...] = Field(default=(), max_length=2)
    assessment: Assessment | None = None
    acquire_market: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def consistent_plan(self) -> "ReviewPlan":
        if self.decision != "tasks":
            if self.tasks or self.assessment is None or self.acquire_market is not None:
                raise ValueError("A non-task decision requires an assessment and no tasks.")
            if self.decision == "needs_information" and self.assessment.disposition != "needs_information":
                raise ValueError("Missing information must suspend scheduled review.")
            if self.decision in {"wait", "stop"} and self.assessment.disposition != "wait":
                raise ValueError("Wait and stop decisions cannot also request missing inputs.")
            return self
        if not self.tasks or self.assessment is not None:
            raise ValueError("A task plan requires tasks and no final assessment.")
        known: set[str] = set()
        capabilities: set[str] = set()
        for task in self.tasks:
            if task.task_id in known or not set(task.depends_on) <= known or task.capability in capabilities:
                raise ValueError("Task IDs and capabilities must be unique; dependencies must precede each task.")
            known.add(task.task_id)
            capabilities.add(task.capability)
        return self


class TaskPlan(Contract):
    """Provider schema makes task/answer exclusivity visible before generation."""

    decision: Literal["tasks"]
    rationale: str = Field(min_length=1, max_length=2000)
    tasks: tuple[PlannedTask, ...] = Field(min_length=1, max_length=2)
    assessment: None = None
    acquire_market: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")


class InformationAssessment(Assessment):
    disposition: Literal["needs_information"]


class WaitingAssessment(Assessment):
    disposition: Literal["wait"]


class InformationPlan(Contract):
    decision: Literal["needs_information"]
    rationale: str = Field(min_length=1, max_length=2000)
    tasks: tuple[PlannedTask, ...] = Field(default=(), max_length=0)
    assessment: InformationAssessment
    acquire_market: None = None


class AnswerPlan(Contract):
    decision: Literal["answer", "wait", "stop"]
    rationale: str = Field(min_length=1, max_length=2000)
    tasks: tuple[PlannedTask, ...] = Field(default=(), max_length=0)
    assessment: WaitingAssessment
    acquire_market: None = None


class PlannerOutput(Contract):
    """Object-root union supported by structured-output providers; API plan stays unchanged."""

    plan: TaskPlan | InformationPlan | AnswerPlan
