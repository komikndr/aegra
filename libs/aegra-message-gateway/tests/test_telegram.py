import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aegra_message_gateway.chart_renderer import ChartRenderError
from aegra_message_gateway.service import GatewayTurn
from aegra_message_gateway.telegram import (
    CHART_RENDER_FAILURE,
    NO_VISIBLE_RESPONSE,
    TelegramGateway,
    UnauthorizedTelegramUserError,
)
from PIL import Image
from telegram import Message, Update
from telegram.error import TelegramError


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def test_gateway_disabled_without_token(settings) -> None:
    settings.telegram_bot_token = None
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    assert not gateway.enabled


def test_validated_image_mime_uses_actual_format(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    assert gateway._validated_image_mime(_png_bytes()) == "image/png"


@pytest.mark.asyncio
async def test_stream_reply_edits_final_message(settings) -> None:
    service = AsyncMock()
    gateway = TelegramGateway(settings=settings, service=service)
    sent = SimpleNamespace(edit_text=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=sent))

    async def stream():
        yield "hello"
        yield " world"

    await gateway._deliver_turn(cast(Message, original), GatewayTurn(stream=stream()))

    original.reply_text.assert_awaited_once_with("hello")
    sent.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_stream_reply_explains_successful_empty_output(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    sent = SimpleNamespace(edit_text=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=sent))

    async def stream():
        if False:
            yield ""

    await gateway._stream_reply(cast(Message, original), stream())

    original.reply_text.assert_awaited_once_with(NO_VISIBLE_RESPONSE)


@pytest.mark.asyncio
async def test_static_reply_is_split_without_truncation(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace()))
    text = "x" * 9_001

    await gateway._send_text(cast(Message, original), text)

    chunks = [call.args[0] for call in original.reply_text.await_args_list]
    assert [len(chunk) for chunk in chunks] == [4_000, 4_000, 1_001]
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_stream_reply_uses_accumulated_unedited_characters(settings) -> None:
    settings.telegram_stream_edit_interval_seconds = 0.25
    settings.telegram_stream_min_edit_chars = 5
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    sent = SimpleNamespace(edit_text=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=sent))

    async def stream():
        yield "a"
        await asyncio.sleep(0.3)
        yield "bc"
        yield "de"
        yield "f"
        yield "g"

    await gateway._stream_reply(cast(Message, original), stream())

    edits = [call.args[0] for call in sent.edit_text.await_args_list]
    assert "abcdef" in edits
    assert edits[-1] == "abcdefg"


@pytest.mark.asyncio
async def test_stream_reply_sends_every_long_output_segment(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    sent = [SimpleNamespace(edit_text=AsyncMock()) for _ in range(3)]
    original = SimpleNamespace(reply_text=AsyncMock(side_effect=sent))
    text = "z" * 8_500

    async def stream():
        yield text

    await gateway._stream_reply(cast(Message, original), stream())

    chunks = [call.args[0] for call in original.reply_text.await_args_list]
    assert "".join(chunks) == text
    assert [len(chunk) for chunk in chunks] == [4_000, 4_000, 500]


@pytest.mark.asyncio
async def test_stream_reply_sends_chart_as_png_without_raw_echarts_json(settings) -> None:
    service = AsyncMock()
    service.render_chart.return_value = _png_bytes()
    gateway = TelegramGateway(settings=settings, service=service)
    sent = SimpleNamespace(edit_text=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=sent), reply_photo=AsyncMock())

    async def stream():
        yield 'Before chart.\n```echarts\n{"series":[{"type":"bar","data":[1]}]}'
        yield '\n```\nAfter chart.'

    await gateway._stream_reply(cast(Message, original), stream())

    rendered = service.render_chart.await_args.args[0]
    assert rendered == {"series": [{"type": "bar", "data": [1]}]}
    original.reply_photo.assert_awaited_once()
    sent_text = "".join(call.args[0] for call in original.reply_text.await_args_list)
    assert "echarts" not in sent_text
    assert "Before chart." in sent_text
    assert "After chart." in sent_text


@pytest.mark.asyncio
async def test_stream_reply_replaces_invalid_chart_with_a_short_notice(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    original = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))

    async def stream():
        yield "```echarts\n{not-json}\n```"

    await gateway._stream_reply(cast(Message, original), stream())

    original.reply_text.assert_awaited_once_with(CHART_RENDER_FAILURE)


@pytest.mark.asyncio
async def test_stream_reply_keeps_a_fallback_when_chart_rendering_fails(settings) -> None:
    service = AsyncMock()
    service.render_chart.side_effect = ChartRenderError("renderer offline")
    gateway = TelegramGateway(settings=settings, service=service)
    original = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))

    async def stream():
        yield '```echarts\n{"series":[]}\n```'

    await gateway._stream_reply(cast(Message, original), stream())

    original.reply_text.assert_awaited_once_with(CHART_RENDER_FAILURE)


@pytest.mark.asyncio
async def test_stream_reply_keeps_a_fallback_when_telegram_rejects_chart_upload(settings) -> None:
    service = AsyncMock()
    service.render_chart.return_value = _png_bytes()
    gateway = TelegramGateway(settings=settings, service=service)
    sent = SimpleNamespace(edit_text=AsyncMock())
    original = SimpleNamespace(
        reply_text=AsyncMock(return_value=sent),
        reply_photo=AsyncMock(side_effect=TelegramError("upload rejected")),
    )

    async def stream():
        yield 'Before\n```echarts\n{"series":[]}\n```'

    await gateway._stream_reply(cast(Message, original), stream())

    original.reply_photo.assert_awaited_once()
    assert any(CHART_RENDER_FAILURE in call.args[0] for call in sent.edit_text.await_args_list)


@pytest.mark.asyncio
async def test_stop_cancels_active_conversation_task(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    started = asyncio.Event()

    async def active() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(active())
    await started.wait()
    gateway._active_tasks["conversation"] = task
    reply = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace()))

    await gateway._stop_active_turn("conversation", cast(Message, reply))

    assert task.cancelled()
    reply.reply_text.assert_awaited_once_with("Stopped the active response.")


@pytest.mark.asyncio
async def test_unauthorized_update_is_silently_dropped(settings) -> None:
    gateway = TelegramGateway(settings=settings, service=AsyncMock())
    gateway._build_inbound = AsyncMock(side_effect=UnauthorizedTelegramUserError)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)

    await gateway._handle_update(cast(Update, update), cast(Any, SimpleNamespace()))

    message.reply_text.assert_not_awaited()


def test_test_image_fixture_is_data_url() -> None:
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    assert encoded
