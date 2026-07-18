"""Tests for bind_tools_with_parallel helper and parallel tool execution."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from analytic_agent.tools import analytic_describe_table, analytic_list_tables, analytic_query_sql
from dbchat_agent.tools import dbchat_describe_table, dbchat_list_tables, dbchat_query_sql
from kms_agent.tools import kms_vector_search
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, ToolRuntime
from langgraph.runtime import get_runtime

from . import utils


@dataclass(frozen=True)
class _TestContext:
    marker: str


def _discard_stream(_: Any) -> None:
    pass


def _make_tool_runtime(context: Any = None) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=_discard_stream,
        tool_call_id=None,
        store=None,
    )


def test_bind_tools_with_parallel_sets_flag_and_returns_bound() -> None:
    """Assert bind_tools is called once with parallel_tool_calls=True and helper returns the bound runnable."""
    mock_model = MagicMock(spec=BaseChatModel)
    sentinel: Runnable[Any, Any] = MagicMock(spec=Runnable)
    mock_model.bind_tools.return_value = sentinel

    def _tool_a() -> str:
        """Tool A."""
        return "a"

    def _tool_b() -> str:
        """Tool B."""
        return "b"

    tools = [_tool_a, _tool_b]
    result = utils.bind_tools_with_parallel(mock_model, tools)

    mock_model.bind_tools.assert_called_once_with(tools, parallel_tool_calls=True)
    assert result is sentinel


@pytest.mark.asyncio
async def test_sync_tools_execute_in_parallel_through_tool_node() -> None:
    """Prove ToolNode dispatches two synchronous tools concurrently via threading.Barrier."""
    barrier = threading.Barrier(2)
    thread_ids: list[int] = []
    runtime_markers: list[str] = []
    results: dict[str, str] = {}

    @tool
    def tool_a() -> str:
        """Tool A."""
        thread_ids.append(threading.get_ident())
        runtime_markers.append(get_runtime(_TestContext).context.marker)
        barrier.wait(timeout=5)
        results["a"] = "a_done"
        return "a_done"

    @tool
    def tool_b() -> str:
        """Tool B."""
        thread_ids.append(threading.get_ident())
        runtime_markers.append(get_runtime(_TestContext).context.marker)
        barrier.wait(timeout=5)
        results["b"] = "b_done"
        return "b_done"

    node = ToolNode([tool_a, tool_b])

    ai_message = AIMessage(
        content="",
        tool_calls=[
            {"name": "tool_a", "args": {}, "id": "call_a", "type": "tool_call"},
            {"name": "tool_b", "args": {}, "id": "call_b", "type": "tool_call"},
        ],
    )

    runtime = _make_tool_runtime(_TestContext(marker="available"))
    config: RunnableConfig = {"configurable": {"__pregel_runtime": runtime}}

    result = await asyncio.wait_for(node.ainvoke({"messages": [ai_message]}, config=config), timeout=10)
    tool_messages: list[ToolMessage] = result["messages"]

    assert len(tool_messages) == 2
    by_id = {tm.tool_call_id: tm for tm in tool_messages}
    assert by_id["call_a"].name == "tool_a"
    assert by_id["call_a"].content == "a_done"
    assert by_id["call_b"].name == "tool_b"
    assert by_id["call_b"].content == "b_done"
    assert results == {"a": "a_done", "b": "b_done"}
    assert runtime_markers == ["available", "available"]
    assert len(set(thread_ids)) == 2, "Tools must run on different threads to pass the barrier"


def test_converted_tools_are_sync() -> None:
    """Confirm all seven converted entry points are synchronous."""
    functions: tuple[tuple[str, Callable[..., Any]], ...] = (
        ("dbchat_list_tables", dbchat_list_tables),
        ("dbchat_describe_table", dbchat_describe_table),
        ("dbchat_query_sql", dbchat_query_sql),
        ("analytic_list_tables", analytic_list_tables),
        ("analytic_describe_table", analytic_describe_table),
        ("analytic_query_sql", analytic_query_sql),
        ("kms_vector_search", kms_vector_search),
    )

    for name, fn in functions:
        assert not inspect.iscoroutinefunction(fn), f"{name} should be sync"
        assert not inspect.isasyncgenfunction(fn), f"{name} should not be async generator"
