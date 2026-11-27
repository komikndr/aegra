import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime
from react_agent.state import State

from qa_agent import docs_search, graph
from qa_agent.context import ChatContext

KAFKA = {"title": "Kafka", "body": "Kafka example identifier: fff.", "url": "/docs/kafka/"}
HDFS = {
    "title": "ONYX Big Data Platform alpha-0.1.0",
    "body": "| HDFS | 3.4.2-0 | Installed | Apache Hadoop Distributed File System. |",
    "url": "/docs/components/onyx-big-data-platform/alpha-0.1.0/",
}


def test_search_keeps_product_and_release_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KMS_OPENSEARCH_URL", "https://search.invalid")
    monkeypatch.setenv("KMS_DOCS_OPENSEARCH_INDEX", "onyx-kms-docs")
    monkeypatch.setenv("KMS_DOCS_OPENSEARCH_USER", "")
    monkeypatch.setenv("KMS_DOCS_OPENSEARCH_PASSWORD", "")
    http = Mock()
    http.return_value.__enter__ = Mock(return_value=http)
    http.return_value.__exit__ = Mock(return_value=False)
    http.read.return_value = json.dumps({"hits": {"hits": [{"_source": HDFS}]}})
    monkeypatch.setattr(docs_search, "urlopen", http)
    assert docs_search.search_docs("its version?", "onyx", "alpha", "HDFS") == [HDFS]
    query = json.loads(http.call_args.args[0].data)["query"]["bool"]
    assert query["filter"] == [{"term": {"product": "onyx"}}, {"term": {"release": "alpha"}}]
    assert query["should"][1]["multi_match"]["query"] == "HDFS"


@pytest.mark.asyncio
@pytest.mark.parametrize(("question", "candidates", "answer", "sources"), [
    ("Hi!", [], "Hi! How can I help?", []),
    ("What is fff?", [KAFKA], '{"candidates":["Kafka example identifier [Source 1]"],"answer":"Could you clarify what fff means? [Source 9]"}', []),
    ("What HDFS version does ONYX document?", [KAFKA, HDFS], "HDFS 3.4.2-0 [Source 2].", [2]),
    ("What about its version?", [HDFS], "HDFS 3.4.2-0 [Source 1].", [1]),
    ("How does HDFS work?", None, "In general, HDFS stores files in blocks.", []),
])
async def test_portal_turn(
    monkeypatch: pytest.MonkeyPatch, question: str, candidates: list[dict[str, str]] | None,
    answer: str, sources: list[int],
) -> None:
    context = ChatContext(docs_portal=True, docs_product="onyx", docs_release="alpha")
    history = [HumanMessage(content="Tell me about HDFS"), AIMessage(content="HDFS is distributed storage.")]
    state = State(messages=[*history, HumanMessage(content=question)])
    search = Mock(return_value=candidates, side_effect=OSError if candidates is None else None)
    model = Mock(get_num_tokens_from_messages=None, ainvoke=AsyncMock(return_value=AIMessage(content=answer)))
    monkeypatch.setattr(graph, "search_docs", search)
    monkeypatch.setattr(graph, "load_chat_model", Mock(return_value=model))
    monkeypatch.setattr(graph, "apply_reasoning_effort", Mock(return_value=model))
    result = await graph.call_model(state, Runtime(context=context))
    search.assert_called_once_with(question, "onyx", "alpha", "Tell me about HDFS")
    model.ainvoke.assert_awaited_once()
    payload = model.ainvoke.call_args.args[0]
    assert payload[1:-1] == history
    assert "System time:" not in payload[0].content
    assert "Kafka example" not in payload[0].content
    assert json.loads(payload[-1].content)["question"] == question
    assert json.loads(payload[-1].content)["search"] == ("unavailable" if candidates is None else "searched")
    assert model.ainvoke.call_args.kwargs == {"max_tokens": 1024}
    response = result["messages"][-1]
    assert response.response_metadata["kms_docs"] == {
        "status": "answered" if sources else "uncited",
        "sources": [{"number": n, "title": HDFS["title"], "url": HDFS["url"]} for n in sources],
    }
    assert "[Source 9]" not in response.content
    assert "Kafka example identifier" not in str(result)
    assert ("Docs search is unavailable" in response.content) == (candidates is None)


@pytest.mark.asyncio
async def test_portal_limit_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    context = ChatContext(docs_portal=True)
    candidates = [HDFS, {**KAFKA, "body": "x" * 6000}]
    search = Mock(return_value=candidates)
    model = Mock(get_num_tokens_from_messages=None, ainvoke=AsyncMock(side_effect=lambda *a, **kw: AIMessage(content="OK")))
    counter = graph._PortalTokenCounter(model)
    monkeypatch.setattr(graph, "search_docs", search)
    recent = [HumanMessage(content="HDFS"), AIMessage(content="Details " + "a" * 1800)]
    old = [HumanMessage(content="old " + "a" * 4500), AIMessage(content="old answer")]

    async def ask(question: str, history: list[Any]) -> dict[str, list[AIMessage]]:
        return await graph._call_portal(State(messages=[*history, HumanMessage(content=question)]), context, model, counter, question)

    await ask("Version?", [*old, *recent])
    first = model.ainvoke.call_args.args[0]
    assert first[1:-1] == recent
    assert [doc["title"] for doc in json.loads(first[-1].content)["candidates"]] == [HDFS["title"]]
    assert counter.get_num_tokens_from_messages(first) + 1024 + 128 <= 8192
    await ask("And HDFS?", recent)
    second = model.ainvoke.call_args.args[0]
    assert second[:-1] == first[:-1]
    result = await ask("😀" * 2500, recent)
    assert "shorten" in str(result["messages"][-1].content)
    assert model.ainvoke.await_count == 2
    assert search.call_count == 2
