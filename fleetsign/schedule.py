from datetime import datetime, time

from .model import MediaItem


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def is_active(item: MediaItem, now: datetime) -> bool:
    if not item.enabled:
        return False
    s = item.schedule
    if s is None:
        return True
    try:
        start = parse_hhmm(s.start)
        end = parse_hhmm(s.end)
    except (ValueError, AttributeError):
        # A malformed/legacy schedule must never crash the player loop;
        # treat it as inactive so only this item is hidden.
        return False
    t = now.time()
    if start <= end:
        in_window = start <= t < end
        window_day = now.weekday()
    else:
        # Overnight window (e.g. 22:00-02:00): the after-midnight tail belongs to
        # the weekday the window started on, not the calendar day it spills into,
        # so a "Friday 22:00-02:00" item is still active at Saturday 01:00.
        if t >= start:
            in_window, window_day = True, now.weekday()
        elif t < end:
            in_window, window_day = True, (now.weekday() - 1) % 7
        else:
            in_window, window_day = False, now.weekday()
    if not in_window:
        return False
    return not s.days or window_day in s.days
