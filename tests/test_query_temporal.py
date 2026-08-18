from __future__ import annotations

from firestop.hydra.values import decode_path
from firestop.query.temporal import Window, live_at, path_window
from firestop.schema.model import OPEN_INTERVAL_END
from tests.paths import hop, node, service_path

JANUARY = 1_640_995_200
JUNE = 1_654_041_600
DECEMBER = 1_672_531_200


def path_of(*windows: tuple[int, int]):
    """A chain whose hops carry the given validity intervals."""
    nodes = [
        node(index + 1, "Release", key=f"pkg-{index}@1.0.0") for index in range(len(windows) + 1)
    ]
    steps = [
        hop("DEPENDS_ON", index + 1, index + 2, valid_from=start, valid_to=end)
        for index, (start, end) in enumerate(windows)
    ]
    return decode_path({"nodes": nodes, "relationships": steps})


class TestWindows:
    def test_a_window_covers_its_start(self):
        assert Window(JANUARY, DECEMBER).covers(JANUARY)

    def test_a_window_does_not_cover_its_end(self):
        # Half-open, so the moment a dependency is replaced belongs to the
        # replacement.
        assert not Window(JANUARY, JUNE).covers(JUNE)

    def test_intersecting_takes_the_later_start_and_earlier_end(self):
        overlap = Window(JANUARY, DECEMBER).intersect(Window(JUNE, OPEN_INTERVAL_END))

        assert overlap.valid_from == JUNE
        assert overlap.valid_to == DECEMBER

    def test_windows_that_do_not_overlap_intersect_to_nothing(self):
        assert Window(JANUARY, JUNE).intersect(Window(DECEMBER, OPEN_INTERVAL_END)).empty

    def test_an_unbounded_end_reads_as_still_true(self):
        assert Window(JANUARY, OPEN_INTERVAL_END).open_ended


class TestPathWindows:
    def test_a_path_holds_only_while_every_hop_holds(self):
        window = path_window(path_of((JANUARY, DECEMBER), (JUNE, OPEN_INTERVAL_END)))

        assert window.valid_from == JUNE
        assert window.valid_to == DECEMBER

    def test_hops_that_never_coincided_make_a_path_that_never_existed(self):
        # Each edge was real at some point. The chain was not.
        assert path_window(path_of((JANUARY, JUNE), (DECEMBER, OPEN_INTERVAL_END))).empty

    def test_a_hop_without_an_interval_constrains_nothing(self):
        # A lockfile pin has no window of its own.
        path = decode_path(
            {
                "nodes": [node(1, "Lockfile", path="a"), node(2, "Release", key="x@1.0.0")],
                "relationships": [hop("PINS", 1, 2, resolved_version="1.0.0")],
            }
        )

        assert path_window(path).open_ended

    def test_a_path_with_no_hops_is_always_true(self):
        assert not path_window(decode_path({})).empty


class TestLiveAt:
    def test_a_moment_inside_every_window_is_live(self):
        assert live_at(path_of((JANUARY, DECEMBER)), JUNE)

    def test_a_moment_after_the_window_is_not(self):
        assert not live_at(path_of((JANUARY, JUNE)), DECEMBER)

    def test_a_moment_before_the_window_is_not(self):
        assert not live_at(path_of((JUNE, DECEMBER)), JANUARY)

    def test_without_a_moment_only_coherence_matters(self):
        assert live_at(path_of((JANUARY, JUNE)), None)
        assert not live_at(path_of((JANUARY, JUNE), (DECEMBER, OPEN_INTERVAL_END)), None)

    def test_a_real_service_path_is_live_while_its_dependency_edge_is(self):
        raw = service_path(
            "checkout-api",
            package="evil",
            version="1.4.2",
            hops=[("express", "4.17.1")],
            valid_from=JANUARY,
            valid_to=DECEMBER,
        )
        path = decode_path(raw["value"])

        assert live_at(path, JUNE)
        assert not live_at(path, DECEMBER)
