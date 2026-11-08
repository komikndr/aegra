"""Context definitions for the analytic agent."""

from dataclasses import dataclass, field

from react_agent.context import Context as BaseContext

from analytic_agent import prompts


@dataclass(kw_only=True)
class Context(BaseContext):
    """Request options. SQL connection settings come only from the server environment."""


@dataclass(kw_only=True)
class ChatContext(Context):
    agent_id: str = field(default="analytic_chat", metadata={"description": "Agent model routing ID."})

    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic chat interface."},
    )


@dataclass(kw_only=True)
class ExecutiveContext(Context):
    agent_id: str = field(default="analytic_executive", metadata={"description": "Agent model routing ID."})

    system_prompt: str = field(
        default=prompts.EXECUTIVE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic executive interface."},
    )


@dataclass(kw_only=True)
class OfficeContext(Context):
    agent_id: str = field(default="analytic_office", metadata={"description": "Agent model routing ID."})

    system_prompt: str = field(
        default=prompts.OFFICE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic docs builder interface."},
    )


@dataclass(kw_only=True)
class ArtifactEditorContext(Context):
    agent_id: str = field(default="analytic_artifact_editor", metadata={"description": "Agent model routing ID."})

    system_prompt: str = field(
        default=prompts.ARTIFACT_EDITOR_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the analytic artifact editor interface."},
    )
