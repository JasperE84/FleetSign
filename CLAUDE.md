# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A digital-signage player for Raspberry Pis (model 4, 5, or newer) running the desktop session. It loops images and videos fullscreen on wall displays at a gym and is managed entirely through a small password-protected web UI. The current implementation is the **Python package `fleetsign/`**.

It runs as a **master/slave fleet**: one **master** Pi hosts the full web UI where the operator manages media; any number of **slave** Pis run a reduced UI plus a background `SyncClient` that mirrors the master's playlist + media files over HTTP every ~2 min. **Role is config-driven, not a separate build** — a non-empty `master_url` makes a Pi a slave (`config.is_slave()`), empty makes it the master. A lone Pi is just a master with no slaves.

This Python package replaces an earlier **Bash implementation** (feh/VLC driven by cron). That legacy code has been removed from the repository; all current work lives in `fleetsign/`.

## Commands

Everything runs from the repo root. No Pi or mpv is needed for the test suite — the player's mpv interaction is dependency-injected.

```bash
python -m pytest                       # full suite (≈124 tests)
python -m pytest tests/test_store.py -v # one file
python -m pytest tests/test_player.py::test_select_next_cycles_active_only -v  # one test
python -m fleetsign --root . --port 8080  # run the daemon locally (needs mpv on PATH for real playback)
```

There is no separate lint/build step; `pyproject.toml` configures pytest (`pythonpath=["."]`, `testpaths=["tests"]`). Runtime deps are `flask` + `waitress`; the only system dep is `mpv`. Deployment is via `install.sh` + `systemd/fleetsign.service` (a `systemd --user` unit) — see `INSTALL.md`.

## Architecture (the big picture)

One Python process (`python -m fleetsign`, started by systemd `--user` with `Restart=always`) hosts cooperating parts that share an in-process `PlaylistStore`. `__main__.py` picks the app **by role**: a master runs `web.create_app`; a slave runs `web.create_slave_app` *and* starts a `SyncClient` thread.

- **`store.py` — `PlaylistStore`**: the source of truth. Loads/saves `data/manifest.json` atomically (temp file + `os.replace`), thread-safe via a single lock, recovers from a corrupt manifest by backing it up and resetting. Array order *is* play order. Media files live in `media/`. `replace_from_master` overwrites the playlist from a synced manifest while preserving the slave's local `hwdec`.
- **`player.py` — `PlayerController`** (own thread): launches and supervises **one persistent mpv window**, driving it over mpv's JSON IPC socket (`mpv_ipc.py`). Each loop iteration re-reads the store, picks the next *active* item (`select_next` → `schedule.is_active`), and plays it; advances on an image-duration timeout or a video `end-file`. It relaunches mpv if mpv dies (the second self-healing layer). Maintenance mode (F12) un-fullscreens and pauses. Identical on master and slave.
- **`web.py` — `create_app` (master) / `create_slave_app` (slave)**: Flask apps served by Waitress. This is the **only** control surface — there are no user-facing console scripts. Single-password session auth (`config.py`), first-run setup, then: the **master** does media CRUD + ordering + per-item duration + scheduling, settings, playback/maintenance, `/status`, uploads, a screens panel, and token-guarded read-only **sync endpoints** (`/sync/manifest`, `/sync/media/<name>`) that slaves pull. The **slave** UI is reduced — local `hwdec`/connection settings, `become-master` — and relies on the master for media and login password.
- **`sync.py` — `SyncClient`** (slave-only, own thread): polls the master's `/sync/manifest`, validates the whole payload, downloads new/changed media (streamed to `.tmp` → atomic `os.replace`, size-checked against the manifest), applies it via `store.replace_from_master`, prunes files the master no longer serves, and syncs the UI password hash. Loops every 105–135 s (jittered) on success, 15 s backoff on error, and **never lets the loop die**. `manifest_payload` builds the master side (omits `hwdec`; omits items whose file is missing). `FleetTracker` is the master's in-memory registry of recently-polling slave IPs for the screens panel.
- **`validate.py`**: shared input validators (e.g. `positive_seconds`) used by both web routes and the sync boundary.

Edits made in the master web UI mutate the store; the master's player picks them up within the current item's duration, and slaves mirror them on their next sync. This design replaces the entire legacy stack: a shell display loop, a cron-based file-change watcher, an hourly "restart VLC" cron, and a `.desktop` autostart.

Read multiple files together to see a flow: an upload is `web.upload` → `store.add_media` → (next loop) `player._play_item` → `mpv_ipc` → mpv; a slave mirror is `SyncClient.sync_once` → master `/sync/manifest` + `/sync/media` → `store.replace_from_master` → (next loop) `player._play_item`.

## Conventions and constraints to preserve

These are deliberate decisions; keep them when editing:

- **Web-only control.** Never add a console script for the operator. `install.sh` is the only console step, and it's a one-time deployer action.
- **mpv via properties, not positional `loadfile` options.** `_play_item` sets `mute`/`image-display-duration` with `set_property` *before* a plain `loadfile … replace`. This is intentional: mpv ≥ 0.38 changed `loadfile`'s argument order, so positional per-file options get silently dropped. Don't "simplify" it back.
- **The player thread must never crash on bad data.** `is_active` swallows unparseable schedules (returns inactive) and the `_run` loop has a backoff on every failure path, so one bad manifest entry can't black-screen the display. Web routes validate user input (numeric durations, `HH:MM` times, weekday ints) and flash+redirect rather than 500 — follow that pattern for any new input.
- **Maintenance state is in-memory only** (`_maintenance`), reset on every mpv (re)launch, so a reboot always returns to fullscreen. Don't persist it.
- **Atomic writes** for both `manifest.json` and `config.json` (temp + `os.replace`); never write them in place.
- **`tempfile.tempdir`** is set to the data dir in `main()` *only* (not `build()`), so large uploads spill to the SD card instead of a tmpfs `/tmp` — and tests that call `build()` don't mutate global temp state.
- **Portability across mpv/Pi versions:** hardware decode is the web Settings' `hwdec` (default `auto-copy`; plain `auto` blue-screens video on the Pi compositor — it logs "cannot load libcuda.so.1"). Changing it in the UI relaunches mpv. No per-model config.
- **The sync channel is untrusted; validate at the boundary.** A slave treats the master's manifest as hostile input (token-auth only, no TLS): `sync_once` validates every field, requires each filename to be a safe basename (`_is_safe_media_name`), and **rejects the whole manifest rather than skipping a bad item** — a skipped item becomes a fileless playlist entry that also shields a stale local copy from pruning. Master-served media is size-checked; truncated downloads are dropped (`.tmp` deleted), never shown.
- **`hwdec` is per-Pi and never synced.** It's local to each display's hardware: `manifest_payload` omits it and `replace_from_master` preserves the slave's local value. The synced UI password hash (the human login) is kept distinct from the `sync_token` that authenticates the pull.
- **Role lives only in `config.json`** (`master_url`/`sync_token`); there's one codebase and one entry point. Don't fork master/slave into separate packages or persist role anywhere else.

## Testing approach

mpv and sockets are injected so tests run on any platform (including Windows/CI):
`PlayerController` takes `launcher`/`connector`/`clock` callables; tests pass a `FakeIpc` and never start real mpv. `mpv_ipc` tests use `socket.socketpair()`; the real `connect_unix`/`AF_UNIX` path only runs on the Pi. When adding behavior, prefer testing the pure piece (e.g. `select_next`, `is_active`, store methods, route handlers via Flask's test client) rather than the live loop.

## Reference docs

- `README.md` — short overview + quick install (single Pi and multi-Pi fleet).
- `INSTALL.md` — full deployment/ops guide (prerequisites, service management, master/slave setup, on-Pi verification checklist, troubleshooting).
