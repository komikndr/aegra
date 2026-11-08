"""Server-side access boundary for the Control Plane's operational APIs."""

from fastapi import Depends, HTTPException

from aegra_api.core.agent_access import has_superadmin_access
from aegra_api.core.auth_deps import require_auth
from aegra_api.models.auth import User


def require_operations_access(user: User = Depends(require_auth)) -> User:
    if not has_superadmin_access(user.permissions):
        raise HTTPException(403, "SuperAdmin access is required for Operations.")
    return user
