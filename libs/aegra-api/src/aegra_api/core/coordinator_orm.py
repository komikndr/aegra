"""Durable conversation turns and dispatch intents, not graph checkpoints."""

from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from aegra_api.core.orm import Base, JsonbSafe


class CoordinatorTurn(Base):
    __tablename__ = "coordinator_turns"

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    objective_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_objectives.objective_id"), nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonbSafe)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    __table_args__ = (UniqueConstraint("objective_id", "request_key"), Index("ix_coordinator_turns_pending", "status", "created_at"))
