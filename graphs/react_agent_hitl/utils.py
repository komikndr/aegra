"""Utility & helper functions."""

from langchain_core.messages import BaseMessage
from react_agent.utils import (
    build_token_limited_messages,
    is_media_not_supported_error,
    load_chat_model,
)

__all__ = [
    "get_message_text",
    "build_token_limited_messages",
    "is_media_not_supported_error",
    "load_chat_model",
]


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
