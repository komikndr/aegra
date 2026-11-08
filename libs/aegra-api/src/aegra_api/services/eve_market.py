"""Bounded public ESI order-book reads at one configured market location."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from aegra_api.models.paper_trading import PaperQuoteInput


async def fetch_eve_history(*, region_id: int, type_id: int, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    """Regional daily aggregates are context, never executable location prices."""
    async with (
        httpx.AsyncClient(timeout=10, follow_redirects=False, transport=transport, headers={"Accept-Encoding": "identity"}) as client,
        client.stream(
            "GET", f"https://esi.evetech.net/latest/markets/{region_id}/history/", params={"type_id": type_id}
        ) as response,
    ):
        response.raise_for_status()
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Compressed history response is unsupported.")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > 500_000:
                raise ValueError("Market history exceeds its size budget.")
            body.extend(chunk)
        values = json.loads(body)
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("Invalid daily market history.")
    rows: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("date"), str):
            raise ValueError("Invalid market history row.")
        rows.append(value)
    return sorted(rows, key=lambda row: row["date"])[-20:]


async def fetch_eve_quote(
    *,
    region_id: int,
    location_id: int,
    type_id: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[PaperQuoteInput, str, datetime]:
    orders: list[dict[str, Any]] = []
    expires = datetime.now(UTC) + timedelta(minutes=5)
    digest = hashlib.sha256()
    async with (
        asyncio.timeout(30),
        httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "ONYX-paper-market-poc/1.0", "Accept-Encoding": "identity"},
        ) as client,
    ):
        for page in range(1, 6):
            async with client.stream(
                "GET",
                f"https://esi.evetech.net/latest/markets/{region_id}/orders/",
                params={"type_id": type_id, "order_type": "all", "page": page},
            ) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("Compressed market response is unsupported.")
                payload = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(payload) + len(chunk) > 2_000_000:
                        raise ValueError("Market response exceeds its size budget.")
                    payload.extend(chunk)
                values = json.loads(payload, parse_float=Decimal)
                if not isinstance(values, list) or len(values) > 1000:
                    raise ValueError("Invalid market page.")
                if any(not isinstance(order, dict) for order in values):
                    raise ValueError("Invalid market order row.")
                orders.extend(values)
                pages = int(response.headers.get("X-Pages", "1"))
                if pages < 1 or pages > 5:
                    raise ValueError("Market book exceeds the five-page collection budget.")
                if response.headers.get("Expires"):
                    expires = min(expires, parsedate_to_datetime(response.headers["Expires"]))
                digest.update(payload)
                digest.update(response.headers.get("Expires", "").encode())
                if page >= pages:
                    break
    if expires <= datetime.now(UTC):
        raise ValueError("Market snapshot is already expired.")
    local = [order for order in orders if order.get("location_id") == location_id and order.get("type_id") == type_id]
    buys = [order for order in local if order.get("is_buy_order") is True and order.get("min_volume", 1) == 1]
    sells = [order for order in local if order.get("is_buy_order") is False]
    if not buys or not sells:
        raise ValueError("Two-sided quotes are unavailable at the selected location.")
    bid, ask = max(Decimal(order["price"]) for order in buys), min(Decimal(order["price"]) for order in sells)
    quote = PaperQuoteInput(
        type_id=type_id,
        bid=bid,
        ask=ask,
        bid_quantity=sum(order["volume_remain"] for order in buys if Decimal(order["price"]) == bid),
        ask_quantity=sum(order["volume_remain"] for order in sells if Decimal(order["price"]) == ask),
        observed_at=datetime.now(UTC),
    )
    return quote, digest.hexdigest(), expires
