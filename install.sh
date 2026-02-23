#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rownyski/rpi-random-player.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-random-player}"
SERVICE_NAME="rpi-random-player.service"
LEGACY_SERVICE_NAME="player.service"

if [[ -z "$REPO_URL" ]]; then
  echo "REPO_URL is empty; set it to a valid git URL."
  exit 1
fi

sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get install -y mpv ffmpeg python3 python3-pip python3-evdev git

if [[ -d "$INSTALL_DIR/.git" ]]; then
  sudo git -C "$INSTALL_DIR" pull --ff-only
else
  sudo rm -rf "$INSTALL_DIR"
  sudo git clone "$REPO_URL" "$INSTALL_DIR"
fi

sudo python3 -m pip install --break-system-packages -r "$INSTALL_DIR/requirements.txt"

sudo cp "$INSTALL_DIR/player.service" "/etc/systemd/system/$SERVICE_NAME"
# Backward-compatible alias for older instructions that referenced player.service.
sudo cp "$INSTALL_DIR/player.service" "/etc/systemd/system/$LEGACY_SERVICE_NAME"
sudo systemctl daemon-reload
if sudo systemctl list-unit-files | grep -q "^$LEGACY_SERVICE_NAME"; then
  sudo systemctl disable "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
  sudo systemctl stop "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
fi
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Installation complete. Service status:"
sudo systemctl --no-pager status "$SERVICE_NAME" || true
