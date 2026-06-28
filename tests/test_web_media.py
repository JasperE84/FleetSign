import io
import pytest
from fleetsign.config import AppConfig
from fleetsign.store import PlaylistStore
from fleetsign.web import create_app

class StubController:
    def restart_playback(self): pass
    def set_blank(self, blank): pass

@pytest.fixture
def ctx(tmp_path):
    config = AppConfig.load_or_create(tmp_path)
    config.set_password("pw")
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    app = create_app(store, config, StubController())
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/login", data={"password": "pw"})
    return c, store

def test_upload_accepts_image_rejects_other(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "pic.png")},
           content_type="multipart/form-data")
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "notes.txt")},
           content_type="multipart/form-data")
    names = [m.filename for m in store.list_media()]
    assert "pic.png" in names and not any(n.endswith(".txt") for n in names)

def test_upload_plain_post_redirects(ctx):
    c, _ = ctx
    r = c.post("/upload", data={"files": (io.BytesIO(b"x"), "pic.png")},
               content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302

def test_upload_xhr_returns_204_and_adds_media(ctx):
    c, store = ctx
    r = c.post("/upload", data={"files": (io.BytesIO(b"x"), "pic.png")},
               content_type="multipart/form-data",
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 204
    assert "pic.png" in [m.filename for m in store.list_media()]

def test_enable_duration_schedule_delete(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "a.png")},
           content_type="multipart/form-data")
    item = store.list_media()[0]
    c.post(f"/media/{item.id}/enable", data={"enabled": "0"})
    assert store.list_media()[0].enabled is False
    c.post(f"/media/{item.id}/duration", data={"duration": "15"})
    assert store.list_media()[0].image_duration == 15.0
    c.post(f"/media/{item.id}/schedule",
           data={"scheduled": "1", "days": ["0", "4"], "start": "08:00", "end": "18:00"})
    assert store.list_media()[0].schedule.days == [0, 4]
    c.post(f"/media/{item.id}/delete")
    assert store.list_media() == []

def test_index_lists_media(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "shown.png")},
           content_type="multipart/form-data")
    assert b"shown.png" in c.get("/").data

def test_bad_duration_is_rejected_not_500(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "a.png")},
           content_type="multipart/form-data")
    item = store.list_media()[0]
    r = c.post(f"/media/{item.id}/duration", data={"duration": "abc"}, follow_redirects=False)
    assert r.status_code == 302  # flashed + redirect, not a 500
    assert store.list_media()[0].image_duration is None

def test_bad_schedule_time_is_rejected(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "a.png")},
           content_type="multipart/form-data")
    item = store.list_media()[0]
    r = c.post(f"/media/{item.id}/schedule",
               data={"scheduled": "1", "days": ["0"], "start": "notatime", "end": "18:00"},
               follow_redirects=False)
    assert r.status_code == 302
    assert store.list_media()[0].schedule is None  # rejected, not persisted

def test_nonpositive_or_nonfinite_duration_is_rejected(ctx):
    c, store = ctx
    c.post("/upload", data={"files": (io.BytesIO(b"x"), "a.png")},
           content_type="multipart/form-data")
    item = store.list_media()[0]
    for bad in ("0", "-5", "inf", "nan"):
        r = c.post(f"/media/{item.id}/duration", data={"duration": bad}, follow_redirects=False)
        assert r.status_code == 302
        assert store.list_media()[0].image_duration is None  # rejected, not persisted
