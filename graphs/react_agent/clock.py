"""On-demand time for agents without a timestamp in every prompt."""

from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.tools import tool


@tool
def get_current_time() -> str:
    """Get the current date and time in Asia/Jakarta (WIB)."""
    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    return f"{now.isoformat(timespec='seconds')} (Asia/Jakarta)"
