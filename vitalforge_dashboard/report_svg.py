"""Server-side sparkline geometry for the printable report.

Returns plain, pre-formatted coordinate strings only; the template owns the markup, so
Jinja autoescaping covers every attribute and nothing is ever marked safe.
"""

import math
from collections.abc import Sequence

SPARK_WIDTH = 240
SPARK_HEIGHT = 48
SPARK_PAD = 4


def _coordinate(value: float) -> str:
    return f"{value:.1f}"


def _x(index: int, count: int) -> float:
    if count < 2:
        return SPARK_WIDTH / 2
    return SPARK_PAD + index * (SPARK_WIDTH - 2 * SPARK_PAD) / (count - 1)


def _y(value: float, low: float, high: float) -> float:
    span = high - low
    if span == 0 or not math.isfinite(span):  # constant or overflowing range: mid-height, never nan
        return SPARK_HEIGHT / 2
    return (SPARK_HEIGHT - SPARK_PAD) - (value - low) / span * (SPARK_HEIGHT - 2 * SPARK_PAD)


def sparkline(values: Sequence[float | None]) -> dict:
    """One point per calendar date; None (or a non-finite value) is a gap, 0 is a real value."""
    values = [value if value is not None and math.isfinite(value) else None for value in values]
    present = [value for value in values if value is not None]
    if not present:
        return {"width": SPARK_WIDTH, "height": SPARK_HEIGHT, "segments": [], "dots": []}
    low, high = min(present), max(present)
    segments: list[list[str]] = []
    dots: list[dict] = []
    run: list[str] = []
    for index, value in enumerate(values):
        if value is None:
            segments.append(run)
            run = []
            continue
        x, y = _coordinate(_x(index, len(values))), _coordinate(_y(value, low, high))
        run.append(f"{x},{y}")
        dots.append({"cx": x, "cy": y})
    segments.append(run)
    return {
        "width": SPARK_WIDTH, "height": SPARK_HEIGHT, "dots": dots,
        "segments": [" ".join(points) for points in segments if len(points) > 1],
    }
