"""PostgreSQL coordination records; graph state remains owned by LangGraph."""

from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from aegra_api.core.orm import Base, JsonbSafe


class SwarmObjective(Base):
    __tablename__ = "swarm_objectives"

    objective_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    current_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JsonbSafe, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    __table_args__ = (UniqueConstraint("user_id", "request_key"), Index("ix_swarm_objectives_user", "user_id"))


class SwarmReview(Base):
    __tablename__ = "swarm_reviews"

    review_id: Mapped[str] = mapped_column(Text, primary_key=True)
    objective_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_objectives.objective_id"), nullable=False)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JsonbSafe)
    acquisition: Mapped[dict[str, Any] | None] = mapped_column(JsonbSafe)
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("objective_id", "request_key"),
        UniqueConstraint("objective_id", "revision"),
        Index("ix_swarm_reviews_status", "status"),
    )


class SwarmTask(Base):
    __tablename__ = "swarm_tasks"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    review_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_reviews.review_id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_id"), nullable=False)
    last_submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonbSafe)
    __table_args__ = (UniqueConstraint("review_id", "role"), Index("ix_swarm_tasks_status", "status"))


class SwarmEntry(Base):
    __tablename__ = "swarm_entries"

    objective_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_objectives.objective_id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[str | None] = mapped_column(Text, ForeignKey("swarm_reviews.review_id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))


class SwarmStrategyVersion(Base):
    """Append-only branch artifacts; new passes never overwrite earlier attempts."""

    __tablename__ = "swarm_strategy_versions"
    review_id: Mapped[str] = mapped_column(Text, ForeignKey("swarm_reviews.review_id"), primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict[str, Any]] = mapped_column(JsonbSafe, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
