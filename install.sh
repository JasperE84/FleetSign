#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

APP_DIR="$HOME/fleetsign"

sudo apt-get update
sudo apt-get install -y mpv python3-venv

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

mkdir -p "$HOME/.config/systemd/user"
cp "$APP_DIR/systemd/fleetsign.service" "$HOME/.config/systemd/user/fleetsign.service"
systemctl --user daemon-reload

# Autostart on login via the labwc compositor's autostart. The desktop session's
# graphical-session.target is not reliably reached for --user units on Raspberry Pi
# OS, so enabling the unit alone does not start it on boot. labwc runs
# ~/.config/labwc/autostart at session start (with the Wayland environment); seed it
# from the system default so the panel/wallpaper still launch, then append our line.
LABWC_AUTOSTART="$HOME/.config/labwc/autostart"
mkdir -p "$(dirname "$LABWC_AUTOSTART")"
if [ ! -f "$LABWC_AUTOSTART" ] && [ -f /etc/xdg/labwc/autostart ]; then
    cp /etc/xdg/labwc/autostart "$LABWC_AUTOSTART"
fi
if ! grep -q "fleetsign.service" "$LABWC_AUTOSTART" 2>/dev/null; then
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
        cat > "$LABWC_RC" <<'XML'
<?xml version="1.0"?>
<labwc_config>
  <keyboard>
    <default />
  </keyboard>
</labwc_config>
XML
    fi
fi
if ! grep -q 'identifier="mpv"' "$LABWC_RC"; then
    RULE='    <windowRule identifier="mpv" allowAlwaysOnTop="yes" />'
    if grep -q '<windowRules>' "$LABWC_RC"; then
        sed -i "s#<windowRules>#<windowRules>\n${RULE}#" "$LABWC_RC"
    elif grep -q '</labwc_config>' "$LABWC_RC"; then
        sed -i "s#</labwc_config>#  <windowRules>\n${RULE}\n  </windowRules>\n</labwc_config>#" "$LABWC_RC"
    elif grep -q '</openbox_config>' "$LABWC_RC"; then
        sed -i "s#</openbox_config>#  <windowRules>\n${RULE}\n  </windowRules>\n</openbox_config>#" "$LABWC_RC"
    fi
fi

# If labwc is the running compositor, reload it (SIGHUP = reconfigure) so the new
# window rule is loaded before we start the player below, letting the fresh mpv map
# always-on-top without a relogin. Verified with pgrep, not assumed: when labwc
# isn't up (e.g. installing over SSH from a console) this is skipped and the rule
# takes effect on the next login, exactly like the autostart hook above.
if pgrep -x labwc >/dev/null 2>&1; then
    pkill -HUP labwc 2>/dev/null || true
fi

# Start now for this session; the labwc autostart handles subsequent logins/reboots.
systemctl --user start fleetsign.service

IP="$(hostname -I | awk '{print $1}')"
echo "Installed. Open http://${IP}:8080 to set the admin password."
