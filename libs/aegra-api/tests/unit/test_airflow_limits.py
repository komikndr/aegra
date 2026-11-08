"""Byte limits, operation deadlines, and shared status collection."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from aegra_api.services import airflow_environment as environment
from aegra_api.services.airflow_http import MAX_RESPONSE_BYTES, airflow_json


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(100):
            self.read += 1
            yield b"x" * 65_536

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_large_stream_stops_and_closes_before_json_parsing() -> None:
    stream = ChunkStream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(ValueError, match="byte limit"):
            await airflow_json(client, "GET", "https://airflow.example/api/v2/dags")
    assert stream.closed
    assert stream.read == MAX_RESPONSE_BYTES // 65_536 + 1


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_before_decoding() -> None:
    stream = ChunkStream()
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Compressed"):
            await airflow_json(client, "GET", "https://airflow.example/api/v2/dags")
    assert stream.closed
    assert stream.read == 0


@pytest.mark.asyncio
async def test_status_coalesces_callers_without_sharing_mutable_results(monkeypatch: pytest.MonkeyPatch) -> None:
    read = AsyncMock(return_value={"configured": True, "rows": [{"dag": "hello"}]})
    monkeypatch.setattr(environment, "_read_environment_airflow_status", read)
    results = await asyncio.gather(*(environment.environment_airflow_status() for _ in range(8)))
    assert read.await_count == 1
    results[0]["rows"].clear()
    assert (await environment.environment_airflow_status())["rows"] == [{"dag": "hello"}]
    monkeypatch.setenv("AIRFLOW_DAG_IDS", "different")
    await environment.environment_airflow_status()
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_operation_deadline_covers_collection_not_just_http_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_API_URL", "https://airflow.example/api/v2")
    monkeypatch.setenv("AIRFLOW_TOKEN", "fixture")
    monkeypatch.setattr(environment, "_OPERATION_TIMEOUT", 0.01)
    with pytest.raises(httpx.TimeoutException, match="deadline"):
        async with environment._environment_client():
            await asyncio.sleep(1)
