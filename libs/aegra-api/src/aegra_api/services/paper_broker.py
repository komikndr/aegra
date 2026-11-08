"""Deterministic, full-or-reject paper fills. No real trading transport exists."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aegra_api.models.paper_trading import PaperDecision

UNIT = Decimal("0.0001")
BPS = Decimal(10000)


def money(value: Decimal) -> Decimal:
    return value.quantize(UNIT, rounding=ROUND_HALF_UP)


def valuation(cash: Decimal, positions: dict[str, Any], quotes: dict[str, Any]) -> Decimal | None:
    value = cash
    for type_id, position in positions.items():
        if position["quantity"] == 0:
            continue
        quote = quotes.get(type_id)
        if quote is None or datetime.fromisoformat(quote["expires_at"]) <= datetime.now(UTC):
            return None
        value += Decimal(quote["bid"]) * position["quantity"]
    return money(value)


def execute_paper_decision(
    *,
    cash: Decimal,
    positions: dict[str, Any],
    policy: dict[str, Any],
    quotes: dict[str, Any],
    decision: PaperDecision,
) -> tuple[Decimal, dict[str, Any], dict[str, Any]]:
    """All arithmetic is Decimal. Caller serializes account, quote usage, and ledger."""
    result: dict[str, Any] = {"simulation": True, "decision": decision.model_dump(mode="json")}

    def reject(reason: str) -> tuple[Decimal, dict[str, Any], dict[str, Any]]:
        return cash, positions, {**result, "status": "rejected", "reason": reason}

    if decision.type_id not in policy["type_ids"]:
        return reject("Type is outside the account watchlist.")
    if decision.action == "hold":
        return cash, positions, {**result, "status": "held"}
    quote = quotes.get(str(decision.type_id))
    if quote is None or datetime.fromisoformat(quote["expires_at"]) <= datetime.now(UTC):
        return reject("Fresh executable-side market snapshot is unavailable.")
    equity = valuation(cash, positions, quotes)
    if equity is None:
        return reject("Fresh portfolio valuation is unavailable.")
    if decision.action == "buy" and equity <= Decimal(policy["starting_isk"]) * (
        1 - Decimal(policy["max_loss_bps"]) / BPS
    ):
        return reject("Loss limit reached; only hold or risk-reducing sells are permitted.")
    side = "ask" if decision.action == "buy" else "bid"
    available = quote[side + "_quantity"] - quote.get("used_" + side, 0)
    if decision.quantity > available:
        return reject("Requested size exceeds remaining top-of-book paper liquidity.")
    slip = Decimal(policy["slippage_bps"]) / BPS
    price = money(Decimal(quote[side]) * (1 + slip if decision.action == "buy" else 1 - slip))
    if (decision.action == "buy" and price > decision.limit_price) or (
        decision.action == "sell" and price < decision.limit_price
    ):
        return reject("Slippage-adjusted fill price violates the proposed limit.")
    gross = money(price * decision.quantity)
    fee = money(gross * Decimal(policy["fee_bps"]) / BPS)
    if gross + cash + fee >= Decimal("100000000000000000000"):
        return reject("Trade exceeds the supported ledger balance range.")
    updated = deepcopy(positions)
    position = updated.setdefault(str(decision.type_id), {"quantity": 0, "cost": "0"})
    realized = Decimal(0)
    if decision.action == "buy":
        total = gross + fee
        if total > cash:
            return reject("Insufficient virtual cash including fees.")
        exposure = Decimal(quote["bid"]) * (position["quantity"] + decision.quantity)
        post_equity = equity - total + Decimal(quote["bid"]) * decision.quantity
        if post_equity < Decimal(policy["starting_isk"]) * (1 - Decimal(policy["max_loss_bps"]) / BPS):
            return reject("Post-fill marked equity would breach the configured loss limit.")
        if exposure > post_equity * Decimal(policy["max_position_bps"]) / BPS:
            return reject("Post-fill position exceeds the configured equity exposure limit.")
        cash -= total
        position["quantity"] += decision.quantity
        position["cost"] = str(money(Decimal(position["cost"]) + total))
    else:
        if decision.quantity > position["quantity"]:
            return reject("Insufficient holdings; short selling is unavailable.")
        cost_removed = money(Decimal(position["cost"]) * decision.quantity / position["quantity"])
        position["quantity"] -= decision.quantity
        position["cost"] = str(money(Decimal(position["cost"]) - cost_removed))
        cash += gross - fee
        realized = money(gross - fee - cost_removed)
    return (
        money(cash),
        updated,
        {
            **result,
            "status": "filled",
            "quote_id": quote["quote_id"],
            "side": side,
            "quantity": decision.quantity,
            "price": str(price),
            "gross": str(gross),
            "fee": str(fee),
            "realized_pnl": str(realized),
            "cash_after": str(money(cash)),
            "equity_after": str(valuation(cash, updated, quotes)),
        },
    )
