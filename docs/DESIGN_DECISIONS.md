# Design Decisions

Why FleetSign is built the way it is. `CLAUDE.md` carries the terse "do this / never that" rules an agent must follow inline; this file is the reasoning behind them. If you are about to change one of these behaviors, read its entry first: most guard against a specific bug or failure mode that is not obvious from the code.

Each entry records the decision (the rule), why it exists, and where useful what breaks if it is reverted plus pointers into the code. IDs (DD-N) are stable; `CLAUDE.md` links to them.

## Index

Control surface
- [DD-1: Web-only control](#dd-1)

Player and mpv lifecycle
- [DD-2: mpv via properties, not positional loadfile options](#dd-2)
- [DD-3: Player thread must never crash on bad data](#dd-3)
- [DD-4: Maintenance state is in-memory only](#dd-4)
- [DD-5: Exiting maintenance relaunches mpv; never re-fullscreen the live one](#dd-5)
- [DD-6: hwdec is a per-Pi web setting](#dd-6)
- [DD-7: Always-on-top via XWayland plus a focus guard](#dd-7)

State, persistence, and process setup
- [DD-8: Atomic writes for manifest.json and config.json](#dd-8)
- [DD-9: tempfile.tempdir is set to the data dir in main() only](#dd-9)
- [DD-10: Logging is configured in main() only](#dd-10)

Fleet: roles, sync, and the manifest contract
- [DD-11: Role lives only in config.json](#dd-11)
- [DD-12: The sync channel is untrusted; validate at the boundary](#dd-12)
- [DD-13: hwdec is per-Pi, never synced](#dd-13)
- [DD-14: Joining a slave needs only master_url + sync_token](#dd-14)
- [DD-15: The manifest JSON is the compatibility contract; keep it additive](#dd-15)

## Control surface

<a id="dd-1"></a>
### DD-1: Web-only control

**Decision.** The web UI is the only control surface. Never add an operator console script or a CLI management command. `install.sh` is the single console step, run once at deploy.

**Why.** The product is managed by non-technical staff over the LAN through a password-protected web UI, and the Pis run headless on gym walls. A second control path (console scripts) would split the source of truth, bypass the auth and input validation the web routes enforce, and create operations nobody can perform from where the screens actually are.

## Player and mpv lifecycle

<a id="dd-2"></a>
### DD-2: mpv via properties, not positional loadfile options

**Decision.** In `_play_item`, set `mute` and `image-display-duration` with `set_property` before issuing a plain `loadfile … replace`. Do not pass per-file options positionally on the `loadfile` command.

**Why.** mpv 0.38 changed the argument order of `loadfile`. Per-file options passed in the old positional slot are now silently dropped: no error, the file just plays with default duration and audio.

**If reverted.** Image durations and muting stop applying, with no log line or crash to point at the cause.

**See also.** `player.py` `_play_item`.

<a id="dd-3"></a>
### DD-3: Player thread must never crash on bad data

**Decision.** The player loop tolerates bad input rather than throwing. `is_active` treats an unparseable schedule as inactive; `_run` backs off on every failure path instead of propagating. Web routes validate input (durations, `HH:MM` times, weekday integers) and respond with flash + redirect, never a 500.

**Why.** The player runs unattended on a wall display with no operator present. A crash there is a black screen until the next systemd restart, and a 500 in the web UI is an error page a non-technical user cannot act on. Bad data should degrade to "skip that item" or "reject that form field," never to a stopped process.

**How to apply.** For any new input, validate at the route, default or skip on the player side, and make sure no parse failure can escape as an exception into the loop.

<a id="dd-4"></a>
### DD-4: Maintenance state is in-memory only

**Decision.** The maintenance flag (`_maintenance`) lives in memory only and is reset on every mpv (re)launch. Do not persist it to config or the manifest.

**Why.** Maintenance (F12) is a transient "someone is standing at the screen with a keyboard" state. Persisting it would mean a Pi that lost power mid-maintenance boots back into a paused, un-fullscreened window with nobody there to resume it. Resetting on launch guarantees a reboot always returns to normal fullscreen playback.

<a id="dd-5"></a>
### DD-5: Exiting maintenance relaunches mpv; never re-fullscreen the live one

**Decision.** Entering maintenance un-fullscreens and pauses the running mpv (cheap, reversible). Exiting maintenance (web "Resume" or F12) signals a full relaunch via `restart_playback`. It does not call `set_property fullscreen True` on the live mpv. Relatedly, `_teardown_mpv` sends SIGKILL to an mpv that ignores SIGTERM.

**Why.** Re-fullscreening the live mpv recreates its video-output window, and the `loadfile` that immediately follows lands in the middle of that recreation. On the Pi compositor this is a race: the IPC socket dies (`BrokenPipeError`), the screen goes black, and mpv hangs (mpv issues #3678 and #9704). A fresh mpv instead sequences window-create then decode in the same order as a clean boot, which is reliable. The SIGKILL fallback exists so a stuck old mpv cannot leave a stale black window sitting behind the relaunched one.

**If reverted.** Intermittent black screen and a hung player on maintenance exit, visible only on the real Pi compositor and not reproducible in tests.

**See also.** `player.py` `restart_playback`, `_teardown_mpv`.

<a id="dd-6"></a>
### DD-6: hwdec is a per-Pi web setting

**Decision.** Hardware decoding is controlled by a single web Settings value, `hwdec`, defaulting to `auto-copy`. Changing it relaunches mpv. There is no per-model or per-file decode configuration.

**Why.** Plain `auto` blue-screens video on the Pi compositor, so the default has to be `auto-copy`. Exposing one setting instead of per-model config keeps the UI usable by non-technical staff and the fleet uniform, while still letting a single screen be adjusted if its hardware misbehaves. `hwdec` is also the one setting that is never synced between Pis: see [DD-13](#dd-13).

<a id="dd-7"></a>
### DD-7: Always-on-top via XWayland plus a focus guard

**Decision.** The player forces XWayland and reasserts window stacking on a timer, rather than trusting a native Wayland client to stay on top. `default_launcher` sets `WAYLAND_DISPLAY` to a dead socket name (`fleetsign-no-wayland`) to force XWayland; do not merely unset the variable. mpv runs with `--ontop` and a stable window title, and every 10 s `ForegroundGuard` reasserts top-most via `wmctrl` and `xdotool` (disabled while blanked or in maintenance). `install.sh` injects an `allowAlwaysOnTop` labwc rule.

**Why.** A native Wayland client cannot pin itself on top: there is no protocol for it, and mpv's `--ontop` is a no-op under Wayland. Forcing XWayland is what makes `--ontop` work at all. Unsetting `WAYLAND_DISPLAY` is not enough, because libwayland then falls back to `wayland-0`; the variable has to point at a socket that does not exist. The 10 s guard covers compositors that occasionally raise other surfaces. The labwc rule is harmless on 0.8.4 (the version in the field) and helps on compositors that honor it.

## State, persistence, and process setup

<a id="dd-8"></a>
### DD-8: Atomic writes for manifest.json and config.json

**Decision.** Write `manifest.json` and `config.json` by writing a temp file and `os.replace`-ing it into place. Never write in place.

**Why.** A wall-display Pi can lose power at any moment (no UPS). An in-place write that is interrupted leaves a half-written, unparseable file. `os.replace` is atomic on the same filesystem, so a reader always sees either the old complete file or the new complete file. The store keeps a corrupt-manifest recovery path as a backstop, but atomic writes keep it from being needed.

<a id="dd-9"></a>
### DD-9: tempfile.tempdir is set to the data dir in main() only

**Decision.** `main()` sets `tempfile.tempdir` to the data directory. `build()` does not.

**Why.** Two reasons. Large media uploads stream through a temp file; if that landed in the default `/tmp` (tmpfs, RAM) on a Pi, a big upload could exhaust memory, so pointing temp at the data dir spills it to the SD card instead. And setting it only in `main()` keeps `build()` (used by tests) from mutating global process state, so tests stay isolated.

<a id="dd-10"></a>
### DD-10: Logging is configured in main() only

**Decision.** `logsetup.configure_logging` is called from `main()` only, never `build()`. Log through `logging.getLogger(__name__)` to stderr. Never log secrets.

**Why.** Same isolation reason as [DD-9](#dd-9): configuring logging in `build()` would fight pytest's `caplog` fixture. stderr goes to journald under systemd, where a `<N>` priority prefix is added so `journalctl -p` filtering works without the `python-systemd` dependency. The default level is INFO, overridable with `FLEETSIGN_LOG_LEVEL`. Hot loops stay quiet (per-item playback and no-op syncs log at DEBUG; repeated failures latch to once-per-transition) so journald is not flooded. Secrets (passwords, hashes, tokens, the session secret) are never logged.

## Fleet: roles, sync, and the manifest contract

<a id="dd-11"></a>
### DD-11: Role lives only in config.json

**Decision.** A Pi's role is determined entirely by `config.json`: a non-empty `master_url` (with `sync_token`) makes it a slave, empty makes it the master. One codebase, one entry point. Do not fork master and slave into separate packages, and do not persist role anywhere else.

**Why.** A single artifact that behaves differently by config is far simpler to build, deploy, and reason about than two packages that drift apart. It also makes "promote a slave to master" (`become-master`) a config edit rather than a redeploy. `__main__.py` reads the role once and selects `create_app`, or `create_slave_app` plus a `SyncClient` thread.

<a id="dd-12"></a>
### DD-12: The sync channel is untrusted; validate at the boundary

**Decision.** A slave treats the manifest it pulls from the master as hostile input. `sync_once` validates every field, requires safe-basename filenames (`_is_safe_media_name`), and rejects the whole manifest if any item is bad rather than skipping that item. Downloaded media is size-checked; a truncated download is dropped, never displayed.

**Why.** The channel is token-authenticated but has no TLS, so on a shared LAN the payload could be tampered with or spoofed. Path-unsafe filenames are a directory-traversal risk and are rejected outright. Rejecting the whole manifest (rather than skipping one bad item) matters for a subtle reason: a skipped item would become a fileless playlist entry, and that entry would also shield a stale local copy of the file from being pruned. Failing whole keeps the slave on its last-good state instead of a quietly corrupted partial one.

**How to apply.** Any new manifest field is hostile input here and must be validated at this boundary, identically to its web-route validation. See [DD-15](#dd-15).

<a id="dd-13"></a>
### DD-13: hwdec is per-Pi, never synced

**Decision.** `hwdec` is the one setting that does not propagate over sync. `manifest_payload` omits it, and `replace_from_master` preserves the slave's local value while overwriting the rest of the playlist.

**Why.** Decode capability is hardware-specific (a Pi 4 and a Pi 5 may want different values), so it stays local even though everything else about the playlist is mirrored from the master. Note also that the synced UI password hash (for human login) is a different secret from `sync_token` (which authenticates the pull); the two are not interchangeable.

<a id="dd-14"></a>
### DD-14: Joining a slave needs only master_url + sync_token

**Decision.** Joining a Pi to a fleet requires only `master_url` and `sync_token`, never a login password. The `/setup` join branch ignores any password entered, and `setup.html` hides the field in join mode. The slave receives its login password (as a hash) from the master on first successful sync.

**Why.** The login password is a fleet-wide secret owned by the master; making operators set it per-slave would invite mismatches and is redundant. Until the first sync lands, `is_configured()` is False and the slave shows a waiting page. The waiting and status pages render `sync.last_error` through `friendly_sync_error` with the raw detail beneath it, so a mis-join is diagnosable on the screen itself. That raw string is influenced by the master, so it must stay HTML-escaped: rely on Jinja autoescape, never `|safe`. `last_attempt` (updated on every attempt) versus `last_sync` (last success) is what timestamps the never-yet-synced waiting page.

<a id="dd-15"></a>
### DD-15: The manifest JSON is the compatibility contract; keep it additive

**Decision.** The manifest JSON sent from master to slave is the compatibility contract between versions, and schema changes to it must be additive. There is no version negotiation and no version gate.

**Why there is no gate.** `__version__` is advertised in `manifest_payload` and echoed back by slaves in the `X-Sync-Version` header purely to make version skew visible (the slave status banner via `version_mismatch`, and the master's screens panel). It does not block or transform anything. A master upgrade never updates its slaves: deployment is out-of-band (git plus `install.sh`), never code-over-sync, because the channel is untrusted (see [DD-12](#dd-12)). So mixed versions across a fleet are normal and must keep working. This is also why a true `BREAKING CHANGE` commit should be rare here.

**Rules for changing the manifest shape:**

- **Additive only.** Add new fields; never remove, rename, or repurpose a field a deployed peer relies on. Slaves read every field through `.get()` with defaults (`MediaItem.from_dict`, `Settings.from_dict`, `payload.get(...)`), so an old slave ignores unknown keys and a new slave fills in missing ones. Additive changes let any mix of master and slave versions run with zero downtime, upgraded in any order.
- **A non-additive change fails safe, it does not crash.** Per [DD-12](#dd-12), a slave rejects the whole manifest on any unexpected or invalid field, keeps showing its last-good content, and surfaces the sync error on-screen until it is upgraded. Rely on this only as a safety net; prefer additive plus a transition window.
- **Validate a new field in both places, identically.** A new field is user input in the web route (flash + redirect on bad data) and hostile input at the sync boundary (`sync_once`, which rejects the whole manifest, never skips an item). Mirror the two checks, the way `_is_valid_schedule` mirrors the route's schedule validation.
- **Bump `__version__`** (`fleetsign/__init__.py`) for any change operators should see reflected across the fleet, so the skew indicators stay meaningful.
