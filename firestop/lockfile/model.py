"""Normalized lockfile pins, independent of npm/yarn/pnpm format."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class LockfileKind(StrEnum):
    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"


@dataclass(frozen=True, slots=True)
class Pin:
    """One exact release the lockfile installs.

    `direct` marks a dependency the service declares itself. Everything else
    arrived through somebody else's manifest, which is precisely the distinction
    that makes remediation hard: you cannot bump what you did not ask for.
    """

    name: str
    version: str
    direct: bool = False


@dataclass(slots=True)
class Lockfile:
    path: str
    kind: LockfileKind
    format_version: str = ""
    pins: list[Pin] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.pins)

    @property
    def direct(self) -> list[Pin]:
        return [pin for pin in self.pins if pin.direct]


def dedupe(pins: list[Pin]) -> list[Pin]:
    """Collapse repeats of the same release.

    A lockfile names one release many times when several dependents share it, and
    a package can be both declared and pulled in transitively. Declared wins,
    because it is the version the service can actually change.
    """
    best: dict[tuple[str, str], Pin] = {}
    for pin in pins:
        key = (pin.name, pin.version)
        existing = best.get(key)
        if existing is None or (pin.direct and not existing.direct):
            best[key] = pin
    return list(best.values())
