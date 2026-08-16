from __future__ import annotations

from firestop.npm.packument import parse_packument
from firestop.schema.model import DependencyKind
from firestop.times import parse_timestamp

DOCUMENT = {
    "name": "widget",
    "time": {
        "created": "2019-01-01T00:00:00.000Z",
        "modified": "2024-01-01T00:00:00.000Z",
        "1.0.0": "2020-01-01T00:00:00.000Z",
        "1.1.0": "2021-01-01T00:00:00.000Z",
        "2.0.0": "2022-01-01T00:00:00.000Z",
    },
    "maintainers": [
        {"name": "alice", "email": "alice@example.com"},
        {"name": "bob", "email": ""},
    ],
    "versions": {
        "1.0.0": {
            "version": "1.0.0",
            "dependencies": {"lodash": "^4.17.0"},
            "devDependencies": {"jest": "^29.0.0"},
            "dist": {"integrity": "sha512-aaa"},
            "_npmUser": {"name": "alice", "email": "alice@example.com"},
        },
        "1.1.0": {
            "version": "1.1.0",
            "dependencies": {"lodash": "^4.17.0", "semver": "~7.5.0"},
            "peerDependencies": {"react": ">=17"},
            "optionalDependencies": {"fsevents": "^2.3.0"},
            "dist": {"integrity": "sha512-bbb"},
            "_npmUser": {"name": "bob"},
            "deprecated": "use 2.x instead",
        },
        "2.0.0": {
            "version": "2.0.0",
            "dependencies": {},
            "dist": {"shasum": "abc123"},
        },
    },
}


def test_name_and_release_count():
    parsed = parse_packument(DOCUMENT)

    assert parsed.name == "widget"
    assert [release.version for release in parsed.releases] == ["1.0.0", "1.1.0", "2.0.0"]


def test_version_times_exclude_the_created_and_modified_keys():
    parsed = parse_packument(DOCUMENT)

    assert set(parsed.version_times) == {"1.0.0", "1.1.0", "2.0.0"}


def test_first_published_is_the_earliest_version():
    parsed = parse_packument(DOCUMENT)

    assert parsed.first_published == parse_timestamp("2020-01-01T00:00:00.000Z")


def test_dependency_kinds_are_recorded_separately():
    parsed = parse_packument(DOCUMENT)
    by_name = {dep.name: dep for dep in parsed.releases[1].dependencies}

    assert by_name["lodash"].kind is DependencyKind.RUNTIME
    assert by_name["semver"].kind is DependencyKind.RUNTIME
    assert by_name["react"].kind is DependencyKind.PEER
    assert by_name["fsevents"].kind is DependencyKind.OPTIONAL


def test_dev_dependencies_are_recorded_too():
    parsed = parse_packument(DOCUMENT)
    kinds = {dep.kind for dep in parsed.releases[0].dependencies}

    assert DependencyKind.DEV in kinds


def test_runtime_dependencies_are_easy_to_isolate():
    parsed = parse_packument(DOCUMENT)

    assert {dep.name for dep in parsed.releases[0].runtime_dependencies} == {"lodash"}


def test_ranges_are_preserved_verbatim():
    parsed = parse_packument(DOCUMENT)
    ranges = {dep.name: dep.range for dep in parsed.releases[1].dependencies}

    assert ranges["semver"] == "~7.5.0"
    assert ranges["react"] == ">=17"


def test_deprecation_is_a_message_in_the_registry_but_a_flag_here():
    parsed = parse_packument(DOCUMENT)

    assert parsed.releases[0].deprecated is False
    assert parsed.releases[1].deprecated is True


def test_publisher_comes_from_the_per_version_npm_user():
    parsed = parse_packument(DOCUMENT)

    # Per-version publisher, not current maintainers.
    assert parsed.releases[0].publisher == "alice"
    assert parsed.releases[1].publisher == "bob"
    assert parsed.releases[2].publisher == ""


def test_integrity_falls_back_to_the_legacy_shasum():
    parsed = parse_packument(DOCUMENT)

    assert parsed.releases[0].integrity == "sha512-aaa"
    assert parsed.releases[2].integrity == "abc123"


def test_maintainers_are_captured_with_emails():
    parsed = parse_packument(DOCUMENT)

    assert parsed.maintainers == [("alice", "alice@example.com"), ("bob", "")]


def test_maintainers_may_be_legacy_strings():
    parsed = parse_packument({**DOCUMENT, "maintainers": ["carol <carol@example.com>"]})

    assert parsed.maintainers == [("carol", "")]


def test_dependency_names_spans_every_release():
    parsed = parse_packument(DOCUMENT)

    assert parsed.dependency_names() == {"lodash", "jest", "semver", "react", "fsevents"}


class TestVersionLimit:
    def test_only_the_most_recent_manifests_are_kept(self):
        parsed = parse_packument(DOCUMENT, max_versions=2)

        assert [release.version for release in parsed.releases] == ["1.1.0", "2.0.0"]

    def test_the_full_timeline_survives_the_limit(self):
        # A version whose manifest was dropped can still be what a range resolved
        # to, so resolution needs every publish time regardless.
        parsed = parse_packument(DOCUMENT, max_versions=1)

        assert len(parsed.releases) == 1
        assert len(parsed.version_times) == 3

    def test_recency_is_by_publish_date_not_semver(self):
        # A 1.x backport published after 2.0.0 is the more recent release.
        document = {
            "name": "widget",
            "time": {
                "2.0.0": "2022-01-01T00:00:00.000Z",
                "1.9.0": "2023-01-01T00:00:00.000Z",
            },
            "versions": {
                "2.0.0": {"version": "2.0.0"},
                "1.9.0": {"version": "1.9.0"},
            },
        }

        parsed = parse_packument(document, max_versions=1)

        assert [release.version for release in parsed.releases] == ["1.9.0"]

    def test_zero_keeps_everything(self):
        parsed = parse_packument(DOCUMENT, max_versions=0)

        assert len(parsed.releases) == 3


class TestMalformedDocuments:
    def test_an_empty_document_parses_to_nothing(self):
        parsed = parse_packument({})

        assert parsed.name == ""
        assert parsed.releases == []
        assert parsed.first_published == 0

    def test_a_version_without_a_timestamp_is_skipped(self):
        document = {
            "name": "widget",
            "time": {"1.0.0": "2020-01-01T00:00:00.000Z"},
            "versions": {"1.0.0": {"version": "1.0.0"}, "1.1.0": {"version": "1.1.0"}},
        }

        assert [r.version for r in parse_packument(document).releases] == ["1.0.0"]

    def test_non_dict_fields_do_not_raise(self):
        document = {
            "name": "widget",
            "time": "not a map",
            "maintainers": "not a list",
            "versions": {"1.0.0": "not a manifest"},
        }
        parsed = parse_packument(document)

        assert parsed.releases == []
        assert parsed.maintainers == []

    def test_non_string_dependency_specs_are_dropped(self):
        document = {
            "name": "widget",
            "time": {"1.0.0": "2020-01-01T00:00:00.000Z"},
            "versions": {"1.0.0": {"version": "1.0.0", "dependencies": {"a": None, "b": "^1.0.0"}}},
        }

        assert [d.name for d in parse_packument(document).releases[0].dependencies] == ["b"]


class TestTimestamps:
    def test_a_registry_timestamp_parses(self):
        assert parse_timestamp("2020-01-01T00:00:00.000Z") == 1577836800

    def test_rubbish_returns_none_rather_than_raising(self):
        assert parse_timestamp("not a date") is None
        assert parse_timestamp("") is None
