"""The two supplier spikes take their login from `.env`, like Shannon does.

Zach runs these by hand on the Dell, and typing a password at a prompt on
every run is friction that has nothing to teach anyone. The login is in
`.env` already; these tests are that it is read from there, that an
exported variable still wins, and that a missing one still asks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from spike_support import credentials, out_path, transcript  # noqa: E402


def _env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (tmp_path / ".env").write_text(body, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("EMAIL", "USERNAME", "PASSWORD"):
        for prefix in ("ITHRIVE_", ""):
            monkeypatch.delenv(f"{prefix}DYNAREX_{name}", raising=False)
            monkeypatch.delenv(f"{prefix}NAR_{name}", raising=False)


def test_the_login_comes_from_the_env_file_without_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_file(
        tmp_path,
        monkeypatch,
        "ITHRIVE_DYNAREX_EMAIL=zach@example.test\nITHRIVE_DYNAREX_PASSWORD=from-the-file\n",
    )
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("asked for an email"))

    assert credentials("DYNAREX", "dynarex.com") == ("zach@example.test", "from-the-file")


def test_an_exported_variable_beats_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_file(
        tmp_path,
        monkeypatch,
        "ITHRIVE_NAR_EMAIL=file@example.test\nITHRIVE_NAR_PASSWORD=file\n",
    )
    monkeypatch.setenv("ITHRIVE_NAR_EMAIL", "exported@example.test")
    monkeypatch.setenv("ITHRIVE_NAR_PASSWORD", "exported")

    assert credentials("NAR", "narescue.com") == ("exported@example.test", "exported")


def test_the_name_the_env_example_used_is_read_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env.example` said USERNAME while the code read EMAIL. A file
    filled in from it must not send anyone back to the prompt."""
    _env_file(
        tmp_path,
        monkeypatch,
        "ITHRIVE_DYNAREX_USERNAME=zach@example.test\nITHRIVE_DYNAREX_PASSWORD=either\n",
    )

    assert credentials("DYNAREX", "dynarex.com") == ("zach@example.test", "either")


def test_nothing_saved_anywhere_still_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env_file(tmp_path, monkeypatch, "")
    monkeypatch.setattr("builtins.input", lambda *_: "typed@example.test")
    monkeypatch.setattr("spike_support.getpass", lambda *_: "typed")

    assert credentials("DYNAREX", "dynarex.com") == ("typed@example.test", "typed")


def test_a_half_filled_env_file_asks_only_for_the_missing_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_file(tmp_path, monkeypatch, "ITHRIVE_NAR_EMAIL=zach@example.test\n")
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("asked for a known email"))
    monkeypatch.setattr("spike_support.getpass", lambda *_: "typed")

    assert credentials("NAR", "narescue.com") == ("zach@example.test", "typed")


def test_the_whole_run_can_be_written_to_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A spike prints more than fits in a chat message, so `--out` hands
    back a file instead of a scrollback."""
    written = tmp_path / "dynarex-spike.txt"

    with transcript(written):
        print("what the portal said")

    assert "what the portal said" in written.read_text(encoding="utf-8")
    assert "what the portal said" in capsys.readouterr().out


def test_out_takes_a_name_or_falls_back_to_the_default() -> None:
    assert out_path(["--headless"], "nar-spike.txt") is None
    assert out_path(["--out"], "nar-spike.txt") == Path("nar-spike.txt")
    assert out_path(["--out", "--headless"], "nar-spike.txt") == Path("nar-spike.txt")
    assert out_path(["--out", "chosen.txt"], "nar-spike.txt") == Path("chosen.txt")
