"""Tests for aegra_api.core.migrations module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aegra_api.core import migrations as migrations_mod
from aegra_api.core.migrations import find_alembic_ini, get_alembic_config


class TestFindAlembicIni:
    """Tests for find_alembic_ini() resolution order."""

    def test_finds_alembic_ini_in_package(self, tmp_path, monkeypatch):
        """Should find alembic.ini bundled with the aegra_api package."""
        # CWD has no alembic.ini
        monkeypatch.chdir(tmp_path)

        # The package directory should have alembic.ini (from force-include)
        # We verify it finds the file relative to migrations.py's parent.parent
        result = find_alembic_ini()
        # Should be in the package directory (aegra_api/)
        assert result.name == "alembic.ini"
        assert result.exists()

    def test_ignores_foreign_cwd_ini(self, tmp_path, monkeypatch):
        """A host project's Alembic config must never shadow Aegra's."""
        monkeypatch.chdir(tmp_path)
        cwd_ini = tmp_path / "alembic.ini"
        cwd_ini.write_text("[alembic]\nscript_location = not-aegra\n")

        result = find_alembic_ini()

        assert result.resolve() != cwd_ini.resolve()

    def test_discovery_is_independent_of_cwd(self, tmp_path, monkeypatch):
        """Changing CWD must not change the selected migration bundle."""
        baseline = find_alembic_ini()
        monkeypatch.chdir(tmp_path)
        assert find_alembic_ini() == baseline

    def test_raises_when_not_found(self, tmp_path, monkeypatch):
        """Should raise FileNotFoundError when alembic.ini is nowhere."""
        monkeypatch.chdir(tmp_path)

        # Create a fake module location where no alembic.ini exists
        # at any resolution level (CWD, package dir, or dev layout)
        fake_core = tmp_path / "fake" / "pkg" / "core"
        fake_core.mkdir(parents=True)
        (fake_core / "migrations.py").touch()

        monkeypatch.setattr(
            migrations_mod,
            "__file__",
            str(fake_core / "migrations.py"),
        )

        with pytest.raises(FileNotFoundError, match="Could not find alembic.ini"):
            find_alembic_ini()


class TestGetAlembicConfig:
    """Tests for get_alembic_config()."""

    def test_returns_config_object(self, tmp_path, monkeypatch):
        """Should return a valid Alembic Config object."""
        ini_file = tmp_path / "alembic.ini"
        ini_file.write_text("[alembic]\nscript_location = alembic\n")
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        monkeypatch.setattr(migrations_mod, "find_alembic_ini", lambda: ini_file)

        cfg = get_alembic_config()
        assert cfg is not None

        # script_location should be resolved to absolute path
        script_loc = cfg.get_main_option("script_location")
        assert Path(script_loc).is_absolute()
        assert script_loc == str(alembic_dir.resolve())

    def test_resolves_relative_script_location(self, tmp_path, monkeypatch):
        """Should resolve relative script_location to absolute path."""
        ini_file = tmp_path / "alembic.ini"
        ini_file.write_text("[alembic]\nscript_location = alembic\n")

        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        monkeypatch.setattr(migrations_mod, "find_alembic_ini", lambda: ini_file)

        cfg = get_alembic_config()
        script_loc = cfg.get_main_option("script_location")
        assert Path(script_loc).is_absolute()

    def test_preserves_absolute_script_location(self, tmp_path, monkeypatch):
        """Should not modify an already-absolute script_location."""
        abs_path = str(tmp_path / "my_alembic")
        ini_file = tmp_path / "alembic.ini"
        ini_file.write_text(f"[alembic]\nscript_location = {abs_path}\n")
        monkeypatch.setattr(migrations_mod, "find_alembic_ini", lambda: ini_file)

        cfg = get_alembic_config()
        assert cfg.get_main_option("script_location") == abs_path


class TestRunMigrations:
    """Tests for run_migrations() and run_migrations_async()."""

    def test_run_migrations_calls_alembic_upgrade(self, tmp_path, monkeypatch):
        """Should call alembic command.upgrade with 'head'."""
        ini_file = tmp_path / "alembic.ini"
        ini_file.write_text("[alembic]\nscript_location = alembic\n")
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        monkeypatch.setattr(migrations_mod, "find_alembic_ini", lambda: ini_file)

        with patch("aegra_api.core.migrations.command") as mock_command:
            from aegra_api.core.migrations import run_migrations

            run_migrations()
            mock_command.upgrade.assert_called_once()
            args = mock_command.upgrade.call_args
            assert args[0][1] == "head"  # Second positional arg is "head"

    @pytest.mark.asyncio
    async def test_run_migrations_async_runs_lock_free_path_in_thread(self):
        """Startup migrations use the lock-free precheck helper."""
        with patch("aegra_api.core.migrations.asyncio.to_thread") as mock_to_thread:
            from aegra_api.core.migrations import run_migrations_async, run_migrations_if_needed

            await run_migrations_async()
            mock_to_thread.assert_called_once_with(run_migrations_if_needed)


class TestRunMigrationsIfNeeded:
    def _configure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ini_file = tmp_path / "alembic.ini"
        ini_file.write_text("[alembic]\nscript_location = alembic\n")
        (tmp_path / "alembic").mkdir()
        monkeypatch.setattr(migrations_mod, "find_alembic_ini", lambda: ini_file)

    def test_skips_upgrade_when_database_is_at_head(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._configure(tmp_path, monkeypatch)
        with (
            patch("aegra_api.core.migrations._is_database_up_to_date", return_value=True),
            patch("aegra_api.core.migrations.command") as mock_command,
        ):
            migrations_mod.run_migrations_if_needed()
        mock_command.upgrade.assert_not_called()

    def test_runs_upgrade_when_database_is_behind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._configure(tmp_path, monkeypatch)
        with (
            patch("aegra_api.core.migrations._is_database_up_to_date", return_value=False),
            patch("aegra_api.core.migrations.command") as mock_command,
        ):
            migrations_mod.run_migrations_if_needed()
        mock_command.upgrade.assert_called_once()

    def test_runs_upgrade_when_precheck_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._configure(tmp_path, monkeypatch)
        with (
            patch("aegra_api.core.migrations._is_database_up_to_date", side_effect=RuntimeError("missing")),
            patch("aegra_api.core.migrations.command") as mock_command,
        ):
            migrations_mod.run_migrations_if_needed()
        mock_command.upgrade.assert_called_once()


class TestIsDatabaseUpToDate:
    @staticmethod
    def _connection(current_revision: str | None, *, missing_table: bool = False) -> MagicMock:
        cursor = MagicMock()
        if missing_table:
            import psycopg

            cursor.execute.side_effect = psycopg.errors.UndefinedTable("alembic_version")
        else:
            cursor.fetchone.return_value = (current_revision,) if current_revision else None
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.__enter__.return_value = connection
        return connection

    @pytest.mark.parametrize(
        ("current", "head", "expected"),
        [("abc", "abc", True), ("abc", "def", False), (None, "abc", False)],
    )
    def test_compares_database_revision_to_head(self, current: str | None, head: str, expected: bool) -> None:
        script = MagicMock()
        script.get_current_head.return_value = head
        connection = self._connection(current)
        with (
            patch("aegra_api.core.migrations.ScriptDirectory.from_config", return_value=script),
            patch("aegra_api.core.migrations.psycopg.connect", return_value=connection),
        ):
            assert migrations_mod._is_database_up_to_date(MagicMock()) is expected

    def test_missing_version_table_requires_upgrade(self) -> None:
        script = MagicMock()
        script.get_current_head.return_value = "abc"
        connection = self._connection(None, missing_table=True)
        with (
            patch("aegra_api.core.migrations.ScriptDirectory.from_config", return_value=script),
            patch("aegra_api.core.migrations.psycopg.connect", return_value=connection),
        ):
            assert migrations_mod._is_database_up_to_date(MagicMock()) is False

    def test_empty_script_directory_skips_database_connection(self) -> None:
        script = MagicMock()
        script.get_current_head.return_value = None
        with (
            patch("aegra_api.core.migrations.ScriptDirectory.from_config", return_value=script),
            patch("aegra_api.core.migrations.psycopg.connect") as mock_connect,
        ):
            assert migrations_mod._is_database_up_to_date(MagicMock()) is True
        mock_connect.assert_not_called()
