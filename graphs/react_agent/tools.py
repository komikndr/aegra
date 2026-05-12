"""This module provides agent tools for search and private scratchpad memory.

It includes a basic Tavily search function (as an example)

These tools are intended as free examples to get started. For production use,
consider implementing more robust and specialized tools tailored to your needs.
"""

from collections.abc import Callable
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.runtime import get_runtime
from langgraph.types import Command

from react_agent.context import Context

_MAX_SCRATCHPAD_CHARS = 24000


def _get_scratchpad_from_state(state: dict[str, Any]) -> str:
    scratchpad = state.get("scratchpad", "")
    return scratchpad if isinstance(scratchpad, str) else ""


def _trim_scratchpad(content: str) -> str:
    normalized = content.strip()
    if len(normalized) <= _MAX_SCRATCHPAD_CHARS:
        return normalized
    return f"{normalized[-_MAX_SCRATCHPAD_CHARS:].lstrip()}"


def _scratchpad_command(content: str, tool_call_id: str, status: str) -> Command:
    scratchpad = _trim_scratchpad(content)
    return Command(
        update={
            "scratchpad": scratchpad,
            "messages": [ToolMessage(content=status, tool_call_id=tool_call_id)],
        }
    )


async def search(query: str) -> dict[str, Any] | None:
    """Search for general web results.

    This function performs a search using the Tavily search engine, which is designed
    to provide comprehensive, accurate, and trusted results. It's particularly useful
    for answering questions about current events.
    """
    runtime = get_runtime(Context)
    return {
        "query": query,
        "max_search_results": runtime.context.max_search_results,
        "results": f"Simulated search results for '{query}'",
    }


def read_scratchpad(state: Annotated[dict[str, Any], InjectedState]) -> str:
    """Read the private per-thread scratchpad for temporary working memory."""
    scratchpad = _get_scratchpad_from_state(state).strip()
    return scratchpad or "Scratchpad is empty."


def write_scratchpad(content: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Replace the private scratchpad with concise updated working notes."""
    return _scratchpad_command(content, tool_call_id, "Scratchpad updated.")


def append_scratchpad(
    content: str,
    state: Annotated[dict[str, Any], InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Append concise working notes to the private scratchpad."""
    current = _get_scratchpad_from_state(state).strip()
    next_content = f"{current}\n\n{content.strip()}" if current else content
    return _scratchpad_command(next_content, tool_call_id, "Scratchpad appended.")


def clear_scratchpad(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Clear the private per-thread scratchpad when it is no longer useful."""
    return _scratchpad_command("", tool_call_id, "Scratchpad cleared.")


TOOLS: list[Callable[..., Any]] = [search, read_scratchpad, write_scratchpad, append_scratchpad, clear_scratchpad]
