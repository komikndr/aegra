"""Tests for standard and deployment-specific OpenAPI separation."""

from fastapi import FastAPI

from aegra_api.main import CUSTOM_OPENAPI_TAGS, OPENAPI_TAGS, _apply_custom_route_tags


def test_custom_routes_receive_specific_tags() -> None:
    app = FastAPI()

    @app.get("/dbchat/connections")
    async def dbchat() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/pipeline")
    async def pipeline() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/custom/articles/config")
    async def articles() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/custom/executive-artifacts")
    async def artifacts() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/custom/analyze-images")
    async def media() -> dict[str, bool]:
        return {"ok": True}

    _apply_custom_route_tags(app)
    operations = [operation for path in app.openapi()["paths"].values() for operation in path.values()]
    assert [operation["tags"][0] for operation in operations] == [
        "Onyx DBChat",
        "Onyx Pipeline",
        "Onyx Articles",
        "Onyx Artifacts",
        "Onyx Media",
    ]


def test_standard_and_custom_tag_names_do_not_overlap() -> None:
    standard = {tag["name"] for tag in OPENAPI_TAGS}
    custom = {tag["name"] for tag in CUSTOM_OPENAPI_TAGS}
    assert standard.isdisjoint(custom)
