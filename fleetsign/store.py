from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .model import MediaItem, Schedule, Settings, classify, new_id


def safe_unlink(path: Path) -> bool:
    """Delete a file, ignoring it being already gone or unremovable. Returns
    True only if a file was actually removed."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


class PlaylistStore:
    def __init__(self, manifest_path: Path, media_dir: Path):
        self.manifest_path = Path(manifest_path)
        self.media_dir = Path(media_dir)
        self._lock = threading.Lock()
        self._settings = Settings()
        self._media: list[MediaItem] = []
        self._load()

    def _load(self) -> None:
        if not self.manifest_path.exists():
            self._save()
            return
        try:
            d = json.loads(self.manifest_path.read_text("utf-8"))
            self._settings = Settings.from_dict(d.get("settings", {}))
            self._media = [MediaItem.from_dict(m) for m in d.get("media", [])]
        except (ValueError, KeyError, OSError, TypeError, AttributeError):
            # TypeError/AttributeError too: a valid-JSON but mistyped manifest
            # (e.g. schedule a string, days a non-iterable) makes from_dict raise
            # those, not ValueError -- and an uncaught one here crashes __init__
            # into a systemd boot-loop instead of recovering. (sync.py's manifest
            # validation catches the same pair for the same reason.)
            backup = self.manifest_path.with_suffix(f".bad-{int(time.time())}.json")
            try:
                os.replace(self.manifest_path, backup)
            except OSError:
                pass
            self._settings = Settings()
            self._media = []
            self._save()

    def _save(self) -> None:
        d = {"settings": self._settings.to_dict(),
             "media": [m.to_dict() for m in self._media]}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2), "utf-8")
        os.replace(tmp, self.manifest_path)

    def _find(self, item_id: str) -> Optional[MediaItem]:
        return next((m for m in self._media if m.id == item_id), None)

    def list_media(self) -> list[MediaItem]:
        with self._lock:
            return list(self._media)

    def get_settings(self) -> Settings:
        with self._lock:
            return Settings(self._settings.default_image_duration,
                            self._settings.muted, self._settings.hwdec)

    def missing_files(self) -> set[str]:
        with self._lock:
            return {m.id for m in self._media if not (self.media_dir / m.filename).exists()}

    def add_media(self, filename: str) -> MediaItem:
        with self._lock:
            item = MediaItem(id=new_id(), filename=filename, type=classify(filename))
            self._media.append(item)
            self._save()
            return item

    def remove_media(self, item_id: str) -> None:
        with self._lock:
            item = self._find(item_id)
            if not item:
                return
            self._media.remove(item)
            self._save()
        safe_unlink(self.media_dir / item.filename)

    def set_enabled(self, item_id: str, enabled: bool) -> None:
        with self._lock:
            item = self._find(item_id)
            if item:
                item.enabled = enabled
                self._save()

    def set_duration(self, item_id: str, seconds: Optional[float]) -> None:
        with self._lock:
            item = self._find(item_id)
            if item:
                item.image_duration = seconds
                self._save()

    def set_schedule(self, item_id: str, schedule: Optional[Schedule]) -> None:
        with self._lock:
            item = self._find(item_id)
            if item:
                item.schedule = schedule
                self._save()

    def reorder(self, item_id: str, direction: str) -> None:
        with self._lock:
            idx = next((i for i, m in enumerate(self._media) if m.id == item_id), None)
            if idx is None:
                return
            swap = idx - 1 if direction == "up" else idx + 1
            if 0 <= swap < len(self._media):
                self._media[idx], self._media[swap] = self._media[swap], self._media[idx]
                self._save()

    def set_settings(self, default_image_duration: float, muted: bool,
                     hwdec: str = "auto-copy") -> None:
        with self._lock:
            self._settings = Settings(default_image_duration, muted, hwdec)
            self._save()

    def replace_from_master(self, default_image_duration: float, muted: bool,
                            media: list[MediaItem]) -> None:
        with self._lock:
            self._media = list(media)
            self._settings = Settings(default_image_duration, muted,
                                      self._settings.hwdec)
            self._save()
