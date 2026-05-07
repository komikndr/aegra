"""Utility & helper functions."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from math import ceil
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

_MODEL_CACHE_TTL_SECONDS = 10.0
_active_model_cache: dict[str, tuple[float, str | None]] = {}
_vllm_base_url_cache: dict[str, tuple[float, bool]] = {}
_OPENAI_REASONING_PATCHED = False
_REASONING_BUDGET_TOKENS = {
    "none": 0,
    "low": 512,
    "medium": 1024,
    "high": 4096,
    "ultra": 12288,
}
_VLLM_REASONING_BUDGET_TOKENS = {
    "none": 0,
    "low": 128,
    "medium": 256,
    "high": 512,
    "ultra": 1024,
}
_REASONING_STORAGE_KEYS = {"reasoning_content", "reasoning", "thinking"}
_REASONING_CONTENT_TYPES = {"thinking", "reasoning", "reasoning_content"}


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


def _payload_looks_like_vllm(payload: dict[str, Any]) -> bool:
    data = payload.get("data")
    if isinstance(data, list):
        for model in data:
            if isinstance(model, dict) and str(model.get("owned_by", "")).lower() == "vllm":
                return True
    return False


def _fetch_models_payload(base_url: str, api_key: str | None = None) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Accept": "application/json"},
    )
    resolved_api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
    if resolved_api_key.strip():
        request.add_header("Authorization", f"Bearer {resolved_api_key.strip()}")

    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 - env-configured endpoint
            payload = response.read().decode("utf-8")
        parsed = json.loads(payload)
    except (
        TimeoutError,
        ValueError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
    ):
        return None

    return parsed if isinstance(parsed, dict) else None


def get_active_model_name(base_url: str | None = None, api_key: str | None = None) -> str | None:
    resolved_base_url = (base_url or os.environ.get("VLLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not resolved_base_url:
        return None

    now = time.time()
    cached = _active_model_cache.get(resolved_base_url)
    if cached and now - cached[0] < _MODEL_CACHE_TTL_SECONDS:
        return cached[1]

    parsed = _fetch_models_payload(resolved_base_url, api_key=api_key)
    active_model = _extract_active_model_name(parsed) if parsed else None

    _active_model_cache[resolved_base_url] = (now, active_model)
    return active_model


def is_vllm_base_url(base_url: str | None = None, api_key: str | None = None) -> bool:
    resolved_base_url = (base_url or os.environ.get("VLLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not resolved_base_url:
        return False

    now = time.time()
    cached = _vllm_base_url_cache.get(resolved_base_url)
    if cached and now - cached[0] < _MODEL_CACHE_TTL_SECONDS:
        return cached[1]

    parsed = _fetch_models_payload(resolved_base_url, api_key=api_key)
    is_vllm = _payload_looks_like_vllm(parsed) if parsed else False
    _vllm_base_url_cache[resolved_base_url] = (now, is_vllm)
    return is_vllm


def resolve_model_name(
    fully_specified_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    if "/" not in fully_specified_name:
        return "openai", fully_specified_name

    provider, configured_model = fully_specified_name.split("/", maxsplit=1)
    if provider != "vllm":
        return provider, configured_model

    active_model = get_active_model_name(base_url=base_url, api_key=api_key)
    if configured_model.lower() in {"active", "current", "auto"}:
        return provider, active_model or configured_model

    if active_model and active_model != configured_model:
        return provider, active_model

    return provider, configured_model


def load_chat_model(
    fully_specified_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BaseChatModel:
    """Load a chat model from a fully specified name.

    Args:
        fully_specified_name (str): String in the format 'provider/model'.
    """
    provider, model = resolve_model_name(fully_specified_name, base_url=base_url, api_key=api_key)

    if provider in {"openai", "vllm"}:
        _patch_openai_reasoning_conversion()

    if provider == "vllm":
        resolved_base_url = base_url or os.environ.get("VLLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        init_kwargs: dict[str, Any] = {
            "model_provider": "openai",
            "api_key": api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", ""),
        }
        if resolved_base_url:
            init_kwargs["base_url"] = resolved_base_url
        return init_chat_model(model, **init_kwargs)

    init_kwargs = {"model_provider": provider}
    if provider == "openai":
        resolved_base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        if resolved_base_url:
            init_kwargs["base_url"] = resolved_base_url
        if api_key is not None:
            init_kwargs["api_key"] = api_key

    return init_chat_model(model, **init_kwargs)


def _patch_openai_reasoning_conversion() -> None:
    """Preserve reasoning payloads dropped by ChatOpenAI converters."""
    global _OPENAI_REASONING_PATCHED
    if _OPENAI_REASONING_PATCHED:
        return

    try:
        from langchain_core.messages import AIMessage, AIMessageChunk
        from langchain_openai.chat_models import base as openai_base
    except Exception:
        return

    original_delta_converter = openai_base._convert_delta_to_message_chunk
    original_message_converter = openai_base._convert_dict_to_message

    def extract_reasoning(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("reasoning", "reasoning_content"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def convert_delta_with_reasoning(delta: Any, default_class: Any) -> Any:
        chunk = original_delta_converter(delta, default_class)
        reasoning = extract_reasoning(delta)
        if isinstance(chunk, AIMessageChunk) and reasoning:
            additional_kwargs = dict(chunk.additional_kwargs)
            additional_kwargs["reasoning"] = reasoning
            return chunk.model_copy(update={"additional_kwargs": additional_kwargs})
        return chunk

    def convert_message_with_reasoning(message: Any) -> Any:
        converted = original_message_converter(message)
        reasoning = extract_reasoning(message)
        if isinstance(converted, AIMessage) and reasoning:
            additional_kwargs = dict(converted.additional_kwargs)
            additional_kwargs["reasoning"] = reasoning
            return converted.model_copy(update={"additional_kwargs": additional_kwargs})
        return converted

    openai_base._convert_delta_to_message_chunk = convert_delta_with_reasoning
    openai_base._convert_dict_to_message = convert_message_with_reasoning
    _OPENAI_REASONING_PATCHED = True


def normalize_reasoning_effort(reasoning_effort: str | None) -> str:
    normalized = (reasoning_effort or "low").strip().lower()
    if normalized in _REASONING_BUDGET_TOKENS:
        return normalized
    return "low"


def _is_official_openai_base_url(base_url: str | None) -> bool:
    if not base_url:
        return True

    parsed = urllib.parse.urlparse(base_url.strip())
    return parsed.netloc.lower() == "api.openai.com" and parsed.path.rstrip("/") in {"", "/v1"}


def _reasoning_model_kwargs(
    fully_specified_name: str,
    reasoning_effort: str | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    effort = normalize_reasoning_effort(reasoning_effort)
    provider, _ = resolve_model_name(fully_specified_name, base_url=base_url, api_key=api_key)
    openai_base_url = base_url or os.environ.get("OPENAI_BASE_URL")

    if provider == "openai" and _is_official_openai_base_url(openai_base_url):
        # Chat Completions currently rejects both llama-style thinking budgets
        # and reasoning_effort for some OpenAI models deployed here. OpenAI also
        # does not return visible reasoning text, so avoid breaking generation.
        return {}

    if provider in {"openai", "vllm"} and is_vllm_base_url(openai_base_url, api_key=api_key):
        if effort == "none":
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        return {
            "extra_body": {
                "thinking_token_budget": _VLLM_REASONING_BUDGET_TOKENS[effort],
                "chat_template_kwargs": {"enable_thinking": True},
            }
        }

    if effort == "none":
        return {}

    if provider in {"openai", "vllm"}:
        return {"extra_body": {"thinking_budget_tokens": _REASONING_BUDGET_TOKENS[effort]}}

    return {}


def apply_reasoning_effort(
    model: Any,
    fully_specified_name: str,
    reasoning_effort: str | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    kwargs = _reasoning_model_kwargs(fully_specified_name, reasoning_effort, base_url=base_url, api_key=api_key)
    if not kwargs:
        return model
    return model.bind(**kwargs)


def _strip_inline_thinking_tags(text: str) -> str:
    first_close = text.find("</think>")
    first_open = text.find("<think>")
    if first_close != -1 and (first_open == -1 or first_close < first_open):
        return _strip_inline_thinking_tags(text[first_close + len("</think>") :])

    parts: list[str] = []
    cursor = 0

    while cursor < len(text):
        open_index = text.find("<think>", cursor)
        if open_index == -1:
            parts.append(text[cursor:])
            break

        parts.append(text[cursor:open_index])
        close_index = text.find("</think>", open_index + len("<think>"))
        if close_index == -1:
            break
        cursor = close_index + len("</think>")

    return "".join(parts)


def _strip_reasoning_from_content(content: Any) -> tuple[Any, bool]:
    if isinstance(content, str):
        stripped = _strip_inline_thinking_tags(content)
        return stripped, stripped != content

    if not isinstance(content, list):
        return content, False

    changed = False
    stripped_parts: list[Any] = []
    for part in content:
        if isinstance(part, str):
            stripped = _strip_inline_thinking_tags(part)
            changed = changed or stripped != part
            stripped_parts.append(stripped)
            continue

        if not isinstance(part, dict):
            stripped_parts.append(part)
            continue

        part_type = str(part.get("type", "")).lower()
        if part_type in _REASONING_CONTENT_TYPES:
            changed = True
            continue

        next_part = dict(part)
        for key in ("text", "content"):
            value = next_part.get(key)
            if isinstance(value, str):
                stripped = _strip_inline_thinking_tags(value)
                changed = changed or stripped != value
                next_part[key] = stripped
        stripped_parts.append(next_part)

    return stripped_parts, changed


def _message_has_reasoning(message: BaseMessage) -> bool:
    if not isinstance(message, AIMessage):
        return False

    for source in (message.additional_kwargs, getattr(message, "response_metadata", {})):
        if not isinstance(source, dict):
            continue
        for key in _REASONING_STORAGE_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                return True

    content = message.content
    if isinstance(content, str):
        lowered_content = content.lower()
        return "<think>" in lowered_content or "</think>" in lowered_content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                lowered_part = part.lower()
                if "<think>" in lowered_part or "</think>" in lowered_part:
                    return True
            if isinstance(part, dict):
                part_type = str(part.get("type", "")).lower()
                if part_type in _REASONING_CONTENT_TYPES:
                    return True
    return False


def _strip_reasoning_from_message(message: AIMessage) -> AIMessage:
    updates: dict[str, Any] = {}

    additional_kwargs = dict(message.additional_kwargs)
    for key in _REASONING_STORAGE_KEYS:
        additional_kwargs.pop(key, None)
    if additional_kwargs != message.additional_kwargs:
        updates["additional_kwargs"] = additional_kwargs

    response_metadata = dict(message.response_metadata)
    for key in _REASONING_STORAGE_KEYS:
        response_metadata.pop(key, None)
    if response_metadata != message.response_metadata:
        updates["response_metadata"] = response_metadata

    content, content_changed = _strip_reasoning_from_content(message.content)
    if content_changed:
        updates["content"] = content

    if not updates:
        return message
    return message.model_copy(update=updates)


def build_reasoning_storage_update(
    existing_messages: Sequence[BaseMessage], response: AIMessage, keep_latest: int = 2
) -> list[AIMessage]:
    """Return state updates that keep only the newest stored reasoning traces."""
    all_messages: list[BaseMessage] = [*existing_messages, response]
    reasoning_messages = [message for message in all_messages if message.id and _message_has_reasoning(message)]
    keep_ids = {message.id for message in reasoning_messages[-keep_latest:]}

    updates: list[AIMessage] = []
    for message in existing_messages:
        if not isinstance(message, AIMessage) or not message.id or message.id in keep_ids:
            continue
        if _message_has_reasoning(message):
            updates.append(_strip_reasoning_from_message(message))

    updates.append(response)
    return updates


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


def is_tool_choice_not_supported_error(error: Exception) -> bool:
    message = str(error).lower()
    return "auto" in message and "tool choice" in message and "tool-call-parser" in message
