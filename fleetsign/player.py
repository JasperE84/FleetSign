from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from .model import MediaItem
from .mpv_ipc import MpvIpc, connect_unix
from .schedule import is_active
from .store import PlaylistStore

# The single mpv key binding for maintenance; with --no-input-default-bindings this
# is the only key mpv reacts to. F12 (a bare function key) is used rather than a
# Ctrl+Alt combo because the Pi's labwc compositor grabs Ctrl+Alt+<key> for screen
# zoom before mpv ever sees the key.
MAINTENANCE_KEY = "F12"
INPUT_CONF = f"{MAINTENANCE_KEY} cycle fullscreen\n"

# A stable id for our persistent on-screen IP overlay (any unique int).
OSD_OVERLAY_ID = 47

# Stable X11 identity for the foreground guard. The title is what xdotool searches
# for.
MPV_WINDOW_TITLE = "FleetSign Signage"
WAYLAND_DISPLAY_BLOCKER = "fleetsign-no-wayland"
FOREGROUND_GUARD_INTERVAL = 10.0


def local_ip() -> Optional[str]:
    """IP of the interface holding the default route (Ethernet or Wi-Fi).

    Opens a UDP socket toward a public address and reads the local side; no
    packets are actually sent, and it works offline as long as a route exists.
    Returns None when there is no usable network.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def format_ip_overlay(ip: Optional[str], port: int) -> str:
    """ASS event for the corner overlay: tiny white text, bottom-right (\\an3),
    sized for ~12px on 1080p — readable up close, unobtrusive from across a room."""
    label = f"http://{ip}:{port}" if ip else "FleetSign: no network"
    return r"{\an3\fs12\bord1\3c&H000000&\1c&HFFFFFF&}" + label


def select_next(items: list[MediaItem], now: datetime, last_id: Optional[str]) -> Optional[MediaItem]:
    active = [it for it in items if is_active(it, now)]
    if not active:
        return None
    if last_id is not None:
        for i, it in enumerate(active):
            if it.id == last_id:
                return active[(i + 1) % len(active)]
    return active[0]


# mpv's plain --hwdec=auto probes GPU decoders (it logs "cannot load libcuda.so.1"
# on a Pi) and then selects a zero-copy/overlay path the desktop compositor does
# not display — video becomes a solid blue plane while images (no decode) are
# fine. "auto-copy" keeps hardware decoding but copies frames into system memory
# so the gpu output renders them. The active value is the web Settings' hwdec
# (default auto-copy), changeable from the web UI.


def mpv_args(socket_path: str, input_conf: str, hwdec: str) -> list[str]:
    return [
        "mpv", "--idle=yes", "--force-window=yes", "--fullscreen", "--ontop",
        f"--title={MPV_WINDOW_TITLE}",
        f"--hwdec={hwdec}", "--no-osc", "--no-input-default-bindings",
        "--really-quiet", "--image-display-duration=inf",
        "--cursor-autohide=always",
        f"--input-conf={input_conf}",
        f"--input-ipc-server={socket_path}",
    ]


def default_launcher(socket_path: str, input_conf: str, hwdec: str) -> subprocess.Popen:
    # Run mpv as an X11 client so --ontop is actually honored. A native Wayland
    # client cannot pin itself above other windows (no protocol for it), so a
    # terminal raised over the fullscreen signage would stay in front until mpv
    # is relaunched. Under XWayland mpv's --ontop sets the X11 _NET_WM_STATE_ABOVE
    # hint, which labwc keeps above other windows once install.sh's
    # allowAlwaysOnTop rule permits it. Set WAYLAND_DISPLAY to a dead socket name
    # rather than unsetting it: libwayland falls back to the default wayland-0
    # socket when the variable is absent. DISPLAY is defaulted because the
    # systemd --user environment imports WAYLAND_DISPLAY but not necessarily
    # DISPLAY; :0 is the XWayland/Xorg display on a single-screen Pi.
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = WAYLAND_DISPLAY_BLOCKER
    env.setdefault("DISPLAY", ":0")
    return subprocess.Popen(mpv_args(socket_path, input_conf, hwdec), env=env)


def _default_connector(socket_path: str) -> MpvIpc:
    return MpvIpc(connect_unix(socket_path))


def _run_command_text(args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


class ForegroundGuard:
    def __init__(
        self,
        interval: float = FOREGROUND_GUARD_INTERVAL,
        runner: Callable[[Sequence[str]], str] = _run_command_text,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval = interval
        self._runner = runner
        self._clock = clock
        self._next_check = self._clock() + self.interval
        # Latches True while we can't find the mpv window, so the warning fires
        # once per outage (on transition) instead of every interval forever.
        self._warned_missing = False

    def reset(self) -> None:
        self._next_check = self._clock() + self.interval

    def maybe_raise(self, enabled: bool) -> None:
        now = self._clock()
        if not enabled:
            self._next_check = now + self.interval
            return
        if now < self._next_check:
            return
        self._next_check = now + self.interval

        wid = self._find_mpv_window()
        if wid is None:
            # Otherwise this fails silently: missing xdotool/wmctrl, an unmapped
            # window, or an unreachable X server all yield no window and the guard
            # just stops enforcing always-on-top with no trace. Log it once so the
            # outage is diagnosable (see INSTALL.md troubleshooting). _ensure_mpv
            # calls reset() on every relaunch, deferring the next check ~interval,
            # so a fresh mpv has time to map before this would warn.
            if not self._warned_missing:
                self._warned_missing = True
                logging.getLogger(__name__).warning(
                    "ForegroundGuard cannot find the mpv window (xdotool/wmctrl "
                    "installed? signage window mapped under XWayland?); "
                    "always-on-top is not being enforced")
            return
        if self._warned_missing:
            self._warned_missing = False
            logging.getLogger(__name__).info(
                "ForegroundGuard reacquired the mpv window; always-on-top resumed")
        wid_text = str(wid)
        wid_hex = f"0x{wid:x}"
        self._runner(["wmctrl", "-i", "-r", wid_hex, "-b", "add,above"])
        self._runner(["xdotool", "windowactivate", "--sync", wid_text])

    def _find_mpv_window(self) -> Optional[int]:
        for args in (
            ["xdotool", "search", "--name", f"^{MPV_WINDOW_TITLE}$"],
            ["xdotool", "search", "--class", "mpv"],
        ):
            wid = self._last_window_id(self._runner(args))
            if wid is not None:
                return wid
        return None

    @staticmethod
    def _last_window_id(output: str) -> Optional[int]:
        ids = [ForegroundGuard._window_id(line) for line in output.splitlines()]
        ids = [wid for wid in ids if wid is not None]
        return ids[-1] if ids else None

    @staticmethod
    def _window_id(value: str) -> Optional[int]:
        value = value.strip()
        if not value:
            return None
        try:
            return int(value, 0)
        except ValueError:
            return None


class PlayerController:
    def __init__(
        self,
        store: PlaylistStore,
        socket_path: str,
        launcher: Callable[[str, str, str], subprocess.Popen] = default_launcher,
        connector: Callable[[str], MpvIpc] = _default_connector,
        clock: Callable[[], datetime] = datetime.now,
        web_port: int = 8080,
        foreground_guard: Optional[ForegroundGuard] = None,
    ):
        self.store = store
        self.socket_path = socket_path
        self.web_port = web_port
        self._input_conf = str(Path(socket_path).parent / "input.conf")
        self._launcher = launcher
        self._connector = connector
        self._clock = clock
        self._foreground_guard = foreground_guard or ForegroundGuard()
        self._proc: Optional[subprocess.Popen] = None
        self._ipc: Optional[MpvIpc] = None
        self._last_id: Optional[str] = None
        self._blank = False
        self._maintenance = False
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop the loop, wait for the player thread to unwind, and ensure mpv is
        gone. Safe to call from a signal handler for a clean, synchronous exit so
        the daemon tears down its own mpv/socket rather than relying on systemd."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._teardown_mpv()

    def is_blank(self) -> bool:
        return self._blank

    def is_maintenance(self) -> bool:
        return self._maintenance

    def set_blank(self, blank: bool) -> None:
        self._blank = blank
        if blank and self._ipc:
            try:
                self._ipc.command("stop")
            except Exception:
                pass

    def set_maintenance(self, on: bool) -> None:
        was = self._maintenance
        self._maintenance = on
        if on:
            # Enter: drop the live mpv out of fullscreen, clear always-on-top, and
            # pause it so the operator can both reach and see the desktop. Without
            # clearing ontop the windowed mpv would keep floating above everything.
            # Cheap, idempotent (safe on a duplicate "Enter maintenance"), and keeps
            # the same mpv up.
            if self._ipc:
                try:
                    self._ipc.command("set_property", "fullscreen", False)
                    self._ipc.command("set_property", "ontop", False)
                    self._ipc.command("set_property", "pause", True)
                except Exception:
                    pass
        elif was:
            # Exit, but only on a real True->False transition. "Resume signage" is
            # always shown in the UI, so a stray click or duplicate POST while
            # already playing must NOT relaunch mpv and needlessly black/restart the
            # wall (the dedicated "Restart playback" control is for that).
            # Relaunch a fresh fullscreen mpv rather than re-fullscreening the live
            # one: re-entering fullscreen recreates mpv's video-output window, and
            # the loadfile the player thread fires immediately after lands
            # mid-recreation — a race that hangs mpv on the Pi compositor (its IPC
            # socket then dies -> BrokenPipeError, black screen). See mpv issues
            # #3678 / #9704. A clean relaunch is mpv's reliable boot path: it
            # sequences window-create -> first decode internally.
            self.restart_playback()

    def restart_playback(self) -> None:
        # Request a fresh mpv relaunch (restart button, an hwdec change, or
        # exiting maintenance). May be called from a Waitress worker thread or
        # from the player thread itself. Either way DON'T tear mpv down here:
        # that nulls _ipc/_proc while the player thread is parked in
        # _play_item's inner loop, which then can never satisfy an exit
        # condition and busy-spins forever, leaving the screen black. Instead
        # signal the player thread, which owns mpv, to relaunch on its next loop
        # iteration.
        self._restart.set()

    def _teardown_mpv(self) -> None:
        if self._ipc:
            self._ipc.close()
            self._ipc = None
        if self._proc:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    # A mpv hung in the GPU/compositor (e.g. wedged mid video-output
                    # reinit) ignores SIGTERM. SIGKILL it, then wait() again to reap
                    # it: if we returned without reaping, _ensure_mpv could map a
                    # fresh window while the killed one's surface is still up (the
                    # stale-black-window symptom) and leave a zombie behind. If even
                    # SIGKILL doesn't land in time (mpv stuck in an uninterruptible
                    # GPU/IO wait), log it — a brief double window is the visible cost.
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        logging.getLogger(__name__).warning(
                            "mpv did not exit after SIGKILL; relaunch may briefly "
                            "overlap a stale window")
            except Exception:
                pass
            self._proc = None

    def _write_input_conf(self) -> None:
        try:
            p = Path(self._input_conf)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(INPUT_CONF, "utf-8")
        except OSError:
            pass

    def _ensure_mpv(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            if self._ipc is not None:
                try:
                    self._ipc.close()
                except Exception:
                    pass
                self._ipc = None
            self._write_input_conf()
            self._proc = self._launcher(self.socket_path, self._input_conf,
                                        self.store.get_settings().hwdec)
            self._ipc = self._connector(self.socket_path)
            self._maintenance = False
            self._foreground_guard.reset()
            try:
                self._ipc.command("observe_property", 1, "fullscreen")
            except Exception:
                pass

    def _update_ip_overlay(self) -> None:
        # Show http://<ip>:<port> small in the bottom-right so the web UI is
        # discoverable from the screen itself. Re-sent each loop iteration so it
        # survives an mpv relaunch and tracks a changed (DHCP) IP. Best-effort.
        if not self._ipc:
            return
        try:
            self._ipc.command(
                "osd-overlay", OSD_OVERLAY_ID, "ass-events",
                format_ip_overlay(local_ip(), self.web_port),
                1920, 1080, 0, False, False,
            )
        except Exception:
            pass

    def _pump_event(self, timeout: float) -> Optional[dict]:
        ev = self._ipc.get_event(timeout=timeout) if self._ipc else None
        if ev and ev.get("event") == "property-change" and ev.get("name") == "fullscreen":
            maint = not bool(ev.get("data", True))
            if maint != self._maintenance:
                self._maintenance = maint
                if maint:
                    # Entered maintenance (operator pressed F12 to leave fullscreen):
                    # clear always-on-top so the windowed mpv stops covering the
                    # desktop, and pause. (Exiting relaunches a fresh mpv, which
                    # comes back fullscreen + --ontop, so no live restore here.)
                    try:
                        self._ipc.command("set_property", "ontop", False)
                        self._ipc.command("set_property", "pause", maint)
                    except Exception:
                        pass
                else:
                    # Exited (F12 back to fullscreen): relaunch fresh rather than
                    # reusing the just-recreated window — same loadfile-mid-recreation
                    # hang as the web Resume path. See set_maintenance().
                    self.restart_playback()
        return ev

    def _play_item(self, item: MediaItem) -> None:
        settings = self.store.get_settings()
        # Set options as PROPERTIES before loadfile rather than as positional
        # loadfile options: mpv >= 0.38 inserted an <index> arg before <options>
        # in loadfile, which would silently drop positional options. Properties
        # work across mpv versions.
        self._ipc.command("set_property", "mute", settings.muted)
        if item.type == "image":
            dur = item.image_duration or settings.default_image_duration
            self._ipc.command("set_property", "image-display-duration", dur)
        self._ipc.command("loadfile", str(self.store.media_dir / item.filename), "replace")
        while not self._stop.is_set():
            # Bail back to _run on a restart request (it owns teardown/relaunch)
            # or if mpv is gone, so we never spin against a dead/None ipc.
            if self._restart.is_set() or self._ipc is None:
                return
            ev = self._pump_event(0.5)
            if ev and ev.get("event") == "end-file":
                return
            if self._blank or self._maintenance:
                return
            if self._proc is not None and self._proc.poll() is not None:
                return
            # Reassert always-on-top only AFTER pumping events and the blank/
            # maintenance checks above: a pending F12 (fullscreen-off) event is
            # processed by _pump_event and flips _maintenance first, so the guard
            # never raises mpv on the very iteration the operator drops to the
            # desktop. Raising at the top of the loop would fire one reassert
            # against the about-to-be-stale "playing" state — the signage window
            # jumping back on top exactly as maintenance begins.
            self._foreground_guard.maybe_raise(
                not self._maintenance and not self._blank and self._proc is not None)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._restart.is_set():
                    # Serviced here, on the thread that owns mpv, so teardown +
                    # relaunch never races a web worker's restart_playback().
                    self._restart.clear()
                    self._teardown_mpv()
                self._ensure_mpv()
                self._update_ip_overlay()
                if self._maintenance:
                    self._pump_event(0.5)
                    continue
                if self._blank:
                    self._ipc.command("stop")
                    # Drain instead of sleeping: the stop above emits an end-file
                    # event. Left in the queue it would be consumed by the first
                    # _play_item after un-blanking, which would return instantly
                    # on the stale event — the "half a second of video then blank"
                    # flash on resume. Pumping it here keeps resume clean.
                    self._pump_event(0.5)
                    continue
                # Past the maintenance/blank guards above, so the guard is reached
                # only while actually playing (or idle with no active item) — never
                # on a maintenance/blank iteration. Keeps the always-on-top reassert
                # off the moment maintenance/blank begins.
                self._foreground_guard.maybe_raise(
                    not self._maintenance and not self._blank and self._proc is not None)
                item = select_next(self.store.list_media(), self._clock(), self._last_id)
                if item is None:
                    self._stop.wait(1.0)
                    continue
                self._last_id = item.id
                if not (self.store.media_dir / item.filename).exists():
                    self._stop.wait(1.0)
                    continue
                self._play_item(item)
            except Exception:
                logging.getLogger(__name__).exception("player loop error")
                self._teardown_mpv()
                self._stop.wait(1.0)
        self._teardown_mpv()
