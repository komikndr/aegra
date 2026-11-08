"""Paired experiment budgets, receipts, revisions, and replay validation."""

import asyncio
from typing import Any

import pytest
from evaluations.swarm.comparison import (
    Budget,
    Experiment,
    capture_trial,
    comparison_cases,
    score_experiment,
    score_trial,
)
from evaluations.swarm.contracts import EvaluationCase
from evaluations.swarm.run import capture_case
from swarm_planner.graph import PlanningState
from swarm_strategy.graph import StrategyState

from aegra_api.services.swarm_strategy import verify_investigations


def assessment() -> dict[str, Any]:
    return {"summary": "Corrected stock covers 10 days; 210000 units remain after seven days.",
            "findings": [{"text": "700000 units divided by 70000/day covers 10 days.", "evidence_ids": ["stock"]}],
            "uncertainties": ["Consumption is an assumption, not a verified future observation."],
            "recommendation": "Retain the correction; no action was executed.", "disposition": "wait"}


def case() -> EvaluationCase:
    return next(item for item in comparison_cases() if item.case_id == "feedback_revision")


async def short_worker(state: PlanningState) -> dict[str, Any]:
    if state.role == "planner":
        return {"model": "fixture", "plan": {"decision": "answer", "rationale": "Use supplied data.", "tasks": [],
                                               "assessment": assessment(), "acquire_market": None}}
    raise AssertionError("Unexpected short-review stage.")


@pytest.mark.asyncio
async def test_short_runner_honors_comparison_deadline_and_cancels_worker() -> None:
    cancelled = asyncio.Event()

    async def slow_worker(state: PlanningState) -> dict[str, Any]:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return {}

    captured = await capture_case(case(), slow_worker, timeout_seconds=0.01)
    assert cancelled.is_set()
    assert captured.assessment is None
    assert captured.error and "TimeoutError" in captured.error


class StrategicWorker:
    def __init__(self, *, revise: bool = False, forge: bool = False) -> None:
        self.calls: list[StrategyState] = []
        self.revise, self.forge = revise, forge
        self.decisions = 0

    async def __call__(self, state: StrategyState) -> dict[str, Any]:
        self.calls.append(state)
        value: dict[str, Any] = {"model": "fixture"}
        if state.role == "seed":
            value["seed"] = {"candidates": [{"branch_id": "branch_1", "approach": "Correct stock coverage.",
                                             "falsification": "Calculation contradicts the coverage claim."}], "acquire_market": None}
        elif state.role == "develop":
            value["assessment"] = assessment()
        elif state.role == "critique":
            value["critique"] = {"diagnosis": "Calculate, rather than accepting the model's arithmetic.",
                                  "investigations": [{"kind": "stock_calculation", "evidence_id": "stock", "question": "Verify coverage."}]}
        elif state.role == "verify":
            value["verification"] = {"receipts": [] if self.forge else verify_investigations(state.critique, state.snapshot)}
        elif state.role == "decide":
            self.decisions += 1
            action = "revise" if self.revise and self.decisions == 1 else "conclude"
            value["decision"] = {"actions": [{"branch_id": "branch_1", "action": action, "reason": "Use the checked receipt.",
                                                "next_question": "Address the earlier correction." if action == "revise" else ""}],
                                   "assessment": assessment()}
        else:
            raise AssertionError("Unexpected strategic stage.")
        return value


@pytest.mark.asyncio
async def test_paired_inputs_and_identical_output_scoring_without_rewarding_extra_calls() -> None:
    worker = StrategicWorker()
    short = await capture_trial(case(), "planned_review_v1", 1, Budget(), short_worker=short_worker, expected_model="fixture")
    strategic = await capture_trial(case(), "strategy_search_v1", 1, Budget(), strategy_worker=worker, expected_model="fixture")
    assert short.snapshot_sha256 == strategic.snapshot_sha256
    assert short.assessment == strategic.assessment
    assert (short.model_calls, strategic.model_calls) == (1, 4)
    assert strategic.input_tokens is None  # Missing usage is not reported as zero.
    receipt = strategic.versions[0]["verification"]["receipts"][0]
    assert receipt["result"]["coverage_days"] == "10"
    experiment = Experiment(model="fixture", repetitions=1, budget=Budget(), trials=[short, strategic])
    report = score_experiment(experiment, (case(),))
    assert all(item["passed"] for item in report["results"])
    assert report["results"][0]["human_review"]["claim_support"] is None
    replay = score_experiment(Experiment.model_validate(report["capture"]), (case(),))
    assert replay["results"] == report["results"]
    assert replay["summary"] == report["summary"]


@pytest.mark.asyncio
async def test_revision_preserves_own_prior_receipt_and_question() -> None:
    worker = StrategicWorker(revise=True)
    trial = await capture_trial(case(), "strategy_search_v1", 1, Budget(), strategy_worker=worker)
    assert trial.error is None
    assert trial.model_calls == 7
    assert len(trial.versions) == 2
    later = [state for state in worker.calls if state.role == "develop"][1]
    assert later.previous["action"]["next_question"] == "Address the earlier correction."
    assert later.previous["verification"]["receipts"][0]["status"] == "checked"


@pytest.mark.asyncio
async def test_budget_admission_and_forged_verification_fail_closed() -> None:
    worker = StrategicWorker()
    limited = await capture_trial(case(), "strategy_search_v1", 1, Budget(model_calls=1), strategy_worker=worker)
    assert limited.error and limited.model_calls == 1
    assert [state.role for state in worker.calls] == ["seed"]
    forged = await capture_trial(case(), "strategy_search_v1", 1, Budget(), strategy_worker=StrategicWorker(forge=True))
    assert forged.error and forged.assessment is None
    assert not forged.versions


@pytest.mark.asyncio
async def test_model_identity_and_scope_are_checked_without_error_body_leaks() -> None:
    wrong_model = await capture_trial(case(), "planned_review_v1", 1, Budget(), short_worker=short_worker, expected_model="other")
    assert wrong_model.error

    async def foreign(state: PlanningState) -> dict[str, Any]:
        value = await short_worker(state)
        value["plan"]["assessment"]["findings"][0]["evidence_ids"] = ["foreign-secret"]
        return value

    invalid = await capture_trial(case(), "planned_review_v1", 1, Budget(), short_worker=foreign)
    assert invalid.error and invalid.assessment is None
    assert "foreign-secret" not in invalid.error


@pytest.mark.asyncio
async def test_replay_requires_every_pair_and_scores_input_identity() -> None:
    trial = await capture_trial(case(), "planned_review_v1", 1, Budget(), short_worker=short_worker)
    with pytest.raises(ValueError, match="paired"):
        score_experiment(Experiment(model="fixture", repetitions=1, budget=Budget(), trials=[trial]), (case(),))
    with pytest.raises(ValueError, match="Duplicate"):
        Experiment(model="fixture", repetitions=1, budget=Budget(), trials=[trial, trial])
    trial.snapshot_sha256 = "wrong"
    assert not score_trial(case(), trial, Budget())["passed"]


def test_scenarios_include_missing_data_conflicting_assumptions_and_feedback() -> None:
    selected = {item.case_id: item for item in comparison_cases()}
    assert len(selected) == 5
    assert {"expired_quote", "missing_consumption", "negative_round_trip"} <= selected.keys()
    assert selected["conflicting_policies"].snapshot["board"][0]["kind"] == "strategy_hypothesis"
    assert selected["feedback_revision"].snapshot["previous_assessment"] is not None
