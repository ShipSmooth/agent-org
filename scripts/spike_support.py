"""What the two supplier spikes both need: the login, and somewhere to write.

Neither belongs in `src/`. These are diagnostics Zach runs by hand on the
Dell, not part of Shannon's Monday run — but the pair of them had started
to disagree about where a credential comes from, which is exactly how a
script ends up prompting for something the environment already knows.

Two things live here:

- `credentials()`, which reads `.env` the way `shannon` itself does and
  only falls back to a prompt when the file has nothing to offer. It tries
  the entity-prefixed name first (`ITHRIVE_DYNAREX_EMAIL`), because that is
  the name the real integrations use, and says which name it used.
- `transcript()`, which writes the whole run to a file. A spike prints
  more than fits in a chat message, and the useful thing to hand back is a
  file rather than a scrollback.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from typing import IO, cast

from agent_org.env import load_env_file

# The entity whose logins these are. Multi-entity isolation means the real
# variable is prefixed; a bare NAR_EMAIL is accepted too, since that is what
# the spikes asked for before and what someone reading them expects.
PREFIXES = ("ITHRIVE_", "")
# .env.example once said USERNAME while the code read EMAIL. Both are read
# here so a .env written from either does not send anyone to a prompt.
EMAIL_SUFFIXES = ("EMAIL", "USERNAME")


def _first_set(prefix_free_names: tuple[str, ...], supplier: str) -> tuple[str, str] | None:
    for prefix in PREFIXES:
        for name in prefix_free_names:
            full = f"{prefix}{supplier}_{name}"
            value = os.environ.get(full, "").strip()
            if value:
                return full, value
    return None


def credentials(supplier: str, site: str) -> tuple[str, str]:
    """The login for one supplier: from `.env` if it is there, else asked for.

    `supplier` is the bare middle of the variable name — "DYNAREX" reads
    ITHRIVE_DYNAREX_EMAIL, ITHRIVE_DYNAREX_USERNAME, DYNAREX_EMAIL and
    DYNAREX_USERNAME, in that order.
    """
    loaded = load_env_file()
    print(f"reading {loaded.describe()}")

    found_email = _first_set(EMAIL_SUFFIXES, supplier)
    found_password = _first_set(("PASSWORD",), supplier)
    if found_email and found_password:
        print(f"login from {found_email[0]} and {found_password[0]}")
        return found_email[1], found_password[1]

    wanted = f"ITHRIVE_{supplier}_EMAIL and ITHRIVE_{supplier}_PASSWORD"
    print(f"{wanted} not set in the environment or {loaded.describe()}")
    email = found_email[1] if found_email else input(f"{site} email: ").strip()
    password = found_password[1] if found_password else getpass(f"{site} password (not echoed): ")
    return email, password


class _Tee:
    """Everything printed goes to the screen and to the file, both."""

    def __init__(self, screen: IO[str], file: IO[str]) -> None:
        self._screen = screen
        self._file = file

    def write(self, text: str) -> int:
        self._file.write(text)
        return self._screen.write(text)

    def flush(self) -> None:
        self._file.flush()
        self._screen.flush()

    def isatty(self) -> bool:
        return self._screen.isatty()


@contextmanager
def transcript(path: Path | None) -> Iterator[None]:
    """Copy this run's output into `path`, if one was asked for."""
    if path is None:
        yield
        return
    with path.open("w", encoding="utf-8") as handle:
        original = sys.stdout
        sys.stdout = cast("IO[str]", _Tee(original, handle))
        try:
            yield
        finally:
            sys.stdout = original
    print(f"\nThe whole of the above is in {path} — paste or attach that file.")


def out_path(argv: list[str], default: str) -> Path | None:
    """`--out` with no filename means the default one, beside the repo."""
    if "--out" not in argv:
        return None
    after = argv[argv.index("--out") + 1 :]
    named = after[0] if after and not after[0].startswith("--") else default
    return Path(named).expanduser()


__all__ = ["credentials", "out_path", "transcript"]
