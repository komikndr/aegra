"""Context definitions for the casual agent."""

from dataclasses import dataclass, field

from react_agent.context import Context as BaseContext

from casual_agent import prompts


@dataclass(kw_only=True)
class ChatContext(BaseContext):
    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the casual chat interface."},
    )


@dataclass(kw_only=True)
class ExecutiveContext(BaseContext):
    system_prompt: str = field(
        default=prompts.EXECUTIVE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the casual executive interface."},
    )


@dataclass(kw_only=True)
class ArtifactEditorContext(BaseContext):
    system_prompt: str = field(
        default=prompts.ARTIFACT_EDITOR_SYSTEM_PROMPT,
        metadata={
            "description": "System prompt for the casual artifact editor interface."
        },
    )
