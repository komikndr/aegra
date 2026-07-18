"""Deny-by-default role policy for registered graphs."""

from collections.abc import Iterable

from fastapi import HTTPException

from ..models import Assistant, Thread, User

_graph_access_policy: dict[str, frozenset[str]] = {}


def _normalize_role(role: str) -> str:
    return "".join(char.lower() for char in role if char.isalnum())


def _normalized_roles(permissions: Iterable[str] | None) -> set[str]:
    return {
        _normalize_role(permission) for permission in permissions or [] if isinstance(permission, str) and permission
    }


def has_superadmin_access(permissions: Iterable[str] | None) -> bool:
    return "superadmin" in _normalized_roles(permissions)


def configure_graph_access(graph_ids: Iterable[str], policy: dict[str, list[str]]) -> None:
    """Validate and install the complete graph access policy."""
    registered = set(graph_ids)
    configured = set(policy)
    missing = sorted(registered - configured)
    unknown = sorted(configured - registered)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing policies for: {', '.join(missing)}")
        if unknown:
            details.append(f"policies for unknown graphs: {', '.join(unknown)}")
        raise ValueError("Invalid graph_access configuration (" + "; ".join(details) + ")")

    normalized: dict[str, frozenset[str]] = {}
    for graph_id, roles in policy.items():
        if not isinstance(roles, list) or not all(isinstance(role, str) and role.strip() for role in roles):
            raise ValueError(f"graph_access.{graph_id} must be a list of non-empty role names")
        normalized[graph_id] = frozenset(_normalize_role(role) for role in roles)

    global _graph_access_policy
    _graph_access_policy = normalized


def ensure_graph_access(user: User, graph_id: str | None) -> None:
    if graph_id is None:
        return

    if has_superadmin_access(user.permissions):
        return

    required_roles = _graph_access_policy.get(graph_id)
    if required_roles is not None and (not required_roles or required_roles & _normalized_roles(user.permissions)):
        return

    raise HTTPException(status_code=403, detail="You do not have access to this agent.")


def get_accessible_graph_ids(user: User) -> frozenset[str]:
    """Return registered graph IDs visible to the authenticated user."""
    if has_superadmin_access(user.permissions):
        return frozenset(_graph_access_policy)
    roles = _normalized_roles(user.permissions)
    return frozenset(
        graph_id
        for graph_id, required_roles in _graph_access_policy.items()
        if not required_roles or required_roles & roles
    )


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
