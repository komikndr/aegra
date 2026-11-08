"""Objective-scoped durable scheduling; execution remains in existing RunJobs."""

from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from aegra_api.core.orm import Base, JsonbSafe


class SwarmSupervisor(Base):
    __tablename__ = "swarm_supervisors"

    objective_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_objectives.objective_id"), primary_key=True)
    generation: Mapped[str] = mapped_column(Text, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    next_wakeup: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reviews_started: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_calls_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    specialist_tasks_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_id: Mapped[str | None] = mapped_column(Text, ForeignKey("swarm_reviews.review_id"))
    owns_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    wake_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_swarm_supervisors_enabled", "enabled", "checked_at"),)


class SwarmSupervisorCommand(Base):
    __tablename__ = "swarm_supervisor_commands"

    objective_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_objectives.objective_id"), primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
