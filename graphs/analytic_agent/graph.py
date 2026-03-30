"""Graph definitions for the analytic agent."""

from datetime import UTC, datetime
from typing import Literal, cast

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from react_agent.state import InputState, State
from react_agent.utils import (
    build_token_limited_messages,
    is_media_not_supported_error,
    load_chat_model,
)

from analytic_agent.context import ArtifactEditorContext, ChatContext, ExecutiveContext
from analytic_agent.context import Context as BaseContext
from analytic_agent.tools import TOOLS


async def call_model(
    state: State, runtime: Runtime[BaseContext]
) -> dict[str, list[AIMessage]]:
    model = load_chat_model(runtime.context.model).bind_tools(TOOLS)
    system_message = runtime.context.system_prompt.format(
        system_time=datetime.now(tz=UTC).isoformat()
    )
    model_messages = build_token_limited_messages(
        model,
        system_message,
        state.messages,
        num_limit_token=runtime.context.num_limit_token,
        num_limit_response_reserve=runtime.context.num_limit_response_reserve,
        num_limit_safety_buffer=runtime.context.num_limit_safety_buffer,
        num_limit_min_recent_messages=runtime.context.num_limit_min_recent_messages,
    )
    try:
        response = cast("AIMessage", await model.ainvoke(model_messages))
    except Exception as error:
        if is_media_not_supported_error(error):
            response = AIMessage(
                content="Sorry, the model do not have image capability."
            )
        else:
            raise

    if state.is_last_step and response.tool_calls:
        return {
            "messages": [
                AIMessage(
                    id=response.id,
                    content="Sorry, I could not find an answer to your question in the specified number of steps.",
                )
            ]
        }

    return {"messages": [response]}


def route_model_output(state: State) -> Literal["__end__", "tools"]:
    last_message = state.messages[-1]
    if not isinstance(last_message, AIMessage):
        raise ValueError(
            f"Expected AIMessage in output edges, but got {type(last_message).__name__}"
        )
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


chat_graph = build_graph(ChatContext, name="Analytic Chat Agent")
executive_graph = build_graph(ExecutiveContext, name="Analytic Executive Agent")
artifact_editor_graph = build_graph(
    ArtifactEditorContext, name="Analytic Artifact Editor"
)
