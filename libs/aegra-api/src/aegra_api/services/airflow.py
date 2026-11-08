"""Read-only DAG inspection using a server-authenticated Airflow client."""

from typing import Any
from urllib.parse import quote

import httpx

from aegra_api.services.airflow_http import airflow_json


async def inspect_client_dag(*, client: httpx.AsyncClient, base_url: str, dag_id: str) -> dict[str, Any]:
    """Read a DAG's latest run and bounded task status without changing it."""
    dag_url = f"{base_url}/dags/{quote(dag_id, safe='')}"
    dag = await airflow_json(client, "GET", dag_url)
    runs_data = await airflow_json(client, "GET", f"{dag_url}/dagRuns", params={"limit": 1, "order_by": "-start_date"})
    runs = runs_data.get("dag_runs", [])
    tasks: list[dict[str, Any]] = []
    if runs:
        run_id = quote(runs[0]["dag_run_id"], safe="")
        task_data = await airflow_json(client, "GET", f"{dag_url}/dagRuns/{run_id}/taskInstances", params={"limit": 100})
        tasks = [
            {key: task.get(key) for key in ("task_id", "state", "start_date", "end_date", "try_number", "map_index")}
            for task in task_data.get("task_instances", [])
        ]
    run = {key: runs[0].get(key) for key in ("dag_run_id", "state", "start_date", "end_date")} if runs else None
    return {
        "available": True,
        "mock": False,
        "dag_id": dag_id,
        "is_paused": dag.get("is_paused"),
        "latest_run": run,
        "tasks": tasks,
        "task_limit": 100,
        "message": "Read from Airflow 3.",
    }
