import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "graphs"))

from kms_agent import tools
from kms_agent.context import Context


def test_get_embeddings_prefers_dedicated_embedding_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "chat-key")
    tools._get_embeddings.cache_clear()

    with patch("kms_agent.tools.OpenAIEmbeddings") as mock_embeddings:
        tools._get_embeddings("text-embedding-3-small")

    mock_embeddings.assert_called_once_with(
        model="text-embedding-3-small",
        base_url="https://embedding.example/v1",
        api_key="embedding-key",
    )


def test_get_embeddings_falls_back_to_openai_env(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "chat-key")
    tools._get_embeddings.cache_clear()

    with patch("kms_agent.tools.OpenAIEmbeddings") as mock_embeddings:
        tools._get_embeddings("text-embedding-3-small")

    mock_embeddings.assert_called_once_with(
        model="text-embedding-3-small",
        base_url="https://chat.example/v1",
        api_key="chat-key",
    )


def _text_search_context(*, fields: str = "") -> Context:
    return Context(
        kms_opensearch_host="search.example",
        kms_opensearch_port="9200",
        kms_opensearch_user="kms-user",
        kms_opensearch_password="kms-password",
        kms_opensearch_index="documents",
        kms_opensearch_use_ssl=True,
        kms_opensearch_verify_certs=True,
        kms_opensearch_ssl_assert_hostname=True,
        kms_search_k=4,
        kms_text_search_fields=fields,
    )


def test_get_opensearch_client_uses_kms_connection_settings() -> None:
    context = _text_search_context()
    tools._get_opensearch_client.cache_clear()

    with patch("kms_agent.tools.OpenSearch") as mock_opensearch:
        tools.get_opensearch_client(context)

    mock_opensearch.assert_called_once_with(
        hosts=["https://search.example:9200"],
        http_auth=("kms-user", "kms-password"),
        use_ssl=True,
        verify_certs=True,
        ssl_assert_hostname=True,
    )
    tools._get_opensearch_client.cache_clear()


def test_text_search_uses_bm25_fields_and_serializes_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _text_search_context(fields="title^3, content, tags")
    client = MagicMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "doc-1",
                    "_index": "documents",
                    "_score": 7.5,
                    "_source": {"title": "OpenSearch guide", "content": "Native text search"},
                    "highlight": {"content": ["Native <em>text</em> search"]},
                }
            ]
        }
    }
    monkeypatch.setattr(tools, "get_runtime", lambda _: SimpleNamespace(context=context))
    monkeypatch.setattr(tools, "get_opensearch_client", lambda _: client)

    with patch("kms_agent.tools.OpenAIEmbeddings") as mock_embeddings:
        result = tools.kms_text_search("  native search  ", k=8)

    mock_embeddings.assert_not_called()
    client.search.assert_called_once_with(
        index="documents",
        body={
            "size": 8,
            "query": {
                "simple_query_string": {
                    "query": "native search",
                    "default_operator": "and",
                    "fields": ["title^3", "content", "tags"],
                }
            },
            "highlight": {"fields": {"*": {}}},
        },
    )
    assert result == {
        "available": True,
        "index": "documents",
        "query": "  native search  ",
        "matches": [
            {
                "id": "doc-1",
                "index": "documents",
                "score": 7.5,
                "source": {"title": "OpenSearch guide", "content": "Native text search"},
                "highlight": {"content": ["Native <em>text</em> search"]},
            }
        ],
    }


@pytest.mark.parametrize(("requested_k", "expected_size"), [(0, 1), (101, 100)])
def test_text_search_bounds_result_count(
    monkeypatch: pytest.MonkeyPatch,
    requested_k: int,
    expected_size: int,
) -> None:
    context = _text_search_context()
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    monkeypatch.setattr(tools, "get_runtime", lambda _: SimpleNamespace(context=context))
    monkeypatch.setattr(tools, "get_opensearch_client", lambda _: client)

    tools.kms_text_search("native search", k=requested_k)

    body = client.search.call_args.kwargs["body"]
    assert body["size"] == expected_size
    assert "fields" not in body["query"]["simple_query_string"]


def test_text_search_skips_opensearch_for_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _text_search_context()
    client_factory = MagicMock()
    monkeypatch.setattr(tools, "get_runtime", lambda _: SimpleNamespace(context=context))
    monkeypatch.setattr(tools, "get_opensearch_client", client_factory)

    result = tools.kms_text_search("   ")

    client_factory.assert_not_called()
    assert result == {
        "available": True,
        "index": "documents",
        "query": "   ",
        "matches": [],
    }


def test_text_search_reports_unavailable_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KMS_OPENSEARCH_HOST", "KMS_OPENSEARCH_USER", "KMS_OPENSEARCH_PASSWORD", "KMS_OPENSEARCH_INDEX"):
        monkeypatch.delenv(name, raising=False)
    context = Context()
    client_factory = MagicMock()
    monkeypatch.setattr(tools, "get_runtime", lambda _: SimpleNamespace(context=context))
    monkeypatch.setattr(tools, "get_opensearch_client", client_factory)

    result = tools.kms_text_search("native search")

    client_factory.assert_not_called()
    assert result["available"] is False
    assert result["matches"] == []


def test_kms_tools_register_vector_and_text_search() -> None:
    assert [tools.kms_vector_search, tools.kms_text_search] == tools.TOOLS
