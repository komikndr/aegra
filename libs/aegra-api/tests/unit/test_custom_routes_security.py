from typing import Any

import custom_routes_example as routes
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from aegra_api.models.auth import User


def _graph(*nodes: routes.PipelineNodePayload) -> routes.PipelineBlueprintPayload:
    return routes.PipelineBlueprintPayload(
        workflowId="workflow",
        description="test",
        nodes=list(nodes),
        edges=[],
    )


def _source() -> routes.PipelineNodePayload:
    return routes.PipelineNodePayload(id="source", type="source", label="Source")


def test_pipeline_sink_nodes_are_rejected() -> None:
    graph = _graph(
        _source(),
        routes.PipelineNodePayload(
            id="sink",
            type="sink",
            label="Sink",
            config={"url": "http://169.254.169.254/latest/meta-data"},
        ),
    )

    with pytest.raises(HTTPException, match="Pipeline sink nodes are disabled"):
        routes._validate_pipeline_graph(graph)


@pytest.mark.parametrize("field", ["model", "baseUrl", "base_url", "apiKey", "api_key"])
def test_pipeline_formatter_rejects_workflow_provider_settings(field: str) -> None:
    graph = _graph(
        _source(),
        routes.PipelineNodePayload(
            id="formatter",
            type="formatter",
            label="Formatter",
            config={"modelId": "safe", field: "attacker-controlled"},
        ),
    )

    with pytest.raises(HTTPException, match="provider settings are server-managed"):
        routes._validate_pipeline_graph(graph)


def test_pipeline_formatter_requires_known_server_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_model(_model_id: str) -> Any:
        raise ValueError("unknown")

    monkeypatch.setattr(routes, "resolve_pipeline_model", reject_model)
    graph = _graph(
        _source(),
        routes.PipelineNodePayload(
            id="formatter",
            type="formatter",
            label="Formatter",
            config={"modelId": "attacker-model"},
        ),
    )

    with pytest.raises(HTTPException, match="Unknown pipeline modelId"):
        routes._validate_pipeline_graph(graph)


@pytest.mark.asyncio
async def test_legacy_sink_is_rejected_before_execution() -> None:
    graph = _graph(
        _source(),
        routes.PipelineNodePayload(
            id="sink",
            type="sink",
            label="Legacy sink",
            config={"url": "http://127.0.0.1:8000"},
        ),
    )

    with pytest.raises(HTTPException, match="Pipeline sink nodes are disabled"):
        await routes._execute_pipeline_graph([{"secret": "value"}], graph, dry_run=False)


def test_articles_routes_require_superadmin() -> None:
    with pytest.raises(HTTPException, match="SuperAdmin permission is required") as exc_info:
        routes._require_articles_admin(User(identity="ordinary", permissions=["Staff"]))

    assert exc_info.value.status_code == 403
    assert routes._require_articles_admin(User(identity="admin", permissions=["SuperAdmin"])).identity == "admin"


@pytest.mark.parametrize(
    "index_name",
    ["kms-private", "articles-*", "articles/secret", "Articles-private", "articles-" + "a" * 100],
)
def test_articles_index_names_are_prefix_scoped(index_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTICLES_INDEX_PREFIX", "articles-")

    with pytest.raises(HTTPException, match="Invalid articles index name"):
        routes._articles_index_name(index_name)


def test_articles_document_payload_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "ARTICLES_MAX_TOTAL_CHARS", 5)
    documents = [routes.IngestDocumentRequest(content="three"), routes.IngestDocumentRequest(content="more")]

    with pytest.raises(HTTPException, match="Articles payload is too large") as exc_info:
        routes._validate_articles_documents(documents)

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_articles_runtime_config_mutation_is_disabled() -> None:
    user = User(identity="admin", permissions=["SuperAdmin"])

    with pytest.raises(HTTPException, match="server-managed") as exc_info:
        await routes.save_articles_config(user)

    assert exc_info.value.status_code == 405


def _png_bytes(width: int = 1, height: int = 1) -> bytes:
    output = routes.io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(output, format="PNG")
    return output.getvalue()


def test_image_validation_uses_decoded_format() -> None:
    assert routes._validated_image_mime(_png_bytes()) == "image/png"

    with pytest.raises(HTTPException, match="Invalid image file"):
        routes._validated_image_mime(b"not really an image")


def test_image_validation_rejects_excessive_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "IMAGE_ANALYSIS_MAX_PIXELS", 1)

    with pytest.raises(HTTPException, match="Image dimensions are too large") as exc_info:
        routes._validated_image_mime(_png_bytes(width=2, height=2))

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_image_count_limit_closes_uploads_before_model_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "IMAGE_ANALYSIS_MAX_FILES", 1)
    monkeypatch.setattr(routes, "load_chat_model", lambda *_args, **_kwargs: pytest.fail("model was invoked"))
    files = [
        UploadFile(filename="one.png", file=routes.io.BytesIO(_png_bytes())),
        UploadFile(filename="two.png", file=routes.io.BytesIO(_png_bytes())),
    ]

    with pytest.raises(HTTPException, match="Too many image files") as exc_info:
        await routes.analyze_images(prompt="analyze", files=files, user=User(identity="user"))

    assert exc_info.value.status_code == 413
    assert all(upload.file.closed for upload in files)
