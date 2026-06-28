from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    config_path: Path
    media_dir: Path
    data_dir: Path
    session_secret: str
    password_hash: Optional[str]
    host: str
    port: int
    master_url: str = ""
    sync_token: str = ""
    # Serializes save() across threads. On a slave the SyncClient thread writes
    # config (set_password_hash) concurrently with operator actions on Waitress
    # workers; without this they could race the same temp file and os.replace.
    _save_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False)

    @classmethod
    def load_or_create(cls, root: Path, host: str = "0.0.0.0", port: int = 8080) -> "AppConfig":
        root = Path(root)
        media_dir = root / "media"
        data_dir = root / "data"
        media_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "config.json"
        d = {}
        if config_path.exists():
            try:
                d = json.loads(config_path.read_text("utf-8"))
            except (ValueError, OSError) as e:
                logger.warning("config.json unreadable (%s); using defaults", e)
                d = {}
        cfg = cls(
            config_path=config_path,
            media_dir=media_dir,
            data_dir=data_dir,
            session_secret=d.get("session_secret") or secrets.token_hex(32),
            password_hash=d.get("password_hash"),
            host=d.get("host", host),
            port=int(d.get("port", port)),
            master_url=d.get("master_url", ""),
            sync_token=d.get("sync_token") or secrets.token_hex(16),
        )
        if not config_path.exists() or not d.get("session_secret") or not d.get("sync_token"):
            cfg.save()
        return cfg

    def is_configured(self) -> bool:
        return bool(self.password_hash)

    def set_password(self, password: str) -> None:
        # Hash outside the lock -- it's a deliberately slow KDF and there's no
        # reason to block other config writers during it. Only the field
        # assignment + persist need to be serialized.
        password_hash = generate_password_hash(password)
        with self._save_lock:
            self.password_hash = password_hash
            self._save_locked()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def is_slave(self) -> bool:
        return bool(self.master_url)

    def join_master(self, master_url: str, sync_token: str) -> None:
        # Two fields mutated + persisted as ONE critical section. Without the lock
        # around the assignments, two concurrent config writes could interleave
        # and persist a mixed state (master_url from one call, sync_token from
        # another) -- a real corruption that nothing later converges back.
        with self._save_lock:
            self.master_url = master_url
            self.sync_token = sync_token
            self._save_locked()
        # Log the address but never the token, hash, or session secret.
        logger.info("joined master %s (now running as a screen)", master_url)

    def become_master(self) -> None:
        with self._save_lock:
            self.master_url = ""
            self._save_locked()
        logger.info("promoted to master")

    def set_sync_token(self, token: str) -> None:
        with self._save_lock:
            self.sync_token = token
            self._save_locked()
        logger.info("sync token changed")

    def set_password_hash(self, password_hash: Optional[str]) -> None:
        # Store a pre-computed hash (e.g. one synced from the master) without
        # re-hashing. This is the UI-login credential, distinct from sync_token.
        with self._save_lock:
            self.password_hash = password_hash
            self._save_locked()

    def save(self) -> None:
        with self._save_lock:
            self._save_locked()

    def _save_locked(self) -> None:
        # Caller MUST already hold _save_lock. Every config mutation runs its
        # field updates AND this persist inside that one lock, so: each field's
        # writes are atomic across concurrent setters; the JSON snapshot is taken
        # under the lock (a snapshot built beforehand leaves a window where a
        # concurrent save completes and is then overwritten by the stale one --
        # a lost update); and the shared config.json.tmp has a single writer, so
        # no garbled temp or os.replace racing into a FileNotFoundError. The lock
        # is a plain (non-reentrant) Lock: public methods acquire exactly once and
        # call this helper, which never re-acquires. Same atomic temp+replace
        # discipline as the store's _save for manifest.json.
        d = {
            "session_secret": self.session_secret,
            "password_hash": self.password_hash,
            "host": self.host,
            "port": self.port,
            "master_url": self.master_url,
            "sync_token": self.sync_token,
        }
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2), "utf-8")
        os.replace(tmp, self.config_path)
