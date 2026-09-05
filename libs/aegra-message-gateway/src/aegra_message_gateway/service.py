"""User linking, assistant selection, and Aegra turn orchestration."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from aegra_message_gateway.aegra_client import AegraClient, AegraClientError
from aegra_message_gateway.chart_renderer import ChartRendererClient, ChartRenderError
from aegra_message_gateway.db import IdentityLink
from aegra_message_gateway.images import InvalidImageError, decode_images
from aegra_message_gateway.oauth import (
    AccessTokenCache,
    KeycloakDeviceClient,
    OAuthDeniedError,
    OAuthExpiredError,
    OAuthPendingError,
    OAuthSlowDownError,
    OAuthTokenError,
)
from aegra_message_gateway.repository import GatewayRepository
from aegra_message_gateway.schemas import AssistantRecord, TransportMessage, TransportSource
from aegra_message_gateway.security import ExternalIdentityHasher, SecretBox
from aegra_message_gateway.settings import GatewaySettings


class GatewayRequestError(Exception):
    pass


@dataclass(frozen=True)
class GatewayTurn:
    text: str | None = None
    stream: AsyncIterator[str] | None = None


class GatewayService:
    def __init__(
        self,
        *,
        settings: GatewaySettings,
        repository: GatewayRepository,
        oauth: KeycloakDeviceClient,
        aegra: AegraClient,
        chart_renderer: ChartRendererClient | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._oauth = oauth
        self._aegra = aegra
        self._chart_renderer = chart_renderer
        self._box = SecretBox(settings.encryption_key_bytes)
        self._hasher = ExternalIdentityHasher(settings.hmac_key_bytes)
        self._cache = AccessTokenCache()

    async def handle(self, request: TransportMessage) -> GatewayTurn:
        source = request.source
        self._validate_source(source)
        text = request.text
        image_urls = request.image_data_urls
        if len(text) > self._settings.max_message_chars:
            raise GatewayRequestError("message is too large")

        principal_ref = self._principal_ref(source)
        link = await self._repository.get_active_link(source.platform, principal_ref)
        command = text.strip()

        if command == "/link":
            return GatewayTurn(text=await self._begin_link(source, principal_ref, link))
        if command in {"/link status", "/link_status"}:
            return GatewayTurn(text=await self._check_link(source, principal_ref))
        if command == "/unlink":
            return GatewayTurn(text=await self._unlink(source, principal_ref, link))

        pending = await self._repository.get_link_attempt(source.platform, principal_ref)
        if link is None and pending is not None:
            status = await self._check_link(source, principal_ref)
            link = await self._repository.get_active_link(source.platform, principal_ref)
            if link is None:
                return GatewayTurn(text=status)
        if link is None:
            return GatewayTurn(text="This Telegram account is not linked. Send /link to connect your Aegra account.")

        try:
            access_token = await self._access_token(link)
        except (OAuthTokenError, ValueError):
            self._cache.remove(link.link_id)
            await self._repository.revoke_link(link.link_id)
            return GatewayTurn(text="Your Aegra link expired. Send /link to connect your account again.")
        except httpx.HTTPError as exc:
            raise GatewayRequestError("Aegra account authentication is temporarily unavailable") from exc
        if command == "/new":
            return GatewayTurn(text=await self._new_thread(source, link, access_token))
        if command == "/agent" or command.startswith("/agent "):
            return GatewayTurn(text=await self._agent_command(link, access_token, command))

        message_ref = self._message_ref(source, principal_ref)
        inbound = await self._repository.claim_inbound_message(
            platform=source.platform,
            message_ref=message_ref,
            link_id=link.link_id,
        )
        if inbound is None:
            return GatewayTurn(text="This message was already processed.")

        try:
            assistant = await self._selected_assistant(link, access_token)
            images = decode_images(image_urls)
            if images:
                analysis = await self._aegra.analyze_images(
                    access_token,
                    prompt=text[:8_000],
                    images=images,
                )
                text = (
                    f"{text or 'Please respond to the attached image.'}\n\n"
                    "<uploaded_image_context>\n"
                    f"{analysis}\n"
                    "</uploaded_image_context>"
                )
            conversation_ref = self._conversation_ref(source)
            mapping = await self._repository.get_or_create_conversation(
                link_id=link.link_id,
                conversation_ref=conversation_ref,
                assistant_id=assistant.assistant_id,
                graph_id=assistant.graph_id,
                thread_id=str(uuid4()),
            )
            await self._aegra.create_thread(
                access_token,
                thread_id=mapping.aegra_thread_id,
                assistant=assistant,
                platform=source.platform,
                principal_ref=principal_ref,
                conversation_ref=conversation_ref,
            )
        except asyncio.CancelledError:
            await self._repository.finish_inbound_message(inbound.inbound_id, success=False)
            raise
        except GatewayRequestError:
            await self._repository.finish_inbound_message(inbound.inbound_id, success=False)
            raise
        except (AegraClientError, InvalidImageError, OAuthTokenError, httpx.HTTPError, ValueError):
            await self._repository.finish_inbound_message(inbound.inbound_id, success=False)
            raise GatewayRequestError("The request could not be processed")

        async def _stream() -> AsyncIterator[str]:
            success = False
            try:
                async for delta in self._aegra.stream_run(
                    access_token,
                    thread_id=mapping.aegra_thread_id,
                    assistant_id=assistant.assistant_id,
                    message=text,
                    platform=source.platform,
                ):
                    yield delta
                success = True
            except (AegraClientError, httpx.HTTPError) as exc:
                raise GatewayRequestError("The Aegra run failed") from exc
            finally:
                await self._repository.finish_inbound_message(inbound.inbound_id, success=success)

        return GatewayTurn(stream=_stream())

    async def render_chart(self, option: dict[str, object]) -> bytes:
        if self._chart_renderer is None:
            raise ChartRenderError("chart rendering is unavailable")
        return await self._chart_renderer.render(option)

    async def _begin_link(self, source: TransportSource, principal_ref: str, link: IdentityLink | None) -> str:
        if link is not None:
            return "This Telegram account is already linked. Use /unlink before linking a different account."
        authorization = await self._oauth.begin()
        encrypted_code = self._box.encrypt(
            authorization.device_code,
            associated_data=self._attempt_aad(source.platform, principal_ref),
        )
        await self._repository.save_link_attempt(
            platform=source.platform,
            principal_ref=principal_ref,
            device_code_encrypted=encrypted_code,
            user_code=authorization.user_code,
            verification_uri=authorization.verification_uri,
            verification_uri_complete=authorization.verification_uri_complete,
            expires_at=authorization.expires_at,
            interval_seconds=authorization.interval_seconds,
        )
        target = authorization.verification_uri_complete or authorization.verification_uri
        return (
            "Open this URL to link your Aegra account:\n"
            f"{target}\n\n"
            f"Code: {authorization.user_code}\n"
            "After approving, send /link status or send your next message."
        )

    async def _check_link(self, source: TransportSource, principal_ref: str) -> str:
        attempt = await self._repository.get_link_attempt(source.platform, principal_ref)
        if attempt is None:
            active = await self._repository.get_active_link(source.platform, principal_ref)
            return "Your Aegra account is linked." if active else "No link is pending. Send /link first."
        now = datetime.now(UTC)
        if attempt.expires_at <= now:
            await self._repository.delete_link_attempt(source.platform, principal_ref)
            return "The link code expired. Send /link to start again."
        if attempt.last_polled_at and attempt.last_polled_at + timedelta(seconds=attempt.interval_seconds) > now:
            return "Authorization is still pending. Please wait a few seconds before checking again."
        await self._repository.mark_attempt_polled(attempt.attempt_id, now)
        try:
            device_code = self._box.decrypt(
                attempt.device_code_encrypted,
                associated_data=self._attempt_aad(source.platform, principal_ref),
            )
        except ValueError:
            await self._repository.delete_link_attempt(source.platform, principal_ref)
            return "The saved link request is no longer valid. Send /link to start again."
        try:
            tokens = await self._oauth.exchange_device_code(device_code)
        except OAuthPendingError:
            return "Authorization is still pending. Approve the request in your browser, then try again."
        except OAuthSlowDownError:
            return "Authorization is still pending. Please wait a little longer before checking again."
        except (OAuthExpiredError, OAuthDeniedError):
            await self._repository.delete_link_attempt(source.platform, principal_ref)
            return "The link request expired or was denied. Send /link to start again."
        try:
            userinfo = await self._oauth.userinfo(tokens.access_token)
            subject = str(userinfo["sub"])
            default_assistants = await self._aegra.search_assistants(tokens.access_token, graph_id="casual_chat")
            if not default_assistants:
                raise AegraClientError("default casual_chat assistant is unavailable")
        except (AegraClientError, OAuthTokenError, httpx.HTTPError, ValueError) as exc:
            with suppress(OAuthTokenError, httpx.HTTPError):
                await self._oauth.revoke(tokens.refresh_token)
            await self._repository.delete_link_attempt(source.platform, principal_ref)
            raise GatewayRequestError("The linked account could not be validated. Send /link to try again.") from exc
        encrypted_refresh = self._box.encrypt(
            tokens.refresh_token,
            associated_data=self._link_aad(source.platform, principal_ref),
        )
        link = await self._repository.activate_link(
            platform=source.platform,
            principal_ref=principal_ref,
            keycloak_subject=subject,
            refresh_token_encrypted=encrypted_refresh,
        )
        self._cache.put(link.link_id, tokens.access_token, tokens.expires_at)
        return "Your Telegram account is now linked to Aegra. The default agent is casual_chat."

    async def _unlink(
        self,
        source: TransportSource,
        principal_ref: str,
        link: IdentityLink | None,
    ) -> str:
        await self._repository.delete_link_attempt(source.platform, principal_ref)
        if link is None:
            return "This Telegram account is not linked."
        if link.refresh_token_encrypted:
            try:
                refresh = self._box.decrypt(
                    link.refresh_token_encrypted,
                    associated_data=self._link_aad(source.platform, principal_ref),
                )
                await self._oauth.revoke(refresh)
            except (OAuthTokenError, httpx.HTTPError, ValueError):
                pass
        self._cache.remove(link.link_id)
        await self._repository.revoke_link(link.link_id)
        return "Your Telegram account has been unlinked from Aegra."

    async def _access_token(self, link: IdentityLink) -> str:
        cached = self._cache.get(link.link_id)
        if cached:
            return cached
        async with self._cache.lock(link.link_id):
            cached = self._cache.get(link.link_id)
            if cached:
                return cached
            if link.refresh_token_encrypted is None:
                raise OAuthTokenError("linked identity has no renewable credential")
            refresh = self._box.decrypt(
                link.refresh_token_encrypted,
                associated_data=self._link_aad(link.platform, link.principal_ref),
            )
            tokens = await self._oauth.refresh(refresh)
            userinfo = await self._oauth.userinfo(tokens.access_token)
            if userinfo.get("sub") != link.keycloak_subject:
                raise OAuthTokenError("refreshed credential subject did not match the linked identity")
            encrypted = self._box.encrypt(
                tokens.refresh_token,
                associated_data=self._link_aad(link.platform, link.principal_ref),
            )
            await self._repository.rotate_refresh_token(link.link_id, encrypted)
            self._cache.put(link.link_id, tokens.access_token, tokens.expires_at)
            return tokens.access_token

    async def _agent_command(self, link: IdentityLink, access_token: str, command: str) -> str:
        assistants = self._telegram_assistants(await self._aegra.search_assistants(access_token))
        argument = command[len("/agent") :].strip()
        if not argument or argument == "list":
            try:
                current = await self._selected_assistant(link, access_token)
                current_label = f"{current.name} ({current.graph_id})"
            except GatewayRequestError:
                current_label = "unavailable"
            lines = [f"Current agent: {current_label}", "Available chat agents:"]
            lines.extend(f"- {item.name} ({item.graph_id}) | {item.assistant_id}" for item in assistants)
            return "\n".join(lines)
        if argument == "current":
            assistant = await self._selected_assistant(link, access_token)
            return f"Current agent: {assistant.name} ({assistant.graph_id}, {assistant.assistant_id})"
        matches = [item for item in assistants if item.assistant_id == argument]
        if not matches:
            matches = [item for item in assistants if item.graph_id == argument]
        if len(matches) != 1:
            return "Agent not found or ambiguous. Use /agent to list assistants you can access."
        selected = matches[0]
        await self._repository.select_assistant(link.link_id, selected.assistant_id, selected.graph_id)
        return f"Selected agent: {selected.name} ({selected.graph_id}). New messages will use its own Aegra thread."

    async def _new_thread(self, source: TransportSource, link: IdentityLink, access_token: str) -> str:
        assistant = await self._selected_assistant(link, access_token)
        await self._repository.delete_conversation(
            link_id=link.link_id,
            conversation_ref=self._conversation_ref(source),
            assistant_id=assistant.assistant_id,
        )
        return f"Started a new Telegram thread for {assistant.name} ({assistant.graph_id})."

    async def _selected_assistant(self, link: IdentityLink, access_token: str) -> AssistantRecord:
        if link.selected_assistant_id:
            assistants = self._telegram_assistants(await self._aegra.search_assistants(access_token))
            match = next((item for item in assistants if item.assistant_id == link.selected_assistant_id), None)
            if match is None:
                raise GatewayRequestError("The selected agent is no longer accessible. Use /agent to choose another.")
            return match
        matches = await self._aegra.search_assistants(access_token, graph_id="casual_chat")
        if not matches:
            raise GatewayRequestError("The default casual_chat agent is not available for this account.")
        selected = next((item for item in matches if item.user_id == "system"), matches[0])
        await self._repository.select_assistant(link.link_id, selected.assistant_id, selected.graph_id)
        link.selected_assistant_id = selected.assistant_id
        link.selected_graph_id = selected.graph_id
        return selected

    @staticmethod
    def _telegram_assistants(assistants: list[AssistantRecord]) -> list[AssistantRecord]:
        return [assistant for assistant in assistants if assistant.graph_id.endswith("_chat")]

    def _validate_source(self, source: TransportSource) -> None:
        if source.platform != "telegram":
            raise GatewayRequestError("Only Telegram is enabled in this release")
        if source.chat_type != "dm":
            raise GatewayRequestError("Only Telegram direct messages are enabled in this release")

    def _principal_ref(self, source: TransportSource) -> str:
        return self._hasher.digest(f"{source.platform}:principal", source.user_id)

    def _conversation_ref(self, source: TransportSource) -> str:
        value = f"{source.chat_id}\x00{source.thread_id or ''}\x00{source.session_id}"
        return self._hasher.digest(f"{source.platform}:conversation", value)

    def _message_ref(self, source: TransportSource, principal_ref: str) -> str:
        value = f"v1\x00{principal_ref}\x00{source.chat_id}\x00{source.thread_id or ''}\x00{source.message_id}"
        return self._hasher.digest(f"{source.platform}:message", value)

    @staticmethod
    def _attempt_aad(platform: str, principal_ref: str) -> str:
        return f"message-gateway:attempt:{platform}:{principal_ref}"

    @staticmethod
    def _link_aad(platform: str, principal_ref: str) -> str:
        return f"message-gateway:link:{platform}:{principal_ref}"
