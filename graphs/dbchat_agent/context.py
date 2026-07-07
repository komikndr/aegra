"""Context definitions for the DBChat agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from react_agent.context import Context as BaseContext

from dbchat_agent import prompts


@dataclass(kw_only=True)
class Context(BaseContext):
    agent_id: str = field(default="analytic_dbchat", metadata={"description": "Agent model routing ID."})
    system_prompt: str = field(
        default=prompts.SYSTEM_PROMPT,
        metadata={"description": "System prompt for DBChat."},
    )

    analytic_db_url: str | None = field(default=None)
    analytic_db_dialect: str = field(default="sqlite")
    analytic_db_host: str | None = field(default=None)
    analytic_db_port: str | None = field(default=None)
    analytic_db_name: str | None = field(default=None)
    analytic_db_user: str | None = field(default=None)
    analytic_db_password: str | None = field(default=None)

    dbchat_context_mode: str = field(default="suggest")
    dbchat_selected_tables: list[dict[str, Any]] = field(default_factory=list)
