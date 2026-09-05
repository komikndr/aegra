import json

import httpx
import pytest
from aegra_message_gateway.aegra_client import AegraClient, _assistant_snapshot_deltas, _assistant_text_delta
from aegra_message_gateway.settings import GatewaySettings


def test_assistant_delta_reads_message_tuple() -> None:
    payload = json.dumps([{"type": "AIMessageChunk", "content": [{"type": "text", "text": "hello"}]}, {}])
    assert _assistant_text_delta(payload) == "hello"


def test_assistant_delta_drops_user_and_tool_messages() -> None:
    assert _assistant_text_delta(json.dumps([{"type": "human", "content": "secret"}, {}])) == ""
    assert _assistant_text_delta(json.dumps([{"type": "tool", "content": "tool output"}, {}])) == ""


def test_assistant_delta_ignores_malformed_payload() -> None:
    assert _assistant_text_delta("not-json") == ""


def test_partial_snapshots_emit_only_new_visible_assistant_text() -> None:
    snapshots: dict[str, str] = {}
    first = json.dumps(
        [
            {
                "id": "assistant-message-1",
                "type": "ai",
                "content": "Hello",
                "additional_kwargs": {"reasoning": "private chain of thought"},
            }
        ]
    )
    second = json.dumps(
        [
            {
                "id": "assistant-message-1",
                "type": "ai",
                "content": "Hello world",
                "additional_kwargs": {"reasoning": "more private reasoning"},
            }
        ]
    )

    assert _assistant_snapshot_deltas(first, snapshots) == ["Hello"]
    assert _assistant_snapshot_deltas(first, snapshots) == []
    assert _assistant_snapshot_deltas(second, snapshots) == [" world"]


def test_partial_snapshots_ignore_non_assistant_and_non_prefix_replacements() -> None:
    snapshots = {"assistant-message-1": "Original response"}
    payload = json.dumps(
        [
            {"id": "human-message-1", "type": "human", "content": "user input"},
            {"id": "assistant-message-1", "type": "ai", "content": "Replaced response"},
        ]
    )

    assert _assistant_snapshot_deltas(payload, snapshots) == []
    assert snapshots["assistant-message-1"] == "Replaced response"


@pytest.mark.asyncio
async def test_assistant_search_reads_every_page(settings: GatewaySettings) -> None:
    records = [
        {
            "assistant_id": f"assistant-{index}",
            "name": f"Assistant {index}",
            "graph_id": "casual_chat",
            "user_id": "system",
        }
        for index in range(230)
    ]
    offsets: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        offset = int(payload["offset"])
        limit = int(payload["limit"])
        offsets.append(offset)
        return httpx.Response(200, json=records[offset : offset + limit])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings.max_assistant_results = 250
    client = AegraClient(settings, http)

    assistants = await client.search_assistants("token", graph_id="casual_chat")

    assert len(assistants) == 230
    assert offsets == [0, 100, 200]
    await http.aclose()


@pytest.mark.asyncio
async def test_stream_run_emits_completed_message_without_duplicating_partial_snapshot(settings: GatewaySettings) -> None:
    body = "\n".join(
        [
            "event: messages/partial",
            'data: [{"id":"assistant-1","type":"ai","content":"Hello"}]',
            "",
            "event: messages/complete",
            'data: [{"id":"assistant-1","type":"ai","content":"Hello world"}]',
            "",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/threads/thread-1/runs/stream"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AegraClient(settings, http)
    received = [
        fragment
        async for fragment in client.stream_run(
            "token",
            thread_id="thread-1",
            assistant_id="assistant-1",
            message="Hello",
            platform="telegram",
        )
    ]

    assert received == ["Hello", " world"]
    await http.aclose()
