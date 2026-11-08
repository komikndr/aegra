"""Paper portfolios and append-only fills, separate from real trading accounts."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, ForeignKey, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from aegra_api.core.orm import Base, JsonbSafe


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, index=True)
    request_key: Mapped[str] = mapped_column(Text)
    policy: Mapped[dict[str, Any]] = mapped_column(JsonbSafe)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    positions: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, default=dict)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    remaining: Mapped[int] = mapped_column(Integer, default=0)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    refresh_esi: Mapped[bool] = mapped_column(Boolean, default=False)
    next_due: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (UniqueConstraint("user_id", "request_key"),)


class PaperQuote(Base):
    __tablename__ = "paper_quotes"
    quote_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, ForeignKey("paper_accounts.account_id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JsonbSafe)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    used_bid: Mapped[int] = mapped_column(BigInteger, default=0)
    used_ask: Mapped[int] = mapped_column(BigInteger, default=0)


class PaperCycle(Base):
    __tablename__ = "paper_cycles"
    cycle_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, ForeignKey("paper_accounts.account_id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_id"))
    status: Mapped[str] = mapped_column(Text, default="pending")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JsonbSafe)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonbSafe)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (UniqueConstraint("account_id", "number"),)


class PaperLedger(Base):
    __tablename__ = "paper_ledger"
    cycle_id: Mapped[str] = mapped_column(Text, ForeignKey("paper_cycles.cycle_id"), primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, ForeignKey("paper_accounts.account_id"), index=True)
    entry: Mapped[dict[str, Any]] = mapped_column(JsonbSafe)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))


class PaperCommand(Base):
    __tablename__ = "paper_commands"
    account_id: Mapped[str] = mapped_column(Text, ForeignKey("paper_accounts.account_id"), primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request: Mapped[dict[str, Any]] = mapped_column(JsonbSafe)
