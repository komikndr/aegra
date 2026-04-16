"""Tools for the KMS agent."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langgraph.runtime import get_runtime

from kms_agent.context import Context


def _build_opensearch_url(context: Context) -> str | None:
    host = (context.kms_opensearch_host or "").strip()
    if not host:
        return None
    if "://" in host:
        return host

    scheme = "https" if context.kms_opensearch_use_ssl else "http"
    port = f":{context.kms_opensearch_port}" if context.kms_opensearch_port else ""
    return f"{scheme}://{host}{port}"


def _kms_configured(context: Context) -> bool:
    return bool(
        context.kms_opensearch_index
        and context.kms_opensearch_host
        and context.kms_opensearch_user
        and context.kms_opensearch_password
    )


@lru_cache(maxsize=8)
def _get_embeddings(model_name: str) -> OpenAIEmbeddings:
    init_kwargs: dict[str, str] = {"model": model_name}

    base_url = os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        init_kwargs["base_url"] = base_url

    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        init_kwargs["api_key"] = api_key

    return OpenAIEmbeddings(**init_kwargs)


@lru_cache(maxsize=8)
def _get_vector_store(
    opensearch_url: str,
    index_name: str,
    username: str,
    password: str,
    embedding_model: str,
    use_ssl: bool,
    verify_certs: bool,
    ssl_assert_hostname: bool,
) -> OpenSearchVectorSearch:
    return OpenSearchVectorSearch(
        opensearch_url=opensearch_url,
        index_name=index_name,
        embedding_function=_get_embeddings(embedding_model),
        http_auth=(username, password),
        use_ssl=use_ssl,
        verify_certs=verify_certs,
        ssl_assert_hostname=ssl_assert_hostname,
    )


def _vector_store_from_runtime() -> OpenSearchVectorSearch | None:
    runtime = get_runtime(Context)
    context = runtime.context
    if not _kms_configured(context):
        return None

    opensearch_url = _build_opensearch_url(context)
    if opensearch_url is None:
        return None

    return _get_vector_store(
        opensearch_url,
        context.kms_opensearch_index or "",
        context.kms_opensearch_user or "",
        context.kms_opensearch_password or "",
        context.kms_embedding_model,
        context.kms_opensearch_use_ssl,
        context.kms_opensearch_verify_certs,
        context.kms_opensearch_ssl_assert_hostname,
    )


def _kms_unavailable_message() -> str:
    return (
        "KMS vector store is not configured. Set the KMS_OPENSEARCH_* env vars first."
    )


def _serialize_documents(documents: list[Document]) -> list[dict[str, Any]]:
    return [
        {
            "content": document.page_content,
            "metadata": document.metadata,
        }
        for document in documents
    ]


async def kms_vector_search(query: str, k: int | None = None) -> dict[str, Any]:
    """Run vector similarity search against the configured KMS OpenSearch index."""
    runtime = get_runtime(Context)
    vector_store = _vector_store_from_runtime()
    if vector_store is None:
        return {
            "available": False,
            "message": _kms_unavailable_message(),
            "matches": [],
        }

    top_k = k or runtime.context.kms_search_k
    documents = vector_store.similarity_search(query, k=top_k)
    return {
        "available": True,
        "index": runtime.context.kms_opensearch_index,
        "query": query,
        "matches": _serialize_documents(documents),
    }


TOOLS: list[Callable[..., Any]] = [kms_vector_search]
