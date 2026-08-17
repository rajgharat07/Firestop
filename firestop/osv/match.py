"""Match OSV ranges onto concrete releases (introduced inclusive, fixed exclusive)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

import nodesemver as semver

from firestop.osv.advisory import AffectedPackage, VersionRange

# Recorded on the edge so a reader knows how the verdict was reached.
FROM_RANGE = "range"
FROM_ENUMERATION = "enumerated"

# No fix exists yet. Kept as a value because properties cannot be null, and
# distinguishable from a version string by not being one.
NO_FIX = ""


@dataclass(frozen=True, slots=True)
class Match:
    version: str
    introduced: str
    fixed_in: str
    source: str

    @property
    def unfixed(self) -> bool:
        return self.fixed_in == NO_FIX


def matches(affected: AffectedPackage, versions: Iterable[str]) -> list[Match]:
    """Which of `versions` this entry marks as vulnerable.

    Ranges are preferred over the enumerated list because they stay true as new
    releases appear. The enumeration is the fallback for entries that carry no
    machine-readable range at all, which older advisories often do not.
    """
    enumerated = set(affected.versions)
    found: list[Match] = []

    for version in versions:
        match = _match_ranges(affected.ranges, version)
        if match is None and version in enumerated:
            # No range covered it but the database named it outright. Older
            # advisories carry only the list, and it is ground truth either way.
            match = Match(
                version=version,
                introduced=version,
                fixed_in=NO_FIX,
                source=FROM_ENUMERATION,
            )
        if match is not None:
            found.append(match)

    return found


def _match_ranges(ranges: tuple[VersionRange, ...], version: str) -> Match | None:
    for interval in ranges:
        if _covers(interval, version):
            return Match(
                version=version,
                introduced=interval.introduced,
                fixed_in=interval.fixed or NO_FIX,
                source=FROM_RANGE,
            )
    return None


def _covers(interval: VersionRange, version: str) -> bool:
    # Non-semver tails can't be placed in an interval — don't guess.
    if not _parseable(version):
        return False

    intro = _compare(version, interval.introduced) if _bounded(interval.introduced) else 0
    fixed = _compare(version, interval.fixed) if _bounded(interval.fixed) else None
    last = _compare(version, interval.last_affected) if _bounded(interval.last_affected) else None
    if intro is None or (_bounded(interval.fixed) and fixed is None):
        return False
    if _bounded(interval.last_affected) and last is None:
        return False

    # "0" means from the beginning — no lower bound.
    before_introduction = _bounded(interval.introduced) and intro < 0
    at_or_past_the_fix = fixed is not None and fixed >= 0
    past_last_affected = last is not None and last > 0

    return not (before_introduction or at_or_past_the_fix or past_last_affected)


def _bounded(bound: str) -> bool:
    return bound not in ("", "0") and _parseable(bound)


@lru_cache(maxsize=100_000)
def _parseable(version: str) -> bool:
    try:
        return semver.valid(version, loose=True) is not None
    except Exception:
        return False


def _compare(left: str, right: str) -> int | None:
    try:
        return semver.compare(left, right, loose=True)
    except Exception:
        return None
