#!/usr/bin/env bash
set -euo pipefail

# Reverses install.sh. Removes the FleetSign service, its autostart hook, and the
# Python venv. Deliberately KEEPS your content: data/ (config.json with the admin
# password + sync token, manifest.json) and media/ (uploaded files) are left in
# place — delete ~/fleetsign yourself if you want a clean slate. Also leaves the
# apt packages (mpv, python3-venv, xdotool, wmctrl) installed, since other
# software may use them.

APP_DIR="$HOME/fleetsign"
UNIT="$HOME/.config/systemd/user/fleetsign.service"
LABWC_AUTOSTART="$HOME/.config/labwc/autostart"
LABWC_RC="$HOME/.config/labwc/rc.xml"

# Stop the running player before removing its unit (so systemctl can find it).
systemctl --user stop fleetsign.service 2>/dev/null || true

# Remove the unit file and forget any failed state.
if [ -f "$UNIT" ]; then
    rm -f "$UNIT"
    systemctl --user daemon-reload
    systemctl --user reset-failed fleetsign.service 2>/dev/null || true
    echo "Removed $UNIT"
fi

# Strip the autostart block install.sh appended (the comment + the two
# systemctl lines that follow it). Other autostart entries are untouched.
if [ -f "$LABWC_AUTOSTART" ] && grep -q "fleetsign.service" "$LABWC_AUTOSTART"; then
    sed -i '/# Start the FleetSign player (systemd --user manages restarts)/,+2d' "$LABWC_AUTOSTART"
    echo "Removed FleetSign block from $LABWC_AUTOSTART"
fi

# Strip the always-on-top rule install.sh injected, matched by our FleetSign marker
# so we never delete an mpv windowRule the user wrote themselves. Other windowRules
# are left alone; an emptied <windowRules> block (or the minimal rc.xml we may have
# seeded, which still loads labwc's default keybinds) is harmless.
if [ -f "$LABWC_RC" ] && grep -q 'FleetSign-managed' "$LABWC_RC"; then
    sed -i '/FleetSign-managed/d' "$LABWC_RC"
    echo "Removed FleetSign window rule from $LABWC_RC"
fi

# Remove the virtualenv (an install artifact, not user content).
if [ -d "$APP_DIR/.venv" ]; then
    rm -rf "$APP_DIR/.venv"
    echo "Removed $APP_DIR/.venv"
fi

echo "Uninstalled. Kept your media/ and data/ (incl. config.json) under $APP_DIR."
echo "Left mpv, python3-venv, xdotool, and wmctrl installed; remove them with apt if unused elsewhere."
