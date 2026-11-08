"""Environment-backed Airflow operations and explicit legacy retirement."""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from aegra_api.core.operations_access import require_operations_access
from aegra_api.services.airflow_environment import environment_airflow_status

router = APIRouter(
    prefix="/analytics/connections",
    tags=["Airflow operations"],
    dependencies=[Depends(require_operations_access)],
)


@router.get("/environment")
async def environment_status() -> dict[str, Any]:
    """Return server-configured Airflow status to authorized operators."""
    try:
        return await environment_airflow_status()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Airflow is unavailable. Check server settings and service health.") from exc


@router.api_route("", methods=["GET", "POST"])
@router.api_route("/test", methods=["POST"])
@router.api_route("/{connection_id}", methods=["PUT", "DELETE"])
async def retired_connections() -> None:
    """Keep stored rows intact while preventing use of the retired configuration path."""
    raise HTTPException(410, "Saved Airflow connections are retired. Configure Airflow in the server environment.")
