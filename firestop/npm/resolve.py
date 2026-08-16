"""Resolve a version range into temporal windows of concrete releases.

Walk publish order, keep the best-matching version, emit a window each time
the winner changes. Windows start no earlier than the dependent's publish time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import nodesemver as semver

from firestop.schema.model import OPEN_INTERVAL_END


@dataclass(frozen=True, slots=True)
class ResolutionWindow:
    version: str
    valid_from: int
    valid_to: int

    def covers(self, moment: int) -> bool:
        return self.valid_from <= moment < self.valid_to

    @property
    def is_open(self) -> bool:
        return self.valid_to == OPEN_INTERVAL_END


class Resolver:
    """Resolves ranges to windows, memoised across the whole crawl.

    `semver.satisfies` costs roughly 200 microseconds, and a crawl asks the same
    question millions of times: the same range against the same package recurs
    once per dependent release. Caching on (package, range) rather than per
    dependent is what keeps ingest CPU-bound for minutes instead of hours.
    """

    def __init__(self, *, max_candidates: int = 60, max_windows: int = 5) -> None:
        self._max_candidates = max_candidates
        self._max_windows = max_windows
        self._windows: dict[tuple[str, str], tuple[ResolutionWindow, ...]] = {}
        self._valid_ranges: dict[str, bool] = {}
        self.unresolvable_ranges = 0
        self.unsatisfied_ranges = 0
        # Resolutions that named a real release which was never written as a
        # vertex, because it fell outside the per-package version cap.
        self.unwritten_targets = 0

    def is_resolvable(self, range_spec: str) -> bool:
        """Cheap gate for specs that are not version ranges at all.

        Manifests are full of `git+https://`, `file:../`, `npm:alias@^1`,
        `workspace:*` and dist-tags like `latest`. None of them resolve to a
        release in the registry timeline, and all of them are common enough that
        checking before attempting resolution matters.
        """
        cached = self._valid_ranges.get(range_spec)
        if cached is None:
            try:
                cached = semver.valid_range(range_spec, loose=True) is not None
            except Exception:
                cached = False
            self._valid_ranges[range_spec] = cached
        return cached

    def windows(
        self, package: str, range_spec: str, version_times: dict[str, int]
    ) -> tuple[ResolutionWindow, ...]:
        """Every window in which `range_spec` resolved to a specific release."""
        cached = self._windows.get((package, range_spec))
        if cached is not None:
            return cached

        if not self.is_resolvable(range_spec):
            self.unresolvable_ranges += 1
            self._windows[(package, range_spec)] = ()
            return ()

        computed = tuple(_windows(version_times, range_spec, self._max_candidates))
        if not computed:
            self.unsatisfied_ranges += 1
        self._windows[(package, range_spec)] = computed
        return computed

    def windows_for(
        self,
        package: str,
        range_spec: str,
        version_times: dict[str, int],
        dependent_published_at: int,
    ) -> list[ResolutionWindow]:
        """Windows as observed by one dependent release."""
        clipped = []
        for window in self.windows(package, range_spec, version_times):
            start = max(window.valid_from, dependent_published_at)
            if start >= window.valid_to:
                # The resolution had already been superseded before the dependent
                # was published, so this dependent never saw it.
                continue
            clipped.append(replace(window, valid_from=start))

        return clipped[-self._max_windows :] if self._max_windows > 0 else clipped


def _windows(
    version_times: dict[str, int], range_spec: str, max_candidates: int
) -> list[ResolutionWindow]:
    if not version_times:
        return []

    ordered = sorted(version_times.items(), key=lambda item: (item[1], item[0]))
    if max_candidates > 0:
        ordered = ordered[-max_candidates:]

    windows: list[ResolutionWindow] = []
    best: str | None = None

    for version, published_at in ordered:
        try:
            if not semver.satisfies(version, range_spec, loose=True):
                continue
        except Exception:
            continue

        if best is not None and _compare(version, best) <= 0:
            # A backport onto an older line. It satisfies the range but is not
            # what npm would pick, so the resolution does not change.
            continue

        if windows:
            windows[-1] = replace(windows[-1], valid_to=published_at)
        windows.append(
            ResolutionWindow(version=version, valid_from=published_at, valid_to=OPEN_INTERVAL_END)
        )
        best = version

    return windows


def _compare(left: str, right: str) -> int:
    try:
        return semver.compare(left, right, loose=True)
    except Exception:
        return -1
