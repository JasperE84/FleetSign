from __future__ import annotations

import logging
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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
        "mpv", "--idle=yes", "--force-window=yes", "--fullscreen",
        f"--hwdec={hwdec}", "--no-osc", "--no-input-default-bindings",
        "--really-quiet", "--image-display-duration=inf",
        "--cursor-autohide=always",
        f"--input-conf={input_conf}",
        f"--input-ipc-server={socket_path}",
    ]


def default_launcher(socket_path: str, input_conf: str, hwdec: str) -> subprocess.Popen:
    return subprocess.Popen(mpv_args(socket_path, input_conf, hwdec))


def _default_connector(socket_path: str) -> MpvIpc:
    return MpvIpc(connect_unix(socket_path))


class PlayerController:
    def __init__(
        self,
        store: PlaylistStore,
        socket_path: str,
        launcher: Callable[[str, str, str], subprocess.Popen] = default_launcher,
        connector: Callable[[str], MpvIpc] = _default_connector,
        clock: Callable[[], datetime] = datetime.now,
        web_port: int = 8080,
    ):
        self.store = store
        self.socket_path = socket_path
        self.web_port = web_port
        self._input_conf = str(Path(socket_path).parent / "input.conf")
        self._launcher = launcher
        self._connector = connector
        self._clock = clock
        self._proc: Optional[subprocess.Popen] = None
        self._ipc: Optional[MpvIpc] = None
        self._last_id: Optional[str] = None
        self._blank = False
        self._maintenance = False
        self._stop = threading.Event()
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
        self._maintenance = on
        if self._ipc:
            try:
                self._ipc.command("set_property", "fullscreen", not on)
                self._ipc.command("set_property", "pause", on)
            except Exception:
                pass

    def restart_playback(self) -> None:
        self._teardown_mpv()

    def _teardown_mpv(self) -> None:
        if self._ipc:
            self._ipc.close()
            self._ipc = None
        if self._proc:
            try:
                self._proc.terminate()
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
                try:
                    self._ipc.command("set_property", "pause", maint)
                except Exception:
                    pass
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
            ev = self._pump_event(0.5)
            if ev and ev.get("event") == "end-file":
                return
            if self._blank or self._maintenance:
                return
            if self._proc is not None and self._proc.poll() is not None:
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._ensure_mpv()
                self._update_ip_overlay()
                if self._maintenance:
                    self._pump_event(0.5)
                    continue
                if self._blank:
                    self._ipc.command("stop")
                    self._stop.wait(0.5)
                    continue
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
