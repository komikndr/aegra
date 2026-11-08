"""Exercise evaluation failure controls, not just reference outputs that pass."""

import copy
import json
from typing import Any

import pytest
from evaluations.swarm.contracts import Capture, CapturedCase
from evaluations.swarm.run import capture_case
from evaluations.swarm.scoring import score_capture, score_case
from evaluations.swarm.suite import SUITE_VERSION, cases, snapshot_digest
from swarm_planner.graph import PlanningState

from aegra_api.services.swarm_capabilities import stock_calculation


def assessment() -> dict[str, Any]:
    return {"summary": "Synthetic stock covers 10 days; seven days needs 490,000 units and leaves 210,000.",
            "findings": [{"text": "Calculated synthetic stock coverage.", "evidence_ids": ["stock"]}],
            "recommendation": "Wait for observations.", "uncertainties": ["Synthetic inputs are not verified inventory."],
            "disposition": "wait"}


def captured() -> CapturedCase:
    case = cases()[0]
    return CapturedCase(case_id=case.case_id, snapshot_sha256=snapshot_digest(case.snapshot),
                        plan={"decision": "tasks", "rationale": "Use the calculator.", "tasks": [
                            {"task_id": "task_1", "capability": "stock_calculation", "question": "Calculate coverage.",
                             "evidence_ids": ["stock"], "depends_on": []}]}, assessment=assessment())


def test_suite_has_unique_pinned_cases_and_reference_arithmetic() -> None:
    suite = cases()
    assert len(suite) == len({case.case_id for case in suite}) == 8
    for case in suite[:2]:
        calculated = stock_calculation(case.snapshot["evidence"][0])
        expected = {fact.label: fact.value for fact in case.numeric_facts}
        assert calculated["required_units"] == expected["required units"]
    assert suite[-1].stage == "planner"


def test_valid_capture_and_reproducible_score() -> None:
    result = score_capture(Capture(suite_version=SUITE_VERSION, origin="imported", cases=(captured(),)), (cases()[0],))
    assert result["cases_passing_all_checks"] == 1
    assert "semantic claim support" in result["results"][0]["not_evaluated"]
    restored = Capture.model_validate(result["capture"])
    assert score_capture(restored, (cases()[0],)) == result


@pytest.mark.parametrize("mutation", ["foreign_citation", "wrong_number", "wrong_plan", "changed_snapshot", "error", "missing_assessment"])
def test_bad_captures_fail(mutation: str) -> None:
    raw = captured().model_dump(mode="json")
    if mutation == "foreign_citation":
        raw["assessment"]["findings"][0]["evidence_ids"] = ["another-objective"]
    elif mutation == "wrong_number":
        raw["assessment"]["summary"] = "Stock covers 9 days; needs 480,000 units and leaves 200,000."
    elif mutation == "wrong_plan":
        raw["plan"]["tasks"][0]["capability"] = "market_analysis"
    elif mutation == "changed_snapshot":
        raw["snapshot_sha256"] = "different"
    elif mutation == "error":
        raw["error"] = "TimeoutError"
    else:
        raw["assessment"] = None
    assert not score_case(cases()[0], CapturedCase.model_validate(raw))["passed"]


def test_valid_citation_id_with_future_availability_fails() -> None:
    case = cases()[0]
    changed = copy.deepcopy(case.snapshot)
    changed["evidence"][0]["available_at"] = "2026-10-28T00:00:00+00:00"
    future_case = case.model_copy(update={"snapshot": changed})
    response = captured().model_copy(update={"snapshot_sha256": snapshot_digest(changed)})
    checks = score_case(future_case, response)["checks"]
    assert next(item for item in checks if item["name"] == "citation_scope_and_time")["status"] == "fail"


def test_capture_mismatch_and_duplicate_ids_rejected() -> None:
    capture = Capture(suite_version=SUITE_VERSION, origin="imported", cases=(captured(),))
    with pytest.raises(ValueError, match="exactly"):
        score_capture(capture, cases())
    with pytest.raises(ValueError, match="version"):
        score_capture(capture.model_copy(update={"suite_version": "old"}), (cases()[0],))
    with pytest.raises(ValueError, match="unique"):
        Capture(suite_version=SUITE_VERSION, origin="imported", cases=(captured(), captured()))


@pytest.mark.asyncio
async def test_runner_uses_selected_evidence_calculator_and_dependency_reports() -> None:
    seen: list[PlanningState] = []

    async def worker(state: PlanningState) -> dict[str, Any]:
        seen.append(state)
        if state.role == "planner":
            return {"plan": captured().plan, "model": "fixture-model"}
        if state.role == "task_1":
            return {"calculation": stock_calculation(state.snapshot["evidence"][0]), "model": "deterministic:stock_coverage_v1"}
        assert state.reports[0]["result"]["coverage_days"] == "10"
        return {"assessment": assessment(), "model": "fixture-model"}

    response = await capture_case(cases()[0], worker)
    assert response.error is None
    assert [state.role for state in seen] == ["planner", "task_1", "coordinator"]
    assert score_case(cases()[0], response)["passed"]


@pytest.mark.asyncio
async def test_acquisition_case_evaluates_plan_without_fetching_market() -> None:
    seen: list[str] = []

    async def worker(state: PlanningState) -> dict[str, Any]:
        seen.append(state.role)
        return {"plan": {"decision": "tasks", "rationale": "Use approved collection.", "acquire_market": "jita-tritanium",
                         "tasks": [{"task_id": "task_1", "capability": "market_analysis", "question": "Analyze.",
                                    "evidence_ids": ["eve:jita-tritanium:quote"]},
                                   {"task_id": "task_2", "capability": "skeptical_review", "question": "Challenge.",
                                    "evidence_ids": ["eve:jita-tritanium:history"], "depends_on": ["task_1"]}]}}

    response = await capture_case(cases()[-1], worker)
    assert seen == ["planner"]
    assert score_case(cases()[-1], response)["passed"]


@pytest.mark.asyncio
async def test_runner_errors_are_failures_and_do_not_leak_upstream_details() -> None:
    async def worker(state: PlanningState) -> dict[str, Any]:
        raise ValueError("secret-upstream-body")

    response = await capture_case(cases()[0], worker)
    assert "ValueError" in str(response.error)
    assert "secret-upstream-body" not in json.dumps(response.model_dump(mode="json"))
    assert not score_case(cases()[0], response)["passed"]
