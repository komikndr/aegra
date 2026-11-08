"""Deterministic checks and explicitly limited proxies; no LLM self-grading."""

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from aegra_api.models.swarm_coordination import Assessment, Evidence
from aegra_api.services.swarm_capabilities import validate_plan
from evaluations.swarm.contracts import Capture, CapturedCase, EvaluationCase
from evaluations.swarm.suite import SUITE_VERSION, snapshot_digest

NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?!\w|\.\d)")


def check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def temporal_scope(assessment: Assessment, case: EvaluationCase) -> bool:
    records = {item["evidence_id"]: Evidence.model_validate(item) for item in case.snapshot["evidence"]}
    cutoff = datetime.fromisoformat(case.snapshot["snapshot_at"])
    for finding in assessment.findings:
        for ref in finding.evidence_ids:
            record = records.get(ref)
            if record is None or record.available_at > cutoff or record.observed_at > cutoff:
                return False
    return True


def score_case(case: EvaluationCase, capture: CapturedCase) -> dict[str, Any]:
    checks = [check("snapshot_identity", capture.snapshot_sha256 == snapshot_digest(case.snapshot),
                    "Captured output must correspond to this exact versioned input."),
              check("execution", capture.error is None, capture.error or "No execution error reported.")]
    try:
        plan = validate_plan(capture.plan, case.snapshot)
    except (ValueError, TypeError, KeyError):
        checks.append(check("valid_plan", False, "Missing, invalid, or out-of-scope plan."))
        return {"case_id": case.case_id, "checks": checks, "passed": False}
    checks.extend([
        check("valid_plan", True, "Plan passed the production schema and scope validator."),
        check("decision", plan.decision in case.decisions, f"Expected one of {case.decisions}; got {plan.decision}."),
        check("capabilities", set(case.required_capabilities) <= {task.capability for task in plan.tasks},
              "The plan must include this case's required approved capabilities."),
        check("acquisition_scope", plan.acquire_market == case.acquire_market,
              "Acquisition must match the fixture's explicit opt-in expectation."),
    ])
    if case.stage == "review":
        try:
            assessment = Assessment.model_validate(capture.assessment)
        except ValidationError:
            checks.append(check("assessment", False, "Missing or malformed final assessment."))
        else:
            checks.extend([
                check("assessment", True, "Final assessment matches the output contract."),
                check("citation_scope_and_time", temporal_scope(assessment, case),
                      "Cited records must exist and must have been observable and available at the snapshot time."),
                check("citation_presence", not case.require_citations or bool(assessment.findings),
                      "This case requires at least one cited finding."),
                check("uncertainty_presence", not case.require_uncertainty or bool(assessment.uncertainties),
                      "Presence is a limited proxy, not a semantic quality judgment."),
            ])
            if plan.assessment is not None:
                checks.append(check("direct_answer_identity", assessment == plan.assessment,
                                    "Non-task final output must be the planner's actual assessment."))
            if plan.decision == "needs_information":
                checks.append(check("abstention_disposition", assessment.disposition == "needs_information",
                                    "Missing-information decisions must retain their waiting disposition."))
            text = " ".join([assessment.summary, assessment.recommendation, *(item.text for item in assessment.findings)])
            numbers = [Decimal(match.group().replace(",", "")) for match in NUMBER.finditer(text.replace("−", "-"))]
            for fact in case.numeric_facts:
                covered = any(abs(number - Decimal(fact.value)) <= Decimal(fact.tolerance) for number in numbers)
                checks.append(check(f"numeric_coverage:{fact.label}", covered,
                                    f"Expected numeric mention {fact.value} within {fact.tolerance}; this does not verify wording or units."))
    return {"case_id": case.case_id, "checks": checks, "passed": all(item["status"] == "pass" for item in checks),
            "elapsed_seconds": capture.elapsed_seconds,
            "not_evaluated": ["semantic claim support", "numeric claim-to-unit alignment", "profitability", "durable worker recovery"]}


def score_capture(capture: Capture, selected: tuple[EvaluationCase, ...]) -> dict[str, Any]:
    if capture.suite_version != SUITE_VERSION:
        raise ValueError("Capture suite version does not match.")
    by_id = {item.case_id: item for item in capture.cases}
    if set(by_id) != {case.case_id for case in selected}:
        raise ValueError("Captured cases must match the selected case IDs exactly.")
    results = [score_case(case, by_id[case.case_id]) for case in selected]
    return {"suite_version": SUITE_VERSION, "origin": capture.origin,
            "case_count": len(results), "cases_passing_all_checks": sum(item["passed"] for item in results),
            "expectations": [case.model_dump(mode="json") for case in selected],
            "results": results, "capture": json.loads(capture.model_dump_json()),
            "limitations": "Synthetic policy regression baseline, not a benchmark of general research accuracy. "
                           "Numeric/uncertainty presence does not prove claim support, correct units, or honest execution claims."}
