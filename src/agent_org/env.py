"""Reading `.env`, and saying out loud where it looked.

`docker compose` reads `.env` from the folder it is run in. Shannon has to
read the same file, or half the system is configured from a file the other
half ignores — which is exactly the hour-long hunt this module exists to
prevent.

Two rules:

- A value already in the real environment always wins. An operator can
  override one setting for one command without editing the file.
- Whatever happens, the path searched is recorded, so an error message can
  name it. "DATABASE_URL is not set" sends someone hunting through a file
  that was correct all along; "...and I looked in C:\\Users\\Zach\\agent-org\\.env
  (found)" does not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ENV_FILENAME = ".env"
# Set this to point at a file somewhere else, or to "" to read no file at all.
ENV_PATH_VAR = "AGENT_ORG_ENV_FILE"


@dataclass(frozen=True)
class EnvFile:
    """Where `.env` was looked for, and what came of it."""

    path: Path | None
    found: bool
    loaded: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.path is None:
            return f"no {ENV_FILENAME} file was looked for ({ENV_PATH_VAR} is empty)"
        return f"{self.path} ({'found' if self.found else 'not found'})"


_LOADED = EnvFile(path=None, found=False)


def find_env_file(start: Path | None = None) -> Path | None:
    """The `.env` this project would use: nearest one at or above `start`.

    Walking up means it works whether the command is run from the
    repository root or from a folder inside it.
    """
    override = os.environ.get(ENV_PATH_VAR)
    if override is not None:
        return Path(override) if override else None
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / ENV_FILENAME
        if candidate.is_file():
            return candidate
    return here / ENV_FILENAME  # nothing found: name where we started looking


def load_env_file(start: Path | None = None) -> EnvFile:
    """Put `.env` into the environment, without overwriting anything real."""
    global _LOADED
    path = find_env_file(start)
    if path is None or not path.is_file():
        _LOADED = EnvFile(path=path, found=False)
        return _LOADED
    loaded: list[str] = []
    # `utf-8-sig`: Notepad and PowerShell's `utf8` write a byte-order mark,
    # which would otherwise become part of the first variable's name.
    for key, value in dotenv_values(path, encoding="utf-8-sig").items():
        if value is None or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    _LOADED = EnvFile(path=path, found=True, loaded=tuple(loaded))
    return _LOADED


def env_file() -> EnvFile:
    """What the last `load_env_file` found, for error messages to quote."""
    return _LOADED


__all__ = ["ENV_FILENAME", "ENV_PATH_VAR", "EnvFile", "env_file", "find_env_file", "load_env_file"]
