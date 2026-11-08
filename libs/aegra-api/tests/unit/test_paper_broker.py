from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import AsyncMock

import httpx
import pytest
from paper_trader import graph as paper_graph

from aegra_api.models.paper_trading import PaperAccountCreate, PaperDecision
from aegra_api.services.eve_market import fetch_eve_history, fetch_eve_quote
from aegra_api.services.paper_broker import execute_paper_decision


def policy() -> dict[str, Any]:
    return PaperAccountCreate(
        request_key="test",
        name="Test",
        starting_isk="10000",
        type_ids=(34,),
        slippage_bps=0,
        fee_bps=100,
        max_position_bps=10000,
    ).model_dump(mode="json")


def quotes() -> dict[str, Any]:
    return {
        "34": {
            "quote_id": "quote-1",
            "bid": "10",
            "ask": "11",
            "bid_quantity": 1000,
            "ask_quantity": 1000,
            "used_bid": 0,
            "used_ask": 0,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }
    }


def action(kind: Literal["buy", "sell", "hold"] = "buy", quantity: int = 100, limit: str = "11") -> PaperDecision:
    return PaperDecision(
        action=kind, type_id=34, quantity=quantity, limit_price=limit, reason="Synthetic test proposal"
    )


def test_buy_sell_conserves_cash_and_cost_with_fees() -> None:
    cash, positions, buy = execute_paper_decision(
        cash=Decimal(10000), positions={}, policy=policy(), quotes=quotes(), decision=action()
    )
    assert cash == Decimal("8889") and positions["34"] == {"quantity": 100, "cost": "1111.0000"}
    assert buy["fee"] == "11.0000"
    cash, positions, sell = execute_paper_decision(
        cash=cash, positions=positions, policy=policy(), quotes=quotes(), decision=action("sell", 100, "10")
    )
    assert cash == Decimal("9879") and positions["34"]["quantity"] == 0
    assert sell["realized_pnl"] == "-121.0000"


@pytest.mark.parametrize(
    "failure", ["cash", "holdings", "liquidity", "expired", "limit", "exposure", "loss", "watchlist", "missing"]
)
def test_constraints_reject_without_mutation(failure: str) -> None:
    cash, positions, rules, book, decision = Decimal(10000), {}, policy(), quotes(), action()
    if failure == "cash":
        cash = Decimal(1)
    elif failure == "holdings":
        decision = action("sell", 100, "10")
    elif failure == "liquidity":
        book["34"]["used_ask"] = 999
    elif failure == "expired":
        book["34"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif failure == "limit":
        rules["slippage_bps"] = 10
    elif failure == "exposure":
        rules["max_position_bps"] = 10
    elif failure == "loss":
        cash = Decimal(8000)
    elif failure == "watchlist":
        rules["type_ids"] = [35]
    elif failure == "missing":
        book = {}
    original = cash
    cash, result_positions, result = execute_paper_decision(
        cash=cash, positions=positions, policy=rules, quotes=book, decision=decision
    )
    assert cash == original and positions == result_positions == {} and result["status"] == "rejected"


def test_hold_needs_no_quote_and_cannot_claim_a_fill() -> None:
    _, _, result = execute_paper_decision(
        cash=Decimal(10000), positions={}, policy=policy(), quotes={}, decision=action("hold", 0, "0")
    )
    assert result["status"] == "held" and result["simulation"] is True


def test_model_price_schema_is_one_bounded_decimal_string() -> None:
    schema = PaperDecision.model_json_schema()["properties"]["limit_price"]
    assert schema["type"] == "string" and schema["maxLength"] == 18
    assert "anyOf" not in schema and schema["pattern"].endswith("$")


@pytest.mark.asyncio
async def test_esi_uses_single_location_and_cached_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "esi.evetech.net"
        assert request.url.params["type_id"] == "34"
        orders = [
            {"location_id": 60003760, "type_id": 34, "is_buy_order": True, "price": 10, "volume_remain": 50},
            {"location_id": 60003760, "type_id": 34, "is_buy_order": False, "price": 11, "volume_remain": 40},
            {"location_id": 123, "type_id": 34, "is_buy_order": True, "price": 100, "volume_remain": 999},
        ]
        return httpx.Response(200, json=orders)

    args = {"region_id": 10000002, "location_id": 60003760, "type_id": 34, "transport": httpx.MockTransport(handler)}
    quote, version, _ = await fetch_eve_quote(**args)
    _, repeated, _ = await fetch_eve_quote(**args)
    assert quote.bid == 10 and quote.ask == 11 and quote.bid_quantity == 50 and version == repeated


@pytest.mark.asyncio
async def test_large_esi_book_is_rejected_instead_of_partial_best_price() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[], headers={"X-Pages": "6"}))
    with pytest.raises(ValueError, match="five-page"):
        await fetch_eve_quote(region_id=10000002, location_id=60003760, type_id=34, transport=transport)


@pytest.mark.asyncio
async def test_history_longer_than_one_year_retains_only_recent_twenty_days() -> None:
    rows = [{"date": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=offset)).date().isoformat(), "average": 4.5} for offset in range(419)]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=rows))
    history = await fetch_eve_history(region_id=10000002, type_id=34, transport=transport)
    assert len(history) == 20 and history[-1] == rows[-1]


@pytest.mark.asyncio
async def test_paper_graph_returns_proposal_without_executing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    structured = AsyncMock()
    structured.ainvoke.return_value = action("hold", 0, "0")
    model = AsyncMock()
    model.model_copy = lambda **_kwargs: model
    model.with_structured_output = lambda *_args, **_kwargs: structured
    monkeypatch.setattr(paper_graph, "load_chat_model", lambda *_args, **_kwargs: model)
    output = await paper_graph.decide(paper_graph.PaperState(snapshot={"cash": "10000"}))
    assert output["decision"]["action"] == "hold" and output["model"]
    assert "No live orders" in structured.ainvoke.await_args.args[0][0].content
    model.bind_tools.assert_not_called()


@pytest.mark.asyncio
async def test_paper_graph_rejects_oversized_context_before_model_access(monkeypatch: pytest.MonkeyPatch) -> None:
    model = AsyncMock()
    monkeypatch.setattr(paper_graph, "load_chat_model", model)
    with pytest.raises(ValueError, match="context budget"):
        await paper_graph.decide(paper_graph.PaperState(snapshot={"unbounded": "x" * 100001}))
    model.assert_not_called()
