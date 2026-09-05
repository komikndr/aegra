"""Persistence operations for linked identities and gateway sessions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from aegra_message_gateway.db import (
    ConversationMapping,
    Database,
    IdentityLink,
    InboundMessage,
    LinkAttempt,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class GatewayRepository:
    def __init__(self, database: Database, *, processing_lease_seconds: int = 7500) -> None:
        self._database = database
        self._processing_lease = timedelta(seconds=processing_lease_seconds)

    async def save_link_attempt(
        self,
        *,
        platform: str,
        principal_ref: str,
        device_code_encrypted: bytes,
        user_code: str,
        verification_uri: str,
        verification_uri_complete: str | None,
        expires_at: datetime,
        interval_seconds: int,
    ) -> LinkAttempt:
        attempt = LinkAttempt(
            attempt_id=str(uuid4()),
            platform=platform,
            principal_ref=principal_ref,
            device_code_encrypted=device_code_encrypted,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            expires_at=expires_at,
            interval_seconds=interval_seconds,
            last_polled_at=None,
            created_at=utcnow(),
        )
        async with self._database.session_maker() as session, session.begin():
            await session.execute(
                delete(LinkAttempt).where(
                    LinkAttempt.platform == platform,
                    LinkAttempt.principal_ref == principal_ref,
                )
            )
            session.add(attempt)
        return attempt

    async def get_link_attempt(self, platform: str, principal_ref: str) -> LinkAttempt | None:
        async with self._database.session_maker() as session:
            return await session.scalar(
                select(LinkAttempt).where(
                    LinkAttempt.platform == platform,
                    LinkAttempt.principal_ref == principal_ref,
                )
            )

    async def mark_attempt_polled(self, attempt_id: str, when: datetime) -> None:
        async with self._database.session_maker() as session, session.begin():
            attempt = await session.get(LinkAttempt, attempt_id, with_for_update=True)
            if attempt is not None:
                attempt.last_polled_at = when

    async def delete_link_attempt(self, platform: str, principal_ref: str) -> None:
        async with self._database.session_maker() as session, session.begin():
            await session.execute(
                delete(LinkAttempt).where(
                    LinkAttempt.platform == platform,
                    LinkAttempt.principal_ref == principal_ref,
                )
            )

    async def activate_link(
        self,
        *,
        platform: str,
        principal_ref: str,
        keycloak_subject: str,
        refresh_token_encrypted: bytes,
    ) -> IdentityLink:
        now = utcnow()
        async with self._database.session_maker() as session, session.begin():
            link = await session.scalar(
                select(IdentityLink)
                .where(IdentityLink.platform == platform, IdentityLink.principal_ref == principal_ref)
                .with_for_update()
            )
            if link is None:
                link = IdentityLink(
                    link_id=str(uuid4()),
                    platform=platform,
                    principal_ref=principal_ref,
                    keycloak_subject=keycloak_subject,
                    refresh_token_encrypted=refresh_token_encrypted,
                    selected_assistant_id=None,
                    selected_graph_id="casual_chat",
                    created_at=now,
                    updated_at=now,
                    revoked_at=None,
                )
                session.add(link)
            else:
                link.keycloak_subject = keycloak_subject
                link.refresh_token_encrypted = refresh_token_encrypted
                link.selected_assistant_id = None
                link.selected_graph_id = "casual_chat"
                link.updated_at = now
                link.revoked_at = None
            await session.execute(
                delete(LinkAttempt).where(
                    LinkAttempt.platform == platform,
                    LinkAttempt.principal_ref == principal_ref,
                )
            )
        return link

    async def get_active_link(self, platform: str, principal_ref: str) -> IdentityLink | None:
        async with self._database.session_maker() as session:
            return await session.scalar(
                select(IdentityLink).where(
                    IdentityLink.platform == platform,
                    IdentityLink.principal_ref == principal_ref,
                    IdentityLink.revoked_at.is_(None),
                    IdentityLink.refresh_token_encrypted.is_not(None),
                )
            )

    async def rotate_refresh_token(self, link_id: str, refresh_token_encrypted: bytes) -> None:
        async with self._database.session_maker() as session, session.begin():
            link = await session.get(IdentityLink, link_id, with_for_update=True)
            if link is None or link.revoked_at is not None:
                raise LookupError("linked identity no longer exists")
            link.refresh_token_encrypted = refresh_token_encrypted
            link.updated_at = utcnow()

    async def revoke_link(self, link_id: str) -> None:
        async with self._database.session_maker() as session, session.begin():
            link = await session.get(IdentityLink, link_id, with_for_update=True)
            if link is None:
                return
            revoked_at = utcnow()
            link.refresh_token_encrypted = None
            link.revoked_at = revoked_at
            link.updated_at = revoked_at
            await session.execute(delete(ConversationMapping).where(ConversationMapping.link_id == link_id))

    async def select_assistant(self, link_id: str, assistant_id: str, graph_id: str) -> None:
        async with self._database.session_maker() as session, session.begin():
            link = await session.get(IdentityLink, link_id, with_for_update=True)
            if link is None or link.revoked_at is not None:
                raise LookupError("linked identity no longer exists")
            link.selected_assistant_id = assistant_id
            link.selected_graph_id = graph_id
            link.updated_at = utcnow()

    async def get_or_create_conversation(
        self,
        *,
        link_id: str,
        conversation_ref: str,
        assistant_id: str,
        graph_id: str,
        thread_id: str,
    ) -> ConversationMapping:
        async with self._database.session_maker() as session:
            existing = await session.scalar(
                select(ConversationMapping).where(
                    ConversationMapping.link_id == link_id,
                    ConversationMapping.conversation_ref == conversation_ref,
                    ConversationMapping.assistant_id == assistant_id,
                )
            )
            if existing is not None:
                return existing
            now = utcnow()
            mapping = ConversationMapping(
                mapping_id=str(uuid4()),
                link_id=link_id,
                conversation_ref=conversation_ref,
                assistant_id=assistant_id,
                graph_id=graph_id,
                aegra_thread_id=thread_id,
                created_at=now,
                updated_at=now,
            )
            session.add(mapping)
            try:
                await session.commit()
                return mapping
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(ConversationMapping).where(
                        ConversationMapping.link_id == link_id,
                        ConversationMapping.conversation_ref == conversation_ref,
                        ConversationMapping.assistant_id == assistant_id,
                    )
                )
                if existing is None:
                    raise
                return existing

    async def delete_conversation(self, *, link_id: str, conversation_ref: str, assistant_id: str) -> None:
        async with self._database.session_maker() as session, session.begin():
            await session.execute(
                delete(ConversationMapping).where(
                    ConversationMapping.link_id == link_id,
                    ConversationMapping.conversation_ref == conversation_ref,
                    ConversationMapping.assistant_id == assistant_id,
                )
            )

    async def claim_inbound_message(
        self,
        *,
        platform: str,
        message_ref: str,
        link_id: str | None,
    ) -> InboundMessage | None:
        now = utcnow()
        inbound_id = str(uuid4())
        lease_expires_at = now + self._processing_lease
        statement = insert(InboundMessage).values(
            inbound_id=inbound_id,
            platform=platform,
            message_ref=message_ref,
            link_id=link_id,
            status="processing",
            created_at=now,
            updated_at=now,
            lease_expires_at=lease_expires_at,
            completed_at=None,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_gateway_inbound_message",
            set_={
                "inbound_id": inbound_id,
                "link_id": link_id,
                "status": "processing",
                "updated_at": now,
                "lease_expires_at": lease_expires_at,
                "completed_at": None,
            },
            where=or_(
                InboundMessage.status == "failed",
                and_(
                    InboundMessage.status == "processing",
                    or_(InboundMessage.lease_expires_at.is_(None), InboundMessage.lease_expires_at < now),
                ),
            ),
        ).returning(InboundMessage)
        async with self._database.session_maker() as session, session.begin():
            result = await session.execute(statement)
            return result.scalar_one_or_none()

    async def finish_inbound_message(self, inbound_id: str, *, success: bool) -> None:
        async with self._database.session_maker() as session, session.begin():
            inbound = await session.get(InboundMessage, inbound_id, with_for_update=True)
            if inbound is not None:
                now = utcnow()
                inbound.status = "completed" if success else "failed"
                inbound.updated_at = now
                inbound.lease_expires_at = None
                inbound.completed_at = now

    async def cleanup_expired(self, *, inbound_retention_days: int) -> None:
        now = utcnow()
        cutoff = now - timedelta(days=inbound_retention_days)
        async with self._database.session_maker() as session, session.begin():
            await session.execute(delete(LinkAttempt).where(LinkAttempt.expires_at < now))
            await session.execute(
                delete(InboundMessage).where(
                    InboundMessage.status.in_(("completed", "failed")),
                    InboundMessage.completed_at < cutoff,
                )
            )
            await session.execute(
                delete(InboundMessage).where(
                    InboundMessage.status == "processing",
                    or_(
                        InboundMessage.lease_expires_at.is_(None),
                        InboundMessage.lease_expires_at < cutoff,
                    ),
                )
            )
