from __future__ import annotations

from firestop.npm.resolve import ResolutionWindow, Resolver
from firestop.schema.model import OPEN_INTERVAL_END

# A package released 1.0.0, then 1.1.0, then a new major, then a further 1.x.
TIMELINE = {"1.0.0": 100, "1.1.0": 200, "2.0.0": 300, "1.2.0": 400}


def windows(spec: str, timeline: dict[str, int] | None = None, **kwargs):
    resolver = Resolver(**kwargs)
    return list(resolver.windows("dep", spec, timeline if timeline is not None else TIMELINE))


def test_a_caret_range_produces_one_window_per_resolution_change():
    assert windows("^1.0.0") == [
        ResolutionWindow("1.0.0", 100, 200),
        ResolutionWindow("1.1.0", 200, 400),
        ResolutionWindow("1.2.0", 400, OPEN_INTERVAL_END),
    ]


def test_windows_are_contiguous_and_non_overlapping():
    computed = windows("^1.0.0")

    for earlier, later in zip(computed, computed[1:], strict=False):
        assert earlier.valid_to == later.valid_from


def test_the_final_window_stays_open():
    assert windows("^1.0.0")[-1].is_open


def test_a_major_outside_the_range_is_ignored():
    assert all(window.version != "2.0.0" for window in windows("^1.0.0"))


def test_an_exact_pin_never_changes_resolution():
    assert windows("=1.1.0") == [ResolutionWindow("1.1.0", 200, OPEN_INTERVAL_END)]


def test_a_backport_onto_an_older_line_does_not_change_the_resolution():
    # Lower semver after a higher one does not open a window.
    timeline = {"1.0.0": 100, "1.5.0": 200, "1.1.0": 300}

    assert windows("^1.0.0", timeline) == [
        ResolutionWindow("1.0.0", 100, 200),
        ResolutionWindow("1.5.0", 200, OPEN_INTERVAL_END),
    ]


def test_prereleases_are_excluded_from_an_ordinary_range():
    timeline = {"1.0.0": 100, "1.1.0-beta.1": 200}

    assert windows("^1.0.0", timeline) == [ResolutionWindow("1.0.0", 100, OPEN_INTERVAL_END)]


def test_a_prerelease_range_admits_prereleases():
    timeline = {"1.0.0-beta.1": 100, "1.0.0": 200}

    assert [w.version for w in windows("^1.0.0-beta.1", timeline)] == ["1.0.0-beta.1", "1.0.0"]


def test_an_empty_timeline_yields_nothing():
    assert windows("^1.0.0", {}) == []


def test_a_range_nothing_satisfies_yields_nothing():
    assert windows("^9.0.0") == []


def test_covers_answers_the_as_of_question():
    first, second, third = windows("^1.0.0")

    assert first.covers(150)
    assert not first.covers(200)
    assert second.covers(200)
    assert second.covers(399)
    assert not second.covers(400)
    assert third.covers(400)
    assert third.covers(10**9)


class TestUnresolvableSpecs:
    # Non-range specs (tags, urls) do not resolve.
    SPECS = (
        "git+https://github.com/a/b.git",
        "github:a/b",
        "file:../local",
        "npm:other@^1.0.0",
        "workspace:*",
        "https://example.com/x.tgz",
        "latest",
        "next",
    )

    def test_none_of_them_resolve(self):
        resolver = Resolver()
        for spec in self.SPECS:
            assert resolver.windows("dep", spec, TIMELINE) == ()

    def test_they_are_counted_rather_than_silently_dropped(self):
        resolver = Resolver()
        for spec in self.SPECS:
            resolver.windows("dep", spec, TIMELINE)

        assert resolver.unresolvable_ranges == len(self.SPECS)

    def test_a_wildcard_is_still_a_real_range(self):
        assert windows("*")[-1].version == "2.0.0"

    def test_an_empty_spec_behaves_as_a_wildcard(self):
        assert windows("")[-1].version == "2.0.0"


class TestClippingToTheDependent:
    def test_a_window_that_closed_before_the_dependent_existed_is_dropped(self):
        resolver = Resolver()
        clipped = resolver.windows_for("dep", "^1.0.0", TIMELINE, 250)

        assert all(window.version != "1.0.0" for window in clipped)

    def test_a_straddling_window_starts_when_the_dependent_was_published(self):
        resolver = Resolver()
        clipped = resolver.windows_for("dep", "^1.0.0", TIMELINE, 250)

        assert clipped[0] == ResolutionWindow("1.1.0", 250, 400)

    def test_later_windows_are_untouched(self):
        resolver = Resolver()
        clipped = resolver.windows_for("dep", "^1.0.0", TIMELINE, 250)

        assert clipped[-1] == ResolutionWindow("1.2.0", 400, OPEN_INTERVAL_END)

    def test_a_dependent_older_than_everything_sees_every_window(self):
        resolver = Resolver()

        assert len(resolver.windows_for("dep", "^1.0.0", TIMELINE, 0)) == 3


class TestWindowLimit:
    def test_only_the_most_recent_windows_are_kept(self):
        resolver = Resolver(max_windows=2)
        kept = resolver.windows_for("dep", "^1.0.0", TIMELINE, 0)

        assert [window.version for window in kept] == ["1.1.0", "1.2.0"]

    def test_zero_means_keep_everything(self):
        resolver = Resolver(max_windows=0)

        assert len(resolver.windows_for("dep", "^1.0.0", TIMELINE, 0)) == 3

    def test_candidate_versions_are_bounded(self):
        # Only the most recently published candidates are considered, which keeps
        # a package with thousands of versions from dominating the crawl's CPU.
        timeline = {f"1.{minor}.0": minor for minor in range(100)}
        resolver = Resolver(max_candidates=5, max_windows=0)

        assert len(resolver.windows("dep", "^1.0.0", timeline)) == 5


class TestCaching:
    def test_a_repeated_question_is_answered_from_cache(self):
        resolver = Resolver()

        first = resolver.windows("dep", "^1.0.0", TIMELINE)
        second = resolver.windows("dep", "^1.0.0", TIMELINE)

        assert first is second

    def test_the_cache_is_keyed_on_package_as_well_as_range(self):
        resolver = Resolver()
        other = {"5.0.0": 100}

        assert resolver.windows("dep", "*", TIMELINE) != resolver.windows("other", "*", other)

    def test_an_unresolvable_spec_is_only_judged_once(self):
        resolver = Resolver()

        resolver.windows("dep", "workspace:*", TIMELINE)
        resolver.windows("dep", "workspace:*", TIMELINE)

        assert resolver.unresolvable_ranges == 1
