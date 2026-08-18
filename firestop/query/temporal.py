"""Client-side as-of filter on returned paths (algo.* can't predicate on edge props)."""

from __future__ import annotations

from dataclasses import dataclass

from firestop.hydra.values import GraphPath, PathStep
from firestop.schema.model import OPEN_INTERVAL_END

# A moment far enough in the past that nothing precedes it, used as the identity
# when intersecting windows.
BEGINNING = 0


@dataclass(frozen=True, slots=True)
class Window:
    """The interval over which a whole path held, as a half-open range."""

    valid_from: int = BEGINNING
    valid_to: int = OPEN_INTERVAL_END

    @property
    def empty(self) -> bool:
        return self.valid_from >= self.valid_to

    @property
    def open_ended(self) -> bool:
        return self.valid_to >= OPEN_INTERVAL_END

    def covers(self, moment: int) -> bool:
        return self.valid_from <= moment < self.valid_to

    def intersect(self, other: Window) -> Window:
        return Window(
            valid_from=max(self.valid_from, other.valid_from),
            valid_to=min(self.valid_to, other.valid_to),
        )


def step_window(step: PathStep) -> Window:
    """The window on one hop, or an unbounded one if it carries none."""
    valid_from = step.get("valid_from")
    valid_to = step.get("valid_to")

    return Window(
        valid_from=int(valid_from) if isinstance(valid_from, int) else BEGINNING,
        valid_to=int(valid_to) if isinstance(valid_to, int) else OPEN_INTERVAL_END,
    )


def path_window(path: GraphPath) -> Window:
    """When the whole path held: the intersection of every hop's window.

    An empty result means the path never existed all at once. That is not an
    error -- it is the difference between a chain of edges that each existed at
    some point and a chain that was ever simultaneously true.
    """
    window = Window()
    for step in path.steps:
        window = window.intersect(step_window(step))
    return window


def live_at(path: GraphPath, moment: int | None) -> bool:
    """Was this path real at `moment`? With no moment, only coherence is required."""
    window = path_window(path)
    if moment is None:
        return not window.empty
    return window.covers(moment)
