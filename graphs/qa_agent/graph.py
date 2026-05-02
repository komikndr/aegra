"""Graph definitions for the QA agent."""

from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from kms_agent.tools import get_vector_store, serialize_documents
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from react_agent.state import InputState, State
from react_agent.utils import (
    build_system_prompt_messages,
    build_token_limited_messages,
    get_message_text,
    is_media_not_supported_error,
    load_chat_model,
)

from qa_agent.context import ChatContext


def _latest_user_query(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return get_message_text(message).strip()
    return ""


def _format_metadata(metadata: object) -> str:
    if not isinstance(metadata, dict) or not metadata:
        return ""

    source_parts = []
    for key in ("source", "filename", "file_name", "title", "page"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            source_parts.append(f"{key}: {value}")
    return "; ".join(source_parts)


def _format_retrieved_context(matches: list[dict[str, object]]) -> str:
    if not matches:
        return "Retrieved knowledge-base context\nNo relevant context was found."

    sections = ["Retrieved knowledge-base context", "Use only relevant passages from this context."]
    for index, match in enumerate(matches, start=1):
        content = str(match.get("content") or "").strip()
        metadata = _format_metadata(match.get("metadata"))
        source = f"\nSource metadata: {metadata}" if metadata else ""
        sections.append(f"[Source {index}]{source}\n{content}")
    return "\n\n".join(sections)


def _retrieve_context(runtime: Runtime[ChatContext], query: str) -> str:
    if not query:
        return "Retrieved knowledge-base context\nNo user query was available for retrieval."

    vector_store = get_vector_store(runtime.context)
    if vector_store is None:
        return "Retrieved knowledge-base context\nKMS vector store is not configured."

    documents = vector_store.similarity_search(query, k=runtime.context.kms_search_k)
    return _format_retrieved_context(serialize_documents(documents))


async def call_model(state: State, runtime: Runtime[ChatContext]) -> dict[str, list[AIMessage]]:
    model = load_chat_model(runtime.context.model)
    retrieval_context = _retrieve_context(runtime, _latest_user_query(list(state.messages)))
    system_prompt = f"{runtime.context.system_prompt}\n\n{retrieval_context}"
    system_messages = build_system_prompt_messages(
        system_prompt,
        datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%A, %d %B %Y %H:%M:%S WIB"),
        runtime.context.user_memory,
    )
    model_messages = build_token_limited_messages(
        model,
        system_messages,
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
            response = AIMessage(content="Sorry, the model do not have image capability.")
        else:
            raise

    return {"messages": [response]}


def build_graph(context_schema: type[ChatContext], name: str):
    builder = StateGraph(State, input_schema=InputState, context_schema=context_schema)
    builder.add_node("call_model", call_model)
    builder.add_edge("__start__", "call_model")
    builder.add_edge("call_model", "__end__")
    return builder.compile(name=name)


chat_graph = build_graph(ChatContext, name="QA Chat Agent")
