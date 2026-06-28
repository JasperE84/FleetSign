import pytest
from fleetsign.config import AppConfig
from fleetsign.store import PlaylistStore
from fleetsign.sync import SyncClient
from fleetsign.web import create_slave_app


class RecordingController:
    def __init__(self):
        self.restarted = False
    def restart_playback(self): self.restarted = True
    def is_maintenance(self): return False
    def is_blank(self): return False


def _build_slave(tmp_path, *, with_password=True):
    config = AppConfig.load_or_create(tmp_path)
    config.join_master("192.168.1.50:8080", "tok")
    if with_password:
        config.set_password("pw")  # simulates the password synced from the master
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    sync = SyncClient(store, config, fetch=lambda u, t: b"{}")  # not started
    ctrl = RecordingController()
    called = []
    app = create_slave_app(store, config, ctrl, sync,
                           restarter=lambda: called.append(1))
    app.config.update(TESTING=True)
    app.sync_client = sync  # test-only handle so a test can inject sync state
    return app.test_client(), store, config, ctrl, called


@pytest.fixture
def slave(tmp_path):
    # A slave that has received its password from the master and is logged in.
    c, store, config, ctrl, called = _build_slave(tmp_path)
    c.post("/login", data={"password": "pw"})
    return c, store, config, ctrl, called


def test_slave_requires_login_when_configured(tmp_path):
    c, store, config, ctrl, called = _build_slave(tmp_path)  # NOT logged in
    assert c.get("/", follow_redirects=False).status_code == 302  # -> /login
    r = c.post("/become-master", follow_redirects=False)
    assert r.status_code == 302                      # gated, not executed
    assert config.is_slave() is True
    assert called == []


def test_slave_login_with_synced_password(tmp_path):
    c, store, config, ctrl, called = _build_slave(tmp_path)
    bad = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert bad.status_code == 200                    # re-renders login
    c.post("/login", data={"password": "pw"})
    assert c.get("/").status_code == 200             # now authorized


def test_slave_waiting_page_offers_recovery_but_hides_token(tmp_path):
    # Freshly joined, no password yet: a waiting page that still hides the sync
    # token, but offers recovery controls so a mis-joined slave isn't locked out.
    c, store, config, ctrl, called = _build_slave(tmp_path, with_password=False)
    config.set_sync_token("S3cret-unique-zzz")       # distinct from page words
    body = c.get("/").get_data(as_text=True)
    assert "wait" in body.lower()
    assert "S3cret-unique-zzz" not in body           # token value NOT rendered
    assert "Become master" in body                   # recovery now available


def test_slave_waiting_page_shows_connection_error(tmp_path):
    # Before the first sync the waiting page must surface WHY it can't connect,
    # so an operator sees refused/timeout/bad-auth instead of a blank "waiting".
    c, store, config, ctrl, called = _build_slave(tmp_path, with_password=False)
    c.application.sync_client.last_error = "HTTP Error 403: Forbidden"
    body = c.get("/").get_data(as_text=True)
    assert "token" in body.lower()                  # plain-language summary
    assert "HTTP Error 403: Forbidden" in body      # raw detail kept too


def test_slave_waiting_page_escapes_raw_error(tmp_path):
    # The raw error is master-influenced (e.g. a rejected filename echoed back),
    # so it must be HTML-escaped, never rendered as live markup.
    c, store, config, ctrl, called = _build_slave(tmp_path, with_password=False)
    c.application.sync_client.last_error = "manifest: bad name '<script>x</script>'"
    body = c.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


def test_slave_status_json_includes_friendly_error(slave):
    c, *_ = slave
    c.application.sync_client.last_error = "<urlopen error [Errno 111] Connection refused>"
    data = c.get("/status").get_json()
    assert "refused" in (data["last_error_friendly"] or "").lower()
    assert data["last_error"] == "<urlopen error [Errno 111] Connection refused>"


def test_slave_status_page_shows_friendly_error(slave):
    c, *_ = slave
    c.application.sync_client.last_error = "<urlopen error timed out>"
    body = c.get("/").get_data(as_text=True)
    assert "responding" in body.lower()   # friendly line on the status card


def test_slave_preconfig_become_master_works(tmp_path):
    # Pre-password recovery: the operator can demote this Pi to master from the
    # waiting page instead of needing SSH/SD-card access.
    c, store, config, ctrl, called = _build_slave(tmp_path, with_password=False)
    r = c.post("/become-master", follow_redirects=False)
    assert r.status_code == 200
    assert config.is_slave() is False
    assert called == [1]


def test_slave_preconfig_repoint_works(tmp_path):
    c, store, config, ctrl, called = _build_slave(tmp_path, with_password=False)
    c.post("/local/connection", data={"master_url": "10.0.0.9:8080",
                                       "sync_token": "t2"})
    assert config.master_url == "10.0.0.9:8080" and config.sync_token == "t2"
    assert called == []     # re-point takes effect next sync; no restart


def test_slave_index_shows_master(slave):
    c, *_ = slave
    body = c.get("/").get_data(as_text=True)
    assert "192.168.1.50:8080" in body


# A phrase that appears ONLY in the rendered mismatch banner -- not in the
# always-present status-bar JS (which references s.version_mismatch literally).
_MISMATCH_BANNER = "Update them to the same version"


def test_slave_shows_version_mismatch_banner(slave):
    # When the slave's code version differs from the master it last synced with,
    # the status page warns so a forgotten upgrade is visible on the screen.
    c, *_ = slave
    c.application.sync_client.master_version = "999.0.0"
    body = c.get("/").get_data(as_text=True)
    assert "999.0.0" in body
    assert _MISMATCH_BANNER in body


def test_slave_no_mismatch_banner_when_versions_match(slave):
    from fleetsign import __version__
    c, *_ = slave
    c.application.sync_client.master_version = __version__
    body = c.get("/").get_data(as_text=True)
    assert _MISMATCH_BANNER not in body


def test_slave_no_mismatch_banner_before_first_sync(slave):
    # master_version is None until a successful sync; absence of data must not be
    # rendered as a mismatch.
    c, *_ = slave
    assert c.application.sync_client.master_version is None
    body = c.get("/").get_data(as_text=True)
    assert _MISMATCH_BANNER not in body


def test_slave_status_json_includes_versions(slave):
    from fleetsign import __version__
    c, *_ = slave
    c.application.sync_client.master_version = "999.0.0"
    data = c.get("/status").get_json()
    assert data["app_version"] == __version__
    assert data["master_version"] == "999.0.0"
    assert data["version_mismatch"] is True


def test_slave_hwdec_change_restarts_playback(slave):
    c, store, _, ctrl, _ = slave
    c.post("/local/hwdec", data={"hwdec": "no"})
    assert store.get_settings().hwdec == "no"
    assert ctrl.restarted is True


def test_slave_repoint_no_restart(slave):
    c, _, config, _, called = slave
    c.post("/local/connection", data={"master_url": "10.0.0.9:8080",
                                       "sync_token": "t2"})
    assert config.master_url == "10.0.0.9:8080" and config.sync_token == "t2"
    assert called == []     # re-point takes effect next sync cycle; no restart


def test_slave_become_master_restarts(slave):
    c, _, config, _, called = slave
    r = c.post("/become-master")
    assert r.status_code == 200
    assert config.is_slave() is False
    assert called == [1]
