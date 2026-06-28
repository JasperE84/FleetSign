# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A digital-signage player for Raspberry Pis (4/5+) on the desktop session: loops images/videos fullscreen on gym wall displays, managed entirely through a small password-protected web UI. Current implementation is the Python package **`fleetsign/`** (it replaced a now-removed Bash/cron + feh/VLC stack).

Runs as a **master/slave fleet**: the master hosts the full web UI for managing media; slaves run a reduced UI plus a background `SyncClient` mirroring the master's playlist + media over HTTP every ~2 min. **Role is config-driven, not a separate build** — a non-empty `master_url` makes a Pi a slave (`config.is_slave()`), empty makes it the master. A lone Pi is just a master with no slaves.

## Commands

Run from the repo root. No Pi or mpv is needed for tests — mpv interaction is dependency-injected.

```bash
python -m pytest                       # full suite (≈208 tests)
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

## Writing docs and prose (no AI prose)

Keep a human-authored voice in user-facing text: Markdown docs (`README.md`, `INSTALL.md`), code comments, and commit bodies. No tell-tale AI prose.

- **Punctuation.** No em-dashes or en-dashes. Use normal punctuation instead: a colon for a definition or list lead-in, parentheses for an aside, a comma or semicolon for a clause break, a plain hyphen for ranges (`Mon-Fri`, `09:00-17:00`). Compound-word hyphens (`always-on-top`) and Markdown `---` rules are fine.
- **No decorative bold.** Keep bold for structure only (section headers, glossary/list-item term labels); drop mid-sentence emphasis like `**only**` or `**fullscreen**`.
- **No AI flourishes.** Avoid "deliberately"/"purpose-built", "it's not just X, it's Y", ornamental rule-of-three lists, and editorializing section-enders. State the fact and move on.

## Architecture

One process (`python -m fleetsign`, run by systemd `--user`, `Restart=always`) hosts cooperating parts sharing an in-process `PlaylistStore`. `__main__.py` picks the app by role: master → `web.create_app`; slave → `web.create_slave_app` + a `SyncClient` thread.

- **`store.py` — `PlaylistStore`**: source of truth. Atomic load/save of `data/manifest.json` (temp + `os.replace`), one lock, recovers a corrupt manifest by backing it up and resetting. Array order *is* play order; media in `media/`. `replace_from_master` overwrites the playlist from a synced manifest but preserves local `hwdec`.
- **`player.py` — `PlayerController`** (own thread): supervises one persistent mpv window over its JSON IPC socket (`mpv_ipc.py`). Each loop re-reads the store, picks the next *active* item (`select_next` → `schedule.is_active`), plays it, advances on image-duration timeout or video `end-file`, and relaunches mpv if it dies. Maintenance (F12) un-fullscreens + pauses. Identical on master and slave.
- **`web.py` — `create_app` / `create_slave_app`**: Flask served by Waitress; the **only** control surface (no operator console scripts). Single-password session auth, first-run setup. **Master**: media CRUD/order/duration/scheduling, settings, playback/maintenance, `/status`, uploads, screens panel, token-guarded read-only sync endpoints (`/sync/manifest`, `/sync/media/<name>`). **Slave**: reduced UI (local `hwdec`/connection, `become-master`); relies on the master for media + login password. Its waiting/status pages surface the sync error so a mis-join is diagnosable on-screen.
- **`sync.py` — `SyncClient`** (slave-only thread): polls `/sync/manifest`, validates the whole payload, downloads changed media (streamed to `.tmp` → atomic replace, size-checked), applies via `store.replace_from_master`, prunes dropped files, syncs the UI password hash, and records the master's advertised `version`. Loops 105–135 s (jittered) on success / 15 s on error, and **never dies**. `manifest_payload` builds the master side (adds `version`; omits `hwdec` and items with a missing file). `FleetTracker` is the master's in-memory registry of recently-polling slaves (IP + the `X-Sync-Version` they sent) for the screens panel.
- **`validate.py`**: shared validators (e.g. `positive_seconds`) used by web routes and the sync boundary.

Flows: upload = `web.upload` → `store.add_media` → (next loop) `player._play_item` → `mpv_ipc` → mpv. Slave mirror = `SyncClient.sync_once` → master `/sync/*` → `store.replace_from_master` → (next loop) `player._play_item`.

## Conventions and constraints to preserve

Deliberate decisions — keep them when editing. Each links to its rationale in [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md); read that entry before changing the behavior, because most guard against a specific bug that isn't visible in the code.

- **Web-only control.** Never add an operator console script; `install.sh` is the only console step (one-time deploy). ([DD-1](docs/DESIGN_DECISIONS.md#dd-1))
- **mpv via properties, not positional `loadfile` options.** Set `mute`/`image-display-duration` with `set_property` *before* a plain `loadfile … replace`; positional per-file options are silently dropped on mpv ≥ 0.38. Don't revert it. ([DD-2](docs/DESIGN_DECISIONS.md#dd-2))
- **Player thread must never crash on bad data.** `is_active` treats unparseable schedules as inactive; `_run` backs off on every failure path. Web routes validate input (durations, `HH:MM`, weekday ints) and flash+redirect, never 500 — follow that for new input. ([DD-3](docs/DESIGN_DECISIONS.md#dd-3))
- **Maintenance state is in-memory only** (`_maintenance`), reset on every mpv (re)launch so a reboot returns to fullscreen. Don't persist it. ([DD-4](docs/DESIGN_DECISIONS.md#dd-4))
- **Exiting maintenance relaunches mpv; never re-fullscreen the live one.** Exiting (web "Resume" or F12) signals a relaunch (`restart_playback`), not `set_property fullscreen True` — re-fullscreening races mpv's video-output recreation and hangs it on the Pi compositor (dead IPC, black screen; mpv #3678/#9704). Same reason `_teardown_mpv` SIGKILLs an mpv that ignores SIGTERM. ([DD-5](docs/DESIGN_DECISIONS.md#dd-5))
- **Atomic writes** for `manifest.json` and `config.json` (temp + `os.replace`); never in place. ([DD-8](docs/DESIGN_DECISIONS.md#dd-8))
- **`tempfile.tempdir`** is set to the data dir in `main()` *only* (not `build()`), so large uploads spill to the SD card not tmpfs `/tmp`, and `build()` tests don't mutate global temp state. ([DD-9](docs/DESIGN_DECISIONS.md#dd-9))
- **Logging is configured in `main()` only** (`logsetup.configure_logging`), never `build()`. Log via `logging.getLogger(__name__)` to stderr → journald; keep hot loops quiet (DEBUG + latch repeated failures); never log secrets (passwords, hashes, tokens, session secret). ([DD-10](docs/DESIGN_DECISIONS.md#dd-10))
- **Portability:** `hwdec` is the web Settings value (default `auto-copy`; plain `auto` blue-screens video on the Pi compositor). Changing it relaunches mpv. No per-model config. ([DD-6](docs/DESIGN_DECISIONS.md#dd-6))
- **Always-on-top via XWayland + focus guard, on purpose.** `default_launcher` forces XWayland via a dead `WAYLAND_DISPLAY` socket (`fleetsign-no-wayland`) — don't merely unset it (libwayland falls back to `wayland-0`). mpv runs `--ontop` with a stable title; `ForegroundGuard` reasserts it every 10 s via `wmctrl` + `xdotool` (disabled in blank/maintenance). ([DD-7](docs/DESIGN_DECISIONS.md#dd-7))
- **The sync channel is untrusted; validate at the boundary.** A slave treats the manifest as hostile (token-auth, no TLS): `sync_once` validates every field, requires safe-basename filenames (`_is_safe_media_name`), and **rejects the whole manifest rather than skipping a bad item**. Media is size-checked; truncated downloads are dropped. ([DD-12](docs/DESIGN_DECISIONS.md#dd-12))
- **`hwdec` is per-Pi, never synced** (`manifest_payload` omits it; `replace_from_master` preserves it). The synced UI password hash (human login) is distinct from `sync_token` (authenticates the pull). ([DD-13](docs/DESIGN_DECISIONS.md#dd-13))
- **Joining a slave needs only `master_url` + `sync_token`, never a login password** (it arrives from the master on first sync; `is_configured()` False until then → waiting page). Keep the master-influenced `sync.last_error` HTML-escaped (Jinja autoescape — never `|safe`). ([DD-14](docs/DESIGN_DECISIONS.md#dd-14))
- **Role lives only in `config.json`** (`master_url`/`sync_token`); one codebase, one entry point. Don't fork master/slave into separate packages or persist role elsewhere. ([DD-11](docs/DESIGN_DECISIONS.md#dd-11))
- **The manifest JSON is the master↔slave compatibility contract; keep schema changes additive.** No version gate — `__version__`/`X-Sync-Version` only make skew *visible*; mixed versions are normal and **must not** break. New fields: additive only (read via `.get()` + defaults), validated identically in the web route *and* at the sync boundary (`sync_once`), and bump `__version__`. ([DD-15](docs/DESIGN_DECISIONS.md#dd-15))

## Testing

mpv and sockets are injected so tests run anywhere (Windows/CI): `PlayerController` takes `launcher`/`connector`/`clock`; tests pass a `FakeIpc` and never start mpv. `ForegroundGuard` injects its runner/clock (no real `xdotool`/`wmctrl`). `mpv_ipc` tests use `socket.socketpair()`; the real `AF_UNIX` path runs only on the Pi. Prefer testing the pure piece (`select_next`, `is_active`, store methods, routes via Flask's test client) over the live loop.

## Reference docs

- `docs/DESIGN_DECISIONS.md` — the *why* behind the conventions above: rationale, failure modes, and mpv ticket references (DD-1 … DD-15).
- `README.md` — overview + quick install (single Pi and fleet).
- `INSTALL.md` — full deployment/ops guide (prerequisites, service management, master/slave setup, verification checklist, troubleshooting).
