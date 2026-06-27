import logging
from datetime import datetime
from fleetsign.model import MediaItem
from fleetsign.player import (select_next, PlayerController, format_ip_overlay,
                            local_ip, mpv_args, default_launcher, ForegroundGuard,
                            MPV_WINDOW_TITLE, WAYLAND_DISPLAY_BLOCKER)
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
    assert ("set_property", "pause", True) in ctrl._ipc.calls

def test_set_maintenance_drops_ontop_on_enter(tmp_path):
    # Entering maintenance must clear always-on-top too, else the windowed mpv
    # keeps floating above the desktop the operator is trying to use.
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))
    ctrl._ipc = FakeIpc()
    ctrl.set_maintenance(True)
    assert ("set_property", "ontop", False) in ctrl._ipc.calls

def test_set_maintenance_exit_relaunches_not_refullscreen(tmp_path):
    # Resuming from maintenance must NOT re-fullscreen the live mpv: re-entering
    # fullscreen recreates mpv's video-output window and the loadfile that follows
    # lands mid-recreation, hanging mpv on the Pi (BrokenPipeError, black screen,
    # mpv #3678/#9704). Resume instead relaunches a fresh fullscreen mpv.
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))
    ctrl._ipc = FakeIpc()
    ctrl.set_maintenance(True)
    ctrl._ipc.calls.clear()
    ctrl.set_maintenance(False)
    assert ctrl.is_maintenance() is False
    assert ctrl._restart.is_set()  # exit asks the player thread to relaunch
    assert ("set_property", "fullscreen", True) not in ctrl._ipc.calls

def test_set_maintenance_resume_when_off_does_not_relaunch(tmp_path):
    # "Resume signage" is always shown in the UI, so clicking it (or a duplicate
    # POST) while already playing must NOT relaunch mpv — only a real True->False
    # transition does.
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))
    ctrl._ipc = FakeIpc()
    ctrl.set_maintenance(False)            # already off (default state)
    assert ctrl._restart.is_set() is False

def test_pump_event_tracks_fullscreen(tmp_path):
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))

    class EvIpc:
        def __init__(self, ev): self.ev = ev; self.calls = []
        def get_event(self, timeout): return self.ev
        def command(self, *a, **k): self.calls.append(a)

    ctrl._ipc = EvIpc({"event": "property-change", "name": "fullscreen", "data": False})
    ctrl._pump_event(0.01)
    assert ctrl.is_maintenance() is True
    assert ("set_property", "pause", True) in ctrl._ipc.calls  # F12 enter pauses
    assert ("set_property", "ontop", False) in ctrl._ipc.calls  # ...and drops on-top
    ctrl._ipc = EvIpc({"event": "property-change", "name": "fullscreen", "data": True})
    ctrl._pump_event(0.01)
    assert ctrl.is_maintenance() is False
    assert ctrl._restart.is_set()  # F12 exit relaunches, not a live re-fullscreen

def test_teardown_kills_mpv_that_ignores_terminate(tmp_path):
    # A GPU/compositor-hung mpv ignores SIGTERM; teardown must SIGKILL it so the
    # loop never relaunches a second mpv behind a stale black fullscreen window.
    import subprocess
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))

    class HungProc:
        def __init__(self): self.terminated = False; self.killed = False
        def terminate(self): self.terminated = True
        def wait(self, timeout=None): raise subprocess.TimeoutExpired("mpv", timeout)
        def kill(self): self.killed = True

    proc = HungProc()
    ctrl._proc = proc
    ctrl._teardown_mpv()
    assert proc.terminated and proc.killed
    assert ctrl._proc is None

def test_teardown_reaps_after_kill(tmp_path):
    # After SIGKILL the player must wait() again to reap the process, so _run can't
    # launch a new mpv while the killed one's window still lingers (and so we don't
    # leak a zombie). The normal case: SIGTERM ignored -> kill -> reaped.
    import subprocess
    store = PlaylistStore(tmp_path / "manifest.json", tmp_path)
    ctrl = PlayerController(store, str(tmp_path / "mpv.sock"))

    class StubbornProc:
        def __init__(self): self.terminated = self.killed = False; self.waits = 0
        def terminate(self): self.terminated = True
        def kill(self): self.killed = True
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("mpv", timeout)  # ignores SIGTERM
            return 0                                             # reaped after SIGKILL

    proc = StubbornProc()
    ctrl._proc = proc
    ctrl._teardown_mpv()
    assert proc.terminated and proc.killed
    assert proc.waits == 2   # waited again after kill to reap it
    assert ctrl._proc is None

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

def test_mpv_args_pins_window_on_top():
    # mpv must launch always-on-top so a raised terminal can't sit in front of the
    # signage; this is honored only via XWayland (see default_launcher).
    assert "--ontop" in mpv_args("/sock", "/conf.conf", "auto-copy")

def test_mpv_args_sets_stable_window_title():
    args = mpv_args("/sock", "/conf.conf", "auto-copy")
    assert f"--title={MPV_WINDOW_TITLE}" in args

def test_default_launcher_passes_hwdec(monkeypatch):
    captured = {}
    monkeypatch.setattr("fleetsign.player.subprocess.Popen",
                        lambda a, env=None: captured.setdefault("args", a))
    default_launcher("/sock", "/conf.conf", "no")
    assert "--hwdec=no" in captured["args"]

def test_default_launcher_forces_xwayland(monkeypatch):
    # --ontop is a no-op for a native Wayland client, so the launcher runs mpv
    # under XWayland by making Wayland connection fail and ensuring DISPLAY is set.
    captured = {}
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("fleetsign.player.subprocess.Popen",
                        lambda a, env=None: captured.update(args=a, env=env))
    default_launcher("/sock", "/conf.conf", "auto-copy")
    assert captured["env"]["WAYLAND_DISPLAY"] == WAYLAND_DISPLAY_BLOCKER
    assert captured["env"].get("DISPLAY")

def test_foreground_guard_waits_until_interval():
    calls = []
    now = [100.0]
    guard = ForegroundGuard(interval=10.0, runner=lambda a: calls.append(a) or "",
                            clock=lambda: now[0])
    guard.maybe_raise(True)
    assert calls == []
    now[0] = 110.0
    guard.maybe_raise(True)
    assert calls[0] == ["xdotool", "search", "--name", f"^{MPV_WINDOW_TITLE}$"]

def test_foreground_guard_reasserts_mpv_on_interval():
    calls = []

    def runner(args):
        calls.append(args)
        if args[:3] == ["xdotool", "search", "--name"]:
            return "123"
        return ""

    guard = ForegroundGuard(interval=10.0, runner=runner, clock=lambda: 10.0)
    guard._next_check = 0.0
    guard.maybe_raise(True)
    assert ["wmctrl", "-i", "-r", "0x7b", "-b", "add,above"] in calls
    assert ["xdotool", "windowactivate", "--sync", "123"] in calls

def test_foreground_guard_raises_mpv_when_focus_was_stolen():
    calls = []

    def runner(args):
        calls.append(args)
        if args[:3] == ["xdotool", "search", "--name"]:
            return "123\n456"
        return ""

    guard = ForegroundGuard(interval=10.0, runner=runner, clock=lambda: 10.0)
    guard._next_check = 0.0
    guard.maybe_raise(True)
    assert ["wmctrl", "-i", "-r", "0x1c8", "-b", "add,above"] in calls
    assert ["xdotool", "windowactivate", "--sync", "456"] in calls

def test_foreground_guard_disabled_in_maintenance():
    calls = []
    guard = ForegroundGuard(interval=10.0, runner=lambda a: calls.append(a) or "",
                            clock=lambda: 20.0)
    guard._next_check = 0.0
    guard.maybe_raise(False)
    assert calls == []
    assert guard._next_check == 30.0

def test_foreground_guard_warns_once_when_window_missing(caplog):
    # An empty runner means no window is ever found (missing tools / unmapped
    # window / dead X). The guard must surface that once, not silently no-op.
    guard = ForegroundGuard(interval=10.0, runner=lambda a: "", clock=lambda: 10.0)
    with caplog.at_level(logging.WARNING, logger="fleetsign.player"):
        guard._next_check = 0.0
        guard.maybe_raise(True)
        guard._next_check = 0.0  # next interval elapses; still missing
        guard.maybe_raise(True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

def test_foreground_guard_logs_recovery_after_window_returns(caplog):
    state = {"found": False}

    def runner(args):
        if args[:3] == ["xdotool", "search", "--name"] and state["found"]:
            return "123"
        return ""

    guard = ForegroundGuard(interval=10.0, runner=runner, clock=lambda: 10.0)
    with caplog.at_level(logging.INFO, logger="fleetsign.player"):
        guard._next_check = 0.0
        guard.maybe_raise(True)        # missing -> one warning
        state["found"] = True
        guard._next_check = 0.0
        guard.maybe_raise(True)        # back -> one recovery info
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    assert any(r.levelno == logging.INFO and "reacquired" in r.getMessage()
               for r in caplog.records)

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
