import pytest
from fleetsign.config import AppConfig
from fleetsign.store import PlaylistStore
from fleetsign.web import create_app


class StubController:
    def restart_playback(self): pass
    def set_blank(self, blank): pass
    def set_maintenance(self, on): pass
    def is_maintenance(self): return False
    def is_blank(self): return False


def make_app(tmp_path, restarter=None, configured=True):
    config = AppConfig.load_or_create(tmp_path)
    if configured:
        config.set_password("pw")
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    app = create_app(store, config, StubController(), restarter=restarter)
    app.config.update(TESTING=True)
    return app.test_client(), config, store


def test_index_shows_token_and_panel(tmp_path):
    c, config, _ = make_app(tmp_path)
    c.post("/login", data={"password": "pw"})
    body = c.get("/").get_data(as_text=True)
    assert config.sync_token in body          # token displayed for slave setup
    assert "Connected screens" in body         # panel present


def test_panel_lists_recent_screen_ip(tmp_path):
    c, config, _ = make_app(tmp_path)
    c.get("/sync/manifest", headers={"X-Sync-Token": config.sync_token})
    c.post("/login", data={"password": "pw"})
    assert "127.0.0.1" in c.get("/").get_data(as_text=True)  # recorded by /sync/*


def test_panel_shows_screen_version(tmp_path):
    # A slave reports its version via X-Sync-Version; the panel surfaces it so the
    # operator can spot a screen that was missed during an upgrade.
    c, config, _ = make_app(tmp_path)
    c.get("/sync/manifest", headers={"X-Sync-Token": config.sync_token,
                                     "X-Sync-Version": "0.9.9"})
    c.post("/login", data={"password": "pw"})
    assert "0.9.9" in c.get("/").get_data(as_text=True)


def test_panel_shows_master_version(tmp_path):
    from fleetsign import __version__
    c, config, _ = make_app(tmp_path)
    c.post("/login", data={"password": "pw"})
    assert f"v{__version__}" in c.get("/").get_data(as_text=True)


def test_sync_token_update(tmp_path):
    c, config, _ = make_app(tmp_path)
    c.post("/login", data={"password": "pw"})
    c.post("/sync-token", data={"sync_token": "rotated123"})
    assert config.sync_token == "rotated123"


def test_setup_join_makes_slave_and_restarts(tmp_path):
    called = []
    c, config, _ = make_app(tmp_path, restarter=lambda: called.append(1),
                            configured=False)
    r = c.post("/setup", data={"mode": "join",
                               "master_url": "192.168.1.50:8080",
                               "sync_token": "tok"},
               follow_redirects=False)
    assert r.status_code == 200
    assert config.is_slave() is True
    assert config.master_url == "192.168.1.50:8080"
    assert config.sync_token == "tok"
    assert called == [1]
    # The response is an interstitial that sends the browser to the screen page
    # ("/") once the device has restarted, rather than a plain text dead-end.
    body = r.get_data(as_text=True)
    assert "text/html" in r.headers["Content-Type"]
    assert 'href="/"' in body
    assert "screen" in body.lower()


def test_setup_master_path_still_sets_password(tmp_path):
    c, config, _ = make_app(tmp_path, configured=False)
    r = c.post("/setup", data={"mode": "master", "password": "hunter2"},
               follow_redirects=False)
    assert r.status_code == 302
    assert config.is_configured() and config.is_slave() is False


def test_join_master_route_restarts(tmp_path):
    called = []
    c, config, _ = make_app(tmp_path, restarter=lambda: called.append(1))
    c.post("/login", data={"password": "pw"})
    r = c.post("/join-master", data={"master_url": "10.0.0.5:8080",
                                     "sync_token": "tok"})
    assert config.is_slave() is True and called == [1]
    body = r.get_data(as_text=True)
    assert 'href="/"' in body and "screen" in body.lower()


def test_fresh_slave_login_does_not_loop(tmp_path):
    c, config, _ = make_app(tmp_path, configured=False)
    config.join_master("192.168.1.50:8080", "tok")  # slave, no password
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 200                       # info page, not a redirect
    assert "screen" in r.get_data(as_text=True).lower()
