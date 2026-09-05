"""Authenticated HTTP and SSE client for Aegra."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aegra_message_gateway.images import ValidatedImage
from aegra_message_gateway.schemas import AssistantRecord
from aegra_message_gateway.settings import GatewaySettings


class AegraClientError(Exception):
    pass


class AegraClient:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._owns_client = client is None

    async def search_assistants(self, access_token: str, *, graph_id: str | None = None) -> list[AssistantRecord]:
        assistants: list[AssistantRecord] = []
        page_size = 100
        while len(assistants) < self._settings.max_assistant_results:
            payload: dict[str, object] = {
                "limit": min(page_size, self._settings.max_assistant_results - len(assistants)),
                "offset": len(assistants),
                "sort_by": "name",
                "sort_order": "asc",
            }
            if graph_id is not None:
                payload["graph_id"] = graph_id
            response = await self._client.post(
                f"{self._settings.aegra_url}/assistants/search",
                headers=self._headers(access_token),
                json=payload,
            )
            self._raise_for_status(response)
            data = response.json()
            if not isinstance(data, list):
                raise AegraClientError("assistant search returned an invalid response")
            page = [AssistantRecord.model_validate(item) for item in data]
            assistants.extend(page)
            if len(page) < page_size:
                break
        return assistants

    async def health(self) -> None:
        response = await self._client.get(f"{self._settings.aegra_url}/health")
        self._raise_for_status(response)

    async def create_thread(
        self,
        access_token: str,
        *,
        thread_id: str,
        assistant: AssistantRecord,
        platform: str,
        principal_ref: str,
        conversation_ref: str,
    ) -> None:
        response = await self._client.post(
            f"{self._settings.aegra_url}/threads",
            headers=self._headers(access_token),
            json={
                "threadId": thread_id,
                "ifExists": "do_nothing",
                "metadata": {
                    "assistant_id": assistant.assistant_id,
                    "graph_id": assistant.graph_id,
                    "thread_name": f"{platform.title()} conversation",
                    "source": "message_gateway",
                    "platform": platform,
                    "external_principal_ref": principal_ref,
                    "external_conversation_ref": conversation_ref,
                },
            },
        )
        self._raise_for_status(response)

    async def analyze_images(
        self,
        access_token: str,
        *,
        prompt: str,
        images: list[ValidatedImage],
    ) -> str:
        files = [
            ("files", (f"image-{index}", image.data, image.mime_type)) for index, image in enumerate(images, start=1)
        ]
        response = await self._client.post(
            f"{self._settings.aegra_url}/custom/analyze-images",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"prompt": prompt},
            files=files,
        )
        self._raise_for_status(response)
        payload = response.json()
        if payload.get("supported") is not True or not isinstance(payload.get("analysis"), str):
            raise AegraClientError("image analysis is not supported by the configured model")
        return str(payload["analysis"]).strip()

    async def stream_run(
        self,
        access_token: str,
        *,
        thread_id: str,
        assistant_id: str,
        message: str,
        platform: str,
    ) -> AsyncIterator[str]:
        timeout = httpx.Timeout(self._settings.stream_timeout_seconds, connect=self._settings.request_timeout_seconds)
        async with self._client.stream(
            "POST",
            f"{self._settings.aegra_url}/threads/{thread_id}/runs/stream",
            headers=self._headers(access_token),
            json={
                "assistant_id": assistant_id,
                "input": {"messages": [{"role": "user", "content": message}]},
                "stream_mode": "messages",
                "on_disconnect": "cancel",
                "metadata": {"source": "message_gateway", "platform": platform},
            },
            timeout=timeout,
        ) as response:
            self._raise_for_status(response)
            event_name = ""
            data_lines: list[str] = []
            partial_snapshots: dict[str, str] = {}

            def consume_event() -> list[str]:
                if not data_lines:
                    return []
                payload = "\n".join(data_lines)
                if event_name == "error":
                    raise AegraClientError("Aegra run failed")
                if event_name in {"messages/partial", "messages/complete"}:
                    return _assistant_snapshot_deltas(payload, partial_snapshots)
                if event_name in {"messages", "messages-tuple"}:
                    content = _assistant_text_delta(payload)
                    return [content] if content else []
                return []

            async for line in response.aiter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line:
                    for content in consume_event():
                        yield content
                    event_name = ""
                    data_lines.clear()
            for content in consume_event():
                yield content

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise AegraClientError(f"Aegra request failed with status {response.status_code}")


def _assistant_text_delta(raw_payload: str) -> str:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return ""
    chunk: Any = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(chunk, dict):
        return ""
    message_type = str(chunk.get("type") or chunk.get("role") or "").lower()
    if message_type in {"human", "humanmessage", "user", "tool", "toolmessage"}:
        return ""
    content = chunk.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text: list[str] = []
    for block in content:
        if isinstance(block, str):
            text.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "text_delta"}:
            value = block.get("text") or block.get("content")
            if isinstance(value, str):
                text.append(value)
    return "".join(text)


def _assistant_snapshot_deltas(raw_payload: str, snapshots: dict[str, str]) -> list[str]:
    """Convert cumulative assistant snapshots into append-only visible text deltas."""
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return []
    messages = payload if isinstance(payload, list) else [payload]
    deltas: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        message_type = str(message.get("type") or message.get("role") or "").lower()
        if message_type not in {"ai", "aimessage", "aimessagechunk", "assistant"}:
            continue
        content = _message_text(message.get("content"))
        message_id = str(message.get("id") or f"partial-{index}")
        previous = snapshots.get(message_id, "")
        snapshots[message_id] = content
        if content.startswith(previous):
            delta = content[len(previous) :]
            if delta:
                deltas.append(delta)
    return deltas


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text: list[str] = []
    for block in content:
        if isinstance(block, str):
            text.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "text_delta"}:
            value = block.get("text") or block.get("content")
            if isinstance(value, str):
                text.append(value)
    return "".join(text)
