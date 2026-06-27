import json
from fleetsign.store import PlaylistStore
from fleetsign.model import MediaItem

def make_store(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    return PlaylistStore(tmp_path / "manifest.json", media), media

def test_add_classifies_and_persists(tmp_path):
    store, media = make_store(tmp_path)
    (media / "a.png").write_bytes(b"x")
    item = store.add_media("a.png")
    assert item.type == "image"
    reloaded = PlaylistStore(tmp_path / "manifest.json", media)
    assert [m.filename for m in reloaded.list_media()] == ["a.png"]

def test_reorder_and_toggle(tmp_path):
    store, media = make_store(tmp_path)
    (media / "a.png").write_bytes(b"x")
    (media / "b.png").write_bytes(b"x")
    a = store.add_media("a.png")
    b = store.add_media("b.png")
    store.reorder(b.id, "up")
    assert [m.filename for m in store.list_media()] == ["b.png", "a.png"]
    store.set_enabled(a.id, False)
    assert store.list_media()[1].enabled is False

def test_remove_deletes_file(tmp_path):
    store, media = make_store(tmp_path)
    (media / "a.png").write_bytes(b"x")
    item = store.add_media("a.png")
    store.remove_media(item.id)
    assert store.list_media() == []
    assert not (media / "a.png").exists()

def test_missing_files_flagged(tmp_path):
    store, media = make_store(tmp_path)
    (media / "a.png").write_bytes(b"x")
    item = store.add_media("a.png")
    (media / "a.png").unlink()
    assert item.id in store.missing_files()

def test_corrupt_manifest_recovers(tmp_path):
    media = tmp_path / "media"; media.mkdir()
    (tmp_path / "manifest.json").write_text("{ not json", "utf-8")
    store = PlaylistStore(tmp_path / "manifest.json", media)
    assert store.list_media() == []
    assert json.loads((tmp_path / "manifest.json").read_text("utf-8"))["media"] == []


def test_mistyped_manifest_recovers(tmp_path):
    # Valid JSON, but a media item is structurally wrong: `schedule` is a string,
    # or `days` is a non-iterable int. MediaItem/Schedule.from_dict then raise
    # AttributeError/TypeError, which a too-narrow recovery `except` lets escape --
    # crashing PlaylistStore on construction, and (under systemd Restart=always) a
    # boot crash-loop. Must back up + reset, exactly like a syntactically-corrupt
    # manifest.
    media = tmp_path / "media"; media.mkdir()
    manifest = tmp_path / "manifest.json"
    for bad_media in (
        [{"id": "a", "filename": "x.jpg", "type": "image", "schedule": "oops"}],
        [{"id": "a", "filename": "x.jpg", "type": "image",
          "schedule": {"days": 5, "start": "08:00", "end": "17:00"}}],
    ):
        manifest.write_text(json.dumps({"settings": {}, "media": bad_media}), "utf-8")
        store = PlaylistStore(manifest, media)  # must not raise
        assert store.list_media() == []
        assert json.loads(manifest.read_text("utf-8"))["media"] == []
        assert any(p.name.startswith("manifest.bad-") for p in tmp_path.iterdir())

def test_settings_persist(tmp_path):
    store, media = make_store(tmp_path)
    store.set_settings(12.0, False, "no")
    reloaded = PlaylistStore(tmp_path / "manifest.json", media)
    s = reloaded.get_settings()
    assert s.default_image_duration == 12.0 and s.muted is False and s.hwdec == "no"

def test_replace_from_master_swaps_media_and_preserves_hwdec(tmp_path):
    store, media = make_store(tmp_path)
    (media / "old.png").write_bytes(b"x")
    store.add_media("old.png")
    store.set_settings(8.0, True, "no")  # local hwdec = "no"

    new = [MediaItem(id="n1", filename="new.png", type="image")]
    store.replace_from_master(9.0, False, new)

    assert [m.filename for m in store.list_media()] == ["new.png"]
    s = store.get_settings()
    assert s.default_image_duration == 9.0
    assert s.muted is False
    assert s.hwdec == "no"  # preserved, NOT synced

    reloaded = PlaylistStore(tmp_path / "manifest.json", media)
    assert [m.filename for m in reloaded.list_media()] == ["new.png"]
