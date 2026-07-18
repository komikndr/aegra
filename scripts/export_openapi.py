"""Export the OpenAPI spec to docs/openapi.json

Generates the spec from the FastAPI app, normalizes the title/version to
match the Aegra project defaults, and removes any custom-route endpoints
that are project-specific rather than part of the standard API.

NOTE: Environment variables must be set before importing the app, so the
imports below are intentionally placed after os.environ calls.
"""

import argparse
import json
import logging
import os
from importlib.metadata import version
from pathlib import Path

# Suppress all logging before imports — env vars must be set first
logging.disable(logging.CRITICAL)
os.environ["AEGRA__APP__LOG_LEVEL"] = "CRITICAL"
os.environ.setdefault("AEGRA__APP__PROJECT_NAME", "Aegra")
os.environ.setdefault("AEGRA__APP__VERSION", version("aegra-api"))
os.environ.setdefault("AEGRA__APP__DEBUG", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://export:export@localhost:5432/export")
os.environ["AEGRA_CONFIG"] = ""
os.environ["AUTH_TYPE"] = "noop"
os.environ["ENV_MODE"] = "LOCAL"
# Config discovery falls back to ./aegra.json. Run from this script directory,
# which intentionally has no deployment config, so custom routes and schemas do
# not leak into the standard Agent Protocol document.
os.chdir(Path(__file__).resolve().parent)

import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
)

from aegra_api.main import OPENAPI_TAGS, app  # noqa: E402
from aegra_api.settings import settings  # noqa: E402


def _render_schema() -> str:
    """Generate the deterministic standard Agent Protocol schema."""
    schema = app.openapi()

    # Normalize title and description to standard Aegra values
    schema["info"]["title"] = "Aegra"
    schema["info"]["version"] = settings.app.VERSION
    schema["info"]["description"] = "Production-ready Agent Protocol server"

    # Remove custom-route endpoints and untagged paths (root, custom routes)
    core_tags: set[str] = {t["name"] for t in OPENAPI_TAGS}
    paths_to_remove: list[str] = []
    for path, methods in schema.get("paths", {}).items():
        for _method, info in methods.items():
            tags = set(info.get("tags", []))
            if not tags or not tags & core_tags:
                paths_to_remove.append(path)
                break

    for path in paths_to_remove:
        del schema["paths"][path]

    # Keep only core tags
    schema["tags"] = [t for t in schema.get("tags", []) if t["name"] in core_tags]

    return json.dumps(schema, indent=2) + "\n"


def main() -> None:
    """Export or verify the standard Agent Protocol OpenAPI spec."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail when docs/openapi.json is stale")
    args = parser.parse_args()

    output_path = Path(__file__).parent.parent / "docs" / "openapi.json"
    rendered = _render_schema()
    if args.check:
        current = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        if current != rendered:
            raise SystemExit("docs/openapi.json is stale; run scripts/export_openapi.py")
        print(f"OpenAPI schema is current: {output_path}")
        return

    output_path.write_text(rendered, encoding="utf-8")
    print(f"Exported standard Agent Protocol schema to {output_path}")


if __name__ == "__main__":
    main()
