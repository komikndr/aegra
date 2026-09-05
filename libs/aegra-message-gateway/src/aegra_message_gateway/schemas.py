"""Validated transport and gateway domain schemas."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TransportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["telegram", "whatsapp"]
    user_id: str = Field(min_length=1, max_length=256)
    chat_id: str = Field(min_length=1, max_length=256)
    chat_type: str = Field(default="dm", min_length=1, max_length=32)
    thread_id: str | None = Field(default=None, max_length=256)
    message_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)


class TransportMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: TransportSource
    text: str = Field(default="", max_length=20_000)
    image_data_urls: list[str] = Field(default_factory=list, max_length=4)


class AssistantRecord(BaseModel):
    assistant_id: str
    name: str
    description: str | None = None
    graph_id: str
    user_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
