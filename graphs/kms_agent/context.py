"""Context definitions for the KMS agent."""

from dataclasses import dataclass, field

from react_agent.context import Context as BaseContext

from kms_agent import prompts


@dataclass(kw_only=True)
class Context(BaseContext):
    kms_opensearch_host: str | None = field(
        default=None,
        metadata={"description": "OpenSearch host for the KMS agent."},
    )
    kms_opensearch_port: str = field(
        default="9200",
        metadata={"description": "OpenSearch port for the KMS agent."},
    )
    kms_opensearch_user: str | None = field(
        default=None,
        metadata={"description": "OpenSearch username for the KMS agent."},
    )
    kms_opensearch_password: str | None = field(
        default=None,
        metadata={"description": "OpenSearch password for the KMS agent."},
    )
    kms_opensearch_index: str | None = field(
        default=None,
        metadata={"description": "Vector index name for the KMS agent."},
    )
    kms_opensearch_use_ssl: bool = field(
        default=True,
        metadata={
            "description": "Whether the KMS OpenSearch connection should use SSL."
        },
    )
    kms_opensearch_verify_certs: bool = field(
        default=True,
        metadata={
            "description": "Whether TLS certificates should be verified for the KMS OpenSearch connection."
        },
    )
    kms_opensearch_ssl_assert_hostname: bool = field(
        default=True,
        metadata={
            "description": "Whether TLS hostname verification should be enforced for the KMS OpenSearch connection."
        },
    )
    kms_embedding_model: str = field(
        default="text-embedding-3-small",
        metadata={"description": "Embedding model used for KMS vector retrieval."},
    )
    kms_search_k: int = field(
        default=4,
        metadata={
            "description": "Default number of vector search results returned by the KMS agent."
        },
    )


@dataclass(kw_only=True)
class ChatContext(Context):
    system_prompt: str = field(
        default=prompts.CHAT_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the KMS chat interface."},
    )


@dataclass(kw_only=True)
class ExecutiveContext(Context):
    system_prompt: str = field(
        default=prompts.EXECUTIVE_SYSTEM_PROMPT,
        metadata={"description": "System prompt for the KMS executive interface."},
    )


@dataclass(kw_only=True)
class ArtifactEditorContext(Context):
    system_prompt: str = field(
        default=prompts.ARTIFACT_EDITOR_SYSTEM_PROMPT,
        metadata={
            "description": "System prompt for the KMS artifact editor interface."
        },
    )
