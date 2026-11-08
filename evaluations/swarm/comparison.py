"""Paired, bounded short-review versus strategic-search experiments; no database writes."""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import openai
from langchain_core.callbacks import get_usage_metadata_callback
from pydantic import BaseModel, ConfigDict, Field, model_validator
from react_agent.model_config import resolve_agent_model
from swarm_planner.graph import PlanningState
from swarm_planner.graph import execute as execute_short
from swarm_strategy.graph import StrategyState
from swarm_strategy.graph import execute as execute_strategy

from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_coordination import Assessment
from aegra_api.models.swarm_strategy import StrategyCandidate, StrategyCritique, StrategyDecision, StrategySeed
from aegra_api.services.swarm_capabilities import expired_evidence
from aegra_api.services.swarm_strategy import verify_investigations
from evaluations.swarm.contracts import EvaluationCase, NumericFact
from evaluations.swarm.run import capture_case
from evaluations.swarm.scoring import NUMBER, check, temporal_scope
from evaluations.swarm.suite import cases, evidence, snapshot, snapshot_digest

VERSION = "onyx-paired-research-v1"
Workflow = Literal["planned_review_v1", "strategy_search_v1"]
ShortWorker = Callable[[PlanningState], Awaitable[dict[str, Any]]]
StrategyWorker = Callable[[StrategyState], Awaitable[dict[str, Any]]]
EXECUTION_ERRORS = (ValueError, KeyError, TypeError, TimeoutError, httpx.HTTPError, openai.APIError)


class Budget(Contract):
    model_calls: int = Field(default=11, ge=1, le=11)
    output_tokens_per_call: Literal[4096] = 4096
    context_bytes_per_call: Literal[160000] = 160000
    elapsed_seconds: int = Field(default=600, ge=1, le=600)


class Trial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)

    case_id: str
    repetition: int = Field(ge=1, le=5)
    workflow: Workflow
    snapshot_sha256: str
    assessment: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    versions: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    model_calls: int = Field(default=0, ge=0, le=11)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    error: str | None = None


class Experiment(Contract):
    version: Literal["onyx-paired-research-v1"] = VERSION
    model: str
    repetitions: int = Field(ge=1, le=5)
    budget: Budget
    trials: list[Trial] = Field(max_length=50)

    @model_validator(mode="after")
    def unique_trials(self) -> "Experiment":
        keys = {(t.case_id, t.repetition, t.workflow) for t in self.trials}
        if len(keys) != len(self.trials):
            raise ValueError("Duplicate trial identity.")
        return self


def comparison_cases() -> tuple[EvaluationCase, ...]:
    """Same frozen inputs for both arms; correction cases include the earlier assessment."""
    baseline = {case.case_id: case for case in cases()}
    conflict = snapshot("Consumption reports disagree: normal operations report 70000/day, while a stress report suggests 110000/day. "
                        "Compare the normal seven-day requirement with a ten-day high-consumption reserve. "
                        "Calculate both and challenge the earlier sufficient-stock hypothesis; do not assume either report is verified.", [
        evidence("normal", {"inventory_units": "700000", "daily_consumption_units": "70000", "horizon_days": "7"}),
        evidence("stress", {"inventory_units": "700000", "daily_consumption_units": "110000", "horizon_days": "10"}),
    ])
    conflict["board"] = [{"kind": "strategy_hypothesis", "content": "Unverified initial proposal: 700000 units covers both policies without replenishment."}]
    correction = snapshot("Reassess the previous insufficient-stock conclusion after the operator corrected inventory. "
                          "Use the current evidence rather than retaining the superseded 200000-unit assumption.", [
        evidence("stock", {"inventory_units": "700000", "daily_consumption_units": "70000", "horizon_days": "7"})])
    correction["previous_assessment"] = {"summary": "Previous inventory was 200000 units: about 2.86 days and 290000 short.",
                                         "disposition": "needs_information"}
    correction["board"] = [{"kind": "feedback", "content": "Inventory corrected from 200000 to 700000; consumption remains 70000/day."}]
    return (
        EvaluationCase(case_id="conflicting_policies", description="Preserve competing assumptions, compute both, and identify unresolved policy.",
                       snapshot=conflict, decisions=("tasks",), require_citations=True, require_uncertainty=True,
                       numeric_facts=(NumericFact(label="normal coverage days", value="10"),
                                      NumericFact(label="stress coverage days", value="6.363636", tolerance="0.01"),
                                      NumericFact(label="stress shortfall", value="400000"))),
        baseline["missing_consumption"], baseline["expired_quote"], baseline["negative_round_trip"],
        EvaluationCase(case_id="feedback_revision", description="Revise an earlier conclusion when corrected evidence changes the calculation.",
                       snapshot=correction, decisions=("tasks",), require_citations=True, require_uncertainty=True,
                       numeric_facts=(NumericFact(label="corrected coverage days", value="10"),
                                      NumericFact(label="remaining units", value="210000"))),
    )


def validate_assessment(value: Any, case: EvaluationCase) -> dict[str, Any]:
    """Use the experiment's pinned clock, not today's wall clock, for fixture freshness."""
    assessment = Assessment.model_validate(value)
    if not temporal_scope(assessment, case):
        raise ValueError("Assessment cites unavailable evidence.")
    refs = {ref for finding in assessment.findings for ref in finding.evidence_ids}
    if refs & expired_evidence(case.snapshot) and assessment.disposition != "needs_information":
        raise ValueError("Assessment treats expired evidence as current.")
    return assessment.model_dump(mode="json")


class Meter:
    def __init__(self, trial: Trial, budget: Budget, expected_model: str | None) -> None:
        self.trial, self.budget, self.expected_model = trial, budget, expected_model
        self.usage_complete = True
        self.lock = asyncio.Lock()

    async def call(self, state: Any, worker: Callable[..., Awaitable[dict[str, Any]]], *, inference: bool) -> dict[str, Any]:
        if len(json.dumps(vars(state)).encode()) > self.budget.context_bytes_per_call:
            raise ValueError("Shared context ceiling exceeded.")
        async with self.lock:
            if inference and self.trial.model_calls >= self.budget.model_calls:
                raise ValueError("Shared model-call ceiling exhausted.")
            if inference:
                self.trial.model_calls += 1
            step: dict[str, Any] = {"role": state.role, "inference": inference, "input": vars(state).copy()}
            self.trial.steps.append(step)
        began = time.monotonic()
        with get_usage_metadata_callback() as usage:
            try:
                result = await worker(state)
                if inference and self.expected_model is not None and result.get("model") != self.expected_model:
                    raise ValueError("Worker model differs from experiment model.")
                step["output"] = result
                return result
            except EXECUTION_ERRORS as exc:
                step["error_type"] = type(exc).__name__
                raise
            finally:
                step["elapsed_seconds"] = round(time.monotonic() - began, 4)
                if inference:
                    values = list(usage.usage_metadata.values())
                    step["usage"] = values or None
                    if values:
                        self.trial.input_tokens = (self.trial.input_tokens or 0) + sum(v["input_tokens"] for v in values)
                        self.trial.output_tokens = (self.trial.output_tokens or 0) + sum(v["output_tokens"] for v in values)
                    else:
                        self.usage_complete = False


async def strategic_case(case: EvaluationCase, meter: Meter, worker: StrategyWorker) -> dict[str, Any]:
    seed = StrategySeed.model_validate((await meter.call(StrategyState(role="seed", snapshot=case.snapshot), worker, inference=True))["seed"])
    if seed.acquire_market:
        raise ValueError("External collection is excluded from this frozen-input comparison.")
    candidates = list(seed.candidates)
    previous: dict[str, dict[str, Any]] = {}
    for pass_number in (1, 2):
        async def branch(candidate: StrategyCandidate) -> dict[str, Any]:
            inputs: dict[str, Any] = {"snapshot": case.snapshot, "candidate": candidate.model_dump(mode="json"),
                                      "previous": previous.get(candidate.branch_id, {})}
            report: dict[str, Any] = {"candidate": inputs["candidate"], "complete": False}
            try:
                developed = await meter.call(StrategyState(role="develop", **inputs), worker, inference=True)
                report.update(attempt=validate_assessment(developed["assessment"], case), attempt_status="success")
                critiqued = await meter.call(StrategyState(role="critique", attempt=report["attempt"], **inputs), worker, inference=True)
                critique = StrategyCritique.model_validate(critiqued["critique"]).model_dump(mode="json")
                report["critique"] = critique
                checked = await meter.call(StrategyState(role="verify", critique=critique, **inputs), worker, inference=False)
                if checked["verification"] != {"receipts": verify_investigations(critique, case.snapshot)}:
                    raise ValueError("Verification receipt mismatch.")
                report.update(verification=checked["verification"], complete=True)
            except EXECUTION_ERRORS as exc:
                report["error_type"] = type(exc).__name__
            return report

        reports = await asyncio.gather(*(branch(candidate) for candidate in candidates))
        if not any(report["complete"] for report in reports):
            raise ValueError("No complete strategic branch.")
        controlled = await meter.call(StrategyState(role="decide", snapshot=case.snapshot, reports=reports,
                                                    pass_number=pass_number, remaining_passes=2 - pass_number), worker, inference=True)
        decision = StrategyDecision.model_validate(controlled["decision"])
        if {action.branch_id for action in decision.actions} != {candidate.branch_id for candidate in candidates}:
            raise ValueError("Controller did not cover exactly the active branches.")
        assessment = validate_assessment(decision.assessment, case)
        for report in reports:
            branch_id = report["candidate"]["branch_id"]
            action = next(action for action in decision.actions if action.branch_id == branch_id)
            previous[branch_id] = {**report, "action": action.model_dump(mode="json")}
            meter.trial.versions.append({"branch_id": branch_id, "version": pass_number, **previous[branch_id]})
        continuing = {action.branch_id for action in decision.actions if action.action in {"continue", "revise"}}
        if not continuing or pass_number == 2:
            if continuing:
                assessment["uncertainties"].append("Strategic search reached its two-pass budget with unresolved branches.")
            return assessment
        candidates = [candidate for candidate in candidates if candidate.branch_id in continuing]
    raise ValueError("No strategic assessment.")


async def capture_trial(case: EvaluationCase, workflow: Workflow, repetition: int, budget: Budget, *,
                        short_worker: ShortWorker = execute_short, strategy_worker: StrategyWorker = execute_strategy,
                        expected_model: str | None = None) -> Trial:
    trial = Trial(case_id=case.case_id, repetition=repetition, workflow=workflow, snapshot_sha256=snapshot_digest(case.snapshot))
    meter = Meter(trial, budget, expected_model)
    began = time.monotonic()
    try:
        async with asyncio.timeout(budget.elapsed_seconds):
            if workflow == "planned_review_v1":
                async def measured(state: PlanningState) -> dict[str, Any]:
                    return await meter.call(state, short_worker, inference=state.spec.get("capability") != "stock_calculation")
                captured = await capture_case(case, measured, timeout_seconds=budget.elapsed_seconds)
                if captured.error:
                    raise ValueError("Short review failed; see recorded steps.")
                trial.assessment = validate_assessment(captured.assessment, case)
            else:
                trial.assessment = await strategic_case(case, meter, strategy_worker)
    except EXECUTION_ERRORS as exc:
        trial.error = f"Execution failed: {type(exc).__name__}; upstream bodies and credentials are omitted."
    trial.elapsed_seconds = round(time.monotonic() - began, 4)
    if not meter.usage_complete:
        trial.input_tokens = trial.output_tokens = None
    return trial


def score_trial(case: EvaluationCase, trial: Trial, budget: Budget) -> dict[str, Any]:
    # Reuse the same final-output checks for both arms; do not reward a workflow for producing more artifacts.
    checks = [check("snapshot_identity", trial.snapshot_sha256 == snapshot_digest(case.snapshot), "Exact paired input hash."),
              check("execution", trial.error is None, trial.error or "Completed."),
              check("call_ceiling", trial.model_calls <= budget.model_calls, "Same admission ceiling for both arms."),
              check("call_accounting", trial.model_calls == sum(bool(step.get("inference")) for step in trial.steps),
                    "Failed requests also consume a logical model-call admission.")]
    try:
        value = validate_assessment(trial.assessment, case)
        assessment = Assessment.model_validate(value)
    except EXECUTION_ERRORS:
        checks.append(check("valid_scoped_assessment", False, "No valid, temporally scoped assessment."))
    else:
        checks.extend([check("valid_scoped_assessment", True, "Schema, record identity, availability and explicit expiry checked."),
                       check("citation_presence", not case.require_citations or bool(assessment.findings), "Presence does not prove claim support."),
                       check("uncertainty_presence", not case.require_uncertainty or bool(assessment.uncertainties), "Presence only."),
                       check("missing_input_wait", case.case_id not in {"expired_quote", "missing_consumption"} or
                             assessment.disposition == "needs_information", "Unusable inputs require information, not a current opportunity claim.")])
        text = " ".join([assessment.summary, assessment.recommendation, *(finding.text for finding in assessment.findings)])
        numbers = [Decimal(match.group().replace(",", "")) for match in NUMBER.finditer(text.replace("−", "-"))]
        for fact in case.numeric_facts:
            checks.append(check("numeric_coverage:" + fact.label,
                                any(abs(number - Decimal(fact.value)) <= Decimal(fact.tolerance) for number in numbers),
                                "Numeric mention only; units, contradictions and claim alignment require review."))
    return {"case_id": case.case_id, "repetition": trial.repetition, "workflow": trial.workflow,
            "passed": all(item["status"] == "pass" for item in checks), "checks": checks,
            "model_calls": trial.model_calls, "input_tokens": trial.input_tokens, "output_tokens": trial.output_tokens,
            "elapsed_seconds": trial.elapsed_seconds, "branch_versions": len(trial.versions),
            "human_review": {"claim_support": None, "numeric_units_and_contradictions": None,
                             "alternative_quality": None, "correction_quality": None, "unsupported_execution_claims": None}}


def score_experiment(experiment: Experiment, selected: tuple[EvaluationCase, ...]) -> dict[str, Any]:
    expected = {(case.case_id, rep, arm) for case in selected for rep in range(1, experiment.repetitions + 1)
                for arm in ("planned_review_v1", "strategy_search_v1")}
    if {(t.case_id, t.repetition, t.workflow) for t in experiment.trials} != expected:
        raise ValueError("Capture must contain every paired trial exactly once.")
    by_id = {case.case_id: case for case in selected}
    results = [score_trial(by_id[trial.case_id], trial, experiment.budget) for trial in experiment.trials]
    summary = {arm: {"trials": len(rows := [r for r in results if r["workflow"] == arm]),
                     "passing_automated_checks": sum(r["passed"] for r in rows),
                     "model_calls": sum(r["model_calls"] for r in rows),
                     "elapsed_seconds": round(sum(r["elapsed_seconds"] for r in rows), 3),
                     "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rows)
                     if all(r["input_tokens"] is not None and r["output_tokens"] is not None for r in rows) else None}
               for arm in ("planned_review_v1", "strategy_search_v1")}
    return {"version": VERSION, "evaluated_at": datetime.now(UTC).isoformat(), "summary": summary, "results": results, "capture": experiment.model_dump(mode="json"),
            "expectations": [case.model_dump(mode="json") for case in selected],
            "limitations": ["Equal ceilings, not equal realized token use or identical per-call prompts. No forced spending of unused budget.",
                            "Frozen synthetic inputs; actual worker functions and deterministic verifiers, not durable worker/recovery tests.",
                            "Numeric mention and uncertainty presence do not prove semantic correctness or alternative quality.",
                            "Human-review fields are ungraded. No superiority, profitability, or statistical significance claim.",
                            "Feedback case is a revised snapshot with prior assessment, not an end-to-end operator interaction."]}


async def run_experiment(selected: tuple[EvaluationCase, ...], repetitions: int, budget: Budget, *, checkpoint: Path | None = None) -> Experiment:
    model = resolve_agent_model("swarm").model
    trials: list[Trial] = []
    for repetition in range(1, repetitions + 1):
        arms: tuple[Workflow, ...] = ("planned_review_v1", "strategy_search_v1") if repetition % 2 else ("strategy_search_v1", "planned_review_v1")
        for case in selected:
            for arm in arms:
                print(f"{case.case_id} repetition {repetition}: {arm}", file=sys.stderr)
                trials.append(await capture_trial(case, arm, repetition, budget, expected_model=model))
                if checkpoint is not None:
                    partial = Experiment(model=model, repetitions=repetitions, budget=budget, trials=trials)
                    temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
                    temporary.write_text(json.dumps({"status": "in_progress", "capture": partial.model_dump(mode="json")}))
                    temporary.replace(checkpoint)
    return Experiment(model=model, repetitions=repetitions, budget=budget, trials=trials)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--responses", type=Path)
    mode.add_argument("--validate-only", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selected = tuple(case for case in comparison_cases() if not args.case_ids or case.case_id in args.case_ids)
    if not selected or (args.case_ids and set(args.case_ids) != {case.case_id for case in selected}):
        parser.error("Unknown comparison case ID.")
    if not args.output.parent.is_dir():
        parser.error("Output parent directory must already exist.")
    if args.validate_only:
        report: dict[str, Any] = {"version": VERSION, "quality_evaluated": False, "budget": Budget().model_dump(),
                                  "cases": [case.model_dump(mode="json") for case in selected]}
    else:
        try:
            if args.live:
                experiment = asyncio.run(run_experiment(selected, args.repetitions, Budget(), checkpoint=args.output))
            else:
                with args.responses.open("rb") as handle:
                    raw = handle.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError("Oversized capture.")
                payload = json.loads(raw)
                experiment = Experiment.model_validate(payload.get("capture", payload))
            report = score_experiment(experiment, selected)
            report["origin"] = "live" if args.live else "imported"
        except (OSError, ValueError, AttributeError) as exc:
            parser.error(f"Invalid experiment ({type(exc).__name__}); no upstream bodies are printed.")
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"report_path": str(args.output), "summary": report.get("summary"), "quality_evaluated": not args.validate_only}, indent=2))
    return 0 if args.validate_only or all(row["passed"] for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
