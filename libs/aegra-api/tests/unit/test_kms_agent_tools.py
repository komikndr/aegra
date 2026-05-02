import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "graphs"))

from kms_agent import tools


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
