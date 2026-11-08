"""Run an isolated quality baseline, or score previously captured model outputs."""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import openai
from pydantic import ValidationError
from swarm_planner.graph import PlanningState, execute

from aegra_api.services.swarm_capabilities import validate_plan
from evaluations.swarm.contracts import Capture, CapturedCase, EvaluationCase
from evaluations.swarm.scoring import score_capture
from evaluations.swarm.suite import SUITE_VERSION, cases, snapshot_digest

Worker = Callable[[PlanningState], Awaitable[dict[str, Any]]]


async def capture_case(case: EvaluationCase, worker: Worker = execute, *, timeout_seconds: float = 500) -> CapturedCase:
    start = time.monotonic()
    steps: list[dict[str, Any]] = []
    plan_value: dict[str, Any] | None = None
    assessment: dict[str, Any] | None = None
    error: str | None = None

    async def step(state: PlanningState) -> dict[str, Any]:
        began = time.monotonic()
        try:
            result = await worker(state)
        except (ValueError, KeyError, TypeError, TimeoutError, httpx.HTTPError, openai.APIError) as exc:
            steps.append({"role": state.role, "elapsed_seconds": round(time.monotonic() - began, 4),
                          "error_type": type(exc).__name__})
            raise
        steps.append({"role": state.role, "model": result.get("model"), "elapsed_seconds": round(time.monotonic() - began, 4),
                      "output": result})
        return result

    try:
        async with asyncio.timeout(timeout_seconds):
            output = await step(PlanningState(role="planner", snapshot=case.snapshot))
            plan_value = output.get("plan")
            plan = validate_plan(plan_value, case.snapshot)
            if case.stage == "review" and plan.acquire_market is not None:
                raise ValueError("Live acquisition is not permitted by this isolated evaluation runner.")
            if case.stage == "review" and plan.decision != "tasks":
                assessment = plan.assessment.model_dump(mode="json") if plan.assessment else None
            elif case.stage == "review":
                reports: list[dict[str, Any]] = []
                for task in plan.tasks:
                    selected = {**case.snapshot, "evidence": [record for record in case.snapshot["evidence"]
                                                             if record["evidence_id"] in task.evidence_ids]}
                    dependencies = [report for report in reports if report["role"] in task.depends_on]
                    result = await step(PlanningState(role=task.task_id, snapshot=selected,
                                                     reports=dependencies, spec=task.model_dump(mode="json")))
                    reports.append({"role": task.task_id, "status": "success",
                                    "result": result.get("calculation", result.get("assessment"))})
                output = await step(PlanningState(role="coordinator", snapshot=case.snapshot, reports=reports))
                assessment = output.get("assessment")
    except ValidationError as exc:
        locations = [{"location": item["loc"], "type": item["type"]} for item in
                     exc.errors(include_input=False, include_context=False, include_url=False)]
        error = "Schema validation failed: " + json.dumps(locations)[:1500]
    except (ValueError, KeyError, TypeError, TimeoutError, httpx.HTTPError, openai.APIError) as exc:
        error = f"Execution failed: {type(exc).__name__}. Upstream bodies and credentials are omitted."
    return CapturedCase(case_id=case.case_id, snapshot_sha256=snapshot_digest(case.snapshot), plan=plan_value,
                        assessment=assessment, steps=tuple(steps), error=error, elapsed_seconds=round(time.monotonic() - start, 4))


async def capture_suite(selected: tuple[EvaluationCase, ...]) -> Capture:
    # Sequential cases keep provider load and attribution predictable. No production objectives or runs are created.
    captured = []
    for case in selected:
        print(f"Evaluating {case.case_id}...", file=sys.stderr)
        captured.append(await capture_case(case))
    return Capture(suite_version=SUITE_VERSION, origin="live", cases=tuple(captured))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true", help="Validate the suite without inference.")
    mode.add_argument("--live", action="store_true", help="Invoke the configured swarm LLM; incurs provider usage.")
    mode.add_argument("--responses", type=Path, help="Score a Capture JSON file, or a previous report containing capture.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Select a case ID; repeat to select several.")
    parser.add_argument("--output", type=Path, help="Save a JSON report; its parent directory must already exist.")
    args = parser.parse_args()
    all_cases = cases()
    if args.case_ids and set(args.case_ids) - {case.case_id for case in all_cases}:
        parser.error("Unknown case ID.")
    if args.output and not args.output.parent.is_dir():
        parser.error("The output directory must already exist.")
    selected = tuple(case for case in all_cases if not args.case_ids or case.case_id in args.case_ids)
    if args.validate_only:
        report: dict[str, Any] = {"suite_version": SUITE_VERSION, "mode": "input_validation_only",
                                  "cases": [{"case_id": case.case_id, "snapshot_sha256": snapshot_digest(case.snapshot),
                                             "stage": case.stage} for case in selected],
                                  "quality_evaluated": False}
        passed = True
    else:
        if args.live:
            capture = asyncio.run(capture_suite(selected))
        else:
            try:
                with args.responses.open("rb") as handle:
                    raw = handle.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("Response file is too large.")
                payload = json.loads(raw)
                capture = Capture.model_validate(payload.get("capture", payload)).model_copy(update={"origin": "imported"})
            except (OSError, ValueError, AttributeError) as exc:
                parser.error(f"Invalid response file ({type(exc).__name__}); expected bounded Capture JSON.")
        try:
            report = score_capture(capture, selected)
        except (ValueError, ValidationError) as exc:
            parser.error(f"Cannot score capture ({type(exc).__name__}); check suite version and selected IDs.")
        passed = report["cases_passing_all_checks"] == report["case_count"]
    report["evaluated_at"] = datetime.now(UTC).isoformat()
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(encoded)
        print(json.dumps({"report_path": str(args.output), "suite_version": SUITE_VERSION,
                          "results": [{"case_id": item["case_id"], "passed": item["passed"],
                                       "failed_checks": [entry["name"] for entry in item["checks"] if entry["status"] == "fail"]}
                                      for item in report.get("results", [])],
                          "quality_evaluated": not args.validate_only}, indent=2))
    else:
        print(encoded, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
