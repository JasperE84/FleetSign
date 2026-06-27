from __future__ import annotations

import json
import math
import os
import random
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from .model import MediaItem, classify
from .store import PlaylistStore, safe_unlink
from .validate import positive_seconds


def manifest_payload(store: PlaylistStore) -> dict:
    """Master-side: the JSON a slave pulls. Excludes hwdec (local per Pi), and
    excludes any media item whose file is missing on the master so slaves mirror
    only what the master can actually serve. (If a master item is left in `media`
    but absent from `files`, a slave holding an older copy of that filename would
    never prune it and would keep playing stale content the master can't serve.)"""
    files: dict[str, dict] = {}
    served: list[MediaItem] = []
    for m in store.list_media():
        try:
            st = (store.media_dir / m.filename).stat()
        except OSError:
            continue  # missing on master: don't mirror an item we can't serve
        files[m.filename] = {"size": st.st_size, "mtime": st.st_mtime}
        served.append(m)
    s = store.get_settings()
    return {
        "settings": {"default_image_duration": s.default_image_duration,
                     "muted": s.muted},
        "media": [m.to_dict() for m in served],
        "files": files,
    }


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    ok: bool
    error: Optional[str] = None
    downloaded: int = 0
    pruned: int = 0


def urllib_fetch(url: str, token: str, dest: Optional[Path] = None,
                 timeout: float = 30.0) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"X-Sync-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if dest is None:
                return resp.read()
            # Stream straight to the .tmp file so a multi-hundred-MB video never
            # sits whole in RAM on the Pi — mirrors the upload path's SD-card
            # spill rather than re-introducing the RAM-exhaustion failure.
            with open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1024 * 1024)
            return None
    except Exception as e:  # URLError, HTTPError, socket timeout, etc.
        raise SyncError(str(e))


def _is_safe_media_name(name: object) -> bool:
    """True only for a plain filename that stays inside media_dir. Rejects path
    separators, '.'/'..', absolute paths, NULs and non-strings — a master (or a
    MITM on the unauthenticated sync channel) must not be able to steer a write
    outside media/ via the manifest's filenames."""
    return (
        isinstance(name, str)
        and bool(name)
        and name not in (".", "..")
        and name == os.path.basename(name)
        and "/" not in name and "\\" not in name and "\x00" not in name
    )


def _is_finite_number(v: object) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _base_url(master_url: str) -> str:
    u = master_url.strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")


def _up_to_date(dest: Path, meta: dict) -> bool:
    try:
        st = dest.stat()
    except OSError:
        return False
    # mtime copied from master via os.utime, so compare with a small tolerance
    return st.st_size == meta["size"] and abs(st.st_mtime - meta["mtime"]) < 1.0


class SyncClient:
    def __init__(self, store: PlaylistStore, config,
                 fetch: Callable[..., Optional[bytes]] = urllib_fetch,
                 clock: Callable[[], float] = time.time,
                 rng: Callable[[float, float], float] = random.uniform) -> None:
        self.store = store
        self.config = config
        self._fetch = fetch
        self._clock = clock
        self._rng = rng
        self.last_sync: Optional[float] = None
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sync_once(self) -> SyncResult:
        base = _base_url(self.config.master_url)
        token = self.config.sync_token
        try:
            raw = self._fetch(base + "/sync/manifest", token)
            payload = json.loads(raw)
            settings = payload["settings"]
            files = payload["files"]
            if not isinstance(settings, dict) or not isinstance(files, dict):
                raise ValueError("bad manifest shape")
            duration = positive_seconds(settings["default_image_duration"])
            muted = bool(settings["muted"])
            raw_media = payload["media"]
            if not isinstance(raw_media, list):
                raise ValueError("media is not a list")
            for m in raw_media:
                if not isinstance(m, dict):
                    raise ValueError(f"media entry is not an object: {m!r}")
            media = [MediaItem.from_dict(m) for m in raw_media]
            # Validate every item up-front, before touching the filesystem: the
            # filename must be a safe basename, the item's own fields must be
            # sound, and it must carry numeric size/mtime metadata. Reject the
            # WHOLE manifest otherwise — never silently skip an item (that would
            # add a fileless playlist entry and shield a stale local copy from
            # pruning) and never let a "../" name or a wrong-typed mtime reach
            # the write/os.utime path. The item fields matter because the player
            # trusts them: a non-"image" type on an actual image skips the
            # image-display-duration set, so mpv's default `inf` stalls the
            # playlist with no end-file to advance on. So `type` must match what
            # the filename actually classifies as -- not merely be one of the two
            # allowed strings -- which also rejects unclassifiable extensions
            # (classify raises, caught below) that have no honest type at all.
            for m in media:
                if not _is_safe_media_name(m.filename):
                    raise ValueError(f"unsafe filename: {m.filename!r}")
                if not (isinstance(m.id, str) and m.id):
                    raise ValueError(f"bad id for {m.filename!r}")
                if m.type != classify(m.filename):
                    raise ValueError(
                        f"type {m.type!r} does not match {m.filename}")
                if not isinstance(m.enabled, bool):
                    raise ValueError(f"bad enabled flag for {m.filename}")
                if m.image_duration is not None and not (
                        _is_finite_number(m.image_duration)
                        and m.image_duration > 0):
                    raise ValueError(f"bad image_duration for {m.filename}")
                meta = files.get(m.filename)
                if not isinstance(meta, dict):
                    raise ValueError(f"missing file meta for {m.filename}")
                if not _is_finite_number(meta.get("size")) or \
                        not _is_finite_number(meta.get("mtime")):
                    raise ValueError(f"bad file meta for {m.filename}")
        except (SyncError, ValueError, KeyError, TypeError, AttributeError) as e:
            # AttributeError backstops a non-dict nested value (e.g. a media
            # item whose `schedule` is a string), which from_dict touches with
            # .get before our up-front checks can reach it.
            self.last_error = f"manifest: {e}"
            return SyncResult(ok=False, error=str(e))

        media_dir = self.store.media_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        for m in media:
            meta = files[m.filename]  # presence + types validated above
            dest = media_dir / m.filename
            if _up_to_date(dest, meta):
                continue
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            try:
                self._fetch(base + "/sync/media/" + quote(m.filename), token,
                            dest=tmp)
            except SyncError as e:
                self.last_error = f"download {m.filename}: {e}"
                return SyncResult(ok=False, error=str(e), downloaded=downloaded)
            # Verify the download isn't truncated before making it live: a short
            # read (dropped connection, full SD card) would otherwise os.replace
            # the live file with partial content the player then shows. The
            # manifest carries no hash, so size is the only check available — but
            # it catches truncation, the real risk. Drop the .tmp and fail the
            # sync; the next run re-downloads from scratch.
            try:
                actual = tmp.stat().st_size
            except OSError:
                actual = -1
            if actual != meta["size"]:
                safe_unlink(tmp)
                msg = f"download {m.filename}: size {actual} != {meta['size']}"
                self.last_error = msg
                return SyncResult(ok=False, error=msg, downloaded=downloaded)
            os.utime(tmp, (meta["mtime"], meta["mtime"]))
            os.replace(tmp, dest)
            downloaded += 1

        # Only rewrite the manifest when content actually changed: the sync runs
        # every ~2 min and the store lives on an SD card, so skip needless writes.
        cur = self.store.get_settings()
        unchanged = (
            duration == cur.default_image_duration
            and muted == cur.muted
            and media == self.store.list_media()
        )
        if not unchanged:
            self.store.replace_from_master(duration, muted, media)

        # The UI password (a hash) is synced from the master so the slave's web UI
        # requires the same login. It is the human-facing credential, kept separate
        # from the sync token that authenticated this request.
        pw = payload.get("password_hash")
        if isinstance(pw, str) and pw != self.config.password_hash:
            self.config.set_password_hash(pw)

        keep = {m.filename for m in media}
        pruned = 0
        for f in list(media_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name.endswith(".tmp"):
                safe_unlink(f)  # orphaned partial download from an interrupted sync
            elif f.name not in keep and safe_unlink(f):
                pruned += 1

        self.last_sync = self._clock()
        self.last_error = None
        return SyncResult(ok=True, downloaded=downloaded, pruned=pruned)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                res = self.sync_once()
            except Exception as e:  # never let the loop die
                self.last_error = str(e)
                res = SyncResult(ok=False, error=str(e))
            delay = self._rng(105.0, 135.0) if res.ok else 15.0
            self._stop.wait(delay)


class FleetTracker:
    """In-memory record of slave IPs that have polled recently. No persistence."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, ip: str, now: float, ttl: float = 3600.0) -> None:
        with self._lock:
            self._seen[ip] = now
            # Bound the dict: drop IPs unseen for an hour (far beyond the 5-min
            # "recent" window) so a long-lived master doesn't accumulate one
            # entry per DHCP lease forever.
            if len(self._seen) > 1:
                self._seen = {k: t for k, t in self._seen.items()
                              if now - t <= ttl}

    def recent(self, now: float, window: float = 300.0) -> list[str]:
        with self._lock:
            return sorted(ip for ip, ts in self._seen.items()
                          if now - ts <= window)
