"""Context definitions for the QA agent."""

from dataclasses import dataclass, field

from kms_agent.context import Context as KmsContext

from qa_agent import prompts


@dataclass(kw_only=True)
class ChatContext(KmsContext):
    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the QA chat interface."},
    )
