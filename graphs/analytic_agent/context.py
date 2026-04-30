"""Context definitions for the analytic agent."""

from dataclasses import dataclass, field

from react_agent.context import Context as BaseContext

from analytic_agent import prompts


@dataclass(kw_only=True)
class Context(BaseContext):
    analytic_db_url: str | None = field(
        default=None,
        metadata={
            "description": "Optional SQLAlchemy database URL for the analytic agent. If provided, it takes precedence over the individual DB connection fields."
        },
    )
    analytic_db_dialect: str = field(
        default="sqlite",
        metadata={
            "description": "Database dialect for the analytic agent when building a connection URL from separate fields."
        },
    )
    analytic_db_host: str | None = field(
        default=None,
        metadata={"description": "Database host for the analytic agent."},
    )
    analytic_db_port: str | None = field(
        default=None,
        metadata={"description": "Database port for the analytic agent."},
    )
    analytic_db_name: str | None = field(
        default=None,
        metadata={"description": "Database name for the analytic agent."},
    )
    analytic_db_user: str | None = field(
        default=None,
        metadata={"description": "Database username for the analytic agent."},
    )
    analytic_db_password: str | None = field(
        default=None,
        metadata={"description": "Database password for the analytic agent."},
    )


@dataclass(kw_only=True)
class ChatContext(Context):
    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic chat interface."},
    )


@dataclass(kw_only=True)
class ExecutiveContext(Context):
    system_prompt: str = field(
        default=prompts.EXECUTIVE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic executive interface."},
    )


@dataclass(kw_only=True)
class OfficeContext(Context):
    system_prompt: str = field(
        default=prompts.OFFICE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic docs builder interface."},
    )


@dataclass(kw_only=True)
class ArtifactEditorContext(Context):
    system_prompt: str = field(
        default=prompts.ARTIFACT_EDITOR_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic artifact editor interface."},
    )
