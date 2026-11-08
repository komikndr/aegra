"""Pure capability validation and deterministic stock arithmetic, without network I/O."""

import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any

from pydantic import Field

from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_planning import ReviewPlan
from aegra_api.services.swarm_acquisition import prospective_ids

CAPABILITIES: dict[str, dict[str, str]] = {
    "evidence_analysis": {"description": "Assess supplied evidence and its relevance; no external retrieval.", "executor": "model"},
    "market_analysis": {"description": "Assess supplied EVE quotes/history/news, freshness, fees and uncertainty; no live fetch or orders.", "executor": "model"},
    "stock_calculation": {"description": "Calculate coverage from ONE evidence content JSON with inventory_units, daily_consumption_units, horizon_days. No prose extraction.", "executor": "decimal"},
    "skeptical_review": {"description": "Challenge assumptions and supplied findings, including dependency results.", "executor": "model"},
}


class StockInputs(Contract):
    inventory_units: Decimal = Field(ge=0, le=Decimal("1e15"), max_digits=24, decimal_places=8)
    daily_consumption_units: Decimal = Field(gt=0, le=Decimal("1e15"), max_digits=24, decimal_places=8)
    horizon_days: Decimal = Field(gt=0, le=3650, max_digits=12, decimal_places=8)


def stock_calculation(record: dict[str, Any]) -> dict[str, Any]:
    inputs = StockInputs.model_validate_json(record["content"])
    with localcontext() as context:
        context.prec = 60
        needed = inputs.daily_consumption_units * inputs.horizon_days
        coverage = inputs.inventory_units / inputs.daily_consumption_units
        remaining = inputs.inventory_units - needed
    return {"calculation_version": "stock_coverage_v1", "evidence_ids": [record["evidence_id"]],
            "units": {"inventory": "units", "consumption": "units/day", "horizon": "days", "coverage": "days"},
            "inputs": inputs.model_dump(mode="json"),
            "coverage_days": str(coverage),
            "required_units": str(needed), "remaining_units": str(remaining),
            "shortfall_units": str(max(Decimal(0), -remaining)),
            "simulation": record.get("simulation", False)}


def validate_plan(value: Any, snapshot: dict[str, Any]) -> ReviewPlan:
    plan = ReviewPlan.model_validate(value)
    evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
    expired = expired_evidence(snapshot)
    prospective: set[str] = set()
    if plan.acquire_market is not None:
        if plan.acquire_market not in snapshot.get("acquisition_scopes", {}):
            raise ValueError("Market acquisition was not approved for this review.")
        prospective = prospective_ids(plan.acquire_market)
        if prospective & evidence.keys():
            raise ValueError("Acquisition evidence IDs must not shadow supplied evidence.")
    for task in plan.tasks:
        if task.capability in {"market_analysis", "stock_calculation"} and set(task.evidence_ids) & expired:
            raise ValueError("Expired evidence cannot support a current calculation or market assessment.")
        if not set(task.evidence_ids) <= evidence.keys() | prospective:
            raise ValueError("Task evidence is outside the review snapshot.")
        if task.capability == "stock_calculation":
            if len(task.evidence_ids) != 1 or task.evidence_ids[0] not in evidence:
                raise ValueError("Stock calculation requires exactly one structured evidence record.")
            stock_calculation(evidence[task.evidence_ids[0]])
    if plan.assessment:
        cited = {ref for finding in plan.assessment.findings for ref in finding.evidence_ids}
        if not cited <= evidence.keys():
            raise ValueError("Plan answer cites evidence outside the review snapshot.")
        if cited & expired and plan.assessment.disposition != "needs_information":
            raise ValueError("Expired evidence requires fresh information, not a supported current answer.")
    return plan


def expired_evidence(snapshot: dict[str, Any]) -> set[str]:
    """Treat explicit expired/malformed expiry as unusable; age alone is not expiry."""
    at = datetime.fromisoformat(snapshot["snapshot_at"]) if snapshot.get("snapshot_at") else datetime.now(UTC)
    expired: set[str] = set()
    for item in snapshot.get("evidence", []):
        try:
            content = json.loads(item["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(content, dict) or "expires_at" not in content:
            continue
        try:
            expiry = datetime.fromisoformat(content["expires_at"])
            if expiry.tzinfo is None or expiry <= at:
                expired.add(item["evidence_id"])
        except (ValueError, TypeError):
            expired.add(item["evidence_id"])
    return expired


def bounded_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode()) > 160_000:
        raise ValueError("Planned review input exceeds its context budget.")
    return text
