"""Graph definitions for the analytic agent."""

import asyncio
import re
from datetime import datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from react_agent.model_config import resolve_agent_model
from react_agent.state import InputState, State
from react_agent.utils import (
    apply_reasoning_effort,
    bind_tools_with_parallel,
    build_reasoning_storage_update,
    build_system_prompt_messages,
    build_token_limited_messages,
    is_media_not_supported_error,
    load_chat_model,
)

from analytic_agent.airflow_tools import get_environment_airflow_tools
from analytic_agent.capabilities import capability_prompt, get_tools
from analytic_agent.context import (
    ArtifactEditorContext,
    ChatContext,
    ExecutiveContext,
    OfficeContext,
)
from analytic_agent.context import Context as BaseContext
from analytic_agent.tools import analytic_list_tables

_SCHEMA_DISCOVERY_REQUEST = re.compile(
    r"(?:\b(?:list|show|available|what|which|check|inspect|describe)\b.*\b(?:table|tables|view|views|schema|columns?)\b|"
    r"\b(?:table|tables|view|views|schema|columns?)\b.*\b(?:list|show|available|what|which|check|inspect|describe|exist)\b)",
    re.IGNORECASE,
)


def requires_initial_table_listing(state: State) -> bool:
    """Require table discovery before answering a new schema-discovery request."""
    latest_human_index = next(
        (index for index in range(len(state.messages) - 1, -1, -1) if isinstance(state.messages[index], HumanMessage)),
        None,
    )
    if latest_human_index is None:
        return False
    if any(isinstance(message, ToolMessage) for message in state.messages[latest_human_index + 1 :]):
        return False
    return bool(_SCHEMA_DISCOVERY_REQUEST.search(state.messages[latest_human_index].text))


async def call_model(state: State, runtime: Runtime[BaseContext]) -> dict[str, list[AIMessage]]:
    model_config = resolve_agent_model(
        runtime.context.agent_id,
        context_model=runtime.context.model,
        context_window=runtime.context.num_limit_token,
        response_reserve=runtime.context.num_limit_response_reserve,
        safety_buffer=runtime.context.num_limit_safety_buffer,
        min_recent_messages=runtime.context.num_limit_min_recent_messages,
    )
    base_model = load_chat_model(model_config.model, base_url=model_config.base_url, api_key=model_config.api_key)
    tools = [*get_tools(runtime.context), *get_environment_airflow_tools()]
    if analytic_list_tables in tools and requires_initial_table_listing(state):
        model = bind_tools_with_parallel(base_model, [analytic_list_tables], tool_choice="required")
    elif tools:
        model = bind_tools_with_parallel(base_model, tools)
    else:
        model = base_model
    model = apply_reasoning_effort(
        model,
        model_config.model,
        runtime.context.reasoning_effort,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    system_messages = build_system_prompt_messages(
        runtime.context.system_prompt + capability_prompt(tools),
        datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%A, %d %B %Y %H:%M:%S WIB"),
        runtime.context.user_memory,
    )
    # Tokenizer initialization can perform synchronous disk/network I/O on a
    # cold cache. Keep worker heartbeats and SSE responsive during that work.
    model_messages = await asyncio.to_thread(
        build_token_limited_messages,
        model,
        system_messages,
        state.messages,
        num_limit_token=model_config.context_window
        if model_config.context_window is not None
        else runtime.context.num_limit_token,
        num_limit_response_reserve=model_config.response_reserve
        if model_config.response_reserve is not None
        else runtime.context.num_limit_response_reserve,
        num_limit_safety_buffer=model_config.safety_buffer
        if model_config.safety_buffer is not None
        else runtime.context.num_limit_safety_buffer,
        num_limit_min_recent_messages=model_config.min_recent_messages
        if model_config.min_recent_messages is not None
        else runtime.context.num_limit_min_recent_messages,
    )
    try:
        response = cast("AIMessage", await model.ainvoke(model_messages))
    except Exception as error:
        if is_media_not_supported_error(error):
            response = AIMessage(content="Sorry, the model do not have image capability.")
        else:
            raise

    if state.is_last_step and response.tool_calls:
        final_response = AIMessage(
            id=response.id,
            content="Sorry, I could not find an answer to your question in the specified number of steps.",
        )
        return {"messages": build_reasoning_storage_update(state.messages, final_response)}

    return {"messages": build_reasoning_storage_update(state.messages, response)}


def route_model_output(state: State) -> Literal["__end__", "tools"]:
    last_message = state.messages[-1]
    if not isinstance(last_message, AIMessage):
        raise ValueError(f"Expected AIMessage in output edges, but got {type(last_message).__name__}")
    if not last_message.tool_calls:
        return "__end__"
    return "tools"


async def call_tools(state: State, runtime: Runtime[BaseContext]) -> dict[str, Any]:
    """Execute only tools enabled for this invocation, including replayed tool calls."""
    tools = [*get_tools(runtime.context), *get_environment_airflow_tools()]
    return await ToolNode(tools).ainvoke(state, get_config())


def build_graph(
    context_schema: type[BaseContext], name: str
) -> CompiledStateGraph[State, BaseContext, InputState, State]:
    builder = StateGraph(State, input_schema=InputState, context_schema=context_schema)
    builder.add_node("call_model", call_model)
    builder.add_node("tools", call_tools)
    builder.add_edge("__start__", "call_model")
    builder.add_conditional_edges("call_model", route_model_output)
    builder.add_edge("tools", "call_model")
    return builder.compile(name=name)


chat_graph = build_graph(ChatContext, name="Analytic Chat Agent")
executive_graph = build_graph(ExecutiveContext, name="Analytic Executive Agent")
office_graph = build_graph(OfficeContext, name="Analytic Office Agent")
artifact_editor_graph = build_graph(ArtifactEditorContext, name="Analytic Artifact Editor")
