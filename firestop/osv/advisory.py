"""Parse OSV records into ranges Firestop can match against crawled releases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firestop.times import epoch_or_unknown

ECOSYSTEM = "npm"

# GitHub's qualitative rating, which is what an on-call engineer actually triages
# by. CVSS vectors are kept as the score string when no rating is present.
_SEVERITIES = ("CRITICAL", "HIGH", "MODERATE", "MEDIUM", "LOW")
UNKNOWN_SEVERITY = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VersionRange:
    """One `[introduced, fixed)` interval from a SEMVER range.

    `fixed` is empty when the record has no fix, which is the difference between
    "upgrade to this" and "there is nowhere to upgrade to yet". `last_affected`
    is the inclusive form some records use instead.
    """

    introduced: str = "0"
    fixed: str = ""
    last_affected: str = ""

    @property
    def unfixed(self) -> bool:
        return not self.fixed


@dataclass(frozen=True, slots=True)
class AffectedPackage:
    name: str
    ranges: tuple[VersionRange, ...] = ()
    versions: tuple[str, ...] = ()


@dataclass(slots=True)
class Advisory:
    osv_id: str
    summary: str = ""
    severity: str = UNKNOWN_SEVERITY
    published_at: int = 0
    cwe: str = ""
    aliases: str = ""
    affected: list[AffectedPackage] = field(default_factory=list)

    @property
    def package_names(self) -> set[str]:
        return {entry.name for entry in self.affected}


def parse_advisory(document: dict[str, Any]) -> Advisory | None:
    """Reduce one OSV record, or None if it says nothing about live npm packages."""
    osv_id = str(document.get("id") or "")
    if not osv_id or document.get("withdrawn"):
        return None

    affected = [
        entry
        for entry in (_affected(raw) for raw in document.get("affected") or [])
        if entry is not None
    ]
    if not affected:
        return None

    return Advisory(
        osv_id=osv_id,
        summary=_summary(document),
        severity=_severity(document),
        published_at=epoch_or_unknown(document.get("published")),
        cwe=_cwe(document),
        aliases=",".join(str(alias) for alias in document.get("aliases") or []),
        affected=affected,
    )


def _affected(raw: Any) -> AffectedPackage | None:
    if not isinstance(raw, dict):
        return None

    package = raw.get("package") if isinstance(raw.get("package"), dict) else {}
    name = str(package.get("name") or "")
    if not name or str(package.get("ecosystem") or "").lower() != ECOSYSTEM:
        return None

    versions = tuple(
        str(version) for version in raw.get("versions") or [] if isinstance(version, str)
    )
    ranges = _ranges(raw.get("ranges"))
    if not ranges and not versions:
        # Nothing to match against. An entry like this describes the package as a
        # whole, which is not specific enough to point at a release.
        return None

    return AffectedPackage(name=name, ranges=ranges, versions=versions)


def _ranges(raw: Any) -> tuple[VersionRange, ...]:
    if not isinstance(raw, list):
        return ()

    found: list[VersionRange] = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        # ECOSYSTEM ranges use the same event vocabulary and, for npm, the same
        # semver ordering. GIT ranges are commit ids and cannot name a release.
        if str(block.get("type") or "").upper() not in ("SEMVER", "ECOSYSTEM"):
            continue
        found.extend(_events(block.get("events")))
    return tuple(found)


def _events(raw: Any) -> list[VersionRange]:
    """Fold a flat event list into intervals.

    Events arrive sorted, and `introduced` opens an interval that the next
    `fixed` or `last_affected` closes. A trailing `introduced` with nothing after
    it is an interval that is still open, which is the common shape for an
    unpatched advisory.
    """
    if not isinstance(raw, list):
        return []

    intervals: list[VersionRange] = []
    introduced: str | None = None

    for event in raw:
        if not isinstance(event, dict):
            continue

        if "introduced" in event:
            if introduced is not None:
                intervals.append(VersionRange(introduced=introduced))
            introduced = str(event["introduced"] or "0")
        elif "fixed" in event and introduced is not None:
            intervals.append(VersionRange(introduced=introduced, fixed=str(event["fixed"])))
            introduced = None
        elif "last_affected" in event and introduced is not None:
            intervals.append(
                VersionRange(introduced=introduced, last_affected=str(event["last_affected"]))
            )
            introduced = None

    if introduced is not None:
        intervals.append(VersionRange(introduced=introduced))
    return intervals


def _summary(document: dict[str, Any]) -> str:
    summary = str(document.get("summary") or "").strip()
    if summary:
        return summary
    # Some records carry only the long form. Its first line is a usable headline.
    details = str(document.get("details") or "").strip()
    return details.split("\n", 1)[0][:300]


def _severity(document: dict[str, Any]) -> str:
    specific = document.get("database_specific")
    if isinstance(specific, dict):
        rating = str(specific.get("severity") or "").upper()
        if rating in _SEVERITIES:
            return rating

    for entry in document.get("severity") or []:
        if isinstance(entry, dict) and entry.get("score"):
            return str(entry["score"])

    return UNKNOWN_SEVERITY


def _cwe(document: dict[str, Any]) -> str:
    specific = document.get("database_specific")
    if not isinstance(specific, dict):
        return ""
    ids = specific.get("cwe_ids")
    if not isinstance(ids, list):
        return ""
    return ",".join(str(cwe) for cwe in ids if cwe)
