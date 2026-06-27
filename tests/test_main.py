from fleetsign.__main__ import build
from fleetsign.config import AppConfig


def test_build_wires_components(tmp_path):
    app, controller, config, sync = build(tmp_path, "127.0.0.1", 9000)
    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert app is not None and controller is not None
    assert controller.socket_path.endswith("mpv.sock")
    assert sync is None  # master


def test_serve_kwargs_allow_large_uploads(tmp_path):
    from fleetsign.__main__ import serve_kwargs
    _, _, config, _ = build(tmp_path, "127.0.0.1", 9000)
    kw = serve_kwargs(config)
    assert kw["max_request_body_size"] >= 250 * 1024 * 1024
    assert kw["channel_timeout"] >= 300
    assert kw["host"] == "127.0.0.1" and kw["port"] == 9000


def test_build_slave_creates_sync_client(tmp_path):
    AppConfig.load_or_create(tmp_path).join_master("192.168.1.50:8080", "tok")
    app, controller, config, sync = build(tmp_path, "127.0.0.1", 9000)
    assert config.is_slave() is True
    assert sync is not None
