import pytest
from fleetsign.config import AppConfig
from fleetsign.store import PlaylistStore
from fleetsign.web import create_app

class RecordingController:
    def __init__(self):
        self.restarted = False; self.blank = None; self.maint = False
    def restart_playback(self): self.restarted = True
    def set_blank(self, blank): self.blank = blank
    def set_maintenance(self, on): self.maint = on
    def is_maintenance(self): return self.maint
    def is_blank(self): return bool(self.blank)

@pytest.fixture
def ctx(tmp_path):
    config = AppConfig.load_or_create(tmp_path)
    config.set_password("pw")
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    ctrl = RecordingController()
    app = create_app(store, config, ctrl)
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/login", data={"password": "pw"})
    return c, store, config, ctrl

def test_settings_update(ctx):
    c, store, _, _ = ctx
    c.post("/settings", data={"default_image_duration": "10", "muted": "1"})
    s = store.get_settings()
    assert s.default_image_duration == 10.0 and s.muted is True

def test_controls_call_controller(ctx):
    c, _, _, ctrl = ctx
    c.post("/control/restart-playback")
    c.post("/control/blank", data={"blank": "1"})
    assert ctrl.restarted is True and ctrl.blank is True

def test_password_change(ctx):
    c, _, config, _ = ctx
    c.post("/password", data={"password": "newpass"})
    assert config.check_password("newpass")

def test_status_reports_time_and_state(ctx):
    c, _, _, _ = ctx
    data = c.get("/status").get_json()
    assert "now" in data and "weekday" in data and "tz" in data
    assert data["clock_ok"] is True  # test runs in 2026
    assert data["maintenance"] is False

def test_maintenance_toggle_calls_controller(ctx):
    c, _, _, ctrl = ctx
    c.post("/control/maintenance", data={"on": "1"})
    assert ctrl.maint is True
    c.post("/control/maintenance", data={"on": "0"})
    assert ctrl.maint is False

def test_bad_settings_duration_is_rejected_not_500(ctx):
    c, store, _, _ = ctx
    r = c.post("/settings", data={"default_image_duration": "abc"}, follow_redirects=False)
    assert r.status_code == 302  # flashed + redirect, not a 500
    assert store.get_settings().default_image_duration == 20.0  # unchanged default

def test_nonpositive_settings_duration_is_rejected(ctx):
    c, store, _, _ = ctx
    for bad in ("0", "-1", "inf", "nan"):
        r = c.post("/settings", data={"default_image_duration": bad}, follow_redirects=False)
        assert r.status_code == 302
        assert store.get_settings().default_image_duration == 20.0  # unchanged

def test_settings_hwdec_change_restarts(ctx):
    c, store, _, ctrl = ctx
    c.post("/settings", data={"default_image_duration": "8", "muted": "1", "hwdec": "no"})
    assert store.get_settings().hwdec == "no"
    assert ctrl.restarted is True  # decoder change relaunched mpv
