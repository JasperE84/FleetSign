import pytest
from fleetsign.model import (MediaItem, Schedule, Settings, classify, is_supported)

def test_classify():
    assert classify("a.PNG") == "image"
    assert classify("clip.MP4") == "video"
    with pytest.raises(ValueError):
        classify("notes.txt")

def test_is_supported():
    assert is_supported("a.jpeg")
    assert not is_supported("a.exe")

def test_media_item_roundtrip():
    item = MediaItem(id="x1", filename="a.png", type="image",
                     image_duration=10.0,
                     schedule=Schedule(days=[0, 1], start="08:00", end="17:00"))
    assert MediaItem.from_dict(item.to_dict()) == item

def test_settings_defaults():
    s = Settings.from_dict({})
    assert s.default_image_duration == 8.0 and s.muted is True and s.hwdec == "auto-copy"

def test_settings_roundtrip():
    s = Settings(default_image_duration=10.0, muted=False, hwdec="no")
    assert Settings.from_dict(s.to_dict()) == s
