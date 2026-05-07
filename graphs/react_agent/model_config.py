"""Agent-specific model configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")
_CONFIG_CACHE: tuple[float | None, dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class ResolvedAgentModel:
    agent_id: str | None
    model: str
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = None
    response_reserve: int | None = None
    safety_buffer: int | None = None
    min_recent_messages: int | None = None


def _default_model() -> str:
    return os.environ.get("MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"


def _config_path() -> Path:
    return Path(os.environ.get("AGENT_MODELS_CONFIG", "agent-models.json"))


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        fallback = match.group(2) or ""
        return os.environ.get(name, fallback)

    return _ENV_PATTERN.sub(replace, value)


def _expand_config(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env(value)
    if isinstance(value, list):
        return [_expand_config(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_config(item) for key, item in value.items()}
    return value


def _load_config() -> dict[str, Any]:
    global _CONFIG_CACHE

    path = _config_path()
    try:
        stat = path.stat()
    except OSError:
        _CONFIG_CACHE = (None, {})
        return {}

    mtime = stat.st_mtime
    if _CONFIG_CACHE and _CONFIG_CACHE[0] == mtime:
        return _CONFIG_CACHE[1]

    try:
        with path.open(encoding="utf-8") as config_file:
            parsed = json.load(config_file)
    except (OSError, ValueError):
        parsed = {}

    config = _expand_config(parsed) if isinstance(parsed, dict) else {}
    _CONFIG_CACHE = (mtime, config)
    return config


def _entry(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _agent_entry(config: dict[str, Any], agent_id: str | None) -> dict[str, Any]:
    agents = config.get("agents")
    if not isinstance(agents, dict) or not agent_id:
        return {}
    value = agents.get(agent_id)
    return value if isinstance(value, dict) else {}


def _optional_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except ValueError:
                continue
    return None


def _api_key(entry: dict[str, Any], default: dict[str, Any]) -> str | None:
    api_key = _optional_string(entry.get("api_key"), default.get("api_key"))
    if api_key is not None:
        return api_key

    env_name = _optional_string(entry.get("api_key_env"), default.get("api_key_env"))
    if env_name is not None:
        return _optional_string(os.environ.get(env_name))

    return None


def resolve_agent_model(
    agent_id: str | None,
    *,
    context_model: str | None = None,
    context_window: int | None = None,
    response_reserve: int | None = None,
    safety_buffer: int | None = None,
    min_recent_messages: int | None = None,
) -> ResolvedAgentModel:
    """Resolve model settings for an agent, falling back to env defaults."""
    config = _load_config()
    default = _entry(config, "default")
    agent = _agent_entry(config, agent_id)

    model = (
        _optional_string(agent.get("model"), default.get("model"), context_model, _default_model()) or _default_model()
    )
    base_url = _optional_string(agent.get("base_url"), default.get("base_url"))

    return ResolvedAgentModel(
        agent_id=agent_id,
        model=model,
        base_url=base_url,
        api_key=_api_key(agent, default),
        context_window=_optional_int(agent.get("context_window"), default.get("context_window"), context_window),
        response_reserve=_optional_int(
            agent.get("response_reserve"), default.get("response_reserve"), response_reserve
        ),
        safety_buffer=_optional_int(agent.get("safety_buffer"), default.get("safety_buffer"), safety_buffer),
        min_recent_messages=_optional_int(
            agent.get("min_recent_messages"), default.get("min_recent_messages"), min_recent_messages
        ),
    )
