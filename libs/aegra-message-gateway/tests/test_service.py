import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegra_message_gateway.db import IdentityLink
from aegra_message_gateway.oauth import OAuthTokenError, OAuthTokens
from aegra_message_gateway.schemas import AssistantRecord, TransportMessage
from aegra_message_gateway.service import GatewayRequestError, GatewayService
from aegra_message_gateway.settings import GatewaySettings


def _request(
    text: str = "hello",
    *,
    user_id: str = "42",
    chat_id: str = "42",
    message_id: str = "1001",
) -> TransportMessage:
    return TransportMessage.model_validate(
        {
            "source": {
                "platform": "telegram",
                "user_id": user_id,
                "chat_id": chat_id,
                "chat_type": "dm",
                "message_id": message_id,
                "session_id": f"telegram:{user_id}",
            },
            "text": text,
        }
    )


def _link(*, selected_assistant_id: str | None = "assistant-1") -> IdentityLink:
    now = datetime.now(UTC)
    return IdentityLink(
        link_id="link-1",
        platform="telegram",
        principal_ref="principal-ref",
        keycloak_subject="subject-1",
        refresh_token_encrypted=b"encrypted",
        selected_assistant_id=selected_assistant_id,
        selected_graph_id="casual_chat",
        created_at=now,
        updated_at=now,
        revoked_at=None,
    )


def _service(settings: GatewaySettings, link: IdentityLink):
    repository = MagicMock()
    repository.get_active_link = AsyncMock(return_value=link)
    repository.get_link_attempt = AsyncMock(return_value=None)
    repository.mark_attempt_polled = AsyncMock()
    repository.delete_link_attempt = AsyncMock()
    repository.claim_inbound_message = AsyncMock(return_value=SimpleNamespace(inbound_id="inbound-1"))
    repository.finish_inbound_message = AsyncMock()
    repository.get_or_create_conversation = AsyncMock(return_value=SimpleNamespace(aegra_thread_id="thread-1"))
    repository.delete_conversation = AsyncMock()
    repository.select_assistant = AsyncMock()
    repository.revoke_link = AsyncMock()
    oauth = MagicMock()
    aegra = MagicMock()
    service = GatewayService(settings=settings, repository=repository, oauth=oauth, aegra=aegra)
    service._cache.put(link.link_id, "access-token", datetime.now(UTC) + timedelta(minutes=10))
    return service, repository, aegra


@pytest.mark.asyncio
async def test_inaccessible_selected_agent_marks_inbound_failed(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    aegra.search_assistants = AsyncMock(return_value=[])

    with pytest.raises(GatewayRequestError, match="no longer accessible"):
        await service.handle(_request())

    repository.finish_inbound_message.assert_awaited_once_with("inbound-1", success=False)


@pytest.mark.asyncio
async def test_duplicate_message_is_not_sent_to_aegra(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    repository.claim_inbound_message.return_value = None

    turn = await service.handle(_request())

    assert turn.text == "This message was already processed."
    aegra.search_assistants.assert_not_called()


@pytest.mark.asyncio
async def test_successful_turn_uses_authorized_assistant_and_completes(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    assistant = AssistantRecord(
        assistant_id="assistant-1",
        name="Casual",
        graph_id="casual_chat",
        user_id="system",
    )
    aegra.search_assistants = AsyncMock(return_value=[assistant])
    aegra.create_thread = AsyncMock()

    async def stream_run(*_args, **_kwargs):
        yield "response"

    aegra.stream_run = stream_run
    turn = await service.handle(_request("current message"))
    assert turn.stream is not None
    assert [delta async for delta in turn.stream] == ["response"]
    repository.finish_inbound_message.assert_awaited_once_with("inbound-1", success=True)
    aegra.create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_rejects_identity_subject_change(settings: GatewaySettings) -> None:
    link = _link()
    service, _repository, _aegra = _service(settings, link)
    service._cache.remove(link.link_id)
    link.refresh_token_encrypted = service._box.encrypt(
        "old-refresh",
        associated_data=service._link_aad(link.platform, link.principal_ref),
    )
    service._oauth.refresh = AsyncMock(
        return_value=OAuthTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    service._oauth.userinfo = AsyncMock(return_value={"sub": "different-subject"})

    with pytest.raises(OAuthTokenError, match="did not match"):
        await service._access_token(link)


@pytest.mark.asyncio
async def test_new_deletes_selected_conversation_mapping(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    assistant = AssistantRecord(assistant_id="assistant-1", name="Casual", graph_id="casual_chat", user_id="system")
    aegra.search_assistants = AsyncMock(return_value=[assistant])

    turn = await service.handle(_request("/new"))

    assert turn.text == "Started a new Telegram thread for Casual (casual_chat)."
    repository.delete_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_list_only_includes_chat_graphs_and_names_current(settings: GatewaySettings) -> None:
    service, _repository, aegra = _service(settings, _link())
    casual = AssistantRecord(assistant_id="assistant-1", name="Casual", graph_id="casual_chat", user_id="system")
    analytic = AssistantRecord(assistant_id="assistant-2", name="Analytic", graph_id="analytic_chat", user_id="system")
    hidden = AssistantRecord(assistant_id="assistant-hidden", name="Factory", graph_id="factory", user_id="system")
    aegra.search_assistants = AsyncMock(return_value=[casual, analytic, hidden])

    result = await service._agent_command(_link(), "access-token", "/agent")

    assert "Current agent: Casual (casual_chat)" in result
    assert "Casual (casual_chat)" in result
    assert "Analytic (analytic_chat)" in result
    assert "Factory" not in result


@pytest.mark.asyncio
async def test_agent_selection_rejects_hidden_assistant_id(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    hidden = AssistantRecord(
        assistant_id="assistant-hidden", name="DBChat", graph_id="analytic_dbchat", user_id="system"
    )
    aegra.search_assistants = AsyncMock(return_value=[hidden])

    result = await service._agent_command(_link(), "access-token", "/agent assistant-hidden")

    assert result == "Agent not found or ambiguous. Use /agent to list assistants you can access."
    repository.select_assistant.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_selection_accepts_unique_chat_graph(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    analytic = AssistantRecord(assistant_id="assistant-2", name="Analytic", graph_id="analytic_chat", user_id="system")
    aegra.search_assistants = AsyncMock(return_value=[analytic])

    result = await service._agent_command(_link(), "access-token", "/agent analytic_chat")

    assert result == "Selected agent: Analytic (analytic_chat). New messages will use its own Aegra thread."
    repository.select_assistant.assert_awaited_once_with("link-1", "assistant-2", "analytic_chat")


def test_message_reference_includes_user_and_chat_identity(settings: GatewaySettings) -> None:
    service, _repository, _aegra = _service(settings, _link())
    first = _request(user_id="42", chat_id="100", message_id="7").source
    second = _request(user_id="43", chat_id="101", message_id="7").source

    first_ref = service._message_ref(first, service._principal_ref(first))
    second_ref = service._message_ref(second, service._principal_ref(second))

    assert first_ref != second_ref


@pytest.mark.asyncio
async def test_invalid_refresh_revokes_link_and_requires_relink(settings: GatewaySettings) -> None:
    link = _link()
    service, repository, _aegra = _service(settings, link)
    service._cache.remove(link.link_id)
    link.refresh_token_encrypted = service._box.encrypt(
        "expired-refresh",
        associated_data=service._link_aad(link.platform, link.principal_ref),
    )
    service._oauth.refresh = AsyncMock(side_effect=OAuthTokenError("invalid refresh"))

    turn = await service.handle(_request())

    assert turn.text == "Your Aegra link expired. Send /link to connect your account again."
    repository.revoke_link.assert_awaited_once_with(link.link_id)
    repository.claim_inbound_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_claim_marks_inbound_failed(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    aegra.search_assistants = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await service.handle(_request())

    repository.finish_inbound_message.assert_awaited_once_with("inbound-1", success=False)


@pytest.mark.asyncio
async def test_failed_link_preflight_revokes_issued_refresh_token(settings: GatewaySettings) -> None:
    service, repository, aegra = _service(settings, _link())
    source = _request().source
    principal_ref = service._principal_ref(source)
    encrypted_code = service._box.encrypt(
        "device-code",
        associated_data=service._attempt_aad(source.platform, principal_ref),
    )
    repository.get_link_attempt.return_value = SimpleNamespace(
        attempt_id="attempt-1",
        device_code_encrypted=encrypted_code,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        interval_seconds=1,
        last_polled_at=None,
    )
    tokens = OAuthTokens(
        access_token="issued-access",
        refresh_token="issued-refresh",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    service._oauth.exchange_device_code = AsyncMock(return_value=tokens)
    service._oauth.userinfo = AsyncMock(return_value={"sub": "subject-1"})
    service._oauth.revoke = AsyncMock()
    aegra.search_assistants = AsyncMock(return_value=[])

    with pytest.raises(GatewayRequestError, match="could not be validated"):
        await service._check_link(source, principal_ref)

    service._oauth.revoke.assert_awaited_once_with("issued-refresh")
    repository.delete_link_attempt.assert_awaited_once_with("telegram", principal_ref)


@pytest.mark.asyncio
async def test_unlink_revokes_local_state_when_credential_is_corrupt(settings: GatewaySettings) -> None:
    link = _link()
    link.refresh_token_encrypted = b"corrupt"
    service, repository, _aegra = _service(settings, link)

    turn = await service.handle(_request("/unlink"))

    assert turn.text == "Your Telegram account has been unlinked from Aegra."
    repository.revoke_link.assert_awaited_once_with(link.link_id)
