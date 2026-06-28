import json
from fleetsign.store import PlaylistStore
from fleetsign.sync import (manifest_payload, FleetTracker, SyncClient,
                            SyncError, SyncResult, _base_url,
                            friendly_sync_error)


def make_store(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    return PlaylistStore(tmp_path / "manifest.json", media), media


def test_manifest_payload_shape(tmp_path):
    store, media = make_store(tmp_path)
    (media / "a.png").write_bytes(b"abc")
    store.add_media("a.png")

    p = manifest_payload(store)

    assert p["settings"] == {"default_image_duration": 20.0, "muted": True}
    assert "hwdec" not in p["settings"]
    assert p["media"][0]["filename"] == "a.png"
    assert p["files"]["a.png"]["size"] == 3
    assert isinstance(p["files"]["a.png"]["mtime"], float)
    # round-trips as JSON
    assert json.loads(json.dumps(p))["files"]["a.png"]["size"] == 3


def test_manifest_payload_omits_media_with_missing_file(tmp_path):
    # A master item whose file is gone must NOT be mirrored: it is dropped from
    # both `media` and `files`, so a slave prunes any stale local copy instead of
    # playing content the master can no longer serve.
    store, media = make_store(tmp_path)
    (media / "here.png").write_bytes(b"x")
    store.add_media("here.png")
    store.add_media("gone.png")  # manifest entry, but no file on disk

    p = manifest_payload(store)

    assert [m["filename"] for m in p["media"]] == ["here.png"]
    assert "here.png" in p["files"]
    assert "gone.png" not in p["files"]


def test_fleet_recent_window():
    f = FleetTracker()
    f.record("1.1.1.1", 1000.0)
    f.record("2.2.2.2", 1400.0)
    assert f.recent(1500.0, window=300.0) == ["2.2.2.2"]   # 1.1.1.1 is 500s old
    assert f.recent(1500.0, window=600.0) == ["1.1.1.1", "2.2.2.2"]


def test_fleet_record_updates_timestamp():
    f = FleetTracker()
    f.record("9.9.9.9", 1000.0)
    f.record("9.9.9.9", 1490.0)  # same ip, refreshed
    assert f.recent(1500.0, window=300.0) == ["9.9.9.9"]


def test_base_url_adds_scheme_and_default_port_when_missing():
    # Operators type a bare "192.168.1.50"; the master serves on 8080, so a
    # portless http URL would hit port 80 and silently never connect.
    assert _base_url("192.168.85.84") == "http://192.168.85.84:8080"
    assert _base_url("http://192.168.85.84") == "http://192.168.85.84:8080"
    assert _base_url("  192.168.85.84/  ") == "http://192.168.85.84:8080"


def test_base_url_preserves_explicit_port():
    assert _base_url("192.168.85.84:9000") == "http://192.168.85.84:9000"
    assert _base_url("http://192.168.85.84:8080") == "http://192.168.85.84:8080"
    assert _base_url("http://192.168.85.84:8080/") == "http://192.168.85.84:8080"


def test_base_url_leaves_https_alone():
    # Explicit TLS implies its own default port (443); don't force 8080 onto it.
    assert _base_url("https://master.example") == "https://master.example"


class FakeConfig:
    def __init__(self, master_url="http://m", token="t", password_hash=None):
        self.master_url = master_url
        self.sync_token = token
        self.password_hash = password_hash

    def set_password_hash(self, h):
        self.password_hash = h


def cfg(master_url="http://m", token="t"):
    return FakeConfig(master_url, token)


def make_fetch(payload: dict, files: dict, record=None):
    from urllib.parse import unquote
    body = json.dumps(payload).encode()

    def fetch(url, token, dest=None):
        if record is not None:
            record["token"] = token
        if url.endswith("/sync/manifest"):
            return body
        name = unquote(url.rsplit("/sync/media/", 1)[1])
        if name not in files:
            raise SyncError("404 " + name)
        data = files[name]
        if dest is not None:        # streaming download straight to disk
            dest.write_bytes(data)
            return None
        return data
    return fetch


def payload_for(media_items, files_meta, duration=9.0, muted=False):
    return {
        "settings": {"default_image_duration": duration, "muted": muted},
        "media": media_items,
        "files": files_meta,
    }


ITEM_A = {"id": "a1", "filename": "a.png", "type": "image",
          "enabled": True, "image_duration": None, "schedule": None}


def test_sync_once_downloads_applies_and_prunes(tmp_path):
    store, media = make_store(tmp_path)
    (media / "old.png").write_bytes(b"stale")  # must be pruned
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}),
                        clock=lambda: 123.0)

    res = client.sync_once()

    assert res.ok and res.downloaded == 1 and res.pruned == 1
    assert (media / "a.png").read_bytes() == b"abc"
    assert not (media / "old.png").exists()
    assert [m.filename for m in store.list_media()] == ["a.png"]
    assert store.get_settings().default_image_duration == 9.0
    assert store.get_settings().muted is False
    assert client.last_sync == 123.0 and client.last_error is None


def test_sync_preserves_local_hwdec(tmp_path):
    store, media = make_store(tmp_path)
    store.set_settings(8.0, True, "no")
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    client.sync_once()
    assert store.get_settings().hwdec == "no"


def test_sync_applies_master_password(tmp_path):
    # The master's UI password (hash) rides in the payload; the slave adopts it.
    store, media = make_store(tmp_path)
    config = cfg()  # password_hash is None
    p = payload_for([], {})
    p["password_hash"] = "hashed-from-master"
    client = SyncClient(store, config, fetch=make_fetch(p, {}))
    client.sync_once()
    assert config.password_hash == "hashed-from-master"


def test_sync_without_password_hash_keeps_existing(tmp_path):
    store, media = make_store(tmp_path)
    config = cfg()
    config.password_hash = "existing"
    p = payload_for([], {})  # no password_hash key in payload
    client = SyncClient(store, config, fetch=make_fetch(p, {}))
    client.sync_once()
    assert config.password_hash == "existing"  # unchanged


def test_sync_null_password_hash_keeps_existing(tmp_path):
    store, media = make_store(tmp_path)
    config = cfg()
    config.password_hash = "existing"
    p = payload_for([], {})
    p["password_hash"] = None  # explicit JSON null is ignored (not a str)
    client = SyncClient(store, config, fetch=make_fetch(p, {}))
    client.sync_once()
    assert config.password_hash == "existing"


def test_unchanged_file_not_redownloaded(tmp_path):
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    assert client.sync_once().downloaded == 1
    assert client.sync_once().downloaded == 0  # size+mtime match -> skip


def test_download_failure_keeps_prior_state(tmp_path):
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {}))  # media 404s
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []         # not applied
    assert not (media / "a.png").exists()   # no partial file
    assert client.last_error is not None


def test_malformed_manifest_skips(tmp_path):
    store, media = make_store(tmp_path)

    def fetch(url, token):
        return b"{ not json"
    client = SyncClient(store, cfg(), fetch=fetch)
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []


def test_token_is_passed_to_fetch(tmp_path):
    store, media = make_store(tmp_path)
    rec = {}
    p = payload_for([], {})
    client = SyncClient(store, cfg(token="secret"),
                        fetch=make_fetch(p, {}, record=rec))
    client.sync_once()
    assert rec["token"] == "secret"


def test_incomplete_settings_skips(tmp_path):
    store, media = make_store(tmp_path)
    p = {
        "settings": {},  # missing default_image_duration and muted
        "media": [ITEM_A],
        "files": {"a.png": {"size": 3, "mtime": 1000.0}},
    }
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []  # store not mutated


def test_malformed_file_meta_skips(tmp_path):
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {}})  # meta present but missing size/mtime
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []  # store not mutated
    assert not (media / "a.png").exists()
    assert not (media / "a.png.tmp").exists()


def test_token_passed_to_media_fetch(tmp_path):
    store, media = make_store(tmp_path)
    calls = []

    def fetch(url, token, dest=None):
        calls.append((url, token))
        if url.endswith("/sync/manifest"):
            return json.dumps(
                payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
            ).encode()
        if dest is not None:
            dest.write_bytes(b"abc")
            return None
        return b"abc"

    client = SyncClient(store, cfg(token="secret"), fetch=fetch)
    client.sync_once()
    media_calls = [(u, t) for u, t in calls if "/sync/media/" in u]
    assert len(media_calls) == 1
    assert media_calls[0][1] == "secret"


def test_run_loops_then_stops(tmp_path):
    store, media = make_store(tmp_path)
    client = SyncClient(store, cfg(), fetch=lambda u, t: b"{}")
    calls = []

    def fake_once():
        calls.append(1)
        client.stop()           # stop after the first iteration
        return SyncResult(ok=True)

    client.sync_once = fake_once          # type: ignore[assignment]
    client._rng = lambda a, b: 0.0        # no real wait
    client._run()                          # returns once stop is set

    assert calls == [1]


def test_run_uses_short_backoff_on_failure(tmp_path):
    store, media = make_store(tmp_path)
    client = SyncClient(store, cfg(), fetch=lambda u, t: b"{}")
    waits = []
    client._stop.wait = lambda d: (waits.append(d), client._stop.set())[1] and None

    def fake_once():
        return SyncResult(ok=False, error="x")

    client.sync_once = fake_once          # type: ignore[assignment]
    client._run()

    assert waits and waits[0] == 15.0     # short retry backoff on failure


def test_bad_duration_skips_and_keeps_state(tmp_path):
    store, media = make_store(tmp_path)
    for bad_duration in (0, "x"):
        p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}},
                        duration=bad_duration)
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()
        assert res.ok is False
        assert store.list_media() == []  # store not mutated
        # must not raise


def test_unsafe_filename_is_rejected(tmp_path):
    # A master (or a MITM on the unauthenticated channel) must not be able to
    # steer a write outside media/ via a "../" filename in the manifest.
    store, media = make_store(tmp_path)
    evil = {"id": "x1", "filename": "../escaped.png", "type": "image",
            "enabled": True, "image_duration": None, "schedule": None}
    p = payload_for([evil], {"../escaped.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(),
                        fetch=make_fetch(p, {"../escaped.png": b"abc"}))

    res = client.sync_once()

    assert res.ok is False
    assert store.list_media() == []                       # store not mutated
    assert not (media.parent / "escaped.png").exists()    # nothing written outside
    assert not (media.parent / "escaped.png.tmp").exists()


def test_absolute_filename_is_rejected(tmp_path):
    store, media = make_store(tmp_path)
    bad = {"id": "x1", "filename": "/tmp/pwned.png", "type": "image",
           "enabled": True, "image_duration": None, "schedule": None}
    p = payload_for([bad], {"/tmp/pwned.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"/tmp/pwned.png": b"abc"}))
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []


def test_media_item_without_file_meta_fails_whole_manifest(tmp_path):
    # An item listed in `media` but absent from `files` must fail the sync,
    # not be silently applied (which would add a fileless playlist entry and
    # shield a stale local copy from pruning).
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {})  # a.png in media, but no files entry
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))

    res = client.sync_once()

    assert res.ok is False
    assert store.list_media() == []  # store not mutated


def test_non_numeric_file_meta_fails(tmp_path):
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": "3", "mtime": "soon"}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []
    assert not (media / "a.png.tmp").exists()  # never reached the download/utime


def test_unchanged_sync_does_not_rewrite_manifest(tmp_path):
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    client.sync_once()  # first sync applies

    calls = []
    orig = store.replace_from_master
    store.replace_from_master = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    res = client.sync_once()  # identical payload

    assert res.ok and res.downloaded == 0
    assert calls == []  # nothing changed -> no SD-card write


def test_orphan_tmp_file_is_pruned(tmp_path):
    store, media = make_store(tmp_path)
    (media / "leftover.png.tmp").write_bytes(b"partial")  # crashed earlier download
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))

    res = client.sync_once()

    assert res.ok
    assert not (media / "leftover.png.tmp").exists()  # stale .tmp cleaned up


def test_fleet_evicts_entries_older_than_an_hour():
    f = FleetTracker()
    f.record("1.1.1.1", 0.0)
    f.record("2.2.2.2", 4000.0)  # >1h later: 1.1.1.1 should be evicted
    # Even with an enormous window, the long-stale IP is gone from the dict.
    assert f.recent(4000.0, window=1e9) == ["2.2.2.2"]


def test_truncated_download_is_rejected(tmp_path):
    # The manifest declares 3 bytes but the fetch delivers only 1: a partial or
    # corrupt download must NOT be made live. The bad content stays out of media/
    # and the store is untouched, so the player never shows truncated media.
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"x"}))  # 1 byte
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []            # store not mutated
    assert not (media / "a.png").exists()      # truncated file not made live
    assert not (media / "a.png.tmp").exists()  # partial cleaned up
    assert client.last_error is not None


def test_non_dict_media_entry_fails(tmp_path):
    # A media entry that isn't an object, or one carrying a non-dict schedule,
    # must fail the sync gracefully (SyncResult ok=False) rather than raise out
    # of sync_once and let the player loop's catch-all swallow it as a crash.
    store, media = make_store(tmp_path)
    bad_sched = {"id": "x1", "filename": "a.png", "type": "image",
                 "enabled": True, "image_duration": None, "schedule": "not-a-dict"}
    for media_list in (["not-a-dict"], [bad_sched]):
        p = payload_for(media_list, {"a.png": {"size": 3, "mtime": 1000.0}})
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()               # must not raise
        assert res.ok is False
        assert store.list_media() == []        # store not mutated


def test_bad_media_type_fails_whole_manifest(tmp_path):
    # The threat model treats the master's manifest as hostile (token-auth, no
    # TLS). A tampered/buggy `type` is the dangerous field: _play_item only sets
    # image-display-duration when type == "image", so an actual image arriving
    # with any other type gets mpv's default --image-display-duration=inf and
    # never advances -- the playlist stalls with no end-file. Reject the whole
    # manifest rather than persist an item the player can't advance past.
    store, media = make_store(tmp_path)
    for bad_type in ("evil", "", "Image", 5, None):
        item = dict(ITEM_A, type=bad_type)
        p = payload_for([item], {"a.png": {"size": 3, "mtime": 1000.0}})
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()
        assert res.ok is False, f"bad type {bad_type!r} was accepted"
        assert store.list_media() == []  # store not mutated


def test_bad_media_fields_fail_whole_manifest(tmp_path):
    store, media = make_store(tmp_path)
    bad_items = [
        dict(ITEM_A, enabled="yes"),              # non-bool
        dict(ITEM_A, image_duration="soon"),      # non-numeric
        dict(ITEM_A, image_duration=0),           # not strictly positive
        dict(ITEM_A, image_duration=-4),          # negative
        dict(ITEM_A, image_duration=float("inf")),  # non-finite
        dict(ITEM_A, id=""),                      # empty id
        dict(ITEM_A, id=7),                       # non-string id
    ]
    for item in bad_items:
        p = payload_for([item], {"a.png": {"size": 3, "mtime": 1000.0}})
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()
        assert res.ok is False, f"bad item accepted: {item!r}"
        assert store.list_media() == []


def test_type_mismatching_filename_fails_whole_manifest(tmp_path):
    # `type` is one of the allowed strings but contradicts the file extension.
    # _play_item only sets image-display-duration when type == "image", so an
    # actual image arriving tagged "video" gets mpv's default duration and never
    # advances -- the playlist stalls with no end-file. A type that is merely a
    # valid string isn't enough: it must match what the filename classifies as.
    store, media = make_store(tmp_path)
    mismatches = [
        dict(ITEM_A, filename="a.png", type="video"),   # image file tagged video
        {"id": "v1", "filename": "clip.mp4", "type": "image",  # video file tagged image
         "enabled": True, "image_duration": None, "schedule": None},
    ]
    for item in mismatches:
        fname = item["filename"]
        p = payload_for([item], {fname: {"size": 3, "mtime": 1000.0}})
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {fname: b"abc"}))
        res = client.sync_once()
        assert res.ok is False, f"type/filename mismatch accepted: {item!r}"
        assert store.list_media() == []  # store not mutated
        assert not (media / fname).exists()  # never made live


def test_unsupported_extension_fails_whole_manifest(tmp_path):
    # An unclassifiable extension can't be a valid image OR video, so no honest
    # `type` exists for it. Reject rather than download a file the player can't
    # meaningfully play (a name-safe basename alone isn't proof of a usable type).
    store, media = make_store(tmp_path)
    item = dict(ITEM_A, filename="a.txt", type="image")
    p = payload_for([item], {"a.txt": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.txt": b"abc"}))
    res = client.sync_once()
    assert res.ok is False
    assert store.list_media() == []
    assert not (media / "a.txt").exists()


def test_good_media_with_duration_and_video_still_syncs(tmp_path):
    # Guard against over-validation: legitimate items (a video, an image with a
    # real duration) must still pass.
    store, media = make_store(tmp_path)
    good = [
        dict(ITEM_A, type="image", image_duration=12.0),
        {"id": "v1", "filename": "clip.mp4", "type": "video",
         "enabled": False, "image_duration": None, "schedule": None},
    ]
    p = payload_for(good, {"a.png": {"size": 3, "mtime": 1000.0},
                           "clip.mp4": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(),
                        fetch=make_fetch(p, {"a.png": b"abc", "clip.mp4": b"xyz"}))
    res = client.sync_once()
    assert res.ok is True
    assert [m.filename for m in store.list_media()] == ["a.png", "clip.mp4"]


def test_poisoned_mtime_fails_cleanly(tmp_path):
    # A finite but absurd mtime passes the numeric check yet makes os.utime raise
    # (OverflowError: out of range for time_t). That must fail the sync cleanly --
    # drop the .tmp, leave the store untouched -- not propagate a raw exception and
    # leave an un-pruned .tmp the next run keeps re-creating.
    store, media = make_store(tmp_path)
    p = payload_for([ITEM_A], {"a.png": {"size": 3, "mtime": 1e19}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    res = client.sync_once()  # must not raise
    assert res.ok is False
    assert store.list_media() == []
    assert not (media / "a.png").exists()       # not made live
    assert not (media / "a.png.tmp").exists()   # partial cleaned up
    assert client.last_error is not None


def test_run_survives_sync_once_raising(tmp_path):
    # Regression guard for the "never let the loop die" promise: a sync_once that
    # RAISES (not just returns ok=False) must be swallowed, recorded, and retried
    # on the short backoff.
    store, media = make_store(tmp_path)
    client = SyncClient(store, cfg(), fetch=lambda u, t: b"{}")
    waits = []
    client._stop.wait = lambda d: (waits.append(d), client._stop.set())[1] and None

    def boom():
        raise RuntimeError("kaboom")
    client.sync_once = boom               # type: ignore[assignment]
    client._run()                          # must not propagate

    assert waits == [15.0]
    assert client.last_error == "kaboom"


def test_run_survives_rng_raising(tmp_path):
    # The guarantee must be structural, not merely a property of the injected
    # callables: even if the delay computation itself raises, the loop recovers on
    # the short backoff rather than letting the daemon's sync thread die silently.
    store, media = make_store(tmp_path)
    client = SyncClient(store, cfg(), fetch=lambda u, t: b"{}")
    waits = []
    client._stop.wait = lambda d: (waits.append(d), client._stop.set())[1] and None
    client.sync_once = lambda: SyncResult(ok=True)   # success path

    def boom(a, b):
        raise RuntimeError("rng down")
    client._rng = boom                     # type: ignore[assignment]
    client._run()                          # must not propagate

    assert waits == [15.0]


def test_bad_schedule_fails_whole_manifest(tmp_path):
    # The web route validates schedules (weekday ints 0-6, HH:MM times); the sync
    # boundary -- the more hostile input -- must match it. A garbage schedule that
    # from_dict happens to accept (out-of-range days, unparseable/out-of-range
    # times) must reject the WHOLE manifest, not be persisted as an item that
    # silently goes permanently dark.
    store, media = make_store(tmp_path)
    bad_schedules = [
        {"days": [0, 9], "start": "08:00", "end": "17:00"},   # weekday out of range
        {"days": ["mon"], "start": "08:00", "end": "17:00"},  # non-int day
        {"days": [0], "start": "8 oclock", "end": "17:00"},   # unparseable start
        {"days": [0], "start": "08:00", "end": "25:00"},      # out-of-range time
        {"days": "0", "start": "08:00", "end": "17:00"},      # days not a list of ints
    ]
    for sched in bad_schedules:
        item = dict(ITEM_A, schedule=sched)
        p = payload_for([item], {"a.png": {"size": 3, "mtime": 1000.0}})
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()
        assert res.ok is False, f"bad schedule accepted: {sched!r}"
        assert store.list_media() == []  # store not mutated


def test_valid_schedule_still_syncs(tmp_path):
    # Guard against over-validation: a legitimate schedule must still pass.
    store, media = make_store(tmp_path)
    item = dict(ITEM_A, schedule={"days": [0, 4], "start": "08:00", "end": "17:00"})
    p = payload_for([item], {"a.png": {"size": 3, "mtime": 1000.0}})
    client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
    res = client.sync_once()
    assert res.ok is True
    s = store.list_media()[0].schedule
    assert s.days == [0, 4] and s.start == "08:00" and s.end == "17:00"


def test_friendly_sync_error_classifies_common_cases():
    # The raw last_error is a Python exception string; the slave UI shows a
    # plain-language summary so an operator can tell refused/timeout/bad-auth
    # apart without reading errno strings.
    assert friendly_sync_error(None) is None
    assert friendly_sync_error("") is None
    assert friendly_sync_error("some unrecognised failure xyz") is None
    assert "refused" in friendly_sync_error(
        "<urlopen error [Errno 111] Connection refused>").lower()
    assert "responding" in friendly_sync_error(
        "<urlopen error timed out>").lower()
    assert "token" in friendly_sync_error("HTTP Error 403: Forbidden").lower()
    assert "resolve" in friendly_sync_error(
        "<urlopen error [Errno -2] Name or service not known>").lower()
    assert "network" in friendly_sync_error(
        "<urlopen error [Errno 113] No route to host>").lower()
    assert "rejected" in friendly_sync_error(
        "manifest: bad manifest shape").lower()


def test_last_attempt_recorded_on_success_and_failure(tmp_path):
    # last_sync is "last success"; last_attempt is "last time we tried at all",
    # so the waiting page (never synced) can still show when it last attempted.
    store, media = make_store(tmp_path)
    ok = SyncClient(store, cfg(), fetch=make_fetch(payload_for([], {}), {}),
                    clock=lambda: 500.0)
    ok.sync_once()
    assert ok.last_attempt == 500.0 and ok.last_sync == 500.0

    bad = SyncClient(store, cfg(), fetch=lambda u, t: b"{ not json",
                     clock=lambda: 700.0)
    bad.sync_once()
    assert bad.last_attempt == 700.0   # recorded even though the attempt failed
    assert bad.last_sync is None       # failure did not advance last_sync


def test_non_dict_settings_skips(tmp_path):
    store, media = make_store(tmp_path)
    for bad_settings in (None, ["default_image_duration", 8]):
        p = {
            "settings": bad_settings,
            "media": [ITEM_A],
            "files": {"a.png": {"size": 3, "mtime": 1000.0}},
        }
        client = SyncClient(store, cfg(), fetch=make_fetch(p, {"a.png": b"abc"}))
        res = client.sync_once()
        assert res.ok is False
        assert store.list_media() == []  # store not mutated
        # must not raise
