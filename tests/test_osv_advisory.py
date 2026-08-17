from __future__ import annotations

from firestop.osv.advisory import UNKNOWN_SEVERITY, parse_advisory

GHSA = {
    "id": "GHSA-35jh-r3h4-6jhm",
    "summary": "Command Injection in lodash",
    "details": "lodash prior to 4.17.21 is vulnerable to Command Injection.",
    "aliases": ["CVE-2021-23337"],
    "published": "2021-05-06T16:05:36Z",
    "database_specific": {"severity": "HIGH", "cwe_ids": ["CWE-77", "CWE-94"]},
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N"}],
    "affected": [
        {
            "package": {"ecosystem": "npm", "name": "lodash"},
            "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}],
            "versions": ["4.17.20"],
        }
    ],
}


def parsed(**overrides):
    return parse_advisory({**GHSA, **overrides})


class TestParseAdvisory:
    def test_identity_and_headline_are_kept(self):
        advisory = parsed()

        assert advisory.osv_id == "GHSA-35jh-r3h4-6jhm"
        assert advisory.summary == "Command Injection in lodash"

    def test_published_becomes_epoch_seconds(self):
        assert parsed().published_at == 1620317136

    def test_a_missing_publish_date_sorts_before_every_real_one(self):
        assert parsed(published=None).published_at == 0

    def test_the_qualitative_rating_is_preferred_over_the_cvss_vector(self):
        # It is what an on-call engineer triages by.
        assert parsed().severity == "HIGH"

    def test_the_cvss_score_stands_in_when_there_is_no_rating(self):
        assert parsed(database_specific={}).severity.startswith("CVSS:3.1/")

    def test_severity_is_explicit_when_the_record_says_nothing(self):
        assert parsed(database_specific={}, severity=[]).severity == UNKNOWN_SEVERITY

    def test_aliases_are_flattened_because_properties_cannot_hold_lists(self):
        assert parsed().aliases == "CVE-2021-23337"

    def test_weaknesses_are_flattened_too(self):
        assert parsed().cwe == "CWE-77,CWE-94"

    def test_the_first_line_of_the_long_form_stands_in_for_a_missing_summary(self):
        advisory = parsed(summary="")

        assert advisory.summary.startswith("lodash prior to 4.17.21")

    def test_a_withdrawn_record_is_dropped(self):
        # Withdrawn advisories are noise.
        assert parsed(withdrawn="2022-01-01T00:00:00Z") is None

    def test_a_record_without_an_id_is_dropped(self):
        assert parsed(id="") is None


class TestAffectedEntries:
    def test_the_package_name_survives(self):
        assert parsed().affected[0].name == "lodash"

    def test_other_ecosystems_are_ignored(self):
        entry = {
            "package": {"ecosystem": "PyPI", "name": "django"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
        }

        assert parsed(affected=[entry]) is None

    def test_a_record_naming_no_versions_at_all_is_dropped(self):
        # Not specific enough to point at a release.
        entry = {"package": {"ecosystem": "npm", "name": "lodash"}}

        assert parsed(affected=[entry]) is None

    def test_ecosystem_ranges_are_read_alongside_semver_ones(self):
        entry = {
            "package": {"ecosystem": "npm", "name": "lodash"},
            "ranges": [
                {"type": "ECOSYSTEM", "events": [{"introduced": "1.0.0"}, {"fixed": "2.0.0"}]}
            ],
        }
        ranges = parsed(affected=[entry]).affected[0].ranges

        assert [(r.introduced, r.fixed) for r in ranges] == [("1.0.0", "2.0.0")]

    def test_git_ranges_are_ignored_because_a_commit_cannot_name_a_release(self):
        entry = {
            "package": {"ecosystem": "npm", "name": "lodash"},
            "ranges": [{"type": "GIT", "events": [{"introduced": "abc123"}]}],
            "versions": ["4.17.20"],
        }
        affected = parsed(affected=[entry]).affected[0]

        assert affected.ranges == ()
        assert affected.versions == ("4.17.20",)


class TestRangeEvents:
    def affected_with(self, events: list[dict]):
        entry = {
            "package": {"ecosystem": "npm", "name": "widget"},
            "ranges": [{"type": "SEMVER", "events": events}],
        }
        advisory = parse_advisory({**GHSA, "affected": [entry]})
        return advisory.affected[0].ranges

    def test_introduced_and_fixed_pair_into_one_interval(self):
        ranges = self.affected_with([{"introduced": "1.0.0"}, {"fixed": "1.2.3"}])

        assert [(r.introduced, r.fixed) for r in ranges] == [("1.0.0", "1.2.3")]

    def test_several_intervals_are_folded_in_order(self):
        # The shape a backported fix produces: two supported majors, two patches.
        ranges = self.affected_with(
            [
                {"introduced": "1.0.0"},
                {"fixed": "1.2.3"},
                {"introduced": "2.0.0"},
                {"fixed": "2.1.0"},
            ]
        )

        assert [(r.introduced, r.fixed) for r in ranges] == [
            ("1.0.0", "1.2.3"),
            ("2.0.0", "2.1.0"),
        ]

    def test_a_trailing_introduced_leaves_the_interval_open(self):
        ranges = self.affected_with([{"introduced": "1.0.0"}])

        assert ranges[0].unfixed
        assert ranges[0].fixed == ""

    def test_two_introduced_events_in_a_row_both_open_intervals(self):
        ranges = self.affected_with([{"introduced": "1.0.0"}, {"introduced": "2.0.0"}])

        assert [r.introduced for r in ranges] == ["1.0.0", "2.0.0"]
        assert all(r.unfixed for r in ranges)

    def test_last_affected_is_kept_apart_from_fixed(self):
        # Inclusive rather than exclusive, so conflating the two would shift the
        # boundary by one release.
        ranges = self.affected_with([{"introduced": "1.0.0"}, {"last_affected": "1.9.9"}])

        assert ranges[0].last_affected == "1.9.9"
        assert ranges[0].fixed == ""

    def test_a_fix_with_no_introduction_is_not_an_interval(self):
        # Which leaves the entry naming nothing, so the record is dropped whole.
        entry = {
            "package": {"ecosystem": "npm", "name": "widget"},
            "ranges": [{"type": "SEMVER", "events": [{"fixed": "1.2.3"}]}],
        }

        assert parse_advisory({**GHSA, "affected": [entry]}) is None
