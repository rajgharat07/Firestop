"""Epoch-second timestamps (Hydra has no temporal property type)."""

from __future__ import annotations

from datetime import datetime

# Sorts before every real timestamp, so an advisory with no publish date does not
# accidentally look recent.
UNKNOWN_TIME = 0


def parse_timestamp(stamp: str) -> int | None:
    """ISO 8601 with a trailing Z, which `fromisoformat` will not take directly."""
    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError, TypeError):
        return None


def epoch_or_unknown(stamp: object) -> int:
    """Epoch seconds for a value that may be missing or malformed."""
    if not isinstance(stamp, str):
        return UNKNOWN_TIME
    return parse_timestamp(stamp) or UNKNOWN_TIME
