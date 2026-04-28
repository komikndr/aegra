"""A minimal graph that delegates to `react_agent.graph` as a subgraph node."""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import cast

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from react_agent import graph as react_graph
from react_agent.context import Context
from react_agent.state import InputState, State
from react_agent.utils import (
    build_token_limited_messages,
    is_media_not_supported_error,
    load_chat_model,
)

builder = StateGraph(State, input_schema=InputState, context_schema=Context)


async def no_stream(
    state: State, runtime: Runtime[Context]
) -> dict[str, list[AIMessage]]:
    """Call the LLM powering our "agent" with the langsmith:nostream tag.

    This function prepares the prompt, initializes the model with the langsmith:nostream tag, and processes the response.

    Args:
        state (State): The current state of the conversation.
        runtime (Runtime[Context]): The runtime context.

    Returns:
        dict: A dictionary containing the model's response message.
    """
    # Initialize the model with the langsmith:nostream tag
    model = load_chat_model(runtime.context.model).with_config(
        config={"tags": ["langsmith:nostream"]}
    )

    # Format the system prompt
    system_message = runtime.context.system_prompt.format(
        system_time=datetime.now(ZoneInfo("Asia/Jakarta")).strftime(
            "%A, %d %B %Y %H:%M:%S WIB"
        )
    )

    # Get the model's response
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
    # Return the model's response as a list to be added to existing messages
    return {"messages": [response]}


builder.add_node("subgraph_agent", react_graph)
builder.add_node("no_stream", no_stream)

# Always go through the no-stream node.
builder.add_edge("__start__", "no_stream")
builder.add_edge("no_stream", "subgraph_agent")
builder.add_edge("subgraph_agent", "__end__")

graph = builder.compile(name="Subgraph Agent")
