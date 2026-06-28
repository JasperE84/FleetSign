# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A digital-signage player for Raspberry Pis (4/5+) on the desktop session: loops images/videos fullscreen on gym wall displays, managed entirely through a small password-protected web UI. Current implementation is the Python package **`fleetsign/`** (it replaced a now-removed Bash/cron + feh/VLC stack).

Runs as a **master/slave fleet**: the master hosts the full web UI for managing media; slaves run a reduced UI plus a background `SyncClient` mirroring the master's playlist + media over HTTP every ~2 min. **Role is config-driven, not a separate build** — a non-empty `master_url` makes a Pi a slave (`config.is_slave()`), empty makes it the master. A lone Pi is just a master with no slaves.

## Commands

Run from the repo root. No Pi or mpv is needed for tests — mpv interaction is dependency-injected.

```bash
python -m pytest                       # full suite (≈159 tests)
python -m pytest tests/test_store.py -v # one file
python -m pytest tests/test_player.py::test_select_next_cycles_active_only -v  # one test
python -m fleetsign --root . --port 8080  # run the daemon locally (needs mpv on PATH)
```

No separate lint/build step; `pyproject.toml` configures pytest (`pythonpath=["."]`, `testpaths=["tests"]`). Runtime deps: `flask` + `waitress`; system deps: `mpv`, `xdotool`, `wmctrl`. Deploy via `install.sh` + `systemd/fleetsign.service` (a `systemd --user` unit) — see `INSTALL.md`.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org): `type(scope): summary`.

- **Type** — one of `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `chore`, `ci`, `revert`.
- **Scope** (optional but preferred) — the area touched, named after the module: `sync`, `player`, `web`, `store`, `mpv`, `config`, `install`. E.g. `feat(sync): …`.
- **Summary** — imperative mood, lower-case, no trailing period, ≤ ~72 chars ("add X", not "added"/"adds X").
- **Body** (optional, after a blank line) — wrap ~72 cols; explain *why*, not the *what* the diff already shows. Reference issues / upstream mpv tickets where relevant.
- **Breaking change** — add `!` after type/scope (`feat(store)!: …`) and/or a `BREAKING CHANGE:` footer. The manifest is kept additive on purpose (see *Conventions* below), so a true break should be rare.
- **No tooling trailers** — omit `Co-Authored-By` / agent-session lines.

Examples (from this repo): `feat(sync): centralize sync authorization for endpoints` · `fix(player): avoid re-fullscreening live mpv on maintenance exit` · `docs: clarify master/slave version-skew behavior`.

## Architecture

One process (`python -m fleetsign`, run by systemd `--user`, `Restart=always`) hosts cooperating parts sharing an in-process `PlaylistStore`. `__main__.py` picks the app by role: master → `web.create_app`; slave → `web.create_slave_app` + a `SyncClient` thread.

- **`store.py` — `PlaylistStore`**: source of truth. Atomic load/save of `data/manifest.json` (temp + `os.replace`), one lock, recovers a corrupt manifest by backing it up and resetting. Array order *is* play order; media in `media/`. `replace_from_master` overwrites the playlist from a synced manifest but preserves local `hwdec`.
- **`player.py` — `PlayerController`** (own thread): supervises one persistent mpv window over its JSON IPC socket (`mpv_ipc.py`). Each loop re-reads the store, picks the next *active* item (`select_next` → `schedule.is_active`), plays it, advances on image-duration timeout or video `end-file`, and relaunches mpv if it dies. Maintenance (F12) un-fullscreens + pauses. Identical on master and slave.
- **`web.py` — `create_app` / `create_slave_app`**: Flask served by Waitress; the **only** control surface (no operator console scripts). Single-password session auth, first-run setup. **Master**: media CRUD/order/duration/scheduling, settings, playback/maintenance, `/status`, uploads, screens panel, token-guarded read-only sync endpoints (`/sync/manifest`, `/sync/media/<name>`). **Slave**: reduced UI (local `hwdec`/connection, `become-master`); relies on the master for media + login password. Its waiting/status pages surface the sync error so a mis-join is diagnosable on-screen.
- **`sync.py` — `SyncClient`** (slave-only thread): polls `/sync/manifest`, validates the whole payload, downloads changed media (streamed to `.tmp` → atomic replace, size-checked), applies via `store.replace_from_master`, prunes dropped files, syncs the UI password hash, and records the master's advertised `version`. Loops 105–135 s (jittered) on success / 15 s on error, and **never dies**. `manifest_payload` builds the master side (adds `version`; omits `hwdec` and items with a missing file). `FleetTracker` is the master's in-memory registry of recently-polling slaves (IP + the `X-Sync-Version` they sent) for the screens panel.
- **`validate.py`**: shared validators (e.g. `positive_seconds`) used by web routes and the sync boundary.

Flows: upload = `web.upload` → `store.add_media` → (next loop) `player._play_item` → `mpv_ipc` → mpv. Slave mirror = `SyncClient.sync_once` → master `/sync/*` → `store.replace_from_master` → (next loop) `player._play_item`.

## Conventions and constraints to preserve

Deliberate decisions — keep them when editing:

- **Web-only control.** Never add an operator console script; `install.sh` is the only console step (one-time deploy).
- **mpv via properties, not positional `loadfile` options.** `_play_item` sets `mute`/`image-display-duration` with `set_property` *before* a plain `loadfile … replace`: mpv ≥ 0.38 changed `loadfile`'s arg order, so positional per-file options are silently dropped. Don't revert it.
- **Player thread must never crash on bad data.** `is_active` treats unparseable schedules as inactive; `_run` backs off on every failure path. Web routes validate input (durations, `HH:MM`, weekday ints) and flash+redirect, never 500 — follow that for new input.
- **Maintenance state is in-memory only** (`_maintenance`), reset on every mpv (re)launch so a reboot returns to fullscreen. Don't persist it.
- **Exiting maintenance relaunches mpv; never re-fullscreen the live one.** Entering un-fullscreens + pauses the running mpv (cheap); exiting (web "Resume" or F12) signals a relaunch (`restart_playback`), not `set_property fullscreen True`. Re-fullscreening recreates mpv's video-output window and the immediate `loadfile` lands mid-recreation — a race that hangs mpv on the Pi compositor (dead IPC → `BrokenPipeError`, black screen; mpv #3678/#9704). A fresh mpv sequences window-create → decode like boot. Same reason `_teardown_mpv` SIGKILLs an mpv that ignores SIGTERM (no stale black window behind the relaunch).
- **Atomic writes** for `manifest.json` and `config.json` (temp + `os.replace`); never in place.
- **`tempfile.tempdir`** is set to the data dir in `main()` *only* (not `build()`), so large uploads spill to the SD card not tmpfs `/tmp`, and `build()` tests don't mutate global temp state.
- **Logging is configured in `main()` only** (`logsetup.configure_logging`), never `build()` — same reason as `tempfile.tempdir` (keep `caplog` clean). Log via `logging.getLogger(__name__)` to stderr → journald; under systemd a `<N>` priority prefix is added so `journalctl -p` works (no `python-systemd` dep). Default INFO, override with `FLEETSIGN_LOG_LEVEL`. Keep hot loops quiet (per-item playback + no-op syncs at DEBUG; repeated failures latch to once-per-transition). Never log secrets (passwords, hashes, tokens, session secret).
- **Portability:** `hwdec` is the web Settings value (default `auto-copy`; plain `auto` blue-screens video on the Pi compositor). Changing it relaunches mpv. No per-model config.
- **Always-on-top via XWayland + focus guard, on purpose.** A native Wayland client can't pin itself on top (no protocol; `--ontop` no-ops), so `default_launcher` sets `WAYLAND_DISPLAY` to a dead socket (`fleetsign-no-wayland`) to force XWayland — don't merely unset it (libwayland falls back to `wayland-0`). mpv runs `--ontop` with a stable title; every 10 s `ForegroundGuard` reasserts it via `wmctrl` + `xdotool` (disabled in blank/maintenance). `install.sh` injects an `allowAlwaysOnTop` labwc rule (harmless on 0.8.4).
- **The sync channel is untrusted; validate at the boundary.** A slave treats the manifest as hostile (token-auth, no TLS): `sync_once` validates every field, requires safe-basename filenames (`_is_safe_media_name`), and **rejects the whole manifest rather than skipping a bad item** (a skipped item becomes a fileless entry that also shields a stale local copy from pruning). Media is size-checked; truncated downloads are dropped, never shown.
- **`hwdec` is per-Pi, never synced** (`manifest_payload` omits it; `replace_from_master` preserves it). The synced UI password hash (human login) is distinct from `sync_token` (authenticates the pull).
- **Joining a slave needs only `master_url` + `sync_token`, never a login password.** The `/setup` join branch ignores the password (and `setup.html` hides it in join mode) — the slave gets its password from the master on first sync (`is_configured()` False until then → waiting page). The waiting/status pages render `sync.last_error` via `friendly_sync_error` with the raw detail underneath; that raw string is master-influenced, so keep it HTML-escaped (Jinja autoescape — never `|safe`). `last_attempt` (every attempt, vs `last_sync` = last success) timestamps the never-synced waiting page.
- **Role lives only in `config.json`** (`master_url`/`sync_token`); one codebase, one entry point. Don't fork master/slave into separate packages or persist role elsewhere.
- **The manifest JSON is the master↔slave compatibility contract; keep schema changes additive.** There is no version negotiation and **no version gate** — `__version__` is advertised in `manifest_payload` and echoed by slaves via the `X-Sync-Version` header purely so skew is *visible* (slave status banner via `version_mismatch`; master screens panel). A master upgrade never updates its slaves (deploy is out-of-band git/`install.sh`, never code-over-sync — the channel is untrusted), so mixed versions are normal and **must not** break. When you change the manifest shape, take this into account:
  - **Additive only.** Add new fields; never remove/rename/repurpose one a deployed peer relies on. Slaves read every field through `.get()` + defaults (`MediaItem.from_dict`/`Settings.from_dict`, `payload.get(...)`), so an old slave ignores unknown keys and a new slave defaults missing ones. Additive changes let any master/slave version mix run with zero downtime, updated in any order.
  - **A non-additive change is a fail-safe freeze, not a crash.** A slave rejects the *whole* manifest on any unexpected/invalid field (it treats the channel as hostile), keeps showing its last-good content, and surfaces the sync error on-screen — until it's upgraded. Don't rely on this; prefer additive + a transition window.
  - **Validate a new field in both places, identically.** It's user input in the web route (flash+redirect on bad data) *and* hostile input at the sync boundary (`sync_once`, which rejects the whole manifest — never skips an item). Mirror the validation, like `_is_valid_schedule` mirrors the route's schedule check.
  - **Bump `__version__`** (`fleetsign/__init__.py`) for any change operators should see reflected across the fleet, so the skew indicators are meaningful.

## Testing

mpv and sockets are injected so tests run anywhere (Windows/CI): `PlayerController` takes `launcher`/`connector`/`clock`; tests pass a `FakeIpc` and never start mpv. `ForegroundGuard` injects its runner/clock (no real `xdotool`/`wmctrl`). `mpv_ipc` tests use `socket.socketpair()`; the real `AF_UNIX` path runs only on the Pi. Prefer testing the pure piece (`select_next`, `is_active`, store methods, routes via Flask's test client) over the live loop.

## Reference docs

- `README.md` — overview + quick install (single Pi and fleet).
- `INSTALL.md` — full deployment/ops guide (prerequisites, service management, master/slave setup, verification checklist, troubleshooting).
