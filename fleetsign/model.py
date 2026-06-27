from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import Optional

IMAGE_EXTS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "bmp", "gif", "webp"})
VIDEO_EXTS: frozenset[str] = frozenset({"mp4", "m4v", "mkv", "mov", "avi", "webm", "mpg", "mpeg", "wmv", "flv"})


def ext_of(filename: str) -> str:
    return PurePath(filename).suffix.lower().lstrip(".")


def is_supported(filename: str) -> bool:
    e = ext_of(filename)
    return e in IMAGE_EXTS or e in VIDEO_EXTS


def classify(filename: str) -> str:
    e = ext_of(filename)
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    raise ValueError(f"Unsupported file type: {filename}")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Schedule:
    days: list[int]
    start: str
    end: str

    def to_dict(self) -> dict:
        return {"days": list(self.days), "start": self.start, "end": self.end}

    @staticmethod
    def from_dict(d: dict) -> "Schedule":
        return Schedule(days=list(d.get("days", [])), start=d["start"], end=d["end"])


@dataclass
class MediaItem:
    id: str
    filename: str
    type: str
    enabled: bool = True
    image_duration: Optional[float] = None
    schedule: Optional[Schedule] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "type": self.type,
            "enabled": self.enabled,
            "image_duration": self.image_duration,
            "schedule": self.schedule.to_dict() if self.schedule else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "MediaItem":
        sched = d.get("schedule")
        return MediaItem(
            id=d["id"],
            filename=d["filename"],
            type=d["type"],
            enabled=d.get("enabled", True),
            image_duration=d.get("image_duration"),
            schedule=Schedule.from_dict(sched) if sched else None,
        )


# mpv --hwdec choices offered in the web UI. "auto-copy" is the safe default on a
# Raspberry Pi (plain "auto" can blue-screen video); "no" forces software decode.
HWDEC_CHOICES = ("auto-copy", "no", "auto")


@dataclass
class Settings:
    default_image_duration: float = 8.0
    muted: bool = True
    hwdec: str = "auto-copy"

    def to_dict(self) -> dict:
        return {
            "default_image_duration": self.default_image_duration,
            "muted": self.muted,
            "hwdec": self.hwdec,
        }

    @staticmethod
    def from_dict(d: dict) -> "Settings":
        return Settings(
            default_image_duration=d.get("default_image_duration", 8.0),
            muted=d.get("muted", True),
            hwdec=d.get("hwdec", "auto-copy"),
        )
