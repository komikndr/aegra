"""Read-only deployment inspection and secret-redaction regression checks."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def inspection() -> ModuleType:
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location("onyx_staging_inspection", root / "deployments/ansible/inspect_stack.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inspection_preserves_stack_env_and_redacts_secrets(inspection: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = {
        "POSTGRES_PASSWORD": "secret-database-password",
        "REDIS_BROKER_ENABLED": "true",
        "ANALYTIC_DB_URL": "sqlite:///./analytics.db",
        "KEYCLOAK_JWKS_URL": "http://login:secret-url-password@10.10.5.11:8080/keys?token=secret-query",
    }
    config = {"name": "old-aegra-project", "services": {"aegra": {"environment": environment}}}
    containers = [
        {"Config": {"Labels": {
            "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": "/home/kms/aegra",
        }}, "Image": "sha256:previous-image", "State": {"Running": True}, "Mounts": []}
        for service in ("aegra", "postgres", "keycloak", "redis")
    ]
    containers[1]["Mounts"] = [{"Type": "volume", "Name": "old-aegra-project_postgres_data", "Destination": "/var/lib/postgresql"}]

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        if "config" in arguments:
            assert kwargs["env"]["APP_ENV_FILE"] == "/private/env.staging.backend"
            return SimpleNamespace(returncode=0, stdout=json.dumps(config))
        if "inspect" in arguments:
            return SimpleNamespace(returncode=0, stdout=json.dumps(containers))
        assert "label=com.docker.compose.project=old-aegra-project" in arguments
        return SimpleNamespace(returncode=0, stdout="container-id")

    monkeypatch.setattr(inspection.subprocess, "run", run)
    report = inspection.inspect_stack(Path("/home/kms/aegra"), Path("/private/env.staging.backend"), "aegra")
    output = json.dumps(report)
    assert "secret-" not in output
    assert report["configured_origins"]["KEYCLOAK_JWKS_URL"] == "http://10.10.5.11:8080"
    assert report["project"] == "old-aegra-project"
    assert report["persistent_mounts"][0]["name"] == "old-aegra-project_postgres_data"
    assert any("SQLite" in reason for reason in report["blockers"])

    containers[0]["Mounts"] = [{"Type": "bind", "Source": "/data/analytics.db", "Destination": "/app/analytics.db", "RW": True}]
    report = inspection.inspect_stack(Path("/home/kms/aegra"), Path("/private/env.staging.backend"), "aegra")
    assert report["blockers"] == []
    assert report["runtime_override"]["services"]["aegra"]["volumes"][0]["source"] == "/data/analytics.db"

    environment["DATABASE_URL"] = "postgresql://user:secret@external-db/other"
    report = inspection.inspect_stack(Path("/home/kms/aegra"), Path("/private/env.staging.backend"), "aegra")
    assert any("DATABASE_URL" in reason for reason in report["blockers"])


def test_existing_named_data_mount_retains_external_volume_identity(inspection: ModuleType) -> None:
    override = inspection.runtime_override("aegra", [{
        "Type": "volume", "Source": "/var/lib/docker/volumes/old/_data", "Name": "old_data_volume",
        "Destination": "/data", "RW": False,
    }])
    assert override["volumes"]["preserved_aegra_0"] == {"external": True, "name": "old_data_volume"}
    assert override["services"]["aegra"]["volumes"][0] == {
        "type": "volume", "source": "preserved_aegra_0", "target": "/data", "read_only": True,
    }


def test_staging_path_and_origin_validation(inspection: ModuleType) -> None:
    assert inspection.sqlite_path("sqlite:////data/analytics.db") == "/data/analytics.db"
    assert inspection.sqlite_path("sqlite:///./analytics.db") == "/app/analytics.db"
    assert inspection.sqlite_path("sqlite:///:memory:") is None
    assert inspection.sqlite_path("postgresql://user:password@db/main") is None
    assert inspection.public_origin("https://user:secret@example.com/path?token=secret") == "https://example.com:443"
    assert inspection.public_origin("not-a-url") is None
