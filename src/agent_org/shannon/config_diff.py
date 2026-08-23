"""What changed in the configuration since the last run.

Every report opens with one line about this. The point is that a number
moving between weeks is never a mystery: either the world changed or the
configuration did, and this says which.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from agent_org.config.models import LoadedConfig


@dataclass(frozen=True)
class ConfigSnapshot:
    bom_version: str
    components: tuple[str, ...]
    kits: tuple[str, ...]
    parameters: dict[str, str]

    @property
    def digest(self) -> str:
        material = json.dumps(self.as_dict(), sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bom_version": self.bom_version,
            "components": list(self.components),
            "kits": list(self.kits),
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfigSnapshot:
        return cls(
            bom_version=str(data.get("bom_version", "")),
            components=tuple(str(x) for x in data.get("components", [])),
            kits=tuple(str(x) for x in data.get("kits", [])),
            parameters={str(k): str(v) for k, v in data.get("parameters", {}).items()},
        )

    @classmethod
    def of(cls, config: LoadedConfig) -> ConfigSnapshot:
        params = config.shannon.parameters
        return cls(
            bom_version=config.boms.bom_version,
            components=tuple(sorted(str(key) for key in config.boms.components)),
            kits=tuple(sorted(config.boms.kits)),
            parameters={name: str(value) for name, value in sorted(asdict(params).items())},
        )


def describe_changes(current: ConfigSnapshot, previous: ConfigSnapshot | None) -> str:
    """One plain sentence, suitable for the top of a report."""
    if previous is None:
        return (
            "This is the first run on record, so there is nothing to compare the "
            f"configuration against (BOM version {current.bom_version})."
        )
    if previous.digest == current.digest:
        return f"No configuration changes since the last run (BOM version {current.bom_version})."

    parts: list[str] = []
    if previous.bom_version != current.bom_version:
        parts.append(f"BOM version {previous.bom_version} → {current.bom_version}")
    added = sorted(set(current.components) - set(previous.components))
    removed = sorted(set(previous.components) - set(current.components))
    if added:
        parts.append(
            f"{len(added)} component(s) added ({', '.join(added[:3])}…)"
            if len(added) > 3
            else f"components added: {', '.join(added)}"
        )
    if removed:
        parts.append(
            f"{len(removed)} component(s) removed ({', '.join(removed[:3])}…)"
            if len(removed) > 3
            else f"components removed: {', '.join(removed)}"
        )
    kits_added = sorted(set(current.kits) - set(previous.kits))
    kits_removed = sorted(set(previous.kits) - set(current.kits))
    if kits_added:
        parts.append(f"kits added: {', '.join(kits_added)}")
    if kits_removed:
        parts.append(f"kits removed: {', '.join(kits_removed)}")
    for name, value in sorted(current.parameters.items()):
        before = previous.parameters.get(name)
        if before is not None and before != value:
            parts.append(f"{name} {before} → {value}")
    if not parts:
        parts.append("the configuration files changed in a way that affects nothing counted here")
    return "Since the last run: " + "; ".join(parts) + "."


__all__ = ["ConfigSnapshot", "describe_changes"]
