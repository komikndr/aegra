"""Database migration utilities for Aegra.

Provides automatic Alembic migration support for development and installed
deployments without trusting an unrelated alembic.ini from the process CWD.
"""

import asyncio
from pathlib import Path

import psycopg
import structlog
from alembic.config import Config
from alembic.script import ScriptDirectory

from aegra_api.settings import settings
from alembic import command

logger = structlog.get_logger(__name__)


def find_alembic_ini() -> Path:
    """Find alembic.ini file.

    Resolution order:
    1. Bundled with aegra_api package (pip install)
    2. Development source layout

    Returns:
        Absolute path to alembic.ini

    Raises:
        FileNotFoundError: If alembic.ini cannot be found
    """
    # 1. Package bundled (pip install aegra-api)
    # In installed package: site-packages/aegra_api/alembic.ini
    package_dir = Path(__file__).resolve().parent.parent  # aegra_api/
    package_ini = package_dir / "alembic.ini"
    if package_ini.exists():
        return package_ini

    # 2. Development layout (src layout: libs/aegra-api/src/aegra_api/ → libs/aegra-api/)
    dev_root = package_dir.parent.parent  # Up from src/aegra_api/ to libs/aegra-api/
    dev_ini = dev_root / "alembic.ini"
    if dev_ini.exists():
        return dev_ini

    raise FileNotFoundError(
        "Could not find alembic.ini. Ensure aegra-api is properly installed or run from the project root."
    )


def get_alembic_config() -> Config:
    """Create Alembic Config with correct paths.

    Works in both development (repo) and production (pip install) environments.
    Resolves relative script_location to absolute path so migrations work
    regardless of CWD.

    Returns:
        Configured Alembic Config object
    """
    ini_path = find_alembic_ini()
    cfg = Config(str(ini_path))

    # Resolve script_location to absolute path so it works from any CWD
    script_location = cfg.get_main_option("script_location")
    if script_location and not Path(script_location).is_absolute():
        abs_script_location = str((ini_path.parent / script_location).resolve())
        cfg.set_main_option("script_location", abs_script_location)

    return cfg


def run_migrations() -> None:
    """Run all pending database migrations synchronously.

    Uses Alembic's upgrade command to apply all pending migrations.
    Safe to call repeatedly - Alembic is idempotent and uses database
    locks to prevent concurrent migration conflicts.
    """
    cfg = get_alembic_config()
    logger.info("Running database migrations...")
    command.upgrade(cfg, "head")
    logger.info("Database migrations completed")


def _is_database_up_to_date(cfg: Config) -> bool:
    """Return whether the database revision matches the single script head."""
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        return True

    with psycopg.connect(settings.db.database_url_sync) as connection, connection.cursor() as cursor:
        try:
            cursor.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cursor.fetchone()
            current = row[0] if row else None
        except psycopg.errors.UndefinedTable:
            connection.rollback()
            current = None

    return current == head


def run_migrations_if_needed() -> None:
    """Skip Alembic when already at head and bootstrap on precheck failure."""
    cfg = get_alembic_config()
    try:
        if _is_database_up_to_date(cfg):
            logger.debug("Database already at migration head; skipping upgrade")
            return
    except Exception as exc:
        logger.debug("Migration precheck failed; running upgrade", error=str(exc))

    logger.info("Running database migrations...")
    command.upgrade(cfg, "head")
    logger.info("Database migrations completed")


async def run_migrations_async() -> None:
    """Run all pending database migrations (async-safe).

    Wraps the synchronous migration in a thread executor because Alembic's
    env.py uses asyncio.run() internally, which requires its own event loop.
    """
    await asyncio.to_thread(run_migrations_if_needed)
