"""Read-only staging inspection. Never print environment values or subprocess errors."""

import argparse
import json
import os
import posixpath
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def command_json(arguments: list[str], environment: dict[str, str] | None = None) -> Any:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=30, env=environment)
    if result.returncode:
        raise ValueError("A Docker inspection command failed; examine it privately on the VM.")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("Docker returned an unsupported inspection format.") from error


def public_origin(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    return None


def sqlite_path(url: str) -> str | None:
    if not url.startswith("sqlite") or ":///" not in url or ":memory:" in url:
        return None
    path = url.split(":///", 1)[1].split("?", 1)[0]
    return posixpath.normpath(path if path.startswith("/") else f"/app/{path}")


def runtime_override(service: str, mounts: list[dict[str, Any]]) -> dict[str, Any]:
    volumes: dict[str, Any] = {}
    application_mounts: list[dict[str, Any]] = []
    for index, mount in enumerate(mounts):
        if mount["Type"] not in {"bind", "volume"}:
            raise ValueError("Unsupported application mount type.")
        source = mount["Source"]
        if mount["Type"] == "volume":
            source = f"preserved_{service}_{index}"
            volumes[source] = {"external": True, "name": mount["Name"]}
        application_mounts.append({
            "type": mount["Type"], "source": source, "target": mount["Destination"],
            "read_only": not mount.get("RW", True),
        })
    override: dict[str, Any] = {"services": {service: {"volumes": application_mounts}}}
    if volumes:
        override["volumes"] = volumes
    return override


def inspect_stack(directory: Path, env_file: Path, service: str) -> dict[str, Any]:
    compose = ["docker", "compose", "--env-file", str(env_file), "-f", str(directory / "docker-compose-staging.yaml")]
    if (directory / ".onyx-runtime.json").is_file():
        compose.extend(["-f", str(directory / ".onyx-runtime.json")])
    config = command_json([*compose, "config", "--format", "json"], {**os.environ, "APP_ENV_FILE": str(env_file)})
    project = config["name"]
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.split()
    containers = command_json(["docker", "inspect", *ids]) if ids else []
    configured_environment = config["services"][service].get("environment", {})
    actual = next(
        (item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == service), None
    )
    blockers: list[str] = []
    running = sorted(
        item["Config"]["Labels"].get("com.docker.compose.service", "")
        for item in containers if item["State"]["Running"]
    )
    if actual is None or not actual["State"]["Running"]:
        blockers.append(f"Existing {service} container must be running for this update-only playbook.")
    # This deployment path does not silently create or upgrade infrastructure.
    required = {"aegra": {"postgres", "keycloak"}, "app": {"minio"}}[service]
    if service == "aegra" and str(configured_environment.get("REDIS_BROKER_ENABLED", "false")).lower() == "true":
        required.add("redis")
    if not required.issubset(set(running)):
        blockers.append("Required infrastructure is not running in the existing Compose project.")
    database = str(configured_environment.get("ANALYTIC_DB_URL", ""))
    if not database and configured_environment.get("ANALYTIC_DB_DIALECT", "sqlite") == "sqlite":
        name = configured_environment.get("ANALYTIC_DB_NAME", "")
        database = f"sqlite:///{name}" if name else ""
    path = sqlite_path(database)
    mounts = actual.get("Mounts", []) if actual else []
    for mount in mounts:
        target = mount["Destination"].rstrip("/")
        if target in {"/app", "/app/src", "/app/graphs", "/app/node_modules", "/app/.next", "/app/.next-local"}:
            blockers.append("An application source/build mount would hide the release image. Review it before deployment.")
    if path and not any(
        path == mount["Destination"] or path.startswith(mount["Destination"].rstrip("/") + "/")
        for mount in mounts
    ):
        blockers.append("Analytic SQLite storage is inside the application container. Persist it before recreating Aegra.")
    labels = actual["Config"]["Labels"] if actual else {}
    if actual and labels.get("com.docker.compose.project.working_dir") != str(directory):
        blockers.append("Running project uses a different working directory. Inspect its release before another deployment.")
    primary_url = str(configured_environment.get("DATABASE_URL", "")).strip()
    if service == "aegra" and primary_url:
        database_url = urlsplit(primary_url)
        postgres_db = config["services"].get("postgres", {}).get("environment", {}).get("POSTGRES_DB", "")
        if database_url.hostname != "postgres" or database_url.path.lstrip("/") != postgres_db:
            blockers.append("DATABASE_URL does not target this Compose PostgreSQL database. Arrange its backup separately.")
    return {
        "project": project,
        "service": service,
        "running_services": running,
        "application_image_id": actual.get("Image") if actual else None,
        "runtime_override": runtime_override(service, mounts),
        "persistent_mounts": [
            {"service": item["Config"]["Labels"].get("com.docker.compose.service"),
             "type": mount["Type"], "name": mount.get("Name"), "destination": mount["Destination"]}
            for item in containers for mount in item.get("Mounts", [])
            if mount["Type"] == "volume"
        ],
        "configured_origins": {
            key: public_origin(str(configured_environment[key]))
            for key in ("AUTH_URL", "NEXTAUTH_URL", "KEYCLOAK_ISSUER", "KEYCLOAK_JWKS_URL", "LANGGRAPH_URL")
            if configured_environment.get(key)
        },
        "blockers": blockers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    args = parser.parse_args()
    try:
        for name in ("env.staging.backend", "env.staging.frontend", "keycloak-realm-aegra.staging.json"):
            if not (args.secrets / name).is_file():
                raise ValueError(f"Required VM file is missing: {name}")
        result = {
            "backend": inspect_stack(args.backend, args.secrets / "env.staging.backend", "aegra"),
            "frontend": inspect_stack(args.frontend, args.secrets / "env.staging.frontend", "app"),
        }
        print(json.dumps(result))
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"error": "Preflight failed. Check configured paths, Docker access, and Compose privately on the VM."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
