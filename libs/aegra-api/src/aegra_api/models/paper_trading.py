"""Explicit paper-only capital, liquidity, and action contracts."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, WithJsonSchema, model_validator

from aegra_api.models.swarm import Contract

Money = Annotated[Decimal, Field(gt=0, le=Decimal("1000000000000"), max_digits=20, decimal_places=4)]


class PaperAccountCreate(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    objective_id: str | None = Field(default=None, min_length=1, max_length=128)
    starting_isk: Money
    type_ids: tuple[int, ...] = Field(min_length=1, max_length=3)
    region_id: int = Field(default=10000002, gt=0)
    location_id: int = Field(default=60003760, gt=0)
    fee_bps: int = Field(default=100, ge=0, le=1000)
    slippage_bps: int = Field(default=10, ge=0, le=500)
    max_position_bps: int = Field(default=2500, ge=1, le=10000)
    max_loss_bps: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def unique_types(self) -> "PaperAccountCreate":
        if len(set(self.type_ids)) != len(self.type_ids) or any(value <= 0 for value in self.type_ids):
            raise ValueError("Watchlist type IDs must be unique positive integers.")
        return self


class PaperQuoteInput(Contract):
    type_id: int = Field(gt=0)
    bid: Money
    ask: Money
    bid_quantity: int = Field(ge=0, le=10**12)
    ask_quantity: int = Field(ge=0, le=10**12)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def uncrossed(self) -> "PaperQuoteInput":
        if self.bid > self.ask:
            raise ValueError("Bid cannot exceed ask within this location snapshot.")
        return self


class PaperLoopStart(Contract):
    request_key: str = Field(min_length=1, max_length=128)
    cycles: int = Field(default=1, ge=1, le=24)
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    refresh_esi: bool = False


class PaperDecision(Contract):
    action: Literal["buy", "sell", "hold"]
    type_id: int = Field(gt=0)
    quantity: int = Field(ge=0, le=10**12)
    limit_price: Annotated[
        Decimal,
        Field(ge=0, le=Decimal("1000000000000"), max_digits=20, decimal_places=4),
        WithJsonSchema({"type": "string", "pattern": r"^[0-9]{1,13}(\.[0-9]{1,4})?$", "maxLength": 18}),
    ]
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def meaningful_size(self) -> "PaperDecision":
        if self.action != "hold" and (self.quantity == 0 or self.limit_price == 0):
            raise ValueError("A trade needs a positive quantity and limit price.")
        if self.action == "hold" and self.quantity != 0:
            raise ValueError("Hold quantity must be zero.")
        return self
