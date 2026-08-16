"""Errors raised by the HydraDB client."""

from __future__ import annotations


class HydraError(Exception):
    """Base for every HydraDB failure."""


class HydraUnavailable(HydraError):
    """The node could not be reached at all."""


class HydraQueryError(HydraError):
    """Node rejected/failed a query; keep code + message."""

    def __init__(self, code: str, message: str, status: int, query: str = "") -> None:
        self.code = code
        self.message = message
        self.status = status
        self.query = query
        detail = f"[{status} {code}] {message}"
        if query:
            detail = f"{detail}\n  query: {query}"
        if status >= 500:
            # The node answers 5xx with a deliberately vague body and logs the
            # real reason as "HTTP suppressed internal graph error". Without this
            # pointer the useful half of the failure is invisible.
            detail = f"{detail}\n  the node logged the reason: docker compose logs graph-node"
        super().__init__(detail)


class HydraAuthError(HydraQueryError):
    """Bearer authentication was missing, malformed or refused."""


class HydraNotOwner(HydraQueryError):
    """This node does not own the addressed cell.

    HydraDB answers 421 with the owning node when it still has a view of the
    fleet, so the hint is worth surfacing rather than retrying blindly.
    """

    def __init__(self, code: str, message: str, status: int, owner: str | None) -> None:
        self.owner = owner
        super().__init__(code, message, status)
