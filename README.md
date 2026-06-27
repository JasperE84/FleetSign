# FleetSign

**Self-hosted signage that mirrors one playlist across every screen. Runs on any Linux desktop — Raspberry Pi, Debian, or Ubuntu.**

![Platform: Linux](https://img.shields.io/badge/platform-Raspberry%20Pi%20%7C%20Debian%20%7C%20Ubuntu-c51a4a)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Tests: 133 passing](https://img.shields.io/badge/tests-133%20passing-success)
![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue)

FleetSign turns a small Linux box — a Raspberry Pi, or any Debian/Ubuntu
mini-PC — and a wall-mounted screen into an unattended, always-on display that
loops your images and videos fullscreen. You manage
everything — what plays, in what order, for how long, and when — from a small
password-protected web page on the same network. There are **no console
commands, no accounts, and nothing to babysit**: it brings itself back up after
a crash, a power cut, or a reboot.

It was built to drive information screens on gym walls, but nothing about it is
gym-specific — it suits any setting that needs a few screens looping the same
content: lobbies, retail, offices, classrooms, events, menus.

---

## Features

**Playback**
- Loops images and videos **fullscreen** via [mpv](https://mpv.io/), in the
  order you set.
- Per-image display duration, with a global default; videos play to their end.
- Mute videos on/off; selectable hardware-decode mode for awkward GPUs.
- Common formats out of the box — images: JPG, PNG, GIF, WebP, BMP; video: MP4,
  MKV, MOV, AVI, WebM, MPG, WMV, FLV, and more.

**Scheduling & curation**
- Enable / disable individual items without deleting them.
- **Time-of-day + weekday schedules** per item (e.g. show this only Mon–Fri,
  09:00–17:00). Out-of-schedule items drop out of the loop automatically.
- Reorder the playlist with up/down controls — top-to-bottom is play order.

**Operations**
- A single password-protected web UI — the **only** control surface. No SSH, no
  accounts, no config files to edit.
- Upload **large videos** (up to 4 GiB) straight from the browser.
- **Blank / resume** the screen and **restart playback** on demand.
- **Maintenance mode** (web button or **F12** on the Pi) drops mpv out of
  fullscreen and pauses so you can use the desktop; **Resume signage** (or F12
  again) brings playback cleanly back to fullscreen, and a reboot always returns
  to fullscreen signage.
- Each screen shows its own `http://<ip>:<port>` address small in the
  bottom-right corner, so you can always find its web UI.
- A clock panel warns when the Pi's time looks unset (it has no battery-backed
  clock), since schedules depend on a correct time.

**Multiple screens (master / slave fleet)**
- Run **one master** that hosts the management UI and **any number of slaves**
  that mirror its playlist and media over the LAN every ~2 minutes.
- Slaves catch up automatically after downtime; deletions on the master
  propagate to every screen.
- Add screens by configuring one slave and **cloning its SD card** — no per-Pi
  setup.
- Promote any slave to master (**Become master**) if the master fails.
- Role is just configuration — there is **one codebase and one install**; a Pi
  with a master address set is a slave, otherwise it's a master.

**Reliability**
- **Two layers of self-healing**: `systemd` restarts the service if the process
  dies, and the service relaunches mpv if mpv dies.
- **Atomic writes** for all state, and a corrupt playlist is backed up and reset
  rather than crashing.
- The player tolerates bad data — one malformed entry can never black out the
  display.
- The signage window stays **always-on-top**, so a stray terminal or dialog
  can't slip in front of the display.

---

## How it works

A single Python process (started by `systemd --user` with `Restart=always`)
hosts three cooperating parts that share one in-process playlist store:

- **Web UI** ([Flask](https://flask.palletsprojects.com/) +
  [Waitress](https://github.com/Pylons/waitress)) — the management surface and,
  on a master, the sync endpoints that slaves pull from.
- **Player** — supervises one persistent mpv window over mpv's JSON IPC socket,
  re-reading the playlist each loop so your edits take effect within the current
  item, and relaunching mpv if it dies.
- **Sync client** (slaves only) — polls the master, validates the manifest,
  downloads new/changed media, and mirrors it locally.

State lives in `data/manifest.json` (playlist + settings) and uploads in
`media/`. A master's manifest is the source of truth; a slave's is a synced
mirror. The sync channel is treated as untrusted and validated at the boundary.

```
            ┌──────────────── Master Pi ────────────────┐
 browser ─▶ │  Web UI ──▶ PlaylistStore ──▶ Player ─▶ mpv │ ─▶ screen
            │                 │                          │
            └─────────────────┼──────────────────────────┘
                              │  HTTP pull (~2 min)
            ┌─────────────────▼──── Slave Pi ───────────┐
            │  SyncClient ─▶ PlaylistStore ─▶ Player ─▶ mpv│ ─▶ screen
            └────────────────────────────────────────────┘
```

---

## Limitations

FleetSign is deliberately a small, simple tool. Know what it does *not* do
before you choose it:

- **One playlist for the whole fleet.** Every screen mirrors the master's single
  playlist — you can't show different content on different screens, or group them.
  Enable/disable and per-item schedules apply fleet-wide, not per screen. To run
  more than one playlist, run a separate master for each (each with its own
  slaves).
- **Fullscreen media only.** It plays one image or video at a time, edge to edge.
  There are **no overlays** — no text/captions, tickers, logos, layout zones,
  transitions, web pages, or live data. If you need a designed layout, this isn't
  it.
- **One screen per device.** Each host drives a single fullscreen display.
- **One shared password, no accounts.** A single admin password gates the whole
  UI; there are no per-user logins, roles, or audit trail.
- **Trusted LAN only, no TLS.** The web UI and the master↔slave sync run over
  plain HTTP (the sync is token-authenticated but not encrypted). Keep it on a
  private network — don't expose it to the internet; put a VPN or reverse proxy
  in front if you need remote access.
- **Manual failover.** If the master goes down, slaves keep playing their last
  sync, but you can't edit anything until you manually promote a slave to master.

---

## Requirements

FleetSign is a plain **Python + mpv** program — nothing in it is Pi-specific, so
it runs on **any modern Linux desktop**:

- A **Linux desktop session** (X11 or Wayland) that **auto-logs in**, in which
  mpv can draw a fullscreen window. The **reference platform is a Raspberry Pi
  4/5 on Raspberry Pi OS (Bookworm) with the desktop**, but a Debian or Ubuntu
  mini-PC works the same way.
- **Python 3.11+** (shipped with Bookworm; your distro's `python3` elsewhere).
- One system dependency, **mpv**.
- **systemd** for the bundled service supervision and autostart — the app itself
  is just a `python -m fleetsign` process, so this is optional if you supervise
  it another way.
- Network access on the LAN. A device with no real-time clock (like a Pi) relies
  on the network to set its time at boot — schedules depend on a correct clock.

Runtime Python dependencies are just `flask` and `waitress`. The bundled
installer pulls in mpv and builds the virtualenv for you on Debian-family
systems (see [Quick start](#quick-start)).

---

## Quick start

`install.sh` is written for **Raspberry Pi OS**: it installs packages with `apt`
*and* wires autostart through the Pi's default **labwc** compositor.

```bash
git clone <this-repo> ~/fleetsign      # the path must be ~/fleetsign
bash ~/fleetsign/install.sh
```

The installer adds `mpv`, creates a virtualenv, installs the service, wires up
labwc autostart, and starts it. Then, from any browser on the same network:

1. Open **`http://<host-ip>:8080`** (the address also appears in the screen's
   bottom-right corner).
2. On first visit you're sent to a **setup page** — choose the admin password.
   That single password is the only credential.
3. Upload media and configure playback entirely from the web UI.

**Plain Debian or Ubuntu** also use `apt`, so the installer's package, venv, and
service steps run fine and it starts the service for the current session — but
their default desktop is **GNOME, not labwc**, so the autostart line it writes to
`~/.config/labwc/autostart` is never read and playback won't return on reboot.
Add an autostart hook for your actual desktop instead — see
[Managing the service](#managing-the-service).

**Non-`apt` distros** (Fedora, Arch, …) skip the installer entirely: install
`mpv` and a Python 3.11 venv with your package manager, `pip install -e
~/fleetsign`, copy `systemd/fleetsign.service` into `~/.config/systemd/user/`,
then add an autostart hook as above.

For the full deployment guide — service management, the master/slave fleet
setup, an on-host verification checklist, updating, and troubleshooting — see
**[INSTALL.md](INSTALL.md)**.

### Multiple screens, in brief

Install normally on the **master** and give it a static IP. On each **slave**,
open its web UI once, choose *"This screen joins a master,"* and enter the
master's address and the **sync token** shown on the master's *Screens & sync*
card. Clone that slave's SD card to add more screens with no further setup. Full
walkthrough (including failover) is in [INSTALL.md → Multiple screens](INSTALL.md#multiple-screens-master--slaves).

---

## Managing the service

FleetSign runs as a `systemd --user` service named `fleetsign`. Day-to-day you
shouldn't need any of this — playback is controlled from the web UI and the
service self-heals — but for the admin on the host:

```bash
systemctl --user start fleetsign      # start it now
systemctl --user restart fleetsign    # restart the daemon (e.g. after an update)
systemctl --user stop fleetsign       # stop it
systemctl --user status fleetsign     # is it running? (fetch status)
journalctl --user -u fleetsign -f     # live logs (errors, mpv relaunches)
```

`systemd` supervises the process and restarts it if it dies (`Restart=always`),
but it does **not** start it at boot — that's the autostart hook below.

### How autostart works (and how to disable it)

On Raspberry Pi OS the desktop session's `graphical-session.target` isn't
reliably reached for `--user` units, so `systemctl --user enable fleetsign`
would **not** launch it on login. Instead, `install.sh` appends a block to the
**labwc compositor's autostart file**, `~/.config/labwc/autostart`, which labwc
runs at session start with the Wayland environment available:

```bash
# ~/.config/labwc/autostart
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY 2>/dev/null
systemctl --user start fleetsign.service
```

The first line hands the running display's environment to the systemd user
manager (so mpv can find the screen); `DISPLAY` is included because mpv renders
through **XWayland** to stay always-on-top. The second starts the supervised unit.
Autostart therefore **depends on this file** — remove the lines and the player
won't come back after a reboot, even though `systemctl --user start fleetsign`
still works by hand.

```bash
# Disable autostart (leave the service installed, just don't launch on boot):
sed -i '/# Start the FleetSign player/,+2d' ~/.config/labwc/autostart

# Re-enable autostart: re-run the installer (it re-adds the block idempotently)
bash ~/fleetsign/install.sh
```

### Other desktops / compositors

The labwc file is specific to Raspberry Pi OS Bookworm's default (Wayland)
session. On a different desktop, add the equivalent lines to **its** autostart
and FleetSign starts the same way. The key step is always: import the session's
display variables into the systemd user manager, then start the unit. On a Wayland
session import **both** `WAYLAND_DISPLAY` and `DISPLAY` (mpv runs under XWayland for
always-on-top); on X11, `DISPLAY`.

| Session | Autostart location | Lines to add |
|---|---|---|
| labwc — Pi OS, Wayland (default) | `~/.config/labwc/autostart` | `systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY`<br>`systemctl --user start fleetsign.service` |
| X11 — openbox / LXDE (older Pi OS) | `~/.config/openbox/autostart` or `~/.config/lxsession/LXDE-pi/autostart` | `systemctl --user import-environment DISPLAY XDG_RUNTIME_DIR`<br>`systemctl --user start fleetsign.service` |
| Any XDG-compliant desktop | `~/.config/autostart/fleetsign.desktop` | `Exec=sh -c 'systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY; systemctl --user start fleetsign.service'` |

The service unit itself is portable across desktops — only the autostart hook
differs.

---

## Development

No Pi or mpv is needed to run the tests — the player's mpv and socket
interactions are dependency-injected, so the suite runs on any platform
(including Windows/CI).

```bash
python -m pytest                         # full suite (~133 tests)
python -m pytest tests/test_store.py -v   # one file
python -m fleetsign --root . --port 8080    # run the daemon locally (needs mpv for real playback)
```

### Project layout

| Path | What |
|---|---|
| `fleetsign/store.py` | `PlaylistStore` — the source of truth (atomic, thread-safe) |
| `fleetsign/player.py` | `PlayerController` — supervises the mpv window over JSON IPC |
| `fleetsign/web.py` | Flask apps — master management UI + slave UI + sync endpoints |
| `fleetsign/sync.py` | `SyncClient` — the slave-side mirror; `manifest_payload` for the master |
| `fleetsign/schedule.py` | weekday + time-window activeness check |
| `fleetsign/model.py` | data model (`MediaItem`, `Schedule`, `Settings`) |
| `fleetsign/config.py` | per-host config + auth; role (`master_url`/`sync_token`) |
| `fleetsign/mpv_ipc.py` | thin mpv JSON-IPC client |
| `fleetsign/__main__.py` | entry point — picks the master or slave app by role |
| `tests/` | the test suite |
| `install.sh` / `systemd/` | one-time deployment (Raspberry Pi OS; `apt` + venv steps also fit Debian/Ubuntu) |

---

## License

Released into the public domain under **[The Unlicense](https://unlicense.org/)**
— see [LICENSE](LICENSE). This is about as permissive as licensing gets:
copy, modify, publish, use, compile, sell, or distribute it for any purpose, with
no conditions and no attribution required.

Every dependency permits this: Flask, Werkzeug, and Jinja2 are BSD-3-Clause,
Waitress is ZPL-2.1, pytest is MIT — all permissive, none copyleft — and mpv (GPL)
runs as a separate process over IPC, so its license does not propagate to this
code.
