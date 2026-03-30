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

import base64
import json
import logging
import os
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from react_agent.utils import (
    get_active_model_name,
    get_message_text,
    is_media_not_supported_error,
    load_chat_model,
    resolve_model_name,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent_server.core.auth_deps import require_auth
from src.agent_server.core.orm import ExecutiveArtifact, Thread, get_session
from src.agent_server.models.auth import User

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


def _normalize_base64(encoded_value: str) -> str:
    normalized = encoded_value.strip().replace("-", "+").replace("_", "/")
    padding = len(normalized) % 4
    if padding:
        normalized += "=" * (4 - padding)
    return normalized


def _decode_transport_string(
    encoded_value: str | None, raw_value: str | None, field_name: str
) -> str:
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


def _decode_transport_json(
    encoded_value: str | None, field_name: str
) -> list[dict[str, Any]] | None:
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


async def _get_user_thread(
    session: AsyncSession, thread_id: str, user_id: str
) -> Thread:
    thread = await session.scalar(
        select(Thread).where(Thread.thread_id == thread_id, Thread.user_id == user_id)
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
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
async def public_endpoint():
    """Public endpoint - no auth dependency explicitly added.

    This endpoint will be protected if enable_custom_route_auth is True,
    otherwise it will be public. Useful for testing the enable_custom_route_auth config.
    """
    return {
        "message": "This is public by default",
        "note": "Protected if enable_custom_route_auth is enabled",
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
async def model_info(user: User = Depends(require_auth)):
    """Return configured and resolved active model info for the UI."""
    configured_model = _get_configured_model()
    resolved_provider, resolved_model = resolve_model_name(configured_model)
    return {
        "user": user.identity,
        "configured_model": configured_model,
        "active_model": f"{resolved_provider}/{resolved_model}",
        "active_model_name": resolved_model,
        "provider": resolved_provider,
        "vllm_base_url": os.environ.get("VLLM_BASE_URL", "").strip() or None,
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "").strip() or None,
        "endpoint_active_model": get_active_model_name(),
    }


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
        data_url = (
            f"data:{content_type};base64,{base64.b64encode(file_bytes).decode('ascii')}"
        )
        content_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            }
        )

    if valid_image_count == 0:
        raise HTTPException(
            status_code=400, detail="No valid image files were provided"
        )

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
    await _get_user_thread(session, thread_id, user.identity)
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
    return {"artifact": _serialize_artifact(artifact)}


@app.put("/custom/executive-artifacts/thread/{thread_id}")
async def replace_executive_artifacts(
    thread_id: str,
    request: Request,
    user: User = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
):
    await _get_user_thread(session, thread_id, user.identity)

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
            raise HTTPException(
                status_code=422, detail="Invalid JSON request body"
            ) from exc

        parsed_request = ReplaceExecutiveArtifactsRequest.model_validate(raw_payload)
        encoded_artifacts = _decode_transport_json(
            parsed_request.artifactsBase64, "artifacts"
        )
        artifact_payloads = (
            [
                ExecutiveArtifactPayload.model_validate(artifact)
                for artifact in encoded_artifacts
            ]
            if encoded_artifacts is not None
            else parsed_request.artifacts
        )
    else:
        encoded_artifacts = body.decode("utf-8").strip()
        decoded_artifacts = _decode_transport_json(encoded_artifacts, "artifacts") or []
        artifact_payloads = [
            ExecutiveArtifactPayload.model_validate(artifact)
            for artifact in decoded_artifacts
        ]

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
        content = _decode_transport_string(
            artifact.contentBase64, artifact.content, "content"
        )
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
