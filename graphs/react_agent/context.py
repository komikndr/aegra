"""Define the configurable parameters for the agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Annotated

from react_agent import prompts


def _resolve_default_model() -> str:
    model = os.environ.get("MODEL", "").strip()
    if "/" in model:
        return model
    return "openai/gpt-4o-mini"


def _parse_bool(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(kw_only=True)
class Context:
    """The context for the agent."""

    system_prompt: str = field(
        default=prompts.SYSTEM_PROMPT,
        metadata={
            "description": "The system prompt to use for the agent's interactions. "
            "This prompt sets the context and behavior for the agent."
        },
    )

    model: Annotated[str, {"__template_metadata__": {"kind": "llm"}}] = field(
        default_factory=_resolve_default_model,
        metadata={
            "description": "The name of the language model to use for the agent's main interactions. "
            "Should be in the form: provider/model-name."
        },
    )

    max_search_results: int = field(
        default=10,
        metadata={
            "description": "The maximum number of search results to return for each search query."
        },
    )

    num_limit_token: int = field(
        default=65536,
        metadata={
            "description": "Hard token window limit for model context. Messages are pruned before model invocation when this budget is exceeded."
        },
    )

    num_limit_response_reserve: int = field(
        default=4096,
        metadata={
            "description": "Reserved tokens for the model response, excluded from input context budget."
        },
    )

    num_limit_safety_buffer: int = field(
        default=1024,
        metadata={
            "description": "Extra safety margin removed from available input budget to avoid provider-specific tokenizer drift."
        },
    )

    num_limit_min_recent_messages: int = field(
        default=6,
        metadata={
            "description": "Minimum number of most recent non-system messages to keep before aggressive pruning."
        },
    )

    user_memory: str = field(
        default="",
        metadata={
            "description": "Stable cross-agent user memory injected by the server."
        },
    )

    def __post_init__(self) -> None:
        """Fetch env vars for attributes that were not passed as args."""
        for f in fields(self):
            if not f.init:
                continue

            if getattr(self, f.name) == f.default:
                raw_value = os.environ.get(f.name.upper())
                if raw_value is None:
                    continue

                if isinstance(f.default, bool):
                    setattr(self, f.name, _parse_bool(raw_value, f.default))
                elif isinstance(f.default, int):
                    try:
                        setattr(self, f.name, int(raw_value))
                    except ValueError:
                        setattr(self, f.name, f.default)
                else:
                    setattr(self, f.name, raw_value)
