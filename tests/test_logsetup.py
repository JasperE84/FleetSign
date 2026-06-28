import logging

import pytest

from fleetsign.logsetup import (JournaldPriorityFormatter, _resolve_level,
                                configure_logging)


@pytest.fixture(autouse=True)
def _restore_root_logging():
    # configure_logging mutates the global root logger; snapshot and restore it so
    # these tests don't leak handlers/level into the rest of the suite (caplog).
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _record(level):
    return logging.LogRecord("fleetsign.x", level, __file__, 1, "hi", None, None)


def test_resolve_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("FLEETSIGN_LOG_LEVEL", raising=False)
    assert _resolve_level(None) == logging.INFO


def test_resolve_level_from_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("FLEETSIGN_LOG_LEVEL", "debug")
    assert _resolve_level(None) == logging.DEBUG


def test_resolve_level_invalid_env_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("FLEETSIGN_LOG_LEVEL", "verbose")  # not a real level
    assert _resolve_level(None) == logging.INFO


def test_explicit_level_overrides_env(monkeypatch):
    monkeypatch.setenv("FLEETSIGN_LOG_LEVEL", "debug")
    assert _resolve_level("warning") == logging.WARNING


def test_journald_formatter_prefixes_priority_by_level():
    fmt = JournaldPriorityFormatter()
    assert fmt.format(_record(logging.DEBUG)).startswith("<7>")
    assert fmt.format(_record(logging.INFO)).startswith("<6>")
    assert fmt.format(_record(logging.WARNING)).startswith("<4>")
    assert fmt.format(_record(logging.ERROR)).startswith("<3>")
    assert fmt.format(_record(logging.CRITICAL)).startswith("<2>")


def test_configure_logging_uses_priority_formatter_under_journald(monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123456")
    configure_logging("info")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JournaldPriorityFormatter)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_human_formatter_without_journald(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    configure_logging("debug")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert not isinstance(handlers[0].formatter, JournaldPriorityFormatter)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    configure_logging("info")
    configure_logging("info")
    assert len(logging.getLogger().handlers) == 1
