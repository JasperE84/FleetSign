import logging

import pytest
from fleetsign.config import AppConfig
from fleetsign.store import PlaylistStore
from fleetsign.web import create_app

class StubController:
    def restart_playback(self): pass
    def set_blank(self, blank): pass

@pytest.fixture
def client(tmp_path):
    config = AppConfig.load_or_create(tmp_path)
    store = PlaylistStore(config.data_dir / "manifest.json", config.media_dir)
    app = create_app(store, config, StubController())
    app.config.update(TESTING=True)
    return app.test_client(), config

def test_unconfigured_redirects_to_setup(client):
    c, _ = client
    resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 302 and "/setup" in resp.headers["Location"]

def test_setup_sets_password_and_logs_in(client):
    c, config = client
    resp = c.post("/setup", data={"password": "hunter2"}, follow_redirects=False)
    assert resp.status_code == 302
    assert config.is_configured()

def test_login_required_after_setup(client):
    c, config = client
    config.set_password("hunter2")
    assert c.get("/", follow_redirects=False).status_code == 302  # -> /login
    bad = c.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert bad.status_code == 200  # re-renders login
    ok = c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    assert ok.status_code == 302  # -> index
    assert c.get("/").status_code == 200

def test_setup_join_mode_hides_password_field(client):
    # A slave gets its admin password from the master on first sync, so the join
    # form must NOT ask for one. The password input lives in a master-only block
    # the "join" radio hides, leaving just master URL + token.
    c, _ = client
    body = c.get("/setup").get_data(as_text=True)
    assert 'id="masterfields"' in body                  # wrapper around password
    assert "getElementById('masterfields')" in body     # join radio toggles it


def test_upload_limit_allows_large_videos(client):
    c, _ = client
    from fleetsign.web import MAX_UPLOAD_BYTES
    # videos well over 250 MB must not be rejected with 413
    assert MAX_UPLOAD_BYTES >= 250 * 1024 * 1024
    assert c.application.config["MAX_CONTENT_LENGTH"] == MAX_UPLOAD_BYTES

def test_login_logs_success_and_failure(client, caplog):
    c, config = client
    config.set_password("hunter2")
    with caplog.at_level(logging.INFO, logger="fleetsign.web"):
        c.post("/login", data={"password": "nope"})
        c.post("/login", data={"password": "hunter2"})
    assert any(r.levelno == logging.WARNING and "failed login" in r.getMessage()
               for r in caplog.records)
    assert any(r.levelno == logging.INFO and "login ok" in r.getMessage()
               for r in caplog.records)


def test_templates_and_static_are_packaged():
    # Guards that the package-data globs in pyproject keep shipping the templates
    # and CSS that render_template / url_for('static') need at runtime — without
    # them a non-editable wheel install renders nothing.
    import pathlib
    import fleetsign
    pkg = pathlib.Path(fleetsign.__file__).parent
    for tmpl in ("index.html", "login.html", "setup.html", "slave_status.html",
                 "slave_waiting.html"):
        assert (pkg / "templates" / tmpl).exists()
    assert (pkg / "static" / "style.css").exists()
