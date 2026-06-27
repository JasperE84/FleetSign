from datetime import datetime
from fleetsign.model import MediaItem, Schedule
from fleetsign.schedule import is_active

def item(**kw):
    base = dict(id="x", filename="a.png", type="image")
    base.update(kw)
    return MediaItem(**base)

def test_disabled_is_inactive():
    assert is_active(item(enabled=False), datetime(2026, 6, 26, 12, 0)) is False

def test_no_schedule_always_active():
    assert is_active(item(), datetime(2026, 6, 26, 3, 0)) is True

def test_inside_window():
    it = item(schedule=Schedule(days=[], start="08:00", end="17:00"))
    assert is_active(it, datetime(2026, 6, 26, 9, 0)) is True
    assert is_active(it, datetime(2026, 6, 26, 17, 0)) is False  # end exclusive
    assert is_active(it, datetime(2026, 6, 26, 7, 59)) is False

def test_weekday_filter():
    # 2026-06-26 is a Friday (weekday 4)
    it = item(schedule=Schedule(days=[0, 1, 2], start="00:00", end="23:59"))
    assert is_active(it, datetime(2026, 6, 26, 12, 0)) is False

def test_overnight_wrap():
    it = item(schedule=Schedule(days=[], start="22:00", end="02:00"))
    assert is_active(it, datetime(2026, 6, 26, 23, 0)) is True
    assert is_active(it, datetime(2026, 6, 26, 1, 0)) is True
    assert is_active(it, datetime(2026, 6, 26, 12, 0)) is False

def test_malformed_schedule_is_inactive():
    it = item(schedule=Schedule(days=[], start="9am", end="bad"))
    assert is_active(it, datetime(2026, 6, 26, 12, 0)) is False

def test_overnight_window_with_weekday_filter():
    # Friday 22:00-02:00, only Fridays selected (Fri = weekday 4).
    sched = Schedule(days=[4], start="22:00", end="02:00")
    # 2026-06-26 is a Friday; 2026-06-27 is a Saturday.
    assert is_active(item(schedule=sched), datetime(2026, 6, 26, 23, 0)) is True   # Fri night
    assert is_active(item(schedule=sched), datetime(2026, 6, 27, 1, 0)) is True    # Sat 01:00 = Fri's tail
    assert is_active(item(schedule=sched), datetime(2026, 6, 27, 12, 0)) is False  # Sat midday
    assert is_active(item(schedule=sched), datetime(2026, 6, 26, 1, 0)) is False   # Fri 01:00 = Thu's tail
