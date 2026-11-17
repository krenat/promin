"""Reading values that arrive from a page, which may send anything."""
from __future__ import annotations

from typing import Any


def finite(value: Any) -> float:
    """A number from the projector page; anything else, NaN and infinity read as 0."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0
