"""Head routing keeps identity server-owned and forwards user text unchanged."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from head_coordinator import graph as head_graph
from head_coordinator import tools
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from react_agent.context import Context
from react_agent.state import State


@pytest.mark.parametrize("identity", [None, {}, {"identity": "alice", "permissions": ["Executive"]}])
def test_head_cannot_authorize_itself_from_model_input(monkeypatch: pytest.MonkeyPatch, identity: object) -> None:
    monkeypatch.setattr(tools, "get_config", lambda: {"configurable": {"langgraph_auth_user": identity, "user_id": "admin"}})
    with pytest.raises(HTTPException):
        tools.authenticated_user()


@pytest.mark.asyncio
async def test_connect_injects_exact_current_message_and_call_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "get_config", lambda: {"configurable": {"langgraph_auth_user": {"identity": "alice", "permissions": ["SuperAdmin"]}}})
    delegate = AsyncMock(return_value={"status": "completed", "turn_id": "turn", "result": {"receipt": {"status": "applied"}}})
    monkeypatch.setattr(tools, "delegate_message", delegate)
    current = "Please pause the Supply objective."
    builder = StateGraph(State)
    builder.add_node("tools", ToolNode(tools.TOOLS))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = await builder.compile().ainvoke({"messages": [HumanMessage(content="Old context"), HumanMessage(content=current),
        AIMessage(content="", tool_calls=[{"id": "call-123", "name": "connect_objective", "args": {"objective_id": "supply"}}])]})
    assert "applied" in result["messages"][-1].content
    args = delegate.call_args.args
    assert args[0].identity == "alice"
    assert args[1:] == ("supply", current, "call-123")
    schema = tools.connect_objective.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"objective_id"}


@pytest.mark.asyncio
async def test_natural_reply_requires_no_objective_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    model = MagicMock()
    model.model_copy.return_value = model
    model.bind_tools.return_value = model
    model.ainvoke = AsyncMock(return_value=AIMessage(content="Hello! What would you like to work on?"))
    monkeypatch.setattr(head_graph, "resolve_agent_model", lambda name: SimpleNamespace(model="fixture", base_url=None, api_key=None))
    monkeypatch.setattr(head_graph, "load_chat_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(head_graph, "apply_reasoning_effort", lambda value, *args, **kwargs: value)
    monkeypatch.setattr(head_graph, "build_token_limited_messages", lambda model, system, messages, **kwargs: [*system, *messages])
    result = await head_graph.call_model(State(messages=[HumanMessage(content="Hello")]), SimpleNamespace(context=Context()))
    assert result["messages"][0].content.startswith("Hello")
    assert result["messages"][0].tool_calls == []


@pytest.mark.asyncio
async def test_tool_budget_rejects_parallel_side_effects() -> None:
    calls = [{"id": str(i), "name": "connect_objective", "args": {"objective_id": "supply"}} for i in range(2)]
    result = await head_graph.call_tools(State(messages=[HumanMessage(content="Hi"), AIMessage(content="", tool_calls=calls)]))
    assert len(result["messages"]) == 2
    assert all("one tool" in item.content for item in result["messages"])
