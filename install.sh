#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Run as the normal desktop user, never as root. Everything except the apt-get
# below is per-user — $HOME/fleetsign, the venv, ~/.config/systemd/user, the
# labwc autostart/rc.xml, and `systemctl --user` all key off the invoking user.
# Under `sudo install.sh`, $HOME is /root and systemctl --user targets root's
# instance, so the whole install lands in the wrong place with no error. Refuse
# rather than guess which user was meant.
if [ "$(id -u)" -eq 0 ]; then
    echo "install.sh: run as your normal desktop user, not root (no sudo)." >&2
    echo "It self-elevates for apt only. Re-run: bash ~/fleetsign/install.sh" >&2
    exit 1
fi

# The only step needing root is the apt-get install below. Confirm this user can
# sudo and prime the credential up front, so the password is asked once now
# instead of pausing the install partway through.
if ! command -v sudo >/dev/null 2>&1; then
    echo "install.sh: sudo not found; it is needed to install system packages." >&2
    exit 1
fi
sudo -v || { echo "install.sh: this user needs sudo rights to install packages." >&2; exit 1; }

APP_DIR="$HOME/fleetsign"

sudo apt-get update
sudo apt-get install -y mpv python3-venv xdotool wmctrl

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

mkdir -p "$HOME/.config/systemd/user"
cp "$APP_DIR/systemd/fleetsign.service" "$HOME/.config/systemd/user/fleetsign.service"
systemctl --user daemon-reload

# The autostart hook and always-on-top rule below are labwc-specific (Raspberry Pi
# OS Bookworm's default compositor). Set them up only when labwc is actually present:
# on a Wayfire/X11/other desktop they'd be files nothing reads, and the player would
# silently fail to autostart. Gate on "installed" (command -v), not "running", so a
# labwc Pi provisioned headless — labwc not up yet at install time — is still handled.
# Either way the service is installed and started further down.
if command -v labwc >/dev/null 2>&1; then
    # Autostart on login via the labwc compositor's autostart. The desktop session's
    # graphical-session.target is not reliably reached for --user units on Raspberry
    # Pi OS, so enabling the unit alone does not start it on boot. labwc runs
    # ~/.config/labwc/autostart at session start (with the Wayland environment); seed
    # it from the system default so the panel/wallpaper still launch, then append ours.
    LABWC_AUTOSTART="$HOME/.config/labwc/autostart"
    mkdir -p "$(dirname "$LABWC_AUTOSTART")"
    if [ ! -f "$LABWC_AUTOSTART" ] && [ -f /etc/xdg/labwc/autostart ]; then
        cp /etc/xdg/labwc/autostart "$LABWC_AUTOSTART"
    fi
    # Append our block, or migrate an older one that predates the DISPLAY import (added
    # so mpv can reach XWayland for always-on-top — see player.py). A current block is
    # left as-is, so re-runs/upgrades are idempotent.
    if ! grep -q "fleetsign.service" "$LABWC_AUTOSTART" 2>/dev/null; then
        need_autostart=1
    elif ! grep -q "XDG_RUNTIME_DIR DISPLAY" "$LABWC_AUTOSTART"; then
        sed -i '/# Start the FleetSign player (systemd --user manages restarts)/,+2d' "$LABWC_AUTOSTART"
        need_autostart=1
    else
        need_autostart=0
    fi
    if [ "$need_autostart" = 1 ]; then
        {
            echo ""
            echo "# Start the FleetSign player (systemd --user manages restarts)"
            echo "systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY 2>/dev/null"
            echo "systemctl --user start fleetsign.service"
        } >> "$LABWC_AUTOSTART"
    fi

    # Keep the signage window above other windows. On Wayland a client cannot pin
    # itself on top, so the player runs mpv under XWayland (see default_launcher in
    # fleetsign/player.py); labwc disallows X11 always-on-top requests by default, so
    # opt mpv in with a window rule. Seed rc.xml from the system default (or a minimal
    # file that still loads the default keybinds — labwc keeps its defaults when no
    # <keybind> entries are present), then inject the rule once.
    LABWC_RC="$HOME/.config/labwc/rc.xml"
    mkdir -p "$(dirname "$LABWC_RC")"
    if [ ! -f "$LABWC_RC" ]; then
        if [ -f /etc/xdg/labwc/rc.xml ]; then
            cp /etc/xdg/labwc/rc.xml "$LABWC_RC"
        else
            printf '%s\n' '<?xml version="1.0"?>' '<labwc_config>' \
                '  <keyboard>' '    <default />' '  </keyboard>' '</labwc_config>' \
                > "$LABWC_RC"
        fi
    fi
    # Add a FleetSign-owned always-on-top rule for mpv, keyed on our marker comment
    # rather than on "any mpv rule": (a) we still add ours when the user already has an
    # unrelated mpv windowRule (labwc applies both, so allowAlwaysOnTop still takes),
    # and (b) uninstall removes only this line, never a rule the user wrote.
    if ! grep -q 'FleetSign-managed' "$LABWC_RC"; then
        RULE='    <windowRule identifier="mpv" allowAlwaysOnTop="yes" /> <!-- FleetSign-managed -->'
        if grep -q '<windowRules>' "$LABWC_RC"; then
            sed -i "s#<windowRules>#<windowRules>\n${RULE}#" "$LABWC_RC"
        elif grep -q '</labwc_config>' "$LABWC_RC"; then
            sed -i "s#</labwc_config>#  <windowRules>\n${RULE}\n  </windowRules>\n</labwc_config>#" "$LABWC_RC"
        elif grep -q '</openbox_config>' "$LABWC_RC"; then
            sed -i "s#</openbox_config>#  <windowRules>\n${RULE}\n  </windowRules>\n</openbox_config>#" "$LABWC_RC"
        fi
    fi

    # If labwc is currently running, reload it (SIGHUP = reconfigure) so the new rule
    # is loaded before we start the player below — the fresh mpv then maps
    # always-on-top without a relogin. Skipped cleanly when labwc isn't up yet (e.g.
    # installing over SSH before login); the rule still applies at the next login.
    if pgrep -x labwc >/dev/null 2>&1; then
        pkill -HUP labwc 2>/dev/null || true
    fi
else
    echo "Note: labwc not found — skipping its autostart hook and always-on-top rule"
    echo "(both are labwc-specific). The service is still installed and started below."
    echo "If this Pi uses another desktop/compositor, add the matching autostart line"
    echo "from the 'Other desktops / compositors' table in README.md so it launches on"
    echo "boot; on an X11 session mpv's --ontop already keeps the window on top."
fi

# (Re)start now for this session so an upgrade actually relaunches mpv with the
# current code (XWayland + --ontop); plain `start` is a no-op when the daemon is
# already running, leaving the old mpv up. `restart` also starts it if stopped.
# Import the live session env first so the (re)started daemon can reach the display.
# The labwc autostart handles subsequent logins/reboots.
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY 2>/dev/null || true
systemctl --user restart fleetsign.service

IP="$(hostname -I | awk '{print $1}')"
echo "Installed. Open http://${IP}:8080 to set the admin password."
