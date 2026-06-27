from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash


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
            except (ValueError, OSError):
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
        self.password_hash = generate_password_hash(password)
        self.save()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def is_slave(self) -> bool:
        return bool(self.master_url)

    def join_master(self, master_url: str, sync_token: str) -> None:
        self.master_url = master_url
        self.sync_token = sync_token
        self.save()

    def become_master(self) -> None:
        self.master_url = ""
        self.save()

    def set_sync_token(self, token: str) -> None:
        self.sync_token = token
        self.save()

    def set_password_hash(self, password_hash: Optional[str]) -> None:
        # Store a pre-computed hash (e.g. one synced from the master) without
        # re-hashing. This is the UI-login credential, distinct from sync_token.
        self.password_hash = password_hash
        self.save()

    def save(self) -> None:
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
