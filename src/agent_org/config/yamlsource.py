"""YAML loading that remembers where every value came from.

`shannon validate-config` must name a file and a line in plain English, so
the loader keeps source locations alongside the data. Mappings and
sequences are loaded as `YamlMap` / `YamlSeq`, which are ordinary `dict`
and `list` subclasses carrying their own location and the location of each
key, so the rest of the code treats them as plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Loc:
    """A file and a 1-based line number."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


UNKNOWN_LOC = Loc(file="<unknown>", line=0)


class YamlMap(dict[str, Any]):
    """A YAML mapping that knows its own line and each key's line."""

    loc: Loc
    key_locs: dict[str, Loc]

    def loc_of(self, key: str) -> Loc:
        return self.key_locs.get(key, self.loc)


class YamlSeq(list[Any]):
    """A YAML sequence that knows its own line."""

    loc: Loc


def loc_of(value: Any, fallback: Loc = UNKNOWN_LOC) -> Loc:
    """The location of a loaded value, or `fallback` for a bare scalar."""
    if isinstance(value, YamlMap | YamlSeq):
        return value.loc
    return fallback


def load_yaml_file(path: Path) -> Any:
    """Load one YAML file, preserving source locations.

    `utf-8-sig` because Windows Notepad and PowerShell's default `utf8`
    encoding write a byte-order mark, and a file that looks identical on
    screen would otherwise fail to parse on its first character.
    """
    text = path.read_text(encoding="utf-8-sig")
    return load_yaml_text(text, str(path))


def load_yaml_text(text: str, filename: str) -> Any:
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return YamlMap()
        return _convert(node, loader, filename)
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def _loc(node: yaml.Node, filename: str) -> Loc:
    return Loc(file=filename, line=node.start_mark.line + 1)


def _convert(node: yaml.Node, loader: yaml.SafeLoader, filename: str) -> Any:
    if isinstance(node, yaml.MappingNode):
        mapping = YamlMap()
        mapping.loc = _loc(node, filename)
        mapping.key_locs = {}
        for key_node, value_node in node.value:
            key = str(loader.construct_object(key_node))  # type: ignore[no-untyped-call]
            mapping[key] = _convert(value_node, loader, filename)
            mapping.key_locs[key] = _loc(key_node, filename)
        return mapping
    if isinstance(node, yaml.SequenceNode):
        sequence = YamlSeq(_convert(item, loader, filename) for item in node.value)
        sequence.loc = _loc(node, filename)
        return sequence
    return loader.construct_object(node)  # type: ignore[no-untyped-call]
