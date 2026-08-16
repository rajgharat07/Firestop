"""Graph labels, relationship types, and batched upsert Cypher.

Hydra quirks that shaped this: integer vertex ids, no null properties, MERGE on
id then SET, one rel type per MATCH pattern (multi-type walks use algo.*).
"""

from __future__ import annotations

from enum import StrEnum

# Absence has to be encoded as a value, because properties cannot be null. This
# is 2100-01-01T00:00:00Z: far enough out to read as "still current" while
# staying an ordinary integer that comparisons and sorting treat normally.
OPEN_INTERVAL_END = 4_102_444_800


class Label(StrEnum):
    PACKAGE = "Package"
    RELEASE = "Release"
    MAINTAINER = "Maintainer"
    SERVICE = "Service"
    LOCKFILE = "Lockfile"
    ADVISORY = "Advisory"


class Rel(StrEnum):
    VERSION_OF = "VERSION_OF"
    # Two types, not a property: path procedures can't filter on rel props.
    DEPENDS_ON = "DEPENDS_ON"
    DEV_DEPENDS_ON = "DEV_DEPENDS_ON"
    CAN_PUBLISH = "CAN_PUBLISH"
    PUBLISHED = "PUBLISHED"
    USES_LOCKFILE = "USES_LOCKFILE"
    PINS = "PINS"
    AFFECTS = "AFFECTS"


class DependencyKind(StrEnum):
    RUNTIME = "runtime"
    DEV = "dev"
    PEER = "peer"
    OPTIONAL = "optional"


def relationship_for(kind: DependencyKind) -> Rel:
    """Map npm dep kind to DEPENDS_ON vs DEV_DEPENDS_ON."""
    return Rel.DEV_DEPENDS_ON if kind is DependencyKind.DEV else Rel.DEPENDS_ON


# Service -> lockfile -> pin -> runtime deps.
EXPOSURE_RELS: tuple[str, ...] = (
    Rel.USES_LOCKFILE,
    Rel.PINS,
    Rel.DEPENDS_ON,
)

# Include build-time edges (CI exposure).
BUILD_RELS: tuple[str, ...] = (*EXPOSURE_RELS, Rel.DEV_DEPENDS_ON)


def upsert_vertices(label: Label, properties: tuple[str, ...]) -> str:
    """Batched vertex upsert: MERGE on id, then SET properties."""
    assignments = ", ".join(f"n.{name} = row.{name}" for name in properties)
    return f"UNWIND $rows AS row MERGE (n {{id: row.id}}) SET n:{label}, {assignments}"


def upsert_edges(
    relationship: Rel,
    source_label: Label,
    target_label: Label,
    properties: tuple[str, ...] = (),
) -> str:
    """Batched relationship upsert keyed on the edge id."""
    statement = (
        "UNWIND $rows AS row "
        f"MATCH (s:{source_label} {{id: row.source}}), (d:{target_label} {{id: row.target}}) "
        f"MERGE (s)-[r:{relationship} {{id: row.id}}]->(d)"
    )
    if properties:
        assignments = ", ".join(f"r.{name} = row.{name}" for name in properties)
        statement = f"{statement} SET {assignments}"
    return statement


PACKAGE_PROPERTIES = ("name", "ecosystem", "first_published", "dependent_count")

# MSpaths selects by string property, not integer id — hence name@version.
RELEASE_PROPERTIES = ("key", "package", "version", "published_at", "integrity", "deprecated")
MAINTAINER_PROPERTIES = ("username", "ecosystem", "email")
SERVICE_PROPERTIES = ("name", "repo", "criticality")
LOCKFILE_PROPERTIES = ("path", "service", "committed_at")
ADVISORY_PROPERTIES = ("osv_id", "severity", "published_at", "summary", "cwe", "aliases")

# valid_from/valid_to = epoch seconds for when this range resolved here.
DEPENDS_ON_PROPERTIES = ("range", "resolved_to", "kind", "valid_from", "valid_to")
# direct=true means the service declared it, not a transitive pin.
PINS_PROPERTIES = ("resolved_version", "direct")
PUBLISHED_PROPERTIES = ("at",)
AFFECTS_PROPERTIES = ("introduced", "fixed_in", "range_source")

UPSERT_PACKAGES = upsert_vertices(Label.PACKAGE, PACKAGE_PROPERTIES)
UPSERT_RELEASES = upsert_vertices(Label.RELEASE, RELEASE_PROPERTIES)
UPSERT_MAINTAINERS = upsert_vertices(Label.MAINTAINER, MAINTAINER_PROPERTIES)
UPSERT_SERVICES = upsert_vertices(Label.SERVICE, SERVICE_PROPERTIES)
UPSERT_LOCKFILES = upsert_vertices(Label.LOCKFILE, LOCKFILE_PROPERTIES)
UPSERT_ADVISORIES = upsert_vertices(Label.ADVISORY, ADVISORY_PROPERTIES)

# dependent_count is only known after edges exist; don't blank other props.
UPDATE_DEPENDENT_COUNT = upsert_vertices(Label.PACKAGE, ("dependent_count",))

UPSERT_VERSION_OF = upsert_edges(Rel.VERSION_OF, Label.RELEASE, Label.PACKAGE)
UPSERT_DEPENDS_ON = upsert_edges(
    Rel.DEPENDS_ON, Label.RELEASE, Label.RELEASE, DEPENDS_ON_PROPERTIES
)
UPSERT_DEV_DEPENDS_ON = upsert_edges(
    Rel.DEV_DEPENDS_ON, Label.RELEASE, Label.RELEASE, DEPENDS_ON_PROPERTIES
)
UPSERT_CAN_PUBLISH = upsert_edges(Rel.CAN_PUBLISH, Label.MAINTAINER, Label.PACKAGE)
UPSERT_PUBLISHED = upsert_edges(
    Rel.PUBLISHED, Label.MAINTAINER, Label.RELEASE, PUBLISHED_PROPERTIES
)
UPSERT_USES_LOCKFILE = upsert_edges(Rel.USES_LOCKFILE, Label.SERVICE, Label.LOCKFILE)
UPSERT_PINS = upsert_edges(Rel.PINS, Label.LOCKFILE, Label.RELEASE, PINS_PROPERTIES)
UPSERT_AFFECTS = upsert_edges(Rel.AFFECTS, Label.ADVISORY, Label.RELEASE, AFFECTS_PROPERTIES)
