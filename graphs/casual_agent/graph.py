"""Graph definitions for the casual agent."""

from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from react_agent.context import Context as BaseContext
from react_agent.model_config import resolve_agent_model
from react_agent.state import InputState, State
from react_agent.tools import TOOLS
from react_agent.utils import (
    apply_reasoning_effort,
    build_reasoning_storage_update,
    build_system_prompt_messages,
    build_token_limited_messages,
    is_media_not_supported_error,
    is_tool_choice_not_supported_error,
    is_vllm_base_url,
    load_chat_model,
)

from casual_agent.context import (
    ArtifactEditorContext,
    ChatContext,
    ExecutiveContext,
    OfficeContext,
)


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
    vllm_without_tool_parser = is_vllm_base_url(model_config.base_url, api_key=model_config.api_key)
    model = base_model if vllm_without_tool_parser else base_model.bind_tools(TOOLS)
    model = apply_reasoning_effort(
        model,
        model_config.model,
        runtime.context.reasoning_effort,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    fallback_model = apply_reasoning_effort(
        base_model,
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
    scratchpad = state.scratchpad.strip()
    if scratchpad:
        system_messages.append(
            SystemMessage(
                content=(
                    "Private scratchpad for this thread\n"
                    "Use these temporary working notes when relevant. Do not reveal or quote them unless the user explicitly asks.\n\n"
                    f"<scratchpad>\n{scratchpad}\n</scratchpad>"
                )
            )
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
            response = AIMessage(content="Sorry, the model do not have image capability.")
        elif is_tool_choice_not_supported_error(error):
            response = cast("AIMessage", await fallback_model.ainvoke(model_messages))
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


def build_graph(context_schema: type[BaseContext], name: str):
    builder = StateGraph(State, input_schema=InputState, context_schema=context_schema)
    builder.add_node("call_model", call_model)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge("__start__", "call_model")
    builder.add_conditional_edges("call_model", route_model_output)
    builder.add_edge("tools", "call_model")
    return builder.compile(name=name)


chat_graph = build_graph(ChatContext, name="Casual Chat Agent")
executive_graph = build_graph(ExecutiveContext, name="Casual Executive Agent")
office_graph = build_graph(OfficeContext, name="Casual Office Agent")
artifact_editor_graph = build_graph(ArtifactEditorContext, name="Casual Artifact Editor")
