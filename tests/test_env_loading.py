"""Reading `.env`.

The first real Windows run stopped here: `docker compose` read `.env` and
Shannon did not, because on the machine she was written on the variables
were already exported into the shell and nobody noticed. These tests are
the case that was never covered — a `.env` on disk and nothing whatsoever
in the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_org.cli import main
from agent_org.db.connection import DatabaseNotConfigured, DatabaseSettings
from agent_org.env import ENV_PATH_VAR, find_env_file, load_env_file

GOLDEN_CONFIG = Path(__file__).parent / "fixtures" / "golden" / "config"
RUN_CLI = "from agent_org.cli import main; import sys; sys.exit(main(sys.argv[1:]))"


def test_a_dotenv_file_is_read_when_the_environment_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://example/one\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert os.environ["DATABASE_URL"] == "postgresql://example/one"


def test_a_real_environment_variable_beats_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So an operator can override one setting for one command without
    editing a file they may not own."""
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://example/file\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/environment")
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert os.environ["DATABASE_URL"] == "postgresql://example/environment"


def test_a_file_saved_by_notepad_still_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notepad and PowerShell's `utf8` write a byte-order mark. Without
    `utf-8-sig` the first variable is named "\ufeffDATABASE_URL" and the file
    looks perfect on screen while doing nothing."""
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://example/bom\n", encoding="utf-8-sig")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert os.environ["DATABASE_URL"] == "postgresql://example/bom"


def test_the_file_is_found_from_a_folder_inside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://example/two\n", encoding="utf-8")
    inner = tmp_path / "somewhere" / "deeper"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)

    assert find_env_file() == tmp_path / ".env"


def test_the_missing_variable_message_names_the_file_it_looked_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "DATABASE_URL is not set" sent Zach hunting through a file that was
    correct all along. The message names the path and whether it was there."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    load_env_file()

    with pytest.raises(DatabaseNotConfigured) as raised:
        DatabaseSettings.from_env({})

    message = str(raised.value)
    assert str(tmp_path / ".env") in message
    assert "not found" in message


def test_a_command_reads_the_file_with_nothing_in_the_environment(
    tmp_path: Path, migrator_dsn: str
) -> None:
    """The whole bug, end to end: a `.env` on disk, an environment stripped
    of every database variable, and a command that has to connect anyway."""
    (tmp_path / ".env").write_text(
        f"DATABASE_MIGRATOR_URL={migrator_dsn}\nDATABASE_URL={migrator_dsn}\n",
        encoding="utf-8",
    )
    stripped = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABASE_", "POSTGRES_", ENV_PATH_VAR))
    }

    finished = subprocess.run(
        [sys.executable, "-c", RUN_CLI, "--config-root", str(GOLDEN_CONFIG), "migrate"],
        cwd=tmp_path,
        env=stripped,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "Database" in finished.stdout


def test_the_env_file_can_be_pointed_elsewhere_or_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "kept-away" / "settings.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text("DATABASE_URL=postgresql://example/three\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(ENV_PATH_VAR, str(elsewhere))
    load_env_file()
    assert os.environ["DATABASE_URL"] == "postgresql://example/three"

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(ENV_PATH_VAR, "")
    result = load_env_file()
    assert "DATABASE_URL" not in os.environ
    assert "no .env file was looked for" in result.describe()


def test_the_cli_never_shows_a_traceback_when_the_database_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    code = main(["--config-root", str(GOLDEN_CONFIG), "migrate"])

    out = capsys.readouterr().out
    assert code == 1
    assert "DATABASE_URL is not set" in out
    assert str(tmp_path / ".env") in out
    assert "Traceback" not in out
