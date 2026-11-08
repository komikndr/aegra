"""Bound Airflow response bytes before buffering, decoding, or JSON parsing."""

import json
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 1_048_576


async def airflow_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    async with client.stream(method, url, params=params, json=payload, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Compressed Airflow responses are not supported.")
        if int(response.headers.get("content-length", "0")) > MAX_RESPONSE_BYTES:
            raise ValueError("Airflow response exceeds the byte limit.")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65_536):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("Airflow response exceeds the byte limit.")
            body.extend(chunk)
        return json.loads(body)
