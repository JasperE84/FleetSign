from __future__ import annotations

import math


def positive_seconds(raw) -> float:
    """Parse a strictly-positive, finite number of seconds.

    Rejects 0, negatives, inf, and nan — nan/inf would also serialize into
    non-standard JSON and make mpv misbehave (never-advance / instant-skip).
    """
    v = float(raw)
    if not math.isfinite(v) or v <= 0:
        raise ValueError("seconds must be a positive finite number")
    return v
