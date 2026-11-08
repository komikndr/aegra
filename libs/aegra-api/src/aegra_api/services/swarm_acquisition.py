"""Approved public EVE collection, with immutable provenance receipts."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Any

import httpx
from pydantic import AwareDatetime, Field

from aegra_api.models.paper_trading import PaperQuoteInput
from aegra_api.models.swarm import Contract
from aegra_api.models.swarm_coordination import Evidence
from aegra_api.services.eve_market import fetch_eve_history, fetch_eve_quote


class MarketScope(Contract):
    region_id: int = Field(gt=0, le=2147483647)
    location_id: int = Field(gt=0, le=9223372036854775807)
    type_id: int = Field(gt=0, le=2147483647)
    fee_bps: int = Field(default=100, ge=0, le=10000)
    slippage_bps: int = Field(default=10, ge=0, le=10000)


class HistoryRow(Contract):
    date: date
    average: Decimal = Field(ge=0, le=Decimal("1e15"), allow_inf_nan=False)
    highest: Decimal = Field(ge=0, le=Decimal("1e15"), allow_inf_nan=False)
    lowest: Decimal = Field(ge=0, le=Decimal("1e15"), allow_inf_nan=False)
    volume: int = Field(ge=0, le=10**18)
    order_count: int = Field(ge=0, le=10**15)


class AcquisitionReceipt(Contract):
    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    scope: MarketScope
    retrieved_at: AwareDatetime
    expires_at: AwareDatetime
    evidence: tuple[Evidence, Evidence]


def approved_markets() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(os.getenv("SWARM_EVE_MARKETS", "{}"))
        if not isinstance(value, dict) or len(value) > 10:
            raise ValueError("Invalid market catalog.")
        result: dict[str, dict[str, Any]] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in key):
                raise ValueError("Invalid scope ID.")
            result[key] = MarketScope.model_validate(item).model_dump(mode="json")
        return result
    except (ValueError, TypeError):
        raise ValueError("Invalid server EVE market configuration.") from None


def prospective_ids(scope_id: str) -> set[str]:
    return {f"eve:{scope_id}:quote", f"eve:{scope_id}:history"}


def receipt_digest(scope: dict[str, Any], version: str, quote: dict[str, Any], history: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps({"collector": "esi_market_v1", "cost_calculation": "immediate_round_trip_v1",
                                     "scope": scope, "version": version, "quote": quote, "history": history}, sort_keys=True).encode()).hexdigest()


def immediate_cost_check(prices: PaperQuoteInput, scope: MarketScope) -> dict[str, str]:
    with localcontext() as context:
        context.prec = 40
        fee, slip = Decimal(scope.fee_bps) / 10000, Decimal(scope.slippage_bps) / 10000
        entry = prices.ask * (1 + slip) * (1 + fee)
        exit_value = prices.bid * (1 - slip) * (1 - fee)
        return {"calculation_version": "immediate_round_trip_v1", "entry_cash_per_unit_isk": str(entry),
                "exit_receipts_per_unit_isk": str(exit_value), "net_per_unit_isk": str(exit_value - entry),
                "return_pct": str((exit_value - entry) / entry * 100),
                "assumption": "Immediate buy at ask and sell at bid, adverse slippage and fees on both sides, before rounding. Not a forecast or executable order."}


def validate_receipt(value: Any, scope_id: str, snapshot: dict[str, Any]) -> AcquisitionReceipt:
    receipt = AcquisitionReceipt.model_validate(value)
    now = datetime.now(UTC)
    if (receipt.scope_id != scope_id or receipt.scope.model_dump(mode="json") != snapshot.get("acquisition_scopes", {}).get(scope_id)
            or receipt.expires_at <= now or receipt.retrieved_at > now
            or receipt.expires_at <= receipt.retrieved_at
            or {e.evidence_id for e in receipt.evidence} != prospective_ids(scope_id)
            or any(e.available_at != receipt.retrieved_at or e.observed_at > now or e.simulation for e in receipt.evidence)):
        raise ValueError("Acquisition receipt is stale or outside the approved review scope.")
    records = {e.evidence_id: e for e in receipt.evidence}
    quote = json.loads(records[f"eve:{scope_id}:quote"].content)
    history = json.loads(records[f"eve:{scope_id}:history"].content)
    if not isinstance(quote, dict) or not isinstance(history, dict):
        raise ValueError("Invalid acquisition evidence payload.")
    for payload in (quote, history):
        if (payload.get("scope_id") != scope_id or payload.get("scope") != receipt.scope.model_dump(mode="json")
                or payload.get("retrieved_at") != receipt.retrieved_at.isoformat()
                or payload.get("expires_at") != receipt.expires_at.isoformat()
                or payload.get("source_version") != receipt.receipt_id):
            raise ValueError("Acquisition provenance does not match the receipt.")
    if not isinstance(quote.get("quote"), dict):
        raise ValueError("Invalid acquired quote payload.")
    prices = PaperQuoteInput.model_validate({**quote["quote"], "observed_at": receipt.retrieved_at})
    rows = history.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("Invalid acquired history size.")
    normalized = [HistoryRow.model_validate(row) for row in rows]
    expected_history_time = datetime.combine(normalized[-1].date, datetime.min.time(), tzinfo=UTC)
    if (prices.type_id != receipt.scope.type_id or any(row.date > now.date() or row.lowest > row.highest for row in normalized)
            or quote.get("kind") != "current_top_of_book" or history.get("kind") != "regional_daily_history"
            or records[f"eve:{scope_id}:quote"].observed_at != receipt.retrieved_at
            or records[f"eve:{scope_id}:history"].observed_at != expected_history_time
            or records[f"eve:{scope_id}:quote"].source != f"esi:orders:{receipt.scope.region_id}:{receipt.scope.location_id}:{receipt.scope.type_id}"
            or records[f"eve:{scope_id}:history"].source != f"esi:history:{receipt.scope.region_id}:{receipt.scope.type_id}"
            or [row.date for row in normalized] != sorted({row.date for row in normalized})
            or quote.get("cost_check") != immediate_cost_check(prices, receipt.scope)
            or quote.get("upstream_quote_version") != history.get("upstream_quote_version")
            or not isinstance(quote.get("upstream_quote_version"), str)
            or receipt_digest(receipt.scope.model_dump(mode="json"), quote["upstream_quote_version"],
                              prices.model_dump(mode="json", exclude={"observed_at"}),
                              [row.model_dump(mode="json") for row in normalized]) != receipt.receipt_id):
        raise ValueError("Acquisition contents do not match the receipt version.")
    return receipt


async def acquire_market(scope_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    configured = approved_markets().get(scope_id)
    if configured is None or configured != expected:
        raise ValueError("Requested market scope is no longer approved.")
    scope = MarketScope.model_validate(configured)
    try:
        async with asyncio.timeout(35):
            quote, version, expires = await fetch_eve_quote(region_id=scope.region_id, location_id=scope.location_id, type_id=scope.type_id)
            raw_history = await fetch_eve_history(region_id=scope.region_id, type_id=scope.type_id)
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
        raise ValueError("Approved EVE collection failed; no complete fresh receipt is available.") from None
    history = [HistoryRow.model_validate(row) for row in raw_history]
    if not history or any(row.date > datetime.now(UTC).date() or row.lowest > row.highest for row in history):
        raise ValueError("Invalid market history dates or price range.")
    now = datetime.now(UTC)
    prices = quote.model_dump(mode="json", exclude={"observed_at"})
    rows = [row.model_dump(mode="json") for row in history]
    digest = receipt_digest(configured, version, prices, rows)
    common = {"scope_id": scope_id, "scope": configured, "source": "https://esi.evetech.net/latest/markets/",
              "retrieved_at": now.isoformat(), "expires_at": expires.isoformat(), "source_version": digest, "upstream_quote_version": version,
              "upstream_observation_time": None,
              "cache_note": "Retrieval time is not the upstream event time. Public ESI can return a cached snapshot.",
              "cost_note": "fee_bps and slippage_bps are configured assumptions, not observed exchange fees."}
    evidence = [
        Evidence(evidence_id=f"eve:{scope_id}:quote", source=f"esi:orders:{scope.region_id}:{scope.location_id}:{scope.type_id}",
                 observed_at=now, available_at=now, content=json.dumps({**common, "kind": "current_top_of_book", "quote": prices,
                                                                     "cost_check": immediate_cost_check(quote, scope)})),
        Evidence(evidence_id=f"eve:{scope_id}:history", source=f"esi:history:{scope.region_id}:{scope.type_id}",
                 observed_at=datetime.combine(history[-1].date, datetime.min.time(), tzinfo=UTC), available_at=now,
                 content=json.dumps({**common, "kind": "regional_daily_history", "rows": rows,
                                     "note": "Daily regional aggregates are not executable location prices."})),
    ]
    receipt = AcquisitionReceipt(receipt_id=digest, scope_id=scope_id, scope=scope, retrieved_at=now, expires_at=expires, evidence=tuple(evidence))
    return validate_receipt(receipt, scope_id, {"acquisition_scopes": {scope_id: configured}}).model_dump(mode="json")
