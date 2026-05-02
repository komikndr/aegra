"""Utility & helper functions."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from math import ceil
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

_MODEL_CACHE_TTL_SECONDS = 10.0
_active_model_cache: dict[str, tuple[float, str | None]] = {}


def get_message_text(msg: BaseMessage) -> str:
    """Get the text content of a message."""
    content = msg.content
    if isinstance(content, str):
        return content
    elif isinstance(content, dict):
        return content.get("text", "")
    else:
        txts = [c if isinstance(c, str) else (c.get("text") or "") for c in content]
        return "".join(txts).strip()


def _extract_active_model_name(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            model_id = first.get("id")
            if isinstance(model_id, str) and model_id.strip():
                return model_id.strip()

    models = payload.get("models")
    if isinstance(models, list) and models:
        first = models[0]
        if isinstance(first, dict):
            for key in ("id", "model", "name"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    return None


def get_active_model_name(base_url: str | None = None) -> str | None:
    resolved_base_url = (base_url or os.environ.get("VLLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not resolved_base_url:
        return None

    now = time.time()
    cached = _active_model_cache.get(resolved_base_url)
    if cached and now - cached[0] < _MODEL_CACHE_TTL_SECONDS:
        return cached[1]

    request = urllib.request.Request(
        f"{resolved_base_url.rstrip('/')}/models",
        headers={"Accept": "application/json"},
    )
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    active_model: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 - env-configured endpoint
            payload = response.read().decode("utf-8")
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            active_model = _extract_active_model_name(parsed)
    except (
        TimeoutError,
        ValueError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
    ):
        active_model = None

    _active_model_cache[resolved_base_url] = (now, active_model)
    return active_model


def resolve_model_name(fully_specified_name: str) -> tuple[str, str]:
    if "/" not in fully_specified_name:
        return "openai", fully_specified_name

    provider, configured_model = fully_specified_name.split("/", maxsplit=1)
    if provider != "vllm":
        return provider, configured_model

    active_model = get_active_model_name()
    if configured_model.lower() in {"active", "current", "auto"}:
        return provider, active_model or configured_model

    if active_model and active_model != configured_model:
        return provider, active_model

    return provider, configured_model


def load_chat_model(fully_specified_name: str) -> BaseChatModel:
    """Load a chat model from a fully specified name.

    Args:
        fully_specified_name (str): String in the format 'provider/model'.
    """
    provider, model = resolve_model_name(fully_specified_name)

    if provider == "vllm":
        base_url = os.environ.get("VLLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        init_kwargs: dict[str, Any] = {
            "model_provider": "openai",
            "api_key": os.environ.get("OPENAI_API_KEY", ""),
        }
        if base_url:
            init_kwargs["base_url"] = base_url
        return init_chat_model(model, **init_kwargs)

    init_kwargs = {"model_provider": provider}
    if provider == "openai":
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            init_kwargs["base_url"] = base_url

    return init_chat_model(model, **init_kwargs)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return ceil(len(text) / 3)


def _estimate_message_tokens(message: Any) -> int:
    if isinstance(message, dict):
        return _estimate_text_tokens(str(message.get("content", ""))) + 8
    if isinstance(message, BaseMessage):
        return _estimate_text_tokens(get_message_text(message)) + 8
    return _estimate_text_tokens(str(message)) + 8


def _estimate_messages_tokens(messages: list[Any]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages) + 4


def _count_messages_tokens(model: Any, messages: list[Any]) -> int:
    get_num_tokens_from_messages = getattr(model, "get_num_tokens_from_messages", None)
    if callable(get_num_tokens_from_messages):
        try:
            token_count = get_num_tokens_from_messages(messages)
            if isinstance(token_count, int):
                return int(token_count)
            if isinstance(token_count, float):
                return int(token_count)
            if isinstance(token_count, str):
                return int(float(token_count))
            return _estimate_messages_tokens(messages)
        except Exception:
            return _estimate_messages_tokens(messages)
    return _estimate_messages_tokens(messages)


def build_token_limited_messages(
    model: Any,
    system_message: str | Sequence[Any],
    messages: Sequence[BaseMessage],
    *,
    num_limit_token: int,
    num_limit_response_reserve: int,
    num_limit_safety_buffer: int,
    num_limit_min_recent_messages: int,
) -> list[Any]:
    """Build model input messages constrained by a hard token budget.

    Prunes oldest messages first while preserving most recent turns.
    """
    hard_limit = max(1024, int(num_limit_token))
    reserve = max(0, int(num_limit_response_reserve))
    safety = max(0, int(num_limit_safety_buffer))
    min_recent = max(0, int(num_limit_min_recent_messages))

    input_budget = max(256, hard_limit - reserve - safety)
    system_messages = _normalize_system_messages(system_message)

    if not messages:
        return system_messages

    trimmed_messages = list(messages)
    payload: list[Any] = [*system_messages, *trimmed_messages]

    while len(trimmed_messages) > min_recent and _count_messages_tokens(model, payload) > input_budget:
        _drop_oldest_message_group(trimmed_messages)
        payload = [*system_messages, *trimmed_messages]

    while trimmed_messages and _count_messages_tokens(model, payload) > input_budget:
        _drop_oldest_message_group(trimmed_messages)
        payload = [*system_messages, *trimmed_messages]

    return payload


def build_system_prompt_messages(system_prompt: str, system_time: str, user_memory: str) -> list[SystemMessage]:
    stable_prompt = system_prompt.replace("\n\nSystem time: {system_time}", "").strip()
    content_parts = [stable_prompt]
    memory = user_memory.strip()
    if memory:
        content_parts.append(
            "Cross-agent user memory\n"
            "Use this stable cross-agent memory only when it is relevant. "
            "Do not mention this memory block unless the user asks.\n\n"
            f"{memory}"
        )
    content_parts.append(f"System time: {system_time}")
    return [SystemMessage(content="\n\n".join(content_parts))]


def _normalize_system_messages(system_message: str | Sequence[Any]) -> list[Any]:
    if isinstance(system_message, str):
        return [SystemMessage(content=system_message)]
    return list(system_message)


def _drop_oldest_message_group(messages: list[BaseMessage]) -> None:
    if not messages:
        return

    first_message = messages.pop(0)
    if not isinstance(first_message, AIMessage) or not first_message.tool_calls:
        while messages and isinstance(messages[0], ToolMessage):
            messages.pop(0)
        return

    remaining_tool_ids = {tool_call.get("id") for tool_call in first_message.tool_calls}
    remaining_tool_ids.discard(None)

    while messages and isinstance(messages[0], ToolMessage):
        tool_message = messages.pop(0)
        tool_call_id = getattr(tool_message, "tool_call_id", None)
        if tool_call_id in remaining_tool_ids:
            remaining_tool_ids.discard(tool_call_id)


def is_media_not_supported_error(error: Exception) -> bool:
    message = str(error).lower()
    media_terms = ("image", "vision", "media", "multimodal", "multi-modal")
    unsupported_terms = (
        "not support",
        "unsupported",
        "text-only",
        "text only",
        "invalid content",
        "invalid input",
        "cannot process",
    )
    return any(term in message for term in media_terms) and any(term in message for term in unsupported_terms)
