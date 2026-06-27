from fleetsign.config import AppConfig

def test_first_run_unconfigured(tmp_path):
    cfg = AppConfig.load_or_create(tmp_path)
    assert cfg.is_configured() is False
    assert (tmp_path / "media").is_dir()
    assert (tmp_path / "data" / "config.json").exists()

def test_set_and_check_password_persists(tmp_path):
    AppConfig.load_or_create(tmp_path).set_password("secret")
    reloaded = AppConfig.load_or_create(tmp_path)
    assert reloaded.is_configured() is True
    assert reloaded.check_password("secret") is True
    assert reloaded.check_password("wrong") is False

def test_session_secret_stable(tmp_path):
    a = AppConfig.load_or_create(tmp_path).session_secret
    b = AppConfig.load_or_create(tmp_path).session_secret
    assert a == b and len(a) >= 32

def test_malformed_config_recovers(tmp_path):
    import json
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "config.json").write_text("{ not valid json", "utf-8")
    cfg = AppConfig.load_or_create(tmp_path)
    assert cfg.is_configured() is False
    assert cfg.session_secret  # regenerated
    json.loads((data / "config.json").read_text("utf-8"))  # file is valid JSON again

def test_config_json_omits_dead_dir_fields(tmp_path):
    import json
    AppConfig.load_or_create(tmp_path)
    d = json.loads((tmp_path / "data" / "config.json").read_text("utf-8"))
    assert "media_dir" not in d and "data_dir" not in d


def test_fresh_config_is_master_with_generated_token(tmp_path):
    cfg = AppConfig.load_or_create(tmp_path)
    assert cfg.is_slave() is False
    assert cfg.master_url == ""
    assert len(cfg.sync_token) >= 16  # auto-generated


def test_token_persists_across_reload(tmp_path):
    cfg = AppConfig.load_or_create(tmp_path)
    token = cfg.sync_token
    reloaded = AppConfig.load_or_create(tmp_path)
    assert reloaded.sync_token == token


def test_join_master_makes_slave_and_persists(tmp_path):
    cfg = AppConfig.load_or_create(tmp_path)
    cfg.join_master("192.168.1.50:8080", "tok")
    assert cfg.is_slave() is True
    reloaded = AppConfig.load_or_create(tmp_path)
    assert reloaded.master_url == "192.168.1.50:8080"
    assert reloaded.sync_token == "tok"


def test_become_master_clears_url(tmp_path):
    cfg = AppConfig.load_or_create(tmp_path)
    cfg.join_master("192.168.1.50:8080", "tok")
    cfg.become_master()
    assert cfg.is_slave() is False
    assert AppConfig.load_or_create(tmp_path).master_url == ""
