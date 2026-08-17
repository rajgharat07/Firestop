from __future__ import annotations

from firestop.ids import advisory_id, release_id
from firestop.osv.advisory import Advisory, AffectedPackage, VersionRange
from firestop.osv.writer import advisory_rows

RELEASES = {
    "lodash": {
        "4.17.20": release_id("lodash", "4.17.20"),
        "4.17.21": release_id("lodash", "4.17.21"),
    },
    "express": {"4.18.0": release_id("express", "4.18.0")},
}


def advisory(*affected: AffectedPackage) -> Advisory:
    return Advisory(
        osv_id="GHSA-test",
        summary="Command injection",
        severity="HIGH",
        published_at=1620317136,
        cwe="CWE-77",
        aliases="CVE-2021-23337",
        affected=list(affected),
    )


def lodash(*ranges: VersionRange, versions: tuple[str, ...] = ()) -> AffectedPackage:
    return AffectedPackage(name="lodash", ranges=ranges, versions=versions)


class TestAdvisoryRows:
    def test_an_edge_is_written_per_affected_release(self):
        rows = advisory_rows(advisory(lodash(VersionRange(fixed="4.17.21"))), RELEASES)

        assert [row["target"] for row in rows.affects] == [release_id("lodash", "4.17.20")]

    def test_edges_run_from_the_advisory_to_the_release(self):
        rows = advisory_rows(advisory(lodash(VersionRange(fixed="4.17.21"))), RELEASES)

        assert rows.affects[0]["source"] == advisory_id("GHSA-test")

    def test_the_advisory_carries_what_a_responder_reads_first(self):
        rows = advisory_rows(advisory(lodash(VersionRange(fixed="4.17.21"))), RELEASES)

        assert rows.advisories[0]["osv_id"] == "GHSA-test"
        assert rows.advisories[0]["severity"] == "HIGH"
        assert rows.advisories[0]["aliases"] == "CVE-2021-23337"

    def test_the_upgrade_target_is_recorded_on_the_edge(self):
        # The one fact remediation needs, kept where the traversal already is.
        rows = advisory_rows(advisory(lodash(VersionRange(fixed="4.17.21"))), RELEASES)

        assert rows.affects[0]["fixed_in"] == "4.17.21"

    def test_an_advisory_touching_nothing_in_the_graph_is_not_stored(self):
        # Most npm advisories concern packages this graph has never seen. Writing
        # them all would bury the ones that matter.
        entry = AffectedPackage(name="unseen-package", ranges=(VersionRange(),))
        rows = advisory_rows(advisory(entry), RELEASES)

        assert rows.advisories == []
        assert rows.affects == []

    def test_an_advisory_whose_versions_are_all_patched_is_not_stored(self):
        rows = advisory_rows(advisory(lodash(VersionRange(fixed="4.0.0"))), RELEASES)

        assert len(rows) == 0

    def test_several_packages_in_one_advisory_all_get_edges(self):
        rows = advisory_rows(
            advisory(
                lodash(VersionRange(fixed="4.17.21")),
                AffectedPackage(name="express", ranges=(VersionRange(),)),
            ),
            RELEASES,
        )

        assert {row["target"] for row in rows.affects} == {
            release_id("lodash", "4.17.20"),
            release_id("express", "4.18.0"),
        }

    def test_a_release_named_twice_produces_one_edge(self):
        # Two entries of one advisory can overlap, and HydraDB rejects a batch
        # carrying one relationship id twice.
        rows = advisory_rows(
            advisory(lodash(VersionRange(fixed="4.17.21")), lodash(versions=("4.17.20",))),
            RELEASES,
        )

        assert len(rows.affects) == 1

    def test_edge_ids_are_deterministic(self):
        first = advisory_rows(advisory(lodash(VersionRange())), RELEASES)
        second = advisory_rows(advisory(lodash(VersionRange())), RELEASES)

        assert [r["id"] for r in first.affects] == [r["id"] for r in second.affects]
