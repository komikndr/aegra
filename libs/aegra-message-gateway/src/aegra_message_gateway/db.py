"""Database models and session management."""

from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aegra_message_gateway.settings import GatewaySettings


class Base(DeclarativeBase):
    pass


class LinkAttempt(Base):
    __tablename__ = "message_gateway_link_attempt"
    __table_args__ = (UniqueConstraint("platform", "principal_ref", name="uq_gateway_attempt_principal"),)

    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    device_code_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    user_code: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_uri: Mapped[str] = mapped_column(Text, nullable=False)
    verification_uri_complete: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityLink(Base):
    __tablename__ = "message_gateway_identity_link"
    __table_args__ = (UniqueConstraint("platform", "principal_ref", name="uq_gateway_link_principal"),)

    link_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    keycloak_subject: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    selected_assistant_id: Mapped[str | None] = mapped_column(Text)
    selected_graph_id: Mapped[str] = mapped_column(Text, nullable=False, default="casual_chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMapping(Base):
    __tablename__ = "message_gateway_conversation"
    __table_args__ = (
        UniqueConstraint("link_id", "conversation_ref", "assistant_id", name="uq_gateway_conversation_assistant"),
        Index("idx_gateway_conversation_thread", "aegra_thread_id", unique=True),
    )

    mapping_id: Mapped[str] = mapped_column(Text, primary_key=True)
    link_id: Mapped[str] = mapped_column(
        Text, ForeignKey("message_gateway_identity_link.link_id", ondelete="CASCADE"), nullable=False
    )
    conversation_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    assistant_id: Mapped[str] = mapped_column(Text, nullable=False)
    graph_id: Mapped[str] = mapped_column(Text, nullable=False)
    aegra_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InboundMessage(Base):
    __tablename__ = "message_gateway_inbound_message"
    __table_args__ = (UniqueConstraint("platform", "message_ref", name="uq_gateway_inbound_message"),)

    inbound_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    message_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    link_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("message_gateway_identity_link.link_id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Database:
    def __init__(self, settings: GatewaySettings) -> None:
        self.engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_maker() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
