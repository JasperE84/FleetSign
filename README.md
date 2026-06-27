# FleetSign

**Self-hosted signage that mirrors one playlist across every screen. Raspberry Pi compatible.**

![Platform: Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi%204%2F5%2B-c51a4a)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![Tests: 118 passing](https://img.shields.io/badge/tests-118%20passing-success)
![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue)

FleetSign turns a Raspberry Pi and a wall-mounted screen into an unattended,
always-on display that loops your images and videos fullscreen. You manage
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
  fullscreen and pauses so you can use the desktop; a reboot always returns to
  fullscreen signage.
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

## Requirements

- **Raspberry Pi 4, 5, or newer** running **Raspberry Pi OS (Bookworm or newer)
  with the desktop**, auto-logging into the desktop session (the player draws a
  fullscreen window on X11 or Wayland).
- Network access on the LAN. The Pi has no real-time clock, so it relies on the
  network to set the time at boot — schedules depend on it.
- **Python 3.11+** (shipped with Bookworm).
- One system dependency, **mpv**, installed automatically by the installer.

Runtime Python dependencies are just `flask` and `waitress`.

---

## Quick start (on the Pi)

```bash
git clone <this-repo> ~/fleetsign      # the path must be ~/fleetsign
bash ~/fleetsign/install.sh
```

The installer adds `mpv`, creates a virtualenv, installs the service, wires up
desktop autostart, and starts it. Then, from any browser on the same network:

1. Open **`http://<pi-ip>:8080`** (the address also appears in the screen's
   bottom-right corner).
2. On first visit you're sent to a **setup page** — choose the admin password.
   That single password is the only credential.
3. Upload media and configure playback entirely from the web UI.

For the full deployment guide — service management, the master/slave fleet
setup, an on-Pi verification checklist, updating, and troubleshooting — see
**[INSTALL.md](INSTALL.md)**.

### Multiple screens, in brief

Install normally on the **master** and give it a static IP. On each **slave**,
open its web UI once, choose *"This screen joins a master,"* and enter the
master's address and the **sync token** shown on the master's *Screens & sync*
card. Clone that slave's SD card to add more screens with no further setup. Full
walkthrough (including failover) is in [INSTALL.md → Multiple screens](INSTALL.md#multiple-screens-master--slaves).

---

## Development

No Pi or mpv is needed to run the tests — the player's mpv and socket
interactions are dependency-injected, so the suite runs on any platform
(including Windows/CI).

```bash
python -m pytest                         # full suite (~118 tests)
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
| `fleetsign/config.py` | per-Pi config + auth; role (`master_url`/`sync_token`) |
| `fleetsign/mpv_ipc.py` | thin mpv JSON-IPC client |
| `fleetsign/__main__.py` | entry point — picks the master or slave app by role |
| `tests/` | the test suite |
| `install.sh` / `systemd/` | one-time Pi deployment |

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
