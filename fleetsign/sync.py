from __future__ import annotations

import json
import logging
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
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_MASTER_PORT = 8080

from . import __version__
from .model import MediaItem, classify
from .schedule import parse_hhmm
from .store import PlaylistStore, safe_unlink
from .validate import positive_seconds

logger = logging.getLogger(__name__)


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
        # The master advertises its own code version (informational, never a
        # gate): a slave records it for its UI so a forgotten upgrade is visible.
        # Schema changes stay backward-compatible -- an old slave ignores unknown
        # keys, a new slave defaults missing ones -- so this is for humans, not
        # for refusing to sync.
        "version": __version__,
        "settings": {"default_image_duration": s.default_image_duration,
                     "muted": s.muted},
        "media": [m.to_dict() for m in served],
        "files": files,
    }


class SyncError(Exception):
    pass


def friendly_sync_error(raw: Optional[str]) -> Optional[str]:
    """Turn a raw sync error (a Python exception string) into a plain-language
    summary for the slave UI, or None when nothing recognisable matches (the UI
    then shows only the raw detail). Pure and case-insensitive so an operator can
    distinguish refused / timeout / bad-token at a glance without reading errno
    strings. Network causes are checked before payload ones: a download that
    itself fails to connect should read as 'refused', not 'data rejected'."""
    if not raw:
        return None
    low = raw.lower()
    if "connection refused" in low:
        return ("Connection refused — the master may be offline, or its "
                "address/port is wrong.")
    if "timed out" in low or "timeout" in low:
        return ("Timed out — the master isn't responding (check the address "
                "and network).")
    if "forbidden" in low or "error 403" in low or "status 403" in low:
        # Match the HTTP-403 forms ("HTTP Error 403: Forbidden") rather than a
        # bare "403" substring, which false-matches a download size-mismatch
        # ("size 4030 != 5000") and mislabels truncated data as a bad token.
        return "Authentication failed — the sync token is likely wrong."
    if ("name or service" in low or "getaddrinfo" in low
            or "name resolution" in low or "nodename" in low):
        return "Can't resolve that address — check the master IP/hostname."
    if "no route to host" in low or "network is unreachable" in low:
        return "No route to the master — is it on the same network?"
    if low.startswith("manifest:") or "!=" in low:
        return ("Reached the master, but its response was rejected "
                "(data or version mismatch).")
    return None


def version_mismatch(master_version: Optional[str],
                     local_version: str = __version__) -> bool:
    """True only when the master's version is known AND differs from ours. An
    unknown master version -- an older master that advertises none, or a slave
    that has never completed a sync -- returns False so the UI never cries wolf
    over data it simply doesn't have yet."""
    return bool(master_version) and master_version != local_version


@dataclass
class SyncResult:
    ok: bool
    error: Optional[str] = None
    downloaded: int = 0
    pruned: int = 0
    # True when the playlist/settings (not just files) were actually applied, so
    # the loop can log a meaningful "synced" line even for a reorder/schedule
    # change that downloads nothing.
    changed: bool = False


def urllib_fetch(url: str, token: str, dest: Optional[Path] = None,
                 timeout: float = 30.0) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"X-Sync-Token": token,
                                               "X-Sync-Version": __version__})
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


def _is_valid_schedule(sch) -> bool:
    """True only for a schedule the master could have produced via its web UI:
    weekday ints in 0-6 and HH:MM start/end times. The sync channel is hostile,
    so mirror the web route's validation here -- from_dict otherwise accepts
    garbage values (out-of-range days, unparseable times) that is_active then
    silently treats as permanently inactive, darkening an item with no error."""
    if not isinstance(sch.days, list) or not all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6
            for d in sch.days):
        return False
    try:
        parse_hhmm(sch.start)
        parse_hhmm(sch.end)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _base_url(master_url: str) -> str:
    u = master_url.strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    # Operators type a bare "192.168.1.50" in the join form, but the master
    # serves on 8080 (see __main__/install.sh), so a portless http URL resolves
    # to port 80 where nothing listens and the sync silently never connects.
    # Supply the default port when none was given. https is left untouched —
    # explicit TLS implies its own default (443), so don't force 8080 onto it.
    parts = urlsplit(u)
    try:
        has_port = parts.port is not None
    except ValueError:
        has_port = True  # malformed port: leave the operator's value as typed
    if parts.scheme == "http" and parts.hostname and not has_port:
        parts = parts._replace(netloc=f"{parts.netloc}:{DEFAULT_MASTER_PORT}")
        u = urlunsplit(parts)
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
        self.last_attempt: Optional[float] = None
        self.last_error: Optional[str] = None
        # The master's code version, learned from the last accepted manifest;
        # stays None until a successful sync (or against an old master that
        # advertises none). The slave UI compares it to its own __version__.
        self.master_version: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sync_once(self) -> SyncResult:
        # Record the attempt up-front so the UI can show "last tried" even on a
        # slave that has never had a successful sync (last_sync stays None).
        self.last_attempt = self._clock()
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
                if m.schedule is not None and not _is_valid_schedule(m.schedule):
                    raise ValueError(f"bad schedule for {m.filename}")
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
            # utime can raise on a finite-but-out-of-range mtime (OverflowError);
            # treat finalize failure like the truncation case -- drop the .tmp and
            # fail the sync -- so a raw exception can't escape and leave an
            # un-pruned .tmp the next run keeps re-creating.
            try:
                os.utime(tmp, (meta["mtime"], meta["mtime"]))
                os.replace(tmp, dest)
            except (OSError, OverflowError) as e:
                safe_unlink(tmp)
                msg = f"finalize {m.filename}: {e}"
                self.last_error = msg
                return SyncResult(ok=False, error=msg, downloaded=downloaded)
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
            logger.info("UI login password updated from master")

        # Record the master's advertised version for the slave's UI. Informational
        # only: a non-string or absent value (old master) just leaves it unknown.
        mv = payload.get("version")
        self.master_version = mv if isinstance(mv, str) else None

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
        return SyncResult(ok=True, downloaded=downloaded, pruned=pruned,
                          changed=not unchanged)

    def start(self) -> None:
        logger.info("sync client started; master=%s", self.config.master_url)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _log_result(self, res: "SyncResult") -> None:
        if res.changed or res.downloaded or res.pruned:
            logger.info("synced from master: %d downloaded, %d pruned%s",
                        res.downloaded, res.pruned,
                        "" if (res.downloaded or res.pruned) else " (playlist/settings updated)")
        else:
            logger.debug("sync ok, no changes")

    def _run(self) -> None:
        # Latch the failing state so a master that's down for hours logs the
        # outage start (and recovery) once, not a WARNING every 15 s. Repeats of
        # the same error drop to DEBUG; a changed error re-warns.
        failing = False
        last_logged_error: Optional[str] = None
        while not self._stop.is_set():
            try:
                res = self.sync_once()
                # _rng is inside the try so the "never die" guarantee is
                # structural, not a property of the injected callables: a raising
                # _rng still recovers on the short backoff instead of killing the
                # thread.
                if res.ok:
                    if failing:
                        failing = False
                        last_logged_error = None
                        logger.info("sync recovered")
                    self._log_result(res)
                    delay = self._rng(105.0, 135.0)
                else:
                    if not failing or res.error != last_logged_error:
                        logger.warning("sync failed: %s (retrying in 15s)", res.error)
                    else:
                        logger.debug("sync still failing: %s", res.error)
                    failing = True
                    last_logged_error = res.error
                    delay = 15.0
            except Exception as e:  # never let the loop die
                self.last_error = str(e)
                if not failing or str(e) != last_logged_error:
                    logger.warning("sync loop error: %s (retrying in 15s)", e)
                else:
                    logger.debug("sync loop error (repeat): %s", e)
                failing = True
                last_logged_error = str(e)
                delay = 15.0
            self._stop.wait(delay)


class FleetTracker:
    """In-memory record of slave IPs (and their reported code version) that have
    polled recently. No persistence."""

    def __init__(self) -> None:
        # ip -> (last_seen_epoch, reported_version_or_None)
        self._seen: dict[str, tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()

    def record(self, ip: str, now: float, version: Optional[str] = None,
               ttl: float = 3600.0) -> None:
        with self._lock:
            is_new = ip not in self._seen
            # Overwrite, so an upgraded slave's new version replaces the old one
            # on its next poll rather than pinning the first-seen value.
            self._seen[ip] = (now, version)
            # Bound the dict: drop IPs unseen for an hour (far beyond the 5-min
            # "recent" window) so a long-lived master doesn't accumulate one
            # entry per DHCP lease forever.
            if len(self._seen) > 1:
                self._seen = {k: v for k, v in self._seen.items()
                              if now - v[0] <= ttl}
        # Log outside the lock. Only the first poll from an IP logs (until it's
        # pruned after an hour idle), so a steadily-polling fleet stays quiet.
        if is_new:
            logger.info("screen checked in: %s", ip)

    def recent(self, now: float, window: float = 300.0) -> list[dict]:
        with self._lock:
            seen = [(ip, ver) for ip, (ts, ver) in self._seen.items()
                    if now - ts <= window]
        return [{"ip": ip, "version": ver} for ip, ver in sorted(seen)]
