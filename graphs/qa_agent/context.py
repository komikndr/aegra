"""Context definitions for the QA agent."""

from dataclasses import dataclass, field

from kms_agent.context import Context as KmsContext

from qa_agent import prompts

PORTAL_TOKEN_LIMIT = 8192
PORTAL_OUTPUT_LIMIT = 1024
PORTAL_SAFETY_BUFFER = 128


@dataclass(kw_only=True)
class ChatContext(KmsContext):
    agent_id: str = field(default="qa_chat", metadata={"description": "Agent model routing ID."})
    docs_portal: bool = False
    docs_product: str = ""
    docs_release: str = ""

    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the QA chat interface."},
    )
