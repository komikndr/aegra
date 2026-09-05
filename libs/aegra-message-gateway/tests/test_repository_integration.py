import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aegra_message_gateway.db import ConversationMapping, Database, IdentityLink, InboundMessage, LinkAttempt
from aegra_message_gateway.repository import GatewayRepository
from aegra_message_gateway.settings import GatewaySettings
from sqlalchemy import delete, update


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MESSAGE_GATEWAY_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not configured"
)
async def test_repository_link_conversation_and_idempotency() -> None:
    suffix = uuid4().hex
    key = base64.b64encode(b"i" * 32).decode()
    settings = GatewaySettings(
        database_url=os.environ["MESSAGE_GATEWAY_TEST_DATABASE_URL"],
        encryption_key=key,
        hmac_key=key,
    )
    database = Database(settings)
    repository = GatewayRepository(database)
    platform = "telegram"
    principal_ref = f"principal-{suffix}"
    message_ref = f"message-{suffix}"
    link = None
    try:
        link = await repository.activate_link(
            platform=platform,
            principal_ref=principal_ref,
            keycloak_subject=f"subject-{suffix}",
            refresh_token_encrypted=b"encrypted-refresh",
        )
        mapping = await repository.get_or_create_conversation(
            link_id=link.link_id,
            conversation_ref=f"conversation-{suffix}",
            assistant_id="assistant-1",
            graph_id="casual_chat",
            thread_id=str(uuid4()),
        )
        reused = await repository.get_or_create_conversation(
            link_id=link.link_id,
            conversation_ref=f"conversation-{suffix}",
            assistant_id="assistant-1",
            graph_id="casual_chat",
            thread_id=str(uuid4()),
        )
        first = await repository.claim_inbound_message(
            platform=platform,
            message_ref=message_ref,
            link_id=link.link_id,
        )
        duplicate = await repository.claim_inbound_message(
            platform=platform,
            message_ref=message_ref,
            link_id=link.link_id,
        )

        assert reused.mapping_id == mapping.mapping_id
        assert first is not None
        assert duplicate is None
        async with database.session_maker() as session, session.begin():
            await session.execute(
                update(InboundMessage)
                .where(InboundMessage.inbound_id == first.inbound_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        reclaimed = await repository.claim_inbound_message(
            platform=platform,
            message_ref=message_ref,
            link_id=link.link_id,
        )
        assert reclaimed is not None
        assert reclaimed.inbound_id != first.inbound_id
        await repository.finish_inbound_message(reclaimed.inbound_id, success=True)
        assert (
            await repository.claim_inbound_message(
                platform=platform,
                message_ref=message_ref,
                link_id=link.link_id,
            )
            is None
        )
    finally:
        async with database.session_maker() as session, session.begin():
            await session.execute(delete(InboundMessage).where(InboundMessage.message_ref == message_ref))
            if link is not None:
                await session.execute(delete(ConversationMapping).where(ConversationMapping.link_id == link.link_id))
                await session.execute(delete(IdentityLink).where(IdentityLink.link_id == link.link_id))
            await session.execute(
                delete(LinkAttempt).where(LinkAttempt.platform == platform, LinkAttempt.principal_ref == principal_ref)
            )
        await database.close()
