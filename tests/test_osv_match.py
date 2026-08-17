from __future__ import annotations

from firestop.osv.advisory import AffectedPackage, VersionRange
from firestop.osv.match import FROM_ENUMERATION, FROM_RANGE, matches


def affected(*ranges: VersionRange, versions: tuple[str, ...] = ()) -> AffectedPackage:
    return AffectedPackage(name="widget", ranges=ranges, versions=versions)


def matched(entry: AffectedPackage, versions: list[str]) -> list[str]:
    return [match.version for match in matches(entry, versions)]


class TestRangeMatching:
    def test_the_fixed_version_is_excluded(self):
        # fixed is exclusive (upgrade target).
        entry = affected(VersionRange(introduced="0", fixed="4.17.21"))

        assert matched(entry, ["4.17.20", "4.17.21", "4.17.22"]) == ["4.17.20"]

    def test_the_introduced_version_is_included(self):
        entry = affected(VersionRange(introduced="1.2.0", fixed="1.3.0"))

        assert matched(entry, ["1.1.9", "1.2.0", "1.2.9"]) == ["1.2.0", "1.2.9"]

    def test_zero_means_from_the_beginning(self):
        entry = affected(VersionRange(introduced="0", fixed="2.0.0"))

        assert matched(entry, ["0.0.1", "1.9.9", "2.0.0"]) == ["0.0.1", "1.9.9"]

    def test_an_unfixed_range_covers_everything_above_it(self):
        entry = affected(VersionRange(introduced="1.0.0"))

        assert matched(entry, ["0.9.0", "1.0.0", "9.9.9"]) == ["1.0.0", "9.9.9"]

    def test_last_affected_is_inclusive_unlike_fixed(self):
        entry = affected(VersionRange(introduced="1.0.0", last_affected="1.9.9"))

        assert matched(entry, ["1.9.9", "2.0.0"]) == ["1.9.9"]

    def test_a_release_between_two_patched_lines_is_not_affected(self):
        # 1.5.0 is fixed, 2.0.0 reintroduced it: the classic backport shape, and
        # the case a single interval would get wrong.
        entry = affected(
            VersionRange(introduced="1.0.0", fixed="1.5.0"),
            VersionRange(introduced="2.0.0", fixed="2.1.0"),
        )

        assert matched(entry, ["1.4.0", "1.5.0", "1.9.0", "2.0.5", "2.1.0"]) == [
            "1.4.0",
            "2.0.5",
        ]

    def test_semver_ordering_is_used_rather_than_string_ordering(self):
        # "1.10.0" sorts below "1.9.0" as a string and above it as a version.
        entry = affected(VersionRange(introduced="1.9.0", fixed="1.11.0"))

        assert matched(entry, ["1.10.0"]) == ["1.10.0"]

    def test_a_prerelease_inside_the_interval_is_affected(self):
        entry = affected(VersionRange(introduced="1.0.0", fixed="2.0.0"))

        assert matched(entry, ["1.5.0-beta.1"]) == ["1.5.0-beta.1"]

    def test_the_matched_interval_is_reported_on_the_match(self):
        entry = affected(VersionRange(introduced="4.0.0", fixed="4.17.21"))
        match = matches(entry, ["4.17.20"])[0]

        assert (match.introduced, match.fixed_in, match.source) == (
            "4.0.0",
            "4.17.21",
            FROM_RANGE,
        )

    def test_an_unfixed_match_says_so(self):
        entry = affected(VersionRange(introduced="1.0.0"))

        assert matches(entry, ["1.0.0"])[0].unfixed

    def test_a_version_that_is_not_semver_is_left_alone(self):
        # Claiming a release is vulnerable on no evidence is worse than missing it.
        entry = affected(VersionRange(introduced="0"))

        assert matched(entry, ["not-a-version"]) == []

    def test_an_unparseable_bound_does_not_exclude_everything(self):
        entry = affected(VersionRange(introduced="0", fixed="latest"))

        assert matched(entry, ["1.0.0"]) == ["1.0.0"]


class TestEnumeratedVersions:
    def test_an_entry_with_no_ranges_falls_back_to_the_listed_versions(self):
        entry = affected(versions=("1.0.0", "1.0.1"))

        assert matched(entry, ["1.0.0", "1.0.1", "1.0.2"]) == ["1.0.0", "1.0.1"]

    def test_the_fallback_is_recorded_so_a_reader_knows_how_it_was_decided(self):
        entry = affected(versions=("1.0.0",))

        assert matches(entry, ["1.0.0"])[0].source == FROM_ENUMERATION

    def test_a_listed_version_outside_every_range_is_still_affected(self):
        # The database named it outright, which outranks a range that missed it.
        entry = affected(VersionRange(introduced="2.0.0", fixed="2.1.0"), versions=("1.0.0",))

        assert matched(entry, ["1.0.0", "2.0.5", "3.0.0"]) == ["1.0.0", "2.0.5"]

    def test_ranges_win_where_both_apply(self):
        entry = affected(VersionRange(introduced="1.0.0", fixed="2.0.0"), versions=("1.0.0",))

        assert matches(entry, ["1.0.0"])[0].source == FROM_RANGE

    def test_nothing_matches_when_the_entry_names_nothing(self):
        assert matched(affected(), ["1.0.0"]) == []
