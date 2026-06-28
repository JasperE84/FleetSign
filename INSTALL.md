# Installing FleetSign (Raspberry Pi)

This is the deployment guide for whoever provisions the Pi as a digital signage
(narrowcasting) player. It is a one-time setup; after it, the day-to-day operator
manages everything from the web UI and never touches the console.

## Prerequisites

- A Raspberry Pi 4, 5, or newer running Raspberry Pi OS (Bookworm or newer)
  with the desktop, auto-logging into the desktop session. The player draws a
  fullscreen window on that session (X11 or Wayland), so a desktop must be running.
- Network access (Ethernet or Wi-Fi). The Pi reaches the web UI over the LAN, and
  because the Pi has no battery-backed clock it relies on the network to set
  the time at boot (time-of-day schedules depend on a correct clock).
- A login user with `sudo` rights. Run the installer as that normal
  desktop user, not with `sudo`; it self-elevates for the `apt` step only, and
  everything else (the venv, the `--user` service, the labwc config) must be
  owned by your desktop user.
- Python 3.11+ (shipped with Bookworm).

The installer pulls in the system dependencies mpv, xdotool, and wmctrl,
plus `python3-venv`.

## Install

1. Fetch this project to `~/fleetsign` on the Pi (or `scp -r` the folder). The
   path must be `~/fleetsign`; the service unit and installer assume it.

   ```bash
   git clone https://github.com/JasperE84/FleetSign.git ~/fleetsign
   ```

2. Run the installer as your normal desktop user (not with `sudo`; it
   self-elevates for the `apt` step only):

   ```bash
   bash ~/fleetsign/install.sh
   ```

   It will:
   - `apt-get install -y mpv python3-venv xdotool wmctrl`
   - create a virtualenv at `~/fleetsign/.venv` and `pip install -e ~/fleetsign`
   - copy `systemd/fleetsign.service` to `~/.config/systemd/user/`
   - on labwc (Raspberry Pi OS's default compositor) add a start line to
     `~/.config/labwc/autostart` so the desktop session launches it on every
     login/reboot (the Pi's `graphical-session.target` is not reliably reached for
     `--user` units, so enabling the unit alone won't autostart); on other desktops
     it skips this step and prints a note instead
   - start the service for the current session
   - print the URL to open

3. In a browser on the same network, open `http://<pi-ip>:8080`. On first
   visit it redirects to a setup page; choose the admin password there. That
   is the only credential; there are no user accounts.

That's it. Add media and configure playback entirely from the web UI.

## Set the time and time zone (required for schedules)

Each player decides what to show from its own clock, so the date, time, and time
zone must be correct on the master and on every slave. The scheduler picks which
images and videos play in which time slots on which days; if a screen's clock or
time zone is wrong, that screen runs the schedule against the wrong time, so
items appear in the wrong slots or on the wrong days, or drop out of the loop
entirely.

A Raspberry Pi has no CMOS/RTC battery, so it does not keep time while powered
off; it resyncs from an NTP time server on every boot. Each Pi must therefore
have a reachable NTP server set, or its clock (and every schedule) starts wrong
after a reboot. Raspberry Pi OS syncs through `systemd-timesyncd`; to point at a
specific server, set the `NTP=` line in `/etc/systemd/timesyncd.conf` and run
`sudo systemctl restart systemd-timesyncd`.

On a Raspberry Pi, the time zone and time settings live under `raspi-config`. Run
it from a shell:

```bash
sudo raspi-config
```

Choose Localisation Options → Timezone, select yours, then reboot. On other
systems use your distro's tool (e.g. `sudo timedatectl set-timezone Europe/Amsterdam`,
and confirm sync with `timedatectl`). The web UI's system-time panel shows each
screen's clock and warns when it looks unset, so you can verify it afterward.

## Daily use (operator, web UI only)

Everything is at `http://<pi-ip>:8080`:

- **Upload / remove** images and videos (large videos, well over 250 MB, are fine).
- **Reorder** with the ▲/▼ buttons (top-to-bottom is play order).
- **Enable / disable** individual items.
- **Per-image seconds**: how long each image shows (blank = the global default).
- **Schedule**: restrict an item to certain weekdays and a start-end time window.
- **Settings**: default image seconds, mute videos on/off.
- **Controls**: Restart playback, Blank screen / Resume.
- **Maintenance**: Enter maintenance (drops out of fullscreen and pauses so you
  can use the desktop) / Resume signage. You can also press F12 on the Pi
  itself. A reboot always returns to fullscreen signage.
- **System time**: the page shows the Pi's clock; a red warning appears if the
  clock looks unset, meaning schedules are unreliable until it syncs.
- **Change password.**

## Service management (deployer / admin)

The operator never needs these, but for the admin:

```bash
systemctl --user status fleetsign         # is it running?
systemctl --user restart fleetsign        # restart the whole daemon
systemctl --user stop fleetsign           # stop it
journalctl --user -u fleetsign -f         # live logs (errors, mpv relaunches)
journalctl --user -u fleetsign -p warning # only warnings and errors
```

The daemon self-heals on two levels: systemd restarts it if the process dies
(`Restart=always`), and the daemon restarts mpv if mpv dies. You should not need
to babysit it.

### Log verbosity

The daemon logs to the journal at INFO by default: startup, the master/slave
role, mpv launches/relaunches, maintenance and blank toggles, uploads, logins,
sync results, and corrupt-manifest/sync warnings. Because levels are tagged
(`-p warning`, `-p err`) you can filter to just the problems.

To see much more (per-item playback, "no changes" syncs, repeated errors), raise
the level to debug via an environment variable:

```bash
systemctl --user edit fleetsign           # opens a drop-in override
# add these two lines, save, and exit:
#   [Service]
#   Environment=FLEETSIGN_LOG_LEVEL=debug
systemctl --user restart fleetsign        # apply
journalctl --user -u fleetsign -f         # watch the verbose logs
```

(Alternatively, uncomment the `Environment=FLEETSIGN_LOG_LEVEL=debug` line in
`systemd/fleetsign.service` before installing.) Valid levels are `debug`, `info`,
`warning`, `error`; an unrecognised value falls back to `info`. Set it back to
`info` (or remove the override) once you're done; debug is chatty on short image
durations.

## Verify the install (manual, on the Pi)

Walk through these once after installing:

- `systemctl --user status fleetsign` shows active (running).
- The bottom-right corner of the screen shows `http://<ip>:8080`; that's the
  address to open in a browser (or "FleetSign: no network" if the Pi is offline).
- First browser visit redirects to the setup page; after setting a password the
  manage page loads.
- Upload one image and one video; both play fullscreen, in order; the image
  honors its per-item seconds.
- Disable an item → it drops out of rotation within one cycle; re-enable restores it.
- Set a schedule outside the current time → the item hides; inside → it shows.
- Upload a video larger than 250 MB over Wi-Fi → it completes (no error/timeout)
  and plays.
- The manage page shows the Pi's current time; if you deliberately set the clock to
  a wrong year (`< 2024`), the unsynced-clock warning appears.
- Press F12 → mpv leaves fullscreen and pauses; press again (or "Resume
  signage") → fullscreen returns. Reboot → comes back fullscreen automatically.
- With signage playing, open a terminal or other window over it → the signage
  stays in front (it runs always-on-top). If a window can cover it, see the
  always-on-top note under Troubleshooting.
- Reboot the Pi and log in (or let it auto-login) → the player starts on its own
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
`~/.config/labwc/autostart`, and deletes the venv. It keeps your media
(`~/fleetsign/media/`) and state/config (`~/fleetsign/data/`), and leaves the apt
packages (`mpv`, `python3-venv`) installed in case other software uses them.
It also leaves `xdotool` and `wmctrl` installed for the same reason.

To also wipe your content and the app directory:

```bash
rm -rf ~/fleetsign           # removes media, playlist/state, and config; back up first
# optional: loginctl disable-linger "$USER"
# optional: sudo apt-get remove mpv python3-venv xdotool wmctrl   # only if nothing else uses them
```

## Troubleshooting

- **Player doesn't start after a reboot (but `systemctl --user start fleetsign`
  works).** Autostart is driven by `~/.config/labwc/autostart`, not by
  `systemctl enable`; the Pi's desktop session doesn't reliably reach
  `graphical-session.target` for `--user` units. Confirm the file contains a
  `systemctl --user start fleetsign.service` line (`grep fleetsign ~/.config/labwc/autostart`).
  If it's missing, re-run `install.sh`. Note: if `~/.config/labwc/autostart` exists
  it *replaces* the system default, so it must also launch the panel/wallpaper;
  `install.sh` seeds it from `/etc/xdg/labwc/autostart` for that reason.
- **Black screen / nothing plays.** Check `journalctl --user -u fleetsign -f`. An
  empty playlist (nothing enabled, or everything out of schedule) shows black by
  design. A repeating "player loop error" usually means mpv can't open the display;
  confirm the desktop session is up and the service runs as the desktop user.
- **Schedules fire at the wrong time.** The Pi clock is probably wrong/unsynced
  (no RTC). Confirm the web UI's system-time panel and that NTP is working
  (`timedatectl`). The warning banner flags an obviously-unset clock.
- **Images never advance (stuck on one image).** This indicates an mpv that ignored
  the per-image duration. The player sets it via mpv *properties* to stay
  compatible across mpv versions; if you hit this, capture `mpv --version` and the
  service logs.
- **Video shows a solid blue screen (images are fine).** mpv's hardware decoder
  picked an overlay path the desktop compositor can't display (it logs "cannot load
  libcuda.so.1"). In the web UI open Settings → Video decoder and pick
  auto-copy (the default) or no (pure software); playback restarts
  automatically when you change it.
- **A terminal or other window covers the signage.** The player keeps mpv
  always-on-top by running it under XWayland (a native Wayland window cannot
  pin itself on top) together with a labwc window rule and a 10-second foreground
  guard that re-raises and re-activates mpv while signage is active. If a window
  can still sit in front: confirm `xdotool` and `wmctrl` are installed
  (`command -v xdotool wmctrl`), `pgrep -a Xwayland` shows XWayland is running,
  and `xdotool search --name '^FleetSign Signage$'` finds the mpv window. The
  guard is intentionally disabled in maintenance mode. If video regressed at the
  same time, it is the XWayland render path; switch the decoder in
  Settings → Video decoder.
- **Video plays with sound when it shouldn't (or vice-versa).** Toggle "mute videos"
  in Settings (default is muted).
- **Can't reach `http://<pi-ip>:8080`.** Read the address shown in the screen's
  bottom-right corner, or confirm the Pi's IP (`hostname -I`); check the service is
  running and that nothing firewalls port 8080 on the LAN.
- **Forgot the admin password (locked out of the web UI).** The change-password
  control needs you logged in, so reset it on the device. Stop the service
  (`systemctl --user stop fleetsign`), edit `data/config.json` and set
  `"password_hash": null` (the only field to touch), then start it again
  (`systemctl --user start fleetsign`). The next browser visit returns to the setup
  page to choose a new password. A slave needs no reset: it re-receives the master's
  password on the next sync, so fix the password on the master.
- **Large upload fails.** The server accepts up to 4 GiB with a 600 s timeout; a
  failure that large usually means the Pi's SD card is full (`df -h`).
- **A slave isn't mirroring (multi-screen).** Open the slave's own page (read its
  IP off the screen's bottom-right overlay). If it shows a "waiting for first
  sync" page, it has never synced. That page now shows the connection error
  in plain language, e.g. *connection refused* (master off, or wrong address/port),
  *timed out* (master unreachable on the network), or *authentication failed* (the
  sync token is wrong), with the raw error underneath, so you can tell the
  causes apart at a glance. Correct the master address or token right there by
  expanding "Not connecting? Fix the master address or token…", or choose
  Become master to promote this Pi instead, with no re-flash or file edits needed.
  A corrected address takes effect on the next poll (~2 minutes, or ~15 s after a
  failed attempt). Once it has synced, log in with the master password; the
  status page keeps showing the same connection error if a later sync fails. The
  master's Screens & sync card should list the slave within ~2 minutes of it
  polling.

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

All screens mirror one master Pi. Content is managed only on the master's
web UI; every other Pi is a slave that pulls and displays the same playlist.

### 1. Master
1. Install as normal (above).
2. Give the master a fixed IP set statically on the Pi itself, chosen
   *outside* the router's DHCP pool (OS-level network config, not a signage
   feature). This is the address every screen will point at.
3. Open the master's web UI, set the admin password, and note the Sync token
   shown in the "Screens & sync" card.

### 2. First slave
1. Install on a second Pi. Open its web UI once.
2. On the setup screen choose "This screen joins a master" and enter the
   master's static IP (e.g. `192.168.1.50:8080`) and the sync token. A joining
   screen needs no admin password (it receives the master's on the first
   sync), so the form asks only for the address and token.
3. It restarts as a screen and begins mirroring within ~2 minutes. Each screen
   shows its own `http://<ip>:<port>` in the bottom-right corner.

### 3. More screens: clone the card
Power off the configured slave, clone its SD card onto every other Pi, and
boot them. Each clone joins automatically (no setup) because the master address
and token are already on the card. Slaves stay on plain DHCP; only the master
needs a static IP.

### Deletions, decoders, downtime
- Deleting media on the master removes it from every slave on the next sync.
- `hwdec` (video decoder) is per-Pi and not synced. To change it on one
  screen, read that screen's IP off its bottom-right overlay, open its page, and
  log in with the master password.
- A slave that was off catches up automatically when it returns.
- A screen's own page (hwdec, re-point, Become master) is password-protected
  with the *same* password as the master, which the screen receives on its first
  sync. The system keeps two separate secrets: the login password (web-UI
  access, set on the master, synced to screens) and the sync token (authorizes
  a screen to pull content from the master, entered at join/clone). They are
  independent and serve different purposes. Changing the master password
  propagates to screens on their next sync; if you are rotating it because it
  leaked, also `systemctl --user restart fleetsign` on each screen to drop any open
  browser sessions.
- The master's Screens & sync card lists screens that have checked in within
  the last 5 minutes; use it to confirm a new slave connected. You can rotate the
  sync token there; every screen must use the same token.

### Failover (master dies)
Screens keep playing their last content. To restore editing:
1. Open any slave's page (read its IP off the screen) and click Become master.
2. Assign the master's static IP to that promoted Pi (OS-level).
   No other screen needs changing; they all point at that IP.
3. Before returning the old master to service, open it and Join master
   (point it at the static IP) so it rejoins as a screen instead of becoming a
   second master.
