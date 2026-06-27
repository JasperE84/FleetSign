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


@pytest.fixture
def master(tmp_path):
    config = AppConfig.load_or_create(tmp_path)
    config.set_password("pw")
    config.set_sync_token("secret")
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    (config.media_dir / "a.png").write_bytes(b"abc")
    store.add_media("a.png")
    app = create_app(store, config, StubController())
    app.config.update(TESTING=True)
    return app.test_client(), config


def test_manifest_requires_token(master):
    c, _ = master
    assert c.get("/sync/manifest").status_code == 403
    r = c.get("/sync/manifest", headers={"X-Sync-Token": "secret"})
    assert r.status_code == 200
    assert r.get_json()["media"][0]["filename"] == "a.png"
    assert r.get_json().get("password_hash") is not None  # UI password synced


def test_manifest_rejects_wrong_token(master):
    c, _ = master
    assert c.get("/sync/manifest", headers={"X-Sync-Token": "nope"}).status_code == 403


def test_media_served_with_token(master):
    c, _ = master
    assert c.get("/sync/media/a.png").status_code == 403
    r = c.get("/sync/media/a.png", headers={"X-Sync-Token": "secret"})
    assert r.status_code == 200 and r.data == b"abc"
