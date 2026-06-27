# Installing FleetSign (Raspberry Pi)

This is the deployment guide for whoever provisions the Pi. It is a **one-time
setup**; after it, the day-to-day operator manages everything from the web UI and
never touches the console.

## Prerequisites

- A Raspberry Pi **4, 5, or newer** running **Raspberry Pi OS (Bookworm or newer)
  with the desktop**, auto-logging into the desktop session. The player draws a
  fullscreen window on that session (X11 or Wayland), so a desktop must be running.
- Network access (Ethernet or Wi-Fi). The Pi reaches the web UI over the LAN, and
  — because the Pi has no battery-backed clock — it relies on the network to set
  the time at boot (time-of-day schedules depend on a correct clock).
- `sudo` rights for the install (it installs system packages and a user service).
- Python 3.11+ (shipped with Bookworm).

The installer pulls in the only system dependency, **mpv**, plus `python3-venv`.

## Install

1. Copy this project to `~/fleetsign` on the Pi (e.g. `git clone … ~/fleetsign`, or
   `scp -r` the folder). The path **must** be `~/fleetsign` — the service unit and
   installer assume it.

2. Run the installer:

   ```bash
   bash ~/fleetsign/install.sh
   ```

   It will:
   - `apt-get install -y mpv python3-venv`
   - create a virtualenv at `~/fleetsign/.venv` and `pip install -e ~/fleetsign`
   - copy `systemd/fleetsign.service` to `~/.config/systemd/user/`
   - add a start line to `~/.config/labwc/autostart` so the desktop session
     launches it on every login/reboot (the Pi's `graphical-session.target` is not
     reliably reached for `--user` units, so enabling the unit alone won't autostart)
   - start the service for the current session
   - print the URL to open

3. In a browser on the same network, open **`http://<pi-ip>:8080`**. On first
   visit it redirects to a **setup page** — choose the admin password there. That
   is the only credential; there are no user accounts.

That's it. Add media and configure playback entirely from the web UI.

## Daily use (operator — web UI only)

Everything is at `http://<pi-ip>:8080`:

- **Upload / remove** images and videos (large videos, well over 250 MB, are fine).
- **Reorder** with the ▲/▼ buttons (top-to-bottom is play order).
- **Enable / disable** individual items.
- **Per-image seconds** — how long each image shows (blank = the global default).
- **Schedule** — restrict an item to certain weekdays and a start–end time window.
- **Settings** — default image seconds, mute videos on/off.
- **Controls** — Restart playback, Blank screen / Resume.
- **Maintenance** — Enter maintenance (drops out of fullscreen and pauses so you
  can use the desktop) / Resume signage. You can also press **F12** on the Pi
  itself. A reboot always returns to fullscreen signage.
- **System time** — the page shows the Pi's clock; a red warning appears if the
  clock looks unset, meaning schedules are unreliable until it syncs.
- **Change password.**

## Service management (deployer / admin)

The operator never needs these, but for the admin:

```bash
systemctl --user status fleetsign         # is it running?
systemctl --user restart fleetsign        # restart the whole daemon
systemctl --user stop fleetsign           # stop it
journalctl --user -u fleetsign -f         # live logs (errors, mpv relaunches)
```

The daemon self-heals on two levels: systemd restarts it if the process dies
(`Restart=always`), and the daemon restarts mpv if mpv dies. You should not need
to babysit it.

## Verify the install (manual, on the Pi)

Walk through these once after installing:

- `systemctl --user status fleetsign` shows **active (running)**.
- The **bottom-right corner of the screen** shows `http://<ip>:8080` — that's the
  address to open in a browser (or "FleetSign: no network" if the Pi is offline).
- First browser visit redirects to the setup page; after setting a password the
  manage page loads.
- Upload one image and one video — both play **fullscreen**, in order; the image
  honors its per-item seconds.
- Disable an item → it drops out of rotation within one cycle; re-enable restores it.
- Set a schedule **outside** the current time → the item hides; inside → it shows.
- Upload a video **larger than 250 MB over Wi-Fi** → it completes (no error/timeout)
  and plays.
- The manage page shows the Pi's current time; if you deliberately set the clock to
  a wrong year (`< 2024`), the unsynced-clock warning appears.
- Press **F12** → mpv leaves fullscreen and pauses; press again (or "Resume
  signage") → fullscreen returns. **Reboot** → comes back fullscreen automatically.
- With signage playing, open a terminal or other window over it → the signage
  **stays in front** (it runs always-on-top). If a window can cover it, see the
  always-on-top note under Troubleshooting.
- **Reboot the Pi and log in (or let it auto-login)** → the player starts on its own
  (via `~/.config/labwc/autostart`); you should not need to start it by hand.
- `systemctl --user kill -s SIGKILL fleetsign` → systemd restarts it within ~3 s and
  playback resumes.

## Updating

```bash
cd ~/fleetsign
git pull                      # or copy the new files over
.venv/bin/pip install -e .    # if dependencies changed
systemctl --user restart fleetsign
```

Your media (`~/fleetsign/media/`) and playlist/state (`~/fleetsign/data/`) are not
touched by an update.

## Uninstall

```bash
bash ~/fleetsign/uninstall.sh
```

This stops and removes the service, strips the autostart hook from
`~/.config/labwc/autostart`, and deletes the venv. It **keeps** your media
(`~/fleetsign/media/`) and state/config (`~/fleetsign/data/`), and leaves the apt
packages (`mpv`, `python3-venv`) installed in case other software uses them.

To also wipe your content and the app directory:

```bash
rm -rf ~/fleetsign           # removes media, playlist/state, and config — back up first
# optional: loginctl disable-linger "$USER"
# optional: sudo apt-get remove mpv python3-venv   # only if nothing else uses them
```

## Troubleshooting

- **Player doesn't start after a reboot (but `systemctl --user start fleetsign`
  works).** Autostart is driven by `~/.config/labwc/autostart`, not by
  `systemctl enable` — the Pi's desktop session doesn't reliably reach
  `graphical-session.target` for `--user` units. Confirm the file contains a
  `systemctl --user start fleetsign.service` line (`grep fleetsign ~/.config/labwc/autostart`).
  If it's missing, re-run `install.sh`. Note: if `~/.config/labwc/autostart` exists
  it *replaces* the system default, so it must also launch the panel/wallpaper —
  `install.sh` seeds it from `/etc/xdg/labwc/autostart` for that reason.
- **Black screen / nothing plays.** Check `journalctl --user -u fleetsign -f`. An
  empty playlist (nothing enabled, or everything out of schedule) shows black by
  design. A repeating "player loop error" usually means mpv can't open the display
  — confirm the desktop session is up and the service runs as the desktop user.
- **Schedules fire at the wrong time.** The Pi clock is probably wrong/unsynced
  (no RTC). Confirm the web UI's system-time panel and that NTP is working
  (`timedatectl`). The warning banner flags an obviously-unset clock.
- **Images never advance (stuck on one image).** This indicates an mpv that ignored
  the per-image duration. The player sets it via mpv *properties* to stay
  compatible across mpv versions; if you hit this, capture `mpv --version` and the
  service logs.
- **Video shows a solid blue screen (images are fine).** mpv's hardware decoder
  picked an overlay path the desktop compositor can't display (it logs "cannot load
  libcuda.so.1"). In the web UI open **Settings → Video decoder** and pick
  **auto-copy** (the default) or **no** (pure software); playback restarts
  automatically when you change it.
- **A terminal or other window covers the signage.** The player keeps mpv
  always-on-top by running it under **XWayland** (a native Wayland window cannot
  pin itself on top) together with a labwc window rule. If a window can still sit
  in front: confirm `~/.config/labwc/rc.xml` contains a `windowRule` for `mpv`
  with `allowAlwaysOnTop="yes"` (re-running `install.sh` adds it), then reload the
  compositor with `pkill -HUP labwc` or log out and back in. Check `pgrep -a
  Xwayland` shows XWayland is running; if video regressed at the same time, it is
  the XWayland render path — switch the decoder in **Settings → Video decoder**.
- **Video plays with sound when it shouldn't (or vice-versa).** Toggle "mute videos"
  in Settings (default is muted).
- **Can't reach `http://<pi-ip>:8080`.** Read the address shown in the screen's
  bottom-right corner, or confirm the Pi's IP (`hostname -I`); check the service is
  running and that nothing firewalls port 8080 on the LAN.
- **Large upload fails.** The server accepts up to 4 GiB with a 600 s timeout; a
  failure that large usually means the Pi's SD card is full (`df -h`).
- **A slave isn't mirroring (multi-screen).** Open the slave's own page (read its
  IP off the screen's bottom-right overlay). If it shows a **"waiting for first
  sync"** page, it has never synced — confirm its **sync token** matches the
  master's and that it can reach the master's IP and port. If the master address or
  token was entered wrong, you don't need to re-flash or edit files: on that same
  waiting page expand **"Not connecting? Fix the master address or token…"** and
  correct them, or choose **Become master** to promote this Pi instead. A corrected
  address takes effect on the next poll (~2 minutes, or ~15 s after a failed
  attempt). Once it has synced, log in with the **master password** and check
  **Last sync** and any error there. The master's **Screens & sync** card should
  list the slave within ~2 minutes of it polling.

## Where things live

| Path | What |
|---|---|
| `~/fleetsign/` | the application |
| `~/fleetsign/.venv/` | its Python virtualenv |
| `~/fleetsign/media/` | uploaded images and videos |
| `~/fleetsign/data/manifest.json` | playlist + settings (the master's is the source of truth; a slave's is a synced mirror) |
| `~/fleetsign/data/config.json` | password hash, session secret, host/port, and this Pi's role (`master_url` + `sync_token`) |
| `~/.config/systemd/user/fleetsign.service` | the service unit |

## Multiple screens (master + slaves)

All screens mirror one **master** Pi. Content is managed only on the master's
web UI; every other Pi is a **slave** that pulls and displays the same playlist.

### 1. Master
1. Install as normal (above).
2. Give the master a **fixed IP set statically on the Pi itself**, chosen
   *outside* the router's DHCP pool (OS-level network config — not a signage
   feature). This is the address every screen will point at.
3. Open the master's web UI, set the admin password, and note the **Sync token**
   shown in the "Screens & sync" card.

### 2. First slave
1. Install on a second Pi. Open its web UI once.
2. On the setup screen choose **"This screen joins a master"** and enter the
   master's static IP (e.g. `192.168.1.50:8080`) and the sync token.
3. It restarts as a screen and begins mirroring within ~2 minutes. Each screen
   shows its own `http://<ip>:<port>` in the bottom-right corner.

### 3. More screens — clone the card
Power off the configured slave, **clone its SD card** onto every other Pi, and
boot them. Each clone joins automatically (no setup) because the master address
and token are already on the card. Slaves stay on plain DHCP — only the master
needs a static IP.

### Deletions, decoders, downtime
- Deleting media on the master removes it from every slave on the next sync.
- `hwdec` (video decoder) is **per-Pi** and not synced. To change it on one
  screen, read that screen's IP off its bottom-right overlay, open its page, and
  log in with the master password.
- A slave that was off catches up automatically when it returns.
- A screen's own page (hwdec, re-point, **Become master**) is **password-protected**
  with the *same* password as the master, which the screen receives on its first
  sync. The system keeps **two separate secrets**: the **login password** (web-UI
  access — set on the master, synced to screens) and the **sync token** (authorizes
  a screen to pull content from the master — entered at join/clone). They are
  independent and serve different purposes. Changing the master password
  propagates to screens on their next sync; if you are rotating it because it
  leaked, also `systemctl --user restart fleetsign` on each screen to drop any open
  browser sessions.
- The master's **Screens & sync** card lists screens that have checked in within
  the last 5 minutes — use it to confirm a new slave connected. You can rotate the
  sync token there; every screen must use the same token.

### Failover (master dies)
Screens keep playing their last content. To restore editing:
1. Open any slave's page (read its IP off the screen) and click **Become master**.
2. Assign the master's static IP to that promoted Pi (OS-level).
   No other screen needs changing — they all point at that IP.
3. Before returning the old master to service, open it and **Join master**
   (point it at the static IP) so it rejoins as a screen instead of becoming a
   second master.
