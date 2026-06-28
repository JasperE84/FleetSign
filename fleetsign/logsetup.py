from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Union

# Syslog/kernel priority codes that systemd's journal stream parser recognises
# when they prefix a line (see sd-daemon(3): SD_ERR="<3>", SD_WARNING="<4>", ...).
# Mapping Python levels onto them lets `journalctl -p warning` and priority
# colouring work without pulling in the python-systemd dependency.
_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


def _priority_for(levelno: int) -> int:
    # Pick the most severe (lowest-numbered) threshold the record clears, so a
    # custom level between two standard ones still maps to a sensible priority.
    for level in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO):
        if levelno >= level:
            return _PRIORITY[level]
    return _PRIORITY[logging.DEBUG]


class JournaldPriorityFormatter(logging.Formatter):
    """Prefix each record with a `<N>` syslog priority so journald tags the line
    with the right level. No timestamp — journald records its own. Only the first
    line of a multi-line record (e.g. a traceback) carries the prefix; that's
    enough for journalctl to file the whole entry under that priority."""

    def __init__(self) -> None:
        super().__init__("%(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return f"<{_priority_for(record.levelno)}>" + super().format(record)


def _resolve_level(level: Optional[Union[str, int]]) -> int:
    if isinstance(level, int):
        return level
    raw = level if level is not None else os.environ.get("FLEETSIGN_LOG_LEVEL")
    if not raw:
        return logging.INFO
    # getLevelName returns the int for a known name (e.g. "DEBUG"), else the
    # string "Level <x>" — so a typo in the unit file falls back to INFO rather
    # than crashing the daemon at startup.
    resolved = logging.getLevelName(str(raw).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(level: Optional[Union[str, int]] = None) -> None:
    """Configure root logging for the daemon. Call once from main() — never from
    build() — mirroring the tempfile.tempdir convention, so tests that build the
    app don't mutate global logging state (and pytest's caplog keeps working).

    Logs to stderr. Under systemd the journal captures it; we detect that via
    $JOURNAL_STREAM (which systemd sets on a service whose stderr is wired to the
    journal) and emit `<N>`-prefixed lines so journalctl knows each line's
    priority. In a plain terminal we emit a timestamped, human-readable line."""
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM"):
        handler.setFormatter(JournaldPriorityFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    # Idempotent: drop existing handlers so a second call (or a re-import) can't
    # double-print every line.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(_resolve_level(level))

    # Waitress/Werkzeug propagate to root; keep their per-connection chatter out
    # of our INFO stream (we still see their warnings/errors).
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
