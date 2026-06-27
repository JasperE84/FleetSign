from datetime import datetime
from fleetsign.model import MediaItem
from fleetsign.player import (select_next, PlayerController, format_ip_overlay,
                            local_ip, mpv_args, default_launcher)
from fleetsign.store import PlaylistStore

NOW = datetime(2026, 6, 26, 12, 0)

def items(*specs):
    out = []
    for i, enabled in specs:
        out.append(MediaItem(id=i, filename=f"{i}.png", type="image", enabled=enabled))
    return out

def test_select_next_cycles_active_only():
    lst = items(("a", True), ("b", False), ("c", True))
    assert select_next(lst, NOW, None).id == "a"
    assert select_next(lst, NOW, "a").id == "c"
    assert select_next(lst, NOW, "c").id == "a"  # wraps, skips disabled b

def test_select_next_none_when_empty():
    assert select_next(items(("a", False)), NOW, None) is None

def test_select_next_unknown_last_restarts():
    assert select_next(items(("a", True)), NOW, "gone").id == "a"

class FakeIpc:
    def __init__(self):
        self.calls = []
    def command(self, *args, timeout=5.0):
        self.calls.append(args)
        return {"error": "success"}
    def get_event(self, timeout):
        return {"event": "end-file"}
    def close(self):
        pass

def test_play_item_sets_duration_and_mute(tmp_path):
    media = tmp_path / "media"; media.mkdir()
    (media / "a.png").write_bytes(b"x")
    store = PlaylistStore(tmp_path / "manifest.json", media)
    item = store.add_media("a.png")
    store.set_duration(item.id, 12.0)
    ctrl = PlayerController(store, "unused.sock")
    ctrl._ipc = FakeIpc()
    ctrl._play_item(store.list_media()[0])
    calls = ctrl._ipc.calls
    assert ("set_property", "mute", True) in calls
    assert ("set_property", "image-display-duration", 12.0) in calls
    assert any(c[0] == "loadfile" and c[2] == "replace" for c in calls)

def test_set_maintenance_unfullscreens(tmp_path):
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))
    ctrl._ipc = FakeIpc()
    ctrl.set_maintenance(True)
    assert ctrl.is_maintenance() is True
    assert ("set_property", "fullscreen", False) in ctrl._ipc.calls

def test_pump_event_tracks_fullscreen(tmp_path):
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))

    class EvIpc:
        def __init__(self, ev): self.ev = ev
        def get_event(self, timeout): return self.ev
        def command(self, *a, **k): pass

    ctrl._ipc = EvIpc({"event": "property-change", "name": "fullscreen", "data": False})
    ctrl._pump_event(0.01)
    assert ctrl.is_maintenance() is True
    ctrl._ipc = EvIpc({"event": "property-change", "name": "fullscreen", "data": True})
    ctrl._pump_event(0.01)
    assert ctrl.is_maintenance() is False

def test_format_ip_overlay_with_ip():
    s = format_ip_overlay("192.168.1.50", 8080)
    assert "http://192.168.1.50:8080" in s
    assert r"\an3" in s  # bottom-right alignment

def test_format_ip_overlay_no_network():
    assert "no network" in format_ip_overlay(None, 8080)

def test_local_ip_returns_dotted_or_none():
    ip = local_ip()
    assert ip is None or ip.count(".") == 3

def test_update_ip_overlay_sends_osd_command(tmp_path):
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"), web_port=8080)
    ctrl._ipc = FakeIpc()
    ctrl._update_ip_overlay()
    cmd = ctrl._ipc.calls[0]
    assert cmd[0] == "osd-overlay" and cmd[2] == "ass-events"

def test_mpv_args_carries_hwdec():
    args = mpv_args("/sock", "/conf.conf", "auto-copy")
    assert "--hwdec=auto-copy" in args
    assert "--hwdec=auto" not in args  # plain auto blue-screens video on the Pi

def test_default_launcher_passes_hwdec(monkeypatch):
    captured = {}
    monkeypatch.setattr("fleetsign.player.subprocess.Popen",
                        lambda a: captured.setdefault("args", a))
    default_launcher("/sock", "/conf.conf", "no")
    assert "--hwdec=no" in captured["args"]

def test_write_input_conf_binds_f12(tmp_path):
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))
    ctrl._write_input_conf()
    assert (tmp_path / "input.conf").read_text("utf-8").strip() == "F12 cycle fullscreen"


def test_restart_playback_unblocks_play_item(tmp_path):
    # Regression: restart_playback() is called from Waitress worker threads while
    # the player thread is parked in _play_item's inner loop (the common case —
    # the player spends almost all its time waiting out an image/video). If
    # restart tears mpv down cross-thread, _play_item must still exit promptly so
    # _run can relaunch mpv; otherwise the loop busy-spins forever and the screen
    # stays black until something else kicks it.
    import threading, time
    media = tmp_path / "media"; media.mkdir()
    (media / "a.png").write_bytes(b"x")
    store = PlaylistStore(tmp_path / "manifest.json", media)
    store.add_media("a.png")
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))

    class SlowIpc:
        def command(self, *a, timeout=5.0): return {"error": "success"}
        def get_event(self, timeout):
            time.sleep(timeout or 0)   # emulate mpv: block, then no event
            return None
        def close(self): pass

    class AliveProc:
        def poll(self): return None
        def terminate(self): pass

    ctrl._ipc = SlowIpc()
    ctrl._proc = AliveProc()

    done = threading.Event()
    threading.Thread(
        target=lambda: (ctrl._play_item(store.list_media()[0]), done.set()),
        daemon=True,
    ).start()
    time.sleep(0.2)             # let it settle into the inner loop
    ctrl.restart_playback()     # called from "another thread", like a web worker
    assert done.wait(2.0), "play_item did not return after restart_playback"


def test_restart_playback_relaunches_via_run_loop(tmp_path):
    # restart_playback() must cause mpv to be relaunched. With the player thread
    # owning teardown, the request is serviced on the next _run iteration.
    import time
    media = tmp_path / "media"; media.mkdir()
    (media / "a.png").write_bytes(b"x")
    store = PlaylistStore(tmp_path / "manifest.json", media)
    store.add_media("a.png")

    launches = []

    class DummyProc:
        def poll(self): return None
        def terminate(self): pass

    class QuietIpc:
        def command(self, *a, timeout=5.0): return {"error": "success"}
        def get_event(self, timeout):
            time.sleep(timeout or 0)
            return None
        def close(self): pass

    def launcher(*a):
        launches.append(1)
        return DummyProc()

    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"),
                            launcher=launcher, connector=lambda *a: QuietIpc())
    ctrl.start()
    time.sleep(0.4)
    assert len(launches) == 1
    ctrl.restart_playback()
    time.sleep(0.6)
    ctrl.stop()
    assert len(launches) >= 2, "restart_playback did not relaunch mpv"
