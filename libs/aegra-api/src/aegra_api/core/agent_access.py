"""Role-based agent access helpers."""

from collections.abc import Iterable

from fastapi import HTTPException

from ..models import Assistant, Thread, User

KNOWN_AGENT_IDS = {"kms", "qa", "analytic", "casual"}
PUBLIC_AGENT_IDS = {"qa"}
EXECUTIVE_AGENT_IDS = {"analytic", "casual"}
STAFF_AGENT_IDS = {"kms"}
DEFAULT_AGENT_IDS = {"qa", "casual"}


def _normalize_role(role: str) -> str:
    return "".join(char.lower() for char in role if char.isalnum())


def _normalized_roles(permissions: Iterable[str] | None) -> set[str]:
    return {
        _normalize_role(permission) for permission in permissions or [] if isinstance(permission, str) and permission
    }


def has_superadmin_access(permissions: Iterable[str] | None) -> bool:
    return "superadmin" in _normalized_roles(permissions)


def get_allowed_agent_ids(permissions: Iterable[str] | None) -> set[str]:
    roles = _normalized_roles(permissions)
    if "superadmin" in roles:
        return set(KNOWN_AGENT_IDS)

    allowed_agent_ids: set[str] = set(PUBLIC_AGENT_IDS)

    if "executive" in roles or "seniordataengineer" in roles:
        allowed_agent_ids.update(EXECUTIVE_AGENT_IDS)

    if "staff" in roles:
        allowed_agent_ids.update(STAFF_AGENT_IDS)

    if allowed_agent_ids == PUBLIC_AGENT_IDS:
        allowed_agent_ids.update(DEFAULT_AGENT_IDS)

    return allowed_agent_ids


def get_agent_id_from_graph_id(graph_id: str | None) -> str | None:
    if not graph_id:
        return None

    agent_id = graph_id.split("_", 1)[0]
    return agent_id if agent_id in KNOWN_AGENT_IDS else None


def ensure_graph_access(user: User, graph_id: str | None) -> None:
    if not graph_id:
        return

    if has_superadmin_access(user.permissions):
        return

    agent_id = get_agent_id_from_graph_id(graph_id)
    if agent_id is None:
        return

    if agent_id and agent_id in get_allowed_agent_ids(user.permissions):
        return

    raise HTTPException(status_code=403, detail="You do not have access to this agent.")


def assistant_is_accessible(assistant: Assistant, user: User) -> bool:
    try:
        ensure_graph_access(user, assistant.graph_id)
    except HTTPException:
        return False
    return True


def thread_is_accessible(thread: Thread, user: User) -> bool:
    metadata = thread.metadata or {}
    graph_id = metadata.get("graph_id") if isinstance(metadata, dict) else None
    try:
        ensure_graph_access(user, graph_id if isinstance(graph_id, str) else None)
    except HTTPException:
        return False
    return True
