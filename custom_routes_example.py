"""Example custom routes file for Aegra.

This demonstrates how to add custom FastAPI endpoints to your Aegra server,
including examples of authentication integration.

Configuration:
Add this to your aegra.json or langgraph.json:

{
  "graphs": {
    "agent": "./graphs/react_agent/graph.py:graph"
  },
  "auth": {
    "path": "./jwt_mock_auth_example.py:auth"
  },
  "http": {
    "app": "./custom_routes_example.py:app",
    "enable_custom_route_auth": false
  }
}

You can also configure CORS:

{
  "http": {
    "app": "./custom_routes_example.py:app",
    "enable_custom_route_auth": true,
    "cors": {
      "allow_origins": ["https://example.com"],
      "allow_credentials": true
    }
  }
}
"""

# ruff: noqa: E402

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_GRAPHS_DIR = Path(__file__).resolve().parent / "graphs"
if _GRAPHS_DIR.exists() and str(_GRAPHS_DIR) not in sys.path:
    sys.path.insert(0, str(_GRAPHS_DIR))

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import OpenAIEmbeddings
from opensearchpy import OpenSearch as OpenSearchClient
from opensearchpy import TransportError
from pydantic import BaseModel, Field
from react_agent.model_config import resolve_agent_model
from react_agent.utils import (
    apply_reasoning_effort,
    get_active_model_name,
    get_message_text,
    is_media_not_supported_error,
    load_chat_model,
    resolve_model_name,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.agent_access import ensure_graph_access
from aegra_api.core.auth_deps import require_auth
from aegra_api.core.orm import ExecutiveArtifact, PipelineApiKey, PipelineRun, PipelineWorkflow, Thread, get_session
from aegra_api.models.auth import User

# Create your FastAPI app instance
# This will be merged with Aegra's core routes
app = FastAPI(
    title="Custom Routes Example",
    description="Example custom endpoints for Aegra with authentication",
)

logger = logging.getLogger(__name__)


class ExecutiveArtifactPayload(BaseModel):
    id: str
    title: str | None = None
    content: str | None = None
    titleBase64: str | None = None
    contentBase64: str | None = None
    timestamp: datetime
    threadId: str | None = None
    agentId: str | None = None
    artifactKind: str = "executive_report"
    sourceMessageId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplaceExecutiveArtifactsRequest(BaseModel):
    artifacts: list[ExecutiveArtifactPayload] = Field(default_factory=list)
    artifactsBase64: str | None = None


class PipelineNodePayload(BaseModel):
    id: str
    type: str
    label: str
    position: dict[str, float] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class PipelineEdgePayload(BaseModel):
    id: str
    source: str
    target: str
    label: str | None = None


class PipelineBlueprintPayload(BaseModel):
    workflowId: str
    description: str
    nodes: list[PipelineNodePayload]
    edges: list[PipelineEdgePayload]


class PipelineWorkflowRequest(BaseModel):
    workflowId: str | None = None
    name: str
    description: str = ""
    graph: PipelineBlueprintPayload


class PipelineWorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    graph: PipelineBlueprintPayload | None = None


class PipelineApiKeyRequest(BaseModel):
    name: str = "External push key"
    expiresAt: datetime | None = None


class PipelineExecuteRequest(BaseModel):
    workflowId: str
    data: Any = None
    rows: list[Any] | None = None
    dryRun: bool = False


def _normalize_base64(encoded_value: str) -> str:
    normalized = encoded_value.strip().replace("-", "+").replace("_", "/")
    padding = len(normalized) % 4
    if padding:
        normalized += "=" * (4 - padding)
    return normalized


def _decode_transport_string(encoded_value: str | None, raw_value: str | None, field_name: str) -> str:
    if encoded_value:
        try:
            normalized = _normalize_base64(encoded_value)
            return base64.b64decode(normalized).decode("utf-8")
        except Exception as exc:  # pragma: no cover - defensive validation path
            logger.warning(
                "Failed to decode artifact %s payload length=%s",
                field_name,
                len(encoded_value),
            )
            raise HTTPException(
                status_code=422,
                detail=f"Invalid base64 payload for artifact {field_name}",
            ) from exc
    if raw_value is not None:
        return raw_value
    raise HTTPException(status_code=422, detail=f"Missing artifact {field_name}")


def _decode_transport_json(encoded_value: str | None, field_name: str) -> list[dict[str, Any]] | None:
    if not encoded_value:
        return None
    try:
        normalized = _normalize_base64(encoded_value)
        decoded = base64.b64decode(normalized).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:  # pragma: no cover - defensive validation path
        logger.warning(
            "Failed to decode %s payload length=%s",
            field_name,
            len(encoded_value),
        )
        raise HTTPException(
            status_code=422,
            detail=f"Invalid base64 payload for {field_name}",
        ) from exc

    if not isinstance(payload, list):
        raise HTTPException(status_code=422, detail=f"Invalid payload for {field_name}")
    return payload


def _get_configured_model() -> str:
    return os.environ.get("MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"


PIPELINE_KEY_PREFIX = "aegra_pipeline_"


def _slugify_workflow_id(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:64] or f"workflow-{uuid.uuid4().hex[:8]}"


def _hash_pipeline_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _resolve_pipeline_base_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    resolved = os.path.expandvars(value).strip()
    if "$" in resolved:
        return None
    return resolved or None


def _default_pipeline_blueprint(workflow_id: str) -> PipelineBlueprintPayload:
    return PipelineBlueprintPayload(
        workflowId=workflow_id,
        description=(
            "HTTP push workflow. External systems POST rows to /pipeline with workflowId, "
            "processors transform each row, then output nodes return the result."
        ),
        nodes=[
            PipelineNodePayload(
                id="source-1",
                type="source",
                label="Source",
                position={"x": 80, "y": 180},
                config={
                    "method": "POST",
                    "path": "/pipeline",
                    "workflowId": workflow_id,
                    "auth": "Bearer API key",
                    "description": "Accepts a pushed JSON row or rows array.",
                },
            ),
            PipelineNodePayload(
                id="formatter-1",
                type="formatter",
                label="JSON Formatter",
                position={"x": 390, "y": 90},
                config={
                    "model": _get_configured_model(),
                    "systemMessage": "Normalize each inbound row into a clean JSON object suitable for downstream analytics.",
                    "structuredOutput": {
                        "name": "normalized_row",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": True,
                        },
                    },
                },
            ),
            PipelineNodePayload(
                id="output-1",
                type="output",
                label="Output",
                position={"x": 720, "y": 180},
                config={"description": "Returns processed rows in the pipeline response."},
            ),
        ],
        edges=[
            PipelineEdgePayload(id="source-1-to-formatter-1", source="source-1", target="formatter-1", label="rows"),
            PipelineEdgePayload(id="formatter-1-to-output-1", source="formatter-1", target="output-1", label="json"),
        ],
    )


def _serialize_workflow(workflow: PipelineWorkflow) -> dict[str, Any]:
    return {
        "workflowId": workflow.workflow_id,
        "name": workflow.name,
        "description": workflow.description,
        "graph": workflow.graph,
        "createdAt": workflow.created_at.isoformat() if workflow.created_at else None,
        "updatedAt": workflow.updated_at.isoformat() if workflow.updated_at else None,
    }


def _serialize_api_key(api_key: PipelineApiKey) -> dict[str, Any]:
    return {
        "keyId": api_key.key_id,
        "workflowId": api_key.workflow_id,
        "name": api_key.name,
        "createdAt": api_key.created_at.isoformat() if api_key.created_at else None,
        "lastUsedAt": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
        "revokedAt": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
        "expiresAt": api_key.expires_at.isoformat() if api_key.expires_at else None,
    }


def _validate_pipeline_graph(graph: PipelineBlueprintPayload) -> None:
    node_ids = [node.id for node in graph.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise HTTPException(status_code=422, detail="Workflow node ids must be unique")
    if not any(node.type == "source" for node in graph.nodes):
        raise HTTPException(status_code=422, detail="Workflow requires a source node")
    known = set(node_ids)
    for edge in graph.edges:
        if edge.source not in known or edge.target not in known:
            raise HTTPException(status_code=422, detail=f"Invalid edge {edge.id}")


def _pipeline_rows(request: PipelineExecuteRequest) -> list[Any]:
    if request.rows is not None:
        return request.rows
    if isinstance(request.data, list):
        return request.data
    return [request.data if request.data is not None else {}]


def _parse_model_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"text": text}


async def _run_pipeline_processor(row: Any, node: PipelineNodePayload) -> Any:
    return await _run_pipeline_formatter(row, node)


def _structured_schema(config: dict[str, Any]) -> dict[str, Any]:
    structured = config.get("structuredOutput")
    if isinstance(structured, dict) and isinstance(structured.get("schema"), dict):
        return structured["schema"]
    schema = config.get("jsonSchema")
    return schema if isinstance(schema, dict) else {"type": "object"}


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str = "output") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if isinstance(key, str) and key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise ValueError(f"{path} has unexpected keys: {', '.join(sorted(extra))}")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                _validate_schema_value(value[key], child_schema, f"{path}.{key}")
    elif expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else None
        if item_schema:
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{index}]")
    elif expected_type == "string" and not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    elif expected_type == "number" and not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    elif expected_type == "integer" and not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    elif expected_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")


def _csv_from_value(value: Any, config: dict[str, Any]) -> str:
    separator = config.get("separator") if isinstance(config.get("separator"), str) else ","
    delimiter = separator[:1] or ","
    include_header = bool(config.get("includeHeader", True))
    columns = config.get("columns") if isinstance(config.get("columns"), list) else []
    rows = value if isinstance(value, list) else [value]
    dict_rows = [row if isinstance(row, dict) else {"value": row} for row in rows]
    fieldnames = [column for column in columns if isinstance(column, str) and column]
    if not fieldnames:
        fieldnames = list(dict.fromkeys(key for row in dict_rows for key in row)) or ["value"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore")
    if include_header:
        writer.writeheader()
    writer.writerows(dict_rows)
    return output.getvalue().strip("\r\n")


async def _run_pipeline_formatter(row: Any, node: PipelineNodePayload) -> Any:
    config = node.config
    selected_model = config.get("model") if isinstance(config.get("model"), str) else _get_configured_model()
    base_url = _resolve_pipeline_base_url(config.get("baseUrl"))
    model = load_chat_model(selected_model, base_url=base_url)
    model = apply_reasoning_effort(model, selected_model, "none", base_url=base_url)
    schema = _structured_schema(config)
    structured = config.get("structuredOutput") if isinstance(config.get("structuredOutput"), dict) else {}
    strict = bool(structured.get("strict", True))
    system_message = config.get("systemMessage") if isinstance(config.get("systemMessage"), str) else "Process one JSON row."
    prompt = (
        f"{system_message}\n\n"
        "Return only valid JSON. Do not include prose or markdown fences.\n"
        f"Structured output schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=json.dumps(row, ensure_ascii=False, default=str)),
        ]
    )
    parsed = _parse_model_json(get_message_text(response))
    if strict:
        try:
            _validate_schema_value(parsed, schema)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{node.label} output failed schema validation: {exc}") from exc
    if node.type == "csv_formatter":
        return _csv_from_value(parsed, config)
    return parsed


async def _post_pipeline_egress(url: str, payload: Any, *, method: str = "POST", headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}

    def send() -> dict[str, Any]:
        request = urllib.request.Request(  # nosec B310 - user-configured pipeline sink
            url,
            data=body,
            method=method.upper(),
            headers=request_headers,
        )
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310 - user-configured pipeline sink
            response_body = response.read().decode("utf-8")
            return {"status": response.status, "body": response_body[:2000]}

    return await asyncio.to_thread(send)


def _topological_pipeline_nodes(graph: PipelineBlueprintPayload) -> list[PipelineNodePayload]:
    nodes_by_id = {node.id: node for node in graph.nodes}
    indegree = {node.id: 0 for node in graph.nodes}
    outgoing: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
    queue = [node.id for node in graph.nodes if indegree.get(node.id, 0) == 0]
    ordered: list[PipelineNodePayload] = []
    while queue:
        node_id = queue.pop(0)
        ordered.append(nodes_by_id[node_id])
        for target in outgoing.get(node_id, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(ordered) != len(graph.nodes):
        raise HTTPException(status_code=422, detail="Workflow graph cannot contain cycles")
    return ordered


async def _execute_pipeline_graph(rows: list[Any], graph: PipelineBlueprintPayload, dry_run: bool) -> dict[str, Any]:
    ordered = _topological_pipeline_nodes(graph)
    incoming: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        incoming.setdefault(edge.target, []).append(edge.source)

    output_rows: list[Any] = []
    sink_results: list[dict[str, Any]] = []
    for source_row in rows:
        node_values: dict[str, Any] = {}
        for node in ordered:
            parents = incoming.get(node.id, [])
            if node.type == "source":
                node_values[node.id] = source_row
                continue
            if len(parents) > 1 and node.type in {"formatter", "csv_formatter", "sink", "output"}:
                node_input: Any = {parent: node_values.get(parent) for parent in parents}
            elif parents:
                node_input = node_values.get(parents[0])
            else:
                node_input = source_row

            if node.type in {"formatter", "csv_formatter"}:
                node_values[node.id] = node_input if dry_run else await _run_pipeline_formatter(node_input, node)
            elif node.type == "sink":
                node_values[node.id] = node_input
                url = node.config.get("url") if isinstance(node.config.get("url"), str) else ""
                enabled = bool(node.config.get("enabled", True))
                if url and enabled and not dry_run:
                    method = node.config.get("method") if isinstance(node.config.get("method"), str) else "POST"
                    headers = node.config.get("headers") if isinstance(node.config.get("headers"), dict) else {}
                    safe_headers = {str(key): str(value) for key, value in headers.items()}
                    sink_results.append({"nodeId": node.id, **await _post_pipeline_egress(url, node_input, method=method, headers=safe_headers)})
            elif node.type == "output":
                node_values[node.id] = node_input
            else:
                node_values[node.id] = node_input

        outputs = [node_values[node.id] for node in graph.nodes if node.type == "output" and node.id in node_values]
        output_rows.append(outputs[0] if len(outputs) == 1 else outputs if outputs else node_values.get(ordered[-1].id, source_row))
    return {"processed": output_rows, "sinks": sink_results}


def _load_pipeline_model_choices() -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = [{"id": _get_configured_model(), "label": "Default", "model": _get_configured_model()}]
    path = Path(os.environ.get("AGENT_MODELS_CONFIG", "agent-models.json"))
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = {}
    if isinstance(parsed, dict):
        entries: list[tuple[str, dict[str, Any]]] = []
        default = parsed.get("default")
        if isinstance(default, dict):
            entries.append(("Configured default", default))
        agents = parsed.get("agents")
        if isinstance(agents, dict):
            entries.extend((str(name), value) for name, value in agents.items() if isinstance(value, dict))
        for label, entry in entries:
            model = entry.get("model")
            if isinstance(model, str) and model:
                base_url = _resolve_pipeline_base_url(entry.get("base_url"))
                choices.append({"id": f"{label}:{model}", "label": label, "model": model, "baseUrl": base_url})
    deduped: dict[tuple[str, str | None], dict[str, Any]] = {}
    for choice in choices:
        key = (str(choice.get("model")), choice.get("baseUrl") if isinstance(choice.get("baseUrl"), str) else None)
        deduped[key] = choice
    return list(deduped.values())


async def _get_user_workflow(session: AsyncSession, workflow_id: str, user_id: str) -> PipelineWorkflow:
    workflow = await session.scalar(
        select(PipelineWorkflow).where(
            PipelineWorkflow.workflow_id == workflow_id,
            PipelineWorkflow.user_id == user_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


def _pipeline_key_from_request(request: Request) -> str | None:
    explicit_key = request.headers.get("x-pipeline-key")
    if explicit_key:
        return explicit_key.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token.startswith(PIPELINE_KEY_PREFIX):
            return token
    return None


async def _resolve_pipeline_actor(
    request: Request,
    request_payload: PipelineExecuteRequest,
    session: AsyncSession,
) -> tuple[str, PipelineWorkflow]:
    raw_key = _pipeline_key_from_request(request)
    if raw_key:
        api_key = await session.scalar(
            select(PipelineApiKey).where(
                PipelineApiKey.key_hash == _hash_pipeline_key(raw_key),
                PipelineApiKey.workflow_id == request_payload.workflowId,
                PipelineApiKey.revoked_at.is_(None),
            )
        )
        if api_key is None:
            raise HTTPException(status_code=401, detail="Invalid pipeline API key")
        now = datetime.now(UTC)
        expires_at = api_key.expires_at
        if expires_at and expires_at.replace(tzinfo=expires_at.tzinfo or UTC) < now:
            raise HTTPException(status_code=401, detail="Pipeline API key expired")
        workflow = await _get_user_workflow(session, request_payload.workflowId, api_key.user_id)
        api_key.last_used_at = now
        return api_key.user_id, workflow

    user = await require_auth(request)
    workflow = await _get_user_workflow(session, request_payload.workflowId, user.identity)
    return user.identity, workflow


def _serialize_artifact(artifact: ExecutiveArtifact) -> dict[str, Any]:
    timestamp = artifact.updated_at or artifact.created_at
    return {
        "id": artifact.artifact_id,
        "title": artifact.title,
        "content": artifact.content,
        "threadId": artifact.thread_id,
        "timestamp": timestamp.isoformat() if timestamp else None,
        "agentId": artifact.agent_id,
        "artifactKind": artifact.artifact_kind,
        "sourceMessageId": artifact.source_message_id,
        "metadata": artifact.metadata_dict or {},
    }


async def _get_user_thread(session: AsyncSession, thread_id: str, user: User) -> Thread:
    thread = await session.scalar(select(Thread).where(Thread.thread_id == thread_id, Thread.user_id == user.identity))
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    metadata = thread.metadata_json or {}
    graph_id = metadata.get("graph_id") if isinstance(metadata, dict) else None
    ensure_graph_access(user, graph_id if isinstance(graph_id, str) else None)
    return thread


@app.get("/custom/whoami")
async def whoami(user: User = Depends(require_auth)):
    """Return current user info - demonstrates authentication integration.

    This endpoint shows how to access authenticated user data in custom routes.
    Custom fields from your auth handler (e.g., role, team_id) are accessible.
    """
    return {
        "identity": user.identity,
        "display_name": user.display_name,
        "is_authenticated": user.is_authenticated,
        "permissions": user.permissions,
        # Custom fields from auth handler are accessible
        "role": getattr(user, "role", None),
        "subscription_tier": getattr(user, "subscription_tier", None),
        "team_id": getattr(user, "team_id", None),
        "email": getattr(user, "email", None),
    }


@app.get("/custom/public")
async def public_endpoint(user: User = Depends(require_auth)):
    """Example endpoint protected by the active auth configuration."""
    return {
        "message": "This endpoint requires authentication",
        "user": user.identity,
    }


@app.get("/custom/protected")
async def protected_endpoint(user: User = Depends(require_auth)):
    """Protected endpoint - explicitly requires authentication.

    This endpoint always requires authentication regardless of
    enable_custom_route_auth configuration.
    """
    return {
        "message": "This endpoint is always protected",
        "user": user.identity,
        "role": getattr(user, "role", None),
    }


@app.get("/custom/model-info")
async def model_info(agent_id: str | None = Query(default=None, alias="agentId"), user: User = Depends(require_auth)):
    """Return configured and resolved active model info for the UI."""
    model_config = resolve_agent_model(agent_id, context_model=_get_configured_model())
    resolved_provider, resolved_model = resolve_model_name(
        model_config.model,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    resolved_base_url = (
        model_config.base_url
        or os.environ.get("VLLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or None
    )
    return {
        "user": user.identity,
        "agent_id": agent_id,
        "configured_model": model_config.model,
        "active_model": f"{resolved_provider}/{resolved_model}",
        "active_model_name": resolved_model,
        "display_model_name": resolved_model,
        "provider": resolved_provider,
        "base_url": resolved_base_url,
        "vllm_base_url": os.environ.get("VLLM_BASE_URL", "").strip() or None,
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "").strip() or None,
        "endpoint_active_model": get_active_model_name(base_url=model_config.base_url, api_key=model_config.api_key),
        "context_window": model_config.context_window,
        "response_reserve": model_config.response_reserve,
        "safety_buffer": model_config.safety_buffer,
        "min_recent_messages": model_config.min_recent_messages,
    }


@app.get("/pipeline/models")
async def list_pipeline_models(user: User = Depends(require_auth)):
    """Return model choices usable by side-chain pipeline formatter nodes."""
    return {"models": _load_pipeline_model_choices(), "user": user.identity}


@app.get("/pipeline/workflows")
async def list_pipeline_workflows(
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    result = await session.scalars(
        select(PipelineWorkflow)
        .where(PipelineWorkflow.user_id == user.identity)
        .order_by(PipelineWorkflow.updated_at.desc())
    )
    return {"workflows": [_serialize_workflow(workflow) for workflow in result.all()]}


@app.post("/pipeline/workflows")
async def create_pipeline_workflow(
    payload: PipelineWorkflowRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow_id = _slugify_workflow_id(payload.workflowId or payload.name)
    existing = await session.scalar(select(PipelineWorkflow).where(PipelineWorkflow.workflow_id == workflow_id))
    if existing is not None:
        workflow_id = f"{workflow_id}-{uuid.uuid4().hex[:6]}"
    graph = payload.graph.model_copy(update={"workflowId": workflow_id})
    _validate_pipeline_graph(graph)
    workflow = PipelineWorkflow(
        workflow_id=workflow_id,
        user_id=user.identity,
        name=payload.name,
        description=payload.description,
        graph=graph.model_dump(),
    )
    session.add(workflow)
    await session.commit()
    await session.refresh(workflow)
    return {"workflow": _serialize_workflow(workflow)}


@app.get("/pipeline/workflows/{workflow_id}")
async def get_pipeline_workflow(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    return {"workflow": _serialize_workflow(workflow)}


@app.put("/pipeline/workflows/{workflow_id}")
async def update_pipeline_workflow(
    workflow_id: str,
    payload: PipelineWorkflowUpdateRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    if payload.name is not None:
        workflow.name = payload.name
    if payload.description is not None:
        workflow.description = payload.description
    if payload.graph is not None:
        graph = payload.graph.model_copy(update={"workflowId": workflow_id})
        _validate_pipeline_graph(graph)
        workflow.graph = graph.model_dump()
    workflow.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(workflow)
    return {"workflow": _serialize_workflow(workflow)}


@app.delete("/pipeline/workflows/{workflow_id}")
async def delete_pipeline_workflow(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    workflow = await _get_user_workflow(session, workflow_id, user.identity)
    await session.delete(workflow)
    await session.commit()
    return {"ok": True, "workflowId": workflow_id}


@app.get("/pipeline/workflows/{workflow_id}/keys")
async def list_pipeline_api_keys(
    workflow_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    result = await session.scalars(
        select(PipelineApiKey)
        .where(PipelineApiKey.workflow_id == workflow_id, PipelineApiKey.user_id == user.identity)
        .order_by(PipelineApiKey.created_at.desc())
    )
    return {"keys": [_serialize_api_key(api_key) for api_key in result.all()]}


@app.post("/pipeline/workflows/{workflow_id}/keys")
async def create_pipeline_api_key(
    workflow_id: str,
    payload: PipelineApiKeyRequest,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    raw_key = f"{PIPELINE_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    api_key = PipelineApiKey(
        workflow_id=workflow_id,
        user_id=user.identity,
        name=payload.name,
        key_hash=_hash_pipeline_key(raw_key),
        expires_at=payload.expiresAt,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return {"key": _serialize_api_key(api_key), "secret": raw_key}


@app.delete("/pipeline/workflows/{workflow_id}/keys/{key_id}")
async def revoke_pipeline_api_key(
    workflow_id: str,
    key_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_workflow(session, workflow_id, user.identity)
    api_key = await session.scalar(
        select(PipelineApiKey).where(
            PipelineApiKey.key_id == key_id,
            PipelineApiKey.workflow_id == workflow_id,
            PipelineApiKey.user_id == user.identity,
        )
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    api_key.revoked_at = datetime.now(UTC)
    await session.commit()
    return {"ok": True, "keyId": key_id}


@app.post("/pipeline")
async def execute_pipeline(
    request_payload: PipelineExecuteRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Accept pushed JSON, run a persisted workflow, then return or sink the result."""
    user_id, workflow = await _resolve_pipeline_actor(request, request_payload, session)
    graph = PipelineBlueprintPayload.model_validate(workflow.graph)
    _validate_pipeline_graph(graph)
    rows = _pipeline_rows(request_payload)
    output = await _execute_pipeline_graph(rows, graph, request_payload.dryRun)
    result: dict[str, Any] = {
        "ok": True,
        "workflowId": workflow.workflow_id,
        "user": user_id,
        "dryRun": request_payload.dryRun,
        "processed": output["processed"],
        "rowCount": len(output["processed"]),
        "sinks": output["sinks"],
    }
    session.add(
        PipelineRun(
            workflow_id=workflow.workflow_id,
            user_id=user_id,
            status="completed",
            input=request_payload.model_dump(),
            output=result,
        )
    )
    await session.commit()
    return result


@app.post("/custom/analyze-images")
async def analyze_images(
    prompt: str = Form(""),
    files: list[UploadFile] = File(...),
    user: User = Depends(require_auth),
):
    """Analyze uploaded images and return a text description for text-only agent flows."""
    if not files:
        raise HTTPException(status_code=400, detail="No image files were provided")

    content_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Analyze the uploaded image(s) for an agent workflow. "
                "Describe the important visible facts, charts, tables, text, and context that would help answer the user's request. "
                "Keep it concise but specific.\n\n"
                f"User request: {prompt or 'Please analyze this image.'}"
            ),
        }
    ]

    valid_image_count = 0
    for uploaded_file in files:
        content_type = (uploaded_file.content_type or "").strip().lower()
        if not content_type.startswith("image/"):
            continue

        file_bytes = await uploaded_file.read()
        if not file_bytes:
            continue

        valid_image_count += 1
        data_url = f"data:{content_type};base64,{base64.b64encode(file_bytes).decode('ascii')}"
        content_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            }
        )

    if valid_image_count == 0:
        raise HTTPException(status_code=400, detail="No valid image files were provided")

    try:
        model = load_chat_model(_get_configured_model())
        response = await model.ainvoke([HumanMessage(content=content_blocks)])
    except Exception as error:
        if is_media_not_supported_error(error):
            return {
                "supported": False,
                "message": "Sorry, the model do not have image capability.",
                "user": user.identity,
            }
        raise

    return {
        "supported": True,
        "analysis": get_message_text(response).strip(),
        "user": user.identity,
    }


@app.get("/custom/executive-artifacts")
async def list_executive_artifacts(
    thread_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_thread(session, thread_id, user)
    result = await session.scalars(
        select(ExecutiveArtifact)
        .where(
            ExecutiveArtifact.thread_id == thread_id,
            ExecutiveArtifact.user_id == user.identity,
        )
        .order_by(ExecutiveArtifact.created_at.desc())
    )
    artifacts = result.all()
    return {"artifacts": [_serialize_artifact(artifact) for artifact in artifacts]}


@app.get("/custom/executive-artifacts/{artifact_id}")
async def get_executive_artifact(
    artifact_id: str,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    artifact = await session.scalar(
        select(ExecutiveArtifact).where(
            ExecutiveArtifact.artifact_id == artifact_id,
            ExecutiveArtifact.user_id == user.identity,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    await _get_user_thread(session, artifact.thread_id, user)
    return {"artifact": _serialize_artifact(artifact)}


@app.put("/custom/executive-artifacts/thread/{thread_id}")
async def replace_executive_artifacts(
    thread_id: str,
    request: Request,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_thread(session, thread_id, user)

    body = await request.body()
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            raw_payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid JSON request body for executive artifacts content_type=%s length=%s",
                content_type,
                len(body),
            )
            raise HTTPException(status_code=422, detail="Invalid JSON request body") from exc

        parsed_request = ReplaceExecutiveArtifactsRequest.model_validate(raw_payload)
        encoded_artifacts = _decode_transport_json(parsed_request.artifactsBase64, "artifacts")
        artifact_payloads = (
            [ExecutiveArtifactPayload.model_validate(artifact) for artifact in encoded_artifacts]
            if encoded_artifacts is not None
            else parsed_request.artifacts
        )
    else:
        encoded_artifacts = body.decode("utf-8").strip()
        decoded_artifacts = _decode_transport_json(encoded_artifacts, "artifacts") or []
        artifact_payloads = [ExecutiveArtifactPayload.model_validate(artifact) for artifact in decoded_artifacts]

    logger.info(
        "Replacing executive artifacts thread_id=%s count=%s content_type=%s",
        thread_id,
        len(artifact_payloads),
        content_type or "<missing>",
    )

    await session.execute(
        delete(ExecutiveArtifact).where(
            ExecutiveArtifact.thread_id == thread_id,
            ExecutiveArtifact.user_id == user.identity,
        )
    )

    artifacts = []
    for artifact in artifact_payloads:
        title = _decode_transport_string(artifact.titleBase64, artifact.title, "title")
        content = _decode_transport_string(artifact.contentBase64, artifact.content, "content")
        artifacts.append(
            ExecutiveArtifact(
                artifact_id=artifact.id,
                thread_id=thread_id,
                user_id=user.identity,
                title=title,
                content=content,
                agent_id=artifact.agentId,
                artifact_kind=artifact.artifactKind,
                source_message_id=artifact.sourceMessageId,
                metadata_dict=artifact.metadata,
                created_at=artifact.timestamp,
                updated_at=artifact.timestamp,
            )
        )

    session.add_all(artifacts)
    await session.commit()

    return {
        "ok": True,
        "artifacts": [_serialize_artifact(artifact) for artifact in artifacts],
    }


# -- Articles / OpenSearch document ingestion endpoints --

_ARTICLES_CONFIG_PATH = Path(__file__).resolve().parent / ".aegra-articles.json"


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ArticlesConfigRequest(BaseModel):
    opensearch_url: str
    opensearch_user: str
    opensearch_password: str
    embedding_model: str
    use_ssl: bool = False
    verify_certs: bool = True
    ssl_assert_hostname: bool = True


class ArticlesConfigResponse(BaseModel):
    opensearch_url: str
    opensearch_user: str
    embedding_model: str
    use_ssl: bool
    verify_certs: bool
    ssl_assert_hostname: bool


class IngestDocumentRequest(BaseModel):
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentsRequest(BaseModel):
    index: str
    documents: list[IngestDocumentRequest]
    force: bool = False


def _load_articles_config() -> dict[str, Any] | None:
    articles_url = os.environ.get("ARTICLES_OPENSEARCH_URL")
    articles_user = os.environ.get("ARTICLES_OPENSEARCH_USER")
    articles_password = os.environ.get("ARTICLES_OPENSEARCH_PASSWORD")
    articles_model = os.environ.get("ARTICLES_EMBEDDING_MODEL")
    if articles_url and articles_user and articles_password and articles_model:
        return {
            "opensearch_url": articles_url,
            "opensearch_user": articles_user,
            "opensearch_password": articles_password,
            "embedding_model": articles_model,
            "use_ssl": _read_bool_env("ARTICLES_OPENSEARCH_USE_SSL", False),
            "verify_certs": _read_bool_env("ARTICLES_OPENSEARCH_VERIFY_CERTS", True),
            "ssl_assert_hostname": _read_bool_env("ARTICLES_OPENSEARCH_SSL_ASSERT_HOSTNAME", True),
        }

    kms_host = os.environ.get("KMS_OPENSEARCH_HOST")
    kms_user = os.environ.get("KMS_OPENSEARCH_USER")
    kms_password = os.environ.get("KMS_OPENSEARCH_PASSWORD")
    kms_model = os.environ.get("KMS_EMBEDDING_MODEL")
    if kms_host and kms_user and kms_password and kms_model:
        kms_port = os.environ.get("KMS_OPENSEARCH_PORT", "9200")
        use_ssl = _read_bool_env("KMS_OPENSEARCH_USE_SSL", False)
        scheme = "https" if use_ssl else "http"
        return {
            "opensearch_url": kms_host
            if kms_host.startswith(("http://", "https://"))
            else f"{scheme}://{kms_host}:{kms_port}",
            "opensearch_user": kms_user,
            "opensearch_password": kms_password,
            "embedding_model": kms_model,
            "use_ssl": use_ssl,
            "verify_certs": _read_bool_env("KMS_OPENSEARCH_VERIFY_CERTS", True),
            "ssl_assert_hostname": _read_bool_env("KMS_OPENSEARCH_SSL_ASSERT_HOSTNAME", True),
        }

    if not _ARTICLES_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(_ARTICLES_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_articles_config(data: dict[str, Any]) -> None:
    _ARTICLES_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@lru_cache(maxsize=8)
def _get_articles_embeddings(model_name: str) -> OpenAIEmbeddings:
    init_kwargs: dict[str, str] = {"model": model_name}
    base_url = os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if base_url:
        init_kwargs["base_url"] = base_url
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        init_kwargs["api_key"] = api_key
    return OpenAIEmbeddings(**init_kwargs)


def _opensearch_client(config: dict[str, Any]) -> OpenSearchClient:
    return OpenSearchClient(
        hosts=[config["opensearch_url"]],
        http_auth=(config["opensearch_user"], config["opensearch_password"]),
        use_ssl=config.get("use_ssl", False),
        verify_certs=config.get("verify_certs", True),
        ssl_assert_hostname=config.get("ssl_assert_hostname", True),
    )


def _vector_store_for_index(index_name: str, config: dict[str, Any]) -> OpenSearchVectorSearch:
    return OpenSearchVectorSearch(
        opensearch_url=config["opensearch_url"],
        index_name=index_name,
        embedding_function=_get_articles_embeddings(config["embedding_model"]),
        http_auth=(config["opensearch_user"], config["opensearch_password"]),
        use_ssl=config.get("use_ssl", False),
        verify_certs=config.get("verify_certs", True),
        ssl_assert_hostname=config.get("ssl_assert_hostname", True),
    )


def _articles_index_metadata(mapping: dict[str, Any], index_name: str) -> dict[str, Any]:
    mappings = mapping.get(index_name, {}).get("mappings", {})
    meta = mappings.get("_meta", {}).get("aegra_metadata", {})
    if meta:
        return meta

    # Older local test mappings stored metadata under a field mapping.
    props = mappings.get("properties", {})
    field_meta = props.get("aegra_metadata", {})
    if field_meta.get("value"):
        return field_meta["value"]
    return field_meta


@app.get("/custom/articles/config")
async def get_articles_config(user: User = Depends(require_auth)):
    """Return the persisted articles OpenSearch connection config (without password)."""
    data = _load_articles_config()
    if not data:
        raise HTTPException(status_code=404, detail="No articles config saved yet")
    return ArticlesConfigResponse(
        opensearch_url=data["opensearch_url"],
        opensearch_user=data["opensearch_user"],
        embedding_model=data["embedding_model"],
        use_ssl=data.get("use_ssl", False),
        verify_certs=data.get("verify_certs", True),
        ssl_assert_hostname=data.get("ssl_assert_hostname", True),
    ).model_dump()


@app.post("/custom/articles/config")
async def save_articles_config(
    request: ArticlesConfigRequest,
    user: User = Depends(require_auth),
):
    """Persist the articles OpenSearch connection config for future use."""
    data = request.model_dump()
    _save_articles_config(data)
    logger.info(
        "Articles config saved by user=%s url=%s model=%s",
        user.identity,
        request.opensearch_url,
        request.embedding_model,
    )
    return ArticlesConfigResponse(
        opensearch_url=data["opensearch_url"],
        opensearch_user=data["opensearch_user"],
        embedding_model=data["embedding_model"],
        use_ssl=data.get("use_ssl", False),
        verify_certs=data.get("verify_certs", True),
        ssl_assert_hostname=data.get("ssl_assert_hostname", True),
    ).model_dump()


@app.get("/custom/articles/indexes")
async def list_articles_indexes(user: User = Depends(require_auth)):
    """List all OpenSearch indexes with document counts and embedding model metadata."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    client = _opensearch_client(config)
    try:
        all_indexes = client.indices.get_alias(index="*")
    except TransportError as exc:
        logger.error("Failed to list OpenSearch indexes: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    indexes = []
    for index_name in sorted(all_indexes.keys()):
        info: dict[str, Any] = {"name": index_name}
        try:
            stats = client.count(index=index_name)
            info["documentCount"] = stats.get("count", 0)
        except TransportError:
            info["documentCount"] = 0

        try:
            mapping = client.indices.get_mapping(index=index_name)
            info["embeddingModel"] = _articles_index_metadata(mapping, index_name).get("embedding_model")
        except TransportError:
            pass

        indexes.append(info)

    return {"indexes": indexes}


@app.post("/custom/articles/indexes")
async def create_articles_index(
    index_name: str = Query(..., description="Name for the new index"),
    user: User = Depends(require_auth),
):
    """Create a new OpenSearch index with embedding model metadata."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    client = _opensearch_client(config)

    try:
        if client.indices.exists(index=index_name):
            raise HTTPException(status_code=409, detail=f"Index '{index_name}' already exists")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    try:
        client.indices.create(
            index=index_name,
            body={
                "mappings": {
                    "_meta": {
                        "aegra_metadata": {
                            "embedding_model": config["embedding_model"],
                            "created_by": user.identity,
                        }
                    }
                }
            },
        )
    except TransportError as exc:
        logger.error("Failed to create index %s: %s", index_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to create index: {exc.error or str(exc)}",
        ) from exc

    logger.info("Created articles index=%s by user=%s model=%s", index_name, user.identity, config["embedding_model"])
    return {"ok": True, "index": index_name, "embeddingModel": config["embedding_model"]}


@app.post("/custom/articles/ingest")
async def ingest_articles(
    request: IngestDocumentsRequest,
    user: User = Depends(require_auth),
):
    """Ingest documents into an OpenSearch index with optional embedding model validation."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. POST /custom/articles/config first.")

    if not request.documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    client = _opensearch_client(config)

    try:
        if not client.indices.exists(index=request.index):
            raise HTTPException(status_code=404, detail=f"Index '{request.index}' does not exist")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    # Check embedding model mismatch
    if not request.force:
        try:
            mapping = client.indices.get_mapping(index=request.index)
            stored_model = _articles_index_metadata(mapping, request.index).get("embedding_model")
            if stored_model and stored_model != config["embedding_model"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Embedding model mismatch: index uses '{stored_model}', "
                        f"config has '{config['embedding_model']}'. "
                        f"Set force=true to override."
                    ),
                )
        except HTTPException:
            raise
        except TransportError:
            pass

    try:
        vector_store = _vector_store_for_index(request.index, config)
        documents = [Document(page_content=doc.content, metadata=doc.metadata) for doc in request.documents]
        vector_store.add_documents(documents)
    except Exception as exc:
        logger.error("Failed to ingest documents into %s: %s", request.index, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to ingest documents: {exc}",
        ) from exc

    logger.info(
        "Ingested %d documents into index=%s by user=%s",
        len(request.documents),
        request.index,
        user.identity,
    )
    return {"ok": True, "index": request.index, "ingested": len(request.documents)}


class ArticleChatRequest(BaseModel):
    message: str
    index_name: str
    force: bool = False


@app.post("/custom/articles/chat")
async def article_chat(
    request: ArticleChatRequest,
    user: User = Depends(require_auth),
):
    """Chat with AI over a selected OpenSearch index. Retrieval runs automatically."""
    config = _load_articles_config()
    if not config:
        raise HTTPException(status_code=400, detail="No articles config saved. Configure indexes first.")

    index_name = request.index_name.strip()
    if not index_name:
        raise HTTPException(status_code=400, detail="No index selected. Choose an index before chatting.")

    client = _opensearch_client(config)

    try:
        if not client.indices.exists(index=index_name):
            raise HTTPException(status_code=404, detail=f"Index '{index_name}' does not exist")
    except TransportError as exc:
        logger.error("Failed to check index existence: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to OpenSearch: {exc.error or str(exc)}",
        ) from exc

    # Check embedding model mismatch
    if not request.force:
        try:
            mapping = client.indices.get_mapping(index=index_name)
            stored_model = _articles_index_metadata(mapping, index_name).get("embedding_model")
            if stored_model and stored_model != config["embedding_model"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Embedding model mismatch: index uses '{stored_model}', "
                        f"config has '{config['embedding_model']}'. Contact an administrator."
                    ),
                )
        except HTTPException:
            raise
        except TransportError:
            pass

    try:
        vector_store = _vector_store_for_index(index_name, config)
        query = request.message.strip()
        results = vector_store.similarity_search(query, k=5)
    except Exception as exc:
        logger.error("Failed to query index %s: %s", index_name, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not query index '{index_name}': {exc}",
        ) from exc

    if not results:
        context_text = ""
    else:
        context_parts = []
        for i, doc in enumerate(results):
            source = doc.metadata.get("source", doc.metadata.get("file_name", "unknown"))
            context_parts.append(f"[Document {i + 1}] Source: {source}\n{doc.page_content}")
        context_text = "\n\n---\n\n".join(context_parts)

    prompt = f"Use the following retrieved context to answer the question. If the context does not contain relevant information, say so clearly.\n\n{context_text}\n\n---\n\nQuestion: {query}"

    try:
        model = load_chat_model(_get_configured_model())
        response = await model.ainvoke([HumanMessage(content=prompt)])
        answer = get_message_text(response).strip()
    except Exception as exc:
        logger.error("Model generation failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Model generation failed: {exc}",
        ) from exc

    logger.info(
        "Article chat completed index=%s user=%s",
        index_name,
        user.identity,
    )
    return {
        "answer": answer,
        "index": index_name,
        "retrieved_count": len(results),
    }
