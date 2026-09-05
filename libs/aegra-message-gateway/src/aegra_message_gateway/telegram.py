"""Direct Telegram polling transport for the Aegra message gateway."""

import asyncio
import base64
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError
from telegram import Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from aegra_message_gateway.chart_output import ChartFragment, EChartsFenceExtractor, TextFragment
from aegra_message_gateway.chart_renderer import ChartRenderError
from aegra_message_gateway.schemas import TransportMessage, TransportSource
from aegra_message_gateway.service import GatewayRequestError, GatewayService, GatewayTurn
from aegra_message_gateway.settings import GatewaySettings

logger = logging.getLogger(__name__)

MAX_TELEGRAM_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 10_000
TELEGRAM_TEXT_CHUNK = 4_000
NO_VISIBLE_RESPONSE = "The agent completed without a visible response."
CHART_RENDER_FAILURE = "A chart was generated but could not be rendered."
SUPPORTED_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class UnauthorizedTelegramUserError(Exception):
    """An update from outside the configured transport allowlist."""


@dataclass(frozen=True)
class TelegramInbound:
    source: TransportSource
    text: str
    image_data_urls: list[str]
    reply: Message


@dataclass
class SentSegment:
    message: Message
    text: str


class TelegramGateway:
    def __init__(self, *, settings: GatewaySettings, service: GatewayService) -> None:
        self._settings = settings
        self._service = service
        self._application: Application | None = None
        self._allowed_users = settings.telegram_allowed_user_ids
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_request_at: dict[str, float] = {}
        self._rate_lock = asyncio.Lock()
        self._run_semaphore = asyncio.Semaphore(settings.telegram_max_concurrent_runs)

    @property
    def enabled(self) -> bool:
        return bool(self._settings.telegram_bot_token and self._allowed_users)

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return True
        application = self._application
        updater = application.updater if application is not None else None
        return bool(application is not None and application.running and updater is not None and updater.running)

    async def start(self) -> None:
        token = self._settings.telegram_bot_token
        if not token:
            logger.info("Telegram gateway disabled: MESSAGE_GATEWAY_TELEGRAM_BOT_TOKEN is not set")
            return
        if not self._allowed_users:
            logger.warning("Telegram gateway disabled: MESSAGE_GATEWAY_TELEGRAM_ALLOWED_USERS is empty")
            return
        application = (
            ApplicationBuilder()
            .token(token.get_secret_value())
            .concurrent_updates(self._settings.telegram_max_concurrent_updates)
            .build()
        )
        application.add_handler(MessageHandler(filters.ALL, self._handle_update))
        try:
            await application.initialize()
            await application.start()
            if application.updater is None:
                raise RuntimeError("Telegram updater was not initialized")
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        except BaseException:
            await self._shutdown_application(application)
            raise
        self._application = application
        logger.info("Telegram gateway polling started")

    async def stop(self) -> None:
        application = self._application
        self._application = None
        tasks = set(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()
        if application is not None:
            await self._shutdown_application(application)

    async def _shutdown_application(self, application: Application) -> None:
        updater = application.updater
        if updater is not None and updater.running:
            try:
                await asyncio.wait_for(updater.stop(), timeout=15)
            except Exception:
                logger.warning("Failed to stop Telegram updater cleanly", exc_info=True)
        if application.running:
            try:
                await asyncio.wait_for(application.stop(), timeout=15)
            except Exception:
                logger.warning("Failed to stop Telegram application cleanly", exc_info=True)
        try:
            await asyncio.wait_for(application.shutdown(), timeout=15)
        except Exception:
            logger.warning("Failed to shut down Telegram application cleanly", exc_info=True)

    async def _handle_update(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            inbound = await self._build_inbound(update)
        except UnauthorizedTelegramUserError:
            logger.warning("Dropped Telegram update from a user outside the allowlist")
            return
        except GatewayRequestError as exc:
            message = update.effective_message
            if message is not None:
                await self._send_text(message, str(exc))
            return
        except Exception:
            logger.exception("Failed to normalize Telegram update")
            message = update.effective_message
            if message is not None:
                await self._send_text(message, "This Telegram message could not be processed.")
            return

        conversation_key = self._conversation_key(inbound.source)
        if inbound.text.strip() == "/stop":
            await self._stop_active_turn(conversation_key, inbound.reply)
            return

        lock = self._conversation_locks.setdefault(conversation_key, asyncio.Lock())
        async with lock:
            await self._enforce_rate_limit(inbound.source.user_id)
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("Telegram update was not running in an asyncio task")
            self._active_tasks[conversation_key] = task
            try:
                await self._safe_chat_action(inbound.reply, ChatAction.TYPING)
                turn = await self._service.handle(
                    TransportMessage(source=inbound.source, text=inbound.text, image_data_urls=inbound.image_data_urls)
                )
                if turn.stream is None:
                    await self._deliver_turn(inbound.reply, turn)
                else:
                    async with self._run_semaphore:
                        await self._deliver_turn(inbound.reply, turn)
            except asyncio.CancelledError:
                raise
            except GatewayRequestError as exc:
                await self._send_text(inbound.reply, str(exc))
            except Exception:
                logger.exception("Unexpected Telegram gateway failure")
                await self._send_text(inbound.reply, "The message gateway could not process this request.")
            finally:
                if self._active_tasks.get(conversation_key) is task:
                    self._active_tasks.pop(conversation_key, None)

    async def _stop_active_turn(self, conversation_key: str, reply: Message) -> None:
        task = self._active_tasks.get(conversation_key)
        if task is None or task.done():
            await self._send_text(reply, "There is no active response to stop.")
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._send_text(reply, "Stopped the active response.")

    async def _enforce_rate_limit(self, user_id: str) -> None:
        interval = self._settings.telegram_min_request_interval_seconds
        if interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            wait_for = interval - (now - self._last_request_at.get(user_id, 0.0))
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at[user_id] = time.monotonic()

    async def _build_inbound(self, update: Update) -> TelegramInbound:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            raise GatewayRequestError("Unsupported Telegram update")
        if user.id not in self._allowed_users:
            raise UnauthorizedTelegramUserError
        if chat.type != ChatType.PRIVATE:
            raise GatewayRequestError("Only Telegram direct messages are enabled in this release.")
        text = message.text or message.caption or ""
        images = await self._download_images(message)
        source = TransportSource(
            platform="telegram",
            user_id=str(user.id),
            chat_id=str(chat.id),
            chat_type="dm",
            thread_id=str(message.message_thread_id) if message.message_thread_id else None,
            message_id=str(message.message_id),
            session_id=f"telegram:{user.id}",
        )
        return TelegramInbound(source=source, text=text, image_data_urls=images, reply=message)

    async def _download_images(self, message: Message) -> list[str]:
        photos = message.photo or []
        if not photos:
            return []
        selected = [photos[-1]]
        if len(selected) > MAX_TELEGRAM_IMAGES:
            raise GatewayRequestError("Too many images were attached.")
        data_urls: list[str] = []
        total = 0
        for photo in selected:
            if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
                raise GatewayRequestError("One attached image is too large.")
            file = await photo.get_file()
            buffer = BytesIO()
            await file.download_to_memory(out=buffer)
            data = buffer.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                raise GatewayRequestError("One attached image is too large.")
            total += len(data)
            if total > MAX_TOTAL_IMAGE_BYTES:
                raise GatewayRequestError("Attached images are too large in total.")
            mime = self._validated_image_mime(data)
            data_urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
        return data_urls

    @staticmethod
    def _validated_image_mime(data: bytes) -> str:
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
                image_format = image.format
                width, height = image.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise GatewayRequestError("Attached image format is not supported.") from exc
        if image_format not in SUPPORTED_IMAGE_FORMATS:
            raise GatewayRequestError("Attached image format is not supported.")
        if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
            raise GatewayRequestError("Attached image dimensions are too large.")
        return SUPPORTED_IMAGE_FORMATS[image_format]

    async def _deliver_turn(self, message: Message, turn: GatewayTurn) -> None:
        if turn.text is not None:
            await self._send_text(message, turn.text)
            return
        if turn.stream is None:
            await self._send_text(message, "Done.")
            return
        await self._stream_reply(message, turn.stream)

    async def _stream_reply(self, message: Message, stream: AsyncIterator[str]) -> None:
        sent: list[SentSegment] = []
        text = ""
        last_edit = 0.0
        unedited_chars = 0
        visible_output_sent = False
        charts = EChartsFenceExtractor()

        async def sync_text(*, force: bool = False) -> None:
            nonlocal last_edit, unedited_chars, visible_output_sent
            if not text:
                return
            now = time.monotonic()
            needs_new_segment = len(self._split_text(text)) > len(sent)
            if force or not sent or needs_new_segment or (
                now - last_edit >= self._settings.telegram_stream_edit_interval_seconds
                and unedited_chars >= self._settings.telegram_stream_min_edit_chars
            ):
                await self._sync_segments(message, sent, text)
                visible_output_sent = True
                last_edit = now
                unedited_chars = 0

        async def consume(fragment: TextFragment | ChartFragment) -> None:
            nonlocal text, unedited_chars, visible_output_sent, sent
            if isinstance(fragment, TextFragment):
                text += fragment.text
                unedited_chars += len(fragment.text)
                await sync_text()
                return
            await sync_text(force=True)
            if fragment.option is None:
                text += CHART_RENDER_FAILURE
                unedited_chars += len(CHART_RENDER_FAILURE)
                return
            try:
                image = await self._service.render_chart(fragment.option)
                if await self._safe_chart(message, image):
                    visible_output_sent = True
                    text = ""
                    sent = []
                    unedited_chars = 0
                else:
                    text += CHART_RENDER_FAILURE
                    unedited_chars += len(CHART_RENDER_FAILURE)
            except ChartRenderError:
                logger.warning("Chart renderer rejected an ECharts option", exc_info=True)
                text += CHART_RENDER_FAILURE
                unedited_chars += len(CHART_RENDER_FAILURE)

        try:
            async for delta in stream:
                for fragment in charts.feed(delta):
                    await consume(fragment)
            for fragment in charts.feed("", final=True):
                await consume(fragment)
            if text:
                await sync_text(force=True)
            elif not visible_output_sent:
                await self._sync_segments(message, sent, NO_VISIBLE_RESPONSE)
        except asyncio.CancelledError:
            raise
        except GatewayRequestError:
            error_text = "The Aegra run could not be completed."
            combined = f"{text}\n\n{error_text}".strip()
            await self._sync_segments(message, sent, combined)
        finally:
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()

    async def _sync_segments(self, reply_to: Message, sent: list[SentSegment], text: str) -> None:
        chunks = self._split_text(text)
        for index, chunk in enumerate(chunks):
            if index < len(sent):
                segment = sent[index]
                if segment.text != chunk:
                    await self._safe_edit(segment.message, chunk)
                    segment.text = chunk
                continue
            sent_message = await self._safe_reply(reply_to, chunk)
            if sent_message is None:
                return
            sent.append(SentSegment(message=sent_message, text=chunk))

    async def _send_text(self, message: Message, text: str) -> None:
        for chunk in self._split_text(text or "Done."):
            await self._safe_reply(message, chunk)

    @staticmethod
    def _split_text(text: str) -> list[str]:
        return [text[index : index + TELEGRAM_TEXT_CHUNK] for index in range(0, len(text), TELEGRAM_TEXT_CHUNK)] or [""]

    @staticmethod
    def _conversation_key(source: TransportSource) -> str:
        return f"{source.platform}:{source.user_id}:{source.chat_id}:{source.thread_id or ''}"

    @staticmethod
    async def _safe_reply(message: Message, text: str) -> Message | None:
        try:
            return await message.reply_text(text or "Done.")
        except TelegramError:
            logger.exception("Failed to send Telegram reply")
            return None

    @staticmethod
    async def _safe_edit(message: Message, text: str) -> None:
        try:
            await message.edit_text(text or "Done.")
        except TelegramError:
            logger.debug("Failed to edit Telegram message", exc_info=True)

    @staticmethod
    async def _safe_chart(message: Message, image: bytes) -> bool:
        try:
            await message.reply_photo(photo=BytesIO(image), filename="chart.png")
            return True
        except TelegramError:
            logger.exception("Failed to send Telegram chart")
            return False

    @staticmethod
    async def _safe_chat_action(message: Message, action: str) -> None:
        try:
            await message.chat.send_action(action=action)
        except TelegramError:
            logger.debug("Failed to send Telegram chat action", exc_info=True)
