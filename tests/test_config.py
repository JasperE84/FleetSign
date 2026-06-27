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


def test_save_is_serialized_under_lock(tmp_path, monkeypatch):
    # On a slave the SyncClient thread (set_password_hash) writes config
    # concurrently with operator actions on Waitress workers. save() shares a
    # fixed config.json.tmp, so two unsynchronized writers garble that temp or
    # race os.replace (FileNotFoundError / a torn file going live). The fix
    # serializes save() under a lock. Probe os.replace to assert no two saves are
    # ever in the critical section at once -- deterministic on any platform,
    # unlike asserting on filesystem-race outcomes.
    import json
    import threading
    import time
    import fleetsign.config as configmod

    cfg = AppConfig.load_or_create(tmp_path)
    real_replace = configmod.os.replace
    gate = threading.Lock()
    active = 0
    max_active = 0

    def probe_replace(src, dst):
        nonlocal active, max_active
        with gate:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.005)  # widen the window so an unlocked overlap is caught
        with gate:
            active -= 1
        return real_replace(src, dst)

    monkeypatch.setattr(configmod.os, "replace", probe_replace)

    def hammer(tag):
        for i in range(5):
            cfg.set_password_hash(f"hash-{tag}-{i}")

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1, f"save() ran concurrently (peak {max_active})"
    # And the live file is always valid JSON, with no shared temp left behind.
    json.loads((tmp_path / "data" / "config.json").read_text("utf-8"))
    assert not (tmp_path / "data" / "config.json.tmp").exists()


def test_save_snapshots_state_under_lock(tmp_path, monkeypatch):
    # save() must serialize the JSON snapshot while holding _save_lock, not build
    # it beforehand. Snapshotting outside the lock leaves a window where another
    # thread's save() completes in the gap, after which this older snapshot's
    # os.replace overwrites the newer one (a lost update -- e.g. a synced password
    # silently reverts a concurrent become_master). Probe the serialization point
    # and assert the lock is already held: deterministic, unlike racing two saves.
    import fleetsign.config as configmod

    cfg = AppConfig.load_or_create(tmp_path)
    held_at_snapshot = []
    real_dumps = configmod.json.dumps

    def probe_dumps(*a, **k):
        held_at_snapshot.append(cfg._save_lock.locked())
        return real_dumps(*a, **k)

    monkeypatch.setattr(configmod.json, "dumps", probe_dumps)
    cfg.set_password_hash("h")

    assert held_at_snapshot and all(held_at_snapshot), \
        "snapshot serialized outside _save_lock -- lost-update window open"


def test_join_master_field_updates_are_atomic_under_lock(tmp_path):
    # join_master writes two fields (master_url, sync_token); they must mutate as
    # one critical section. Otherwise two concurrent config writes interleave and
    # persist a MIXED state -- master_url from one call, sync_token from another --
    # a real, non-self-healing corruption (not just a transient torn snapshot).
    # Proof via the lock: while _save_lock is held, join_master must not apply
    # EITHER field; before the fix the fields were assigned outside the lock.
    import threading
    import time

    cfg = AppConfig.load_or_create(tmp_path)
    before = (cfg.master_url, cfg.sync_token)  # master "" + an auto-gen token
    cfg._save_lock.acquire()
    done = threading.Event()

    def run():
        cfg.join_master("URL", "TOK")
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.05)  # let the thread reach join_master; it must block on the lock
    mid = (cfg.master_url, cfg.sync_token)
    cfg._save_lock.release()
    assert done.wait(2.0)

    assert mid == before, "join_master applied a field outside the lock"
    assert (cfg.master_url, cfg.sync_token) == ("URL", "TOK")
