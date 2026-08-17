from __future__ import annotations

import orjson
import pytest

from firestop.lockfile import coordinate
from firestop.lockfile.model import LockfileKind
from firestop.lockfile.parse import UnknownLockfile, find_lockfile, parse_bytes


def parse(filename: str, content: str | dict, declared: set[str] | None = None):
    raw = orjson.dumps(content) if isinstance(content, dict) else content.encode()
    return parse_bytes(filename, raw, declared=declared)


def pins(lockfile) -> set[str]:
    return {f"{pin.name}@{pin.version}" for pin in lockfile.pins}


class TestCoordinates:
    def test_a_plain_name_and_version_split(self):
        assert coordinate.split("lodash@4.17.21") == ("lodash", "4.17.21")

    def test_a_scoped_name_keeps_its_leading_at(self):
        assert coordinate.split("@babel/core@7.17.0") == ("@babel/core", "7.17.0")

    def test_a_peer_resolution_suffix_is_not_part_of_the_version(self):
        assert coordinate.split("react-dom@18.2.0(react@18.2.0)") == ("react-dom", "18.2.0")

    def test_the_pnpm_leading_slash_is_dropped(self):
        assert coordinate.split("/lodash@4.17.21") == ("lodash", "4.17.21")

    def test_the_older_pnpm_slash_separator_is_understood(self):
        assert coordinate.split("/lodash/4.17.21") == ("lodash", "4.17.21")

    def test_a_scoped_name_with_the_slash_separator(self):
        assert coordinate.split("/@babel/core/7.17.0") == ("@babel/core", "7.17.0")

    def test_the_yarn_berry_protocol_prefix_is_stripped(self):
        assert coordinate.split("lodash@npm:4.17.21") == ("lodash", "4.17.21")

    def test_something_that_is_not_a_coordinate_yields_nothing(self):
        assert coordinate.split("__metadata") == ("", "")

    def test_a_nested_install_path_names_the_innermost_package(self):
        path = "node_modules/jest/node_modules/@babel/core"

        assert coordinate.from_module_path(path) == "@babel/core"


NPM_V3 = {
    "name": "checkout-api",
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "checkout-api", "dependencies": {"express": "4.17.1"}},
        "node_modules/express": {"version": "4.17.1"},
        "node_modules/lodash": {"version": "4.17.20"},
        "node_modules/express/node_modules/debug": {"version": "2.6.9"},
    },
}


class TestNpmLockfile:
    def test_every_installed_release_is_pinned(self):
        assert pins(parse("package-lock.json", NPM_V3)) == {
            "express@4.17.1",
            "lodash@4.17.20",
            "debug@2.6.9",
        }

    def test_the_lockfile_version_is_recorded(self):
        assert parse("package-lock.json", NPM_V3).format_version == "3"

    def test_the_kind_is_recorded(self):
        assert parse("package-lock.json", NPM_V3).kind is LockfileKind.NPM

    def test_the_root_entry_is_not_a_dependency_of_itself(self):
        assert "checkout-api" not in {pin.name for pin in parse("package-lock.json", NPM_V3).pins}

    def test_a_declared_top_level_package_is_direct(self):
        parsed = parse("package-lock.json", NPM_V3)

        assert [pin.name for pin in parsed.direct] == ["express"]

    def test_a_nested_copy_is_never_direct(self):
        # Nested install ≠ declared direct dep.
        parsed = parse("package-lock.json", NPM_V3, declared={"express", "debug"})

        assert "debug" not in {pin.name for pin in parsed.direct}

    def test_a_workspace_link_pins_nothing(self):
        document = {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "root"},
                "node_modules/@acme/shared": {"resolved": "packages/shared", "link": True},
                "packages/shared": {"version": "1.0.0", "name": "@acme/shared"},
            },
        }
        parsed = parse("package-lock.json", document)

        # The link itself is not a release; the workspace directory behind it is
        # not published either, but it is at least a real version.
        assert "@acme/shared@1.0.0" in pins(parsed)
        assert len(parsed.pins) == 1

    def test_the_version_one_dependency_tree_is_walked(self):
        document = {
            "lockfileVersion": 1,
            "dependencies": {
                "express": {
                    "version": "4.17.1",
                    "dependencies": {"debug": {"version": "2.6.9"}},
                },
                "lodash": {"version": "4.17.20"},
            },
        }

        assert pins(parse("package-lock.json", document)) == {
            "express@4.17.1",
            "debug@2.6.9",
            "lodash@4.17.20",
        }

    def test_version_one_takes_direct_dependencies_from_the_manifest(self):
        # v1 has no root entry, so without package.json there is nothing to say
        # which of the hoisted packages the service actually asked for.
        document = {
            "lockfileVersion": 1,
            "dependencies": {"express": {"version": "4.17.1"}, "debug": {"version": "2.6.9"}},
        }
        parsed = parse("package-lock.json", document, declared={"express"})

        assert [pin.name for pin in parsed.direct] == ["express"]

    def test_a_corrupt_lockfile_yields_no_pins_rather_than_an_error(self):
        assert parse("package-lock.json", "{not json").pins == []


YARN_V1 = """# THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.
# yarn lockfile v1


"@babel/core@^7.0.0", "@babel/core@^7.12.0":
  version "7.17.0"
  resolved "https://registry.yarnpkg.com/@babel/core/-/core-7.17.0.tgz#abc"
  integrity sha512-abc

lodash@4.17.20:
  version "4.17.20"
  resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.20.tgz#def"

react@^17.0.0:
  version "17.0.2"
  dependencies:
    loose-envify "^1.1.0"
"""

YARN_BERRY = """__metadata:
  version: 6
  cacheKey: 8

"lodash@npm:4.17.20":
  version: 4.17.20
  resolution: "lodash@npm:4.17.20"
  languageName: node

"@acme/storefront@workspace:.":
  version: 0.0.0-use.local
  resolution: "@acme/storefront@workspace:."

"react-dom@npm:17.0.2, react-dom@npm:^17.0.0":
  version: 17.0.2
  resolution: "react-dom@npm:17.0.2"
"""


class TestYarnLockfile:
    def test_classic_entries_are_read(self):
        assert pins(parse("yarn.lock", YARN_V1)) == {
            "@babel/core@7.17.0",
            "lodash@4.17.20",
            "react@17.0.2",
        }

    def test_classic_is_reported_as_version_one(self):
        assert parse("yarn.lock", YARN_V1).format_version == "1"

    def test_a_nested_dependencies_block_is_not_mistaken_for_a_version(self):
        # `dependencies:` inside an entry is indented exactly like `version`.
        parsed = parse("yarn.lock", YARN_V1)

        assert "loose-envify" not in {pin.name for pin in parsed.pins}

    def test_one_entry_answering_several_specs_pins_once(self):
        parsed = parse("yarn.lock", YARN_V1)

        assert len([pin for pin in parsed.pins if pin.name == "@babel/core"]) == 1

    def test_berry_is_parsed_as_yaml(self):
        assert pins(parse("yarn.lock", YARN_BERRY)) == {
            "lodash@4.17.20",
            "react-dom@17.0.2",
        }

    def test_berry_reports_its_own_format_version(self):
        assert parse("yarn.lock", YARN_BERRY).format_version == "6"

    def test_a_workspace_member_is_not_a_dependency(self):
        assert "@acme/storefront" not in {pin.name for pin in parse("yarn.lock", YARN_BERRY).pins}

    def test_direct_dependencies_come_from_the_manifest(self):
        parsed = parse("yarn.lock", YARN_V1, declared={"react"})

        assert [pin.name for pin in parsed.direct] == ["react"]


PNPM_V9 = """lockfileVersion: '9.0'

importers:
  .:
    dependencies:
      lodash:
        specifier: 4.17.20
        version: 4.17.20

packages:
  lodash@4.17.20:
    resolution: {integrity: sha512-abc}
  react-dom@17.0.2(react@17.0.2):
    resolution: {integrity: sha512-def}

snapshots:
  lodash@4.17.20: {}
"""

PNPM_V5 = """lockfileVersion: 5.4

packages:
  /lodash/4.17.20:
    resolution: {integrity: sha512-abc}
  /@babel/core/7.17.0:
    resolution: {integrity: sha512-def}
"""


class TestPnpmLockfile:
    def test_version_nine_keys_are_read(self):
        assert pins(parse("pnpm-lock.yaml", PNPM_V9)) == {
            "lodash@4.17.20",
            "react-dom@17.0.2",
        }

    def test_the_snapshots_block_does_not_duplicate_a_pin(self):
        parsed = parse("pnpm-lock.yaml", PNPM_V9)

        assert len([pin for pin in parsed.pins if pin.name == "lodash"]) == 1

    def test_importers_say_which_dependencies_are_direct(self):
        parsed = parse("pnpm-lock.yaml", PNPM_V9)

        assert [pin.name for pin in parsed.direct] == ["lodash"]

    def test_the_older_slash_separated_keys_are_read(self):
        assert pins(parse("pnpm-lock.yaml", PNPM_V5)) == {
            "lodash@4.17.20",
            "@babel/core@7.17.0",
        }

    def test_the_format_version_is_recorded(self):
        assert parse("pnpm-lock.yaml", PNPM_V9).format_version == "9.0"


class TestDispatch:
    def test_an_unrecognised_filename_is_refused(self):
        with pytest.raises(UnknownLockfile):
            parse("Gemfile.lock", "nothing here")

    def test_a_full_path_still_dispatches_on_the_filename(self):
        parsed = parse_bytes("services/api/package-lock.json", orjson.dumps(NPM_V3))

        assert parsed.kind is LockfileKind.NPM

    def test_npm_is_preferred_when_a_directory_holds_two(self, tmp_path):
        # A half-finished migration leaves both behind.
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "yarn.lock").write_text("")

        assert find_lockfile(tmp_path).name == "package-lock.json"

    def test_a_directory_without_a_lockfile_finds_nothing(self, tmp_path):
        assert find_lockfile(tmp_path) is None
