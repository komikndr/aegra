"""Capability selection and execution regression tests."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from react_agent.state import State

from analytic_agent import capabilities, graph
from analytic_agent.context import ChatContext


@pytest.fixture(autouse=True)
def clear_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANALYTIC_DB_URL",
        "ANALYTIC_DB_NAME",
        "ANALYTIC_DB_HOST",
        "ANALYTIC_SQL_ENABLED",
        "ANALYTIC_SEARCH_ENABLED",
        "ANALYTIC_VECTOR_SEARCH_ENABLED",
        "KMS_OPENSEARCH_HOST",
        "KMS_OPENSEARCH_INDEX",
        "KMS_OPENSEARCH_USER",
        "KMS_OPENSEARCH_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def configure_search(monkeypatch: pytest.MonkeyPatch) -> None:
    for field, value in {"HOST": "search.example", "INDEX": "documents", "USER": "user", "PASSWORD": "secret"}.items():
        monkeypatch.setenv(f"KMS_OPENSEARCH_{field}", value)


def test_unconfigured_deployment_has_no_tools() -> None:
    assert capabilities.get_tools(ChatContext()) == []


def test_sql_configuration_exposes_only_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYTIC_DB_URL", "sqlite:///:memory:")
    assert capabilities.get_tools(ChatContext()) == capabilities.SQL_TOOLS


def test_search_only_requires_no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_search(monkeypatch)
    assert [capabilities.analytic_text_search] == capabilities.get_tools(ChatContext())


def test_combined_capabilities_and_explicit_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_search(monkeypatch)
    monkeypatch.setenv("ANALYTIC_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ANALYTIC_VECTOR_SEARCH_ENABLED", "true")
    assert len(capabilities.get_tools(ChatContext())) == 5
    monkeypatch.setenv("ANALYTIC_SEARCH_ENABLED", "false")
    assert capabilities.get_tools(ChatContext()) == capabilities.SQL_TOOLS
    monkeypatch.setenv("ANALYTIC_SQL_ENABLED", "false")
    assert capabilities.get_tools(ChatContext()) == []


def test_partial_search_configuration_is_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_search(monkeypatch)
    monkeypatch.setenv("KMS_OPENSEARCH_INDEX", "  ")
    assert capabilities.get_tools(ChatContext()) == []


def test_search_uses_server_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_search(monkeypatch)
    search = MagicMock(return_value={"available": True, "matches": []})
    monkeypatch.setattr(capabilities, "search_text", search)
    capabilities.analytic_text_search("supplier", 3)
    context, query, count = search.call_args.args
    assert context.kms_opensearch_index == "documents"
    assert query == "supplier"
    assert count == 3


@pytest.mark.asyncio
async def test_disabled_tool_call_is_rejected() -> None:
    state = State(
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "analytic_text_search", "args": {"query": "test"}, "id": "call-1"},
                ],
            )
        ]
    )
    builder = StateGraph(State, context_schema=ChatContext)
    builder.add_node("tools", graph.call_tools)
    builder.add_edge("__start__", "tools")
    builder.add_edge("tools", "__end__")
    result = await builder.compile().ainvoke(state, context=ChatContext())
    assert result["messages"][-1].status == "error"


@pytest.mark.asyncio
async def test_no_sql_does_not_force_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="No sources connected."))
    monkeypatch.setattr(graph, "load_chat_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(graph, "apply_reasoning_effort", lambda model, *args, **kwargs: model)
    monkeypatch.setattr(graph, "build_token_limited_messages", lambda *args, **kwargs: [])
    binding = MagicMock()
    monkeypatch.setattr(graph, "bind_tools_with_parallel", binding)
    await graph.call_model(State(messages=[HumanMessage(content="List tables")]), Runtime(context=ChatContext()))
    binding.assert_not_called()
    model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_compiled_graph_executes_enabled_search(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_search(monkeypatch)
    monkeypatch.setattr(capabilities, "search_text", lambda *args: {"available": True, "matches": [{"id": "doc-1"}]})

    async def model_node(state: State, runtime: Runtime[ChatContext]) -> dict[str, Any]:
        if isinstance(state.messages[-1], HumanMessage):
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "analytic_text_search", "args": {"query": "supplier"}, "id": "call-1"},
                        ],
                    )
                ]
            }
        return {"messages": [AIMessage(content=state.messages[-1].text)]}

    monkeypatch.setattr(graph, "call_model", model_node)
    compiled = graph.build_graph(ChatContext, "capability-test")
    result = await compiled.ainvoke({"messages": [HumanMessage(content="Search suppliers")]}, context=ChatContext())
    assert "doc-1" in result["messages"][-1].text
