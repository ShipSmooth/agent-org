"""Position-aware YAML access for Shannon's config files.

Validation must name file and line in plain English, so config is read
from the composed YAML node tree (which carries positions) rather than
from ``safe_load`` output (which does not). ``TODO`` scalars are legal
YAML and are surfaced to the caller as the string ``"TODO"`` — the
validator decides whether each one is an error, a warning, or fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Loc:
    path: str
    line: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class Issue:
    level: str  # 'error' | 'warning'
    loc: Loc
    message: str

    def render(self) -> str:
        return f"{self.level.upper()}  {self.loc}: {self.message}"


class ConfigError(Exception):
    """Config could not be loaded/validated. Carries plain-English issues."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("; ".join(i.render() for i in issues))


@dataclass
class Scalar:
    value: str | int | float | bool | None
    loc: Loc


@dataclass
class Seq:
    items: list[Node]
    loc: Loc


@dataclass
class Map:
    entries: dict[str, Node]
    key_locs: dict[str, Loc]
    loc: Loc

    def get(self, key: str) -> Node | None:
        return self.entries.get(key)


Node = Scalar | Seq | Map


def _convert(node: yaml.Node, path: str) -> Node:
    loc = Loc(path, node.start_mark.line + 1)
    if isinstance(node, yaml.ScalarNode):
        raw = str(node.value)
        tag = node.tag
        value: str | int | float | bool | None
        if tag.endswith(":null"):
            value = None
        elif tag.endswith(":bool"):
            value = raw.strip().lower() in ("true", "yes", "on")
        elif tag.endswith(":int"):
            try:
                value = int(raw)
            except ValueError:
                value = raw
        elif tag.endswith(":float"):
            try:
                value = float(raw)
            except ValueError:
                value = raw
        else:
            value = raw  # dates and timestamps stay as their literal text
        return Scalar(value, loc)
    if isinstance(node, yaml.SequenceNode):
        return Seq([_convert(child, path) for child in node.value], loc)
    if isinstance(node, yaml.MappingNode):
        entries: dict[str, Node] = {}
        key_locs: dict[str, Loc] = {}
        for key_node, value_node in node.value:
            key = str(key_node.value)
            entries[key] = _convert(value_node, path)
            key_locs[key] = Loc(path, key_node.start_mark.line + 1)
        return Map(entries, key_locs, loc)
    raise ConfigError([Issue("error", loc, "Unsupported YAML structure.")])


def load_file(path: Path) -> Map:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            [Issue("error", Loc(str(path), 1), f"Cannot read this file: {exc.strerror}.")]
        ) from exc
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        line = 1
        if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1
        raise ConfigError(
            [Issue("error", Loc(str(path), line), f"This is not valid YAML: {exc}.")]
        ) from exc
    if root is None:
        raise ConfigError([Issue("error", Loc(str(path), 1), "The file is empty.")])
    converted = _convert(root, str(path))
    if not isinstance(converted, Map):
        raise ConfigError(
            [Issue("error", Loc(str(path), 1), "Expected a mapping at the top level.")]
        )
    return converted


@dataclass
class Reader:
    """Collects issues while pulling typed values out of the node tree."""

    issues: list[Issue] = field(default_factory=list)

    def error(self, loc: Loc, message: str) -> None:
        self.issues.append(Issue("error", loc, message))

    def warning(self, loc: Loc, message: str) -> None:
        self.issues.append(Issue("warning", loc, message))

    def str_at(self, m: Map, key: str, *, required_msg: str | None = None) -> Scalar | None:
        node = m.get(key)
        if node is None:
            if required_msg is not None:
                self.error(m.loc, required_msg)
            return None
        if not isinstance(node, Scalar):
            self.error(node.loc, f"'{key}' should be a single value, not a list or mapping.")
            return None
        return node

    def int_value(self, node: Node, what: str) -> int | None:
        if isinstance(node, Scalar) and isinstance(node.value, bool) is False:
            if isinstance(node.value, int):
                return node.value
            if isinstance(node.value, str) and node.value.lstrip("-").isdigit():
                return int(node.value)
        self.error(node.loc, f"{what} should be a whole number.")
        return None


def scalar_text(node: Node) -> str | None:
    """The scalar's text, or None if it is not a scalar / is null."""
    if isinstance(node, Scalar):
        if node.value is None:
            return None
        return str(node.value)
    return None


def is_todo(node: Node | None) -> bool:
    return (
        node is not None
        and isinstance(node, Scalar)
        and isinstance(node.value, str)
        and node.value.strip().upper() == "TODO"
    )
