"""Graph definition for the DBChat agent."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph
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

from dbchat_agent.context import Context
from dbchat_agent.tools import TOOLS


async def call_model(state: State, runtime: Runtime[Context]) -> dict[str, list[AIMessage]]:
    model_config = resolve_agent_model(
        runtime.context.agent_id,
        context_model=runtime.context.model,
        context_window=runtime.context.num_limit_token,
        response_reserve=runtime.context.num_limit_response_reserve,
        safety_buffer=runtime.context.num_limit_safety_buffer,
        min_recent_messages=runtime.context.num_limit_min_recent_messages,
    )
    base_model = load_chat_model(model_config.model, base_url=model_config.base_url, api_key=model_config.api_key)
    model = bind_tools_with_parallel(base_model, TOOLS)
    model = apply_reasoning_effort(
        model,
        model_config.model,
        runtime.context.reasoning_effort,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    system_messages = build_system_prompt_messages(
        runtime.context.system_prompt,
        datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%A, %d %B %Y %H:%M:%S WIB"),
        runtime.context.user_memory,
    )
    model_messages = build_token_limited_messages(
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
            response = AIMessage(content="Sorry, the model does not have image capability.")
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


builder = StateGraph(State, input_schema=InputState, context_schema=Context)
builder.add_node("call_model", call_model)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge("__start__", "call_model")
builder.add_conditional_edges("call_model", route_model_output)
builder.add_edge("tools", "call_model")

graph = builder.compile(name="DBChat Agent")
