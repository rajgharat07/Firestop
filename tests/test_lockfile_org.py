from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from firestop.ids import lockfile_id, release_id, service_id
from firestop.lockfile import org
from firestop.lockfile.model import Lockfile, LockfileKind, Pin, dedupe
from firestop.lockfile.writer import service_rows

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme"

RELEASES = {
    "lodash": {"4.17.20": release_id("lodash", "4.17.20")},
    "express": {"4.17.1": release_id("express", "4.17.1")},
}


def write_org(root: Path, services: list[dict], lockfiles: dict[str, str]) -> Path:
    (root / "org.json").write_bytes(orjson.dumps({"org": "acme", "services": services}))
    for path, content in lockfiles.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


class TestLoadingAnOrg:
    def test_services_and_their_lockfiles_are_found(self, tmp_path):
        root = write_org(
            tmp_path,
            [{"name": "api", "path": "api", "repo": "acme/api", "criticality": "tier-1"}],
            {"api/package-lock.json": "{}"},
        )
        loaded = org.load(root)

        assert [service.name for service in loaded.services] == ["api"]
        assert loaded.services[0].lockfile.name == "package-lock.json"
        assert loaded.services[0].criticality == "tier-1"

    def test_the_directory_defaults_to_the_service_name(self, tmp_path):
        root = write_org(tmp_path, [{"name": "api"}], {"api/yarn.lock": ""})

        assert org.load(root).services[0].lockfile.name == "yarn.lock"

    def test_a_service_without_a_lockfile_is_skipped(self, tmp_path):
        root = write_org(
            tmp_path,
            [{"name": "api"}, {"name": "docs"}],
            {"api/package-lock.json": "{}"},
        )

        assert [service.name for service in org.load(root).services] == ["api"]

    def test_a_manifest_naming_no_usable_service_is_an_error(self, tmp_path):
        root = write_org(tmp_path, [{"name": "docs"}], {})

        with pytest.raises(org.InvalidOrg):
            org.load(root)

    def test_a_missing_manifest_is_an_error(self, tmp_path):
        with pytest.raises(org.InvalidOrg):
            org.load(tmp_path)

    def test_the_stated_commit_time_wins_over_the_file_on_disk(self, tmp_path):
        # A fresh clone's modification times are the time of the clone, which says
        # nothing about when the service last shipped.
        root = write_org(
            tmp_path,
            [{"name": "api", "committed_at": "2022-03-01T09:12:00Z"}],
            {"api/package-lock.json": "{}"},
        )

        assert org.load(root).services[0].committed_at == 1646125920


class TestServiceRows:
    def service(self) -> org.Service:
        return org.Service(
            name="checkout-api",
            repo="github.com/acme/checkout-api",
            criticality="tier-1",
            committed_at=1646125920,
        )

    def lockfile(self, *pins: Pin) -> Lockfile:
        return Lockfile(
            path="checkout-api/package-lock.json",
            kind=LockfileKind.NPM,
            format_version="3",
            pins=list(pins),
        )

    def test_the_service_and_its_lockfile_both_become_vertices(self):
        rows = service_rows(self.service(), self.lockfile(), RELEASES)

        assert rows.services[0]["id"] == service_id("checkout-api")
        assert rows.lockfiles[0]["id"] == lockfile_id(
            "checkout-api", "checkout-api/package-lock.json"
        )

    def test_the_lockfile_carries_when_it_was_committed(self):
        # Which is what an as-of query pins itself to.
        rows = service_rows(self.service(), self.lockfile(), RELEASES)

        assert rows.lockfiles[0]["committed_at"] == 1646125920

    def test_the_service_points_at_its_lockfile(self):
        rows = service_rows(self.service(), self.lockfile(), RELEASES)

        assert rows.uses_lockfile[0]["source"] == service_id("checkout-api")
        assert rows.uses_lockfile[0]["target"] == rows.lockfiles[0]["id"]

    def test_a_pin_becomes_an_edge_to_the_exact_release(self):
        rows = service_rows(self.service(), self.lockfile(Pin("lodash", "4.17.20")), RELEASES)

        assert rows.pins[0]["target"] == release_id("lodash", "4.17.20")
        assert rows.pins[0]["resolved_version"] == "4.17.20"

    def test_whether_the_service_declared_it_is_kept_on_the_edge(self):
        rows = service_rows(
            self.service(),
            self.lockfile(Pin("lodash", "4.17.20", direct=True), Pin("express", "4.17.1")),
            RELEASES,
        )
        direct = {row["target"]: row["direct"] for row in rows.pins}

        assert direct[release_id("lodash", "4.17.20")] is True
        assert direct[release_id("express", "4.17.1")] is False

    def test_a_release_the_graph_has_never_seen_is_reported_not_invented(self):
        # HydraDB refuses an edge to a vertex that is not there, and guessing at a
        # nearby version would be a lie about what the service installed.
        rows = service_rows(self.service(), self.lockfile(Pin("left-pad", "1.3.0")), RELEASES)

        assert rows.pins == []
        assert rows.unknown == ["left-pad@1.3.0"]

    def test_pins_are_deterministic(self):
        first = service_rows(self.service(), self.lockfile(Pin("lodash", "4.17.20")), RELEASES)
        second = service_rows(self.service(), self.lockfile(Pin("lodash", "4.17.20")), RELEASES)

        assert [row["id"] for row in first.pins] == [row["id"] for row in second.pins]


class TestDeduplication:
    def test_the_same_release_named_twice_collapses(self):
        collapsed = dedupe([Pin("lodash", "4.17.20"), Pin("lodash", "4.17.20")])

        assert len(collapsed) == 1

    def test_a_declared_dependency_stays_declared(self):
        # It is both asked for and pulled in transitively, and the version the
        # service can actually change is the one that matters.
        collapsed = dedupe([Pin("lodash", "4.17.20"), Pin("lodash", "4.17.20", direct=True)])

        assert collapsed[0].direct is True

    def test_two_versions_of_one_package_both_survive(self):
        collapsed = dedupe([Pin("lodash", "4.17.20"), Pin("lodash", "3.10.1")])

        assert len(collapsed) == 2


class TestBundledFixtures:
    # Fixture lockfiles must keep parsing.

    def test_the_fixture_org_loads(self):
        loaded = org.load(FIXTURES)

        assert [service.name for service in loaded.services] == [
            "checkout-api",
            "web-storefront",
            "batch-jobs",
            "admin-console",
        ]

    def test_every_fixture_service_pins_something(self):
        from firestop.lockfile.parse import parse_file

        loaded = org.load(FIXTURES)

        for service in loaded.services:
            parsed = parse_file(service.lockfile, relative_to=loaded.root)
            assert parsed.pins, f"{service.name} parsed to nothing"
            assert parsed.direct, f"{service.name} has no direct dependencies"

    def test_all_three_package_managers_are_represented(self):
        from firestop.lockfile.parse import kind_of

        loaded = org.load(FIXTURES)
        kinds = {kind_of(service.lockfile.name) for service in loaded.services}

        assert kinds == {LockfileKind.NPM, LockfileKind.YARN, LockfileKind.PNPM}
