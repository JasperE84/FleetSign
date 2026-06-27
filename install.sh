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
        echo "systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR 2>/dev/null"
        echo "systemctl --user start fleetsign.service"
    } >> "$LABWC_AUTOSTART"
fi

# Start now for this session; the labwc autostart handles subsequent logins/reboots.
systemctl --user start fleetsign.service

IP="$(hostname -I | awk '{print $1}')"
echo "Installed. Open http://${IP}:8080 to set the admin password."
