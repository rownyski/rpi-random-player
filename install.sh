#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rownyski/rpi-random-player.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-random-player}"
SERVICE_NAME="rpi-random-player.service"
LEGACY_SERVICE_NAME="player.service"
ENV_FILE="/etc/default/rpi-random-player"

detect_audio_device() {
  local connector

  # Prefer HDMI-A-2 first (matches many RPi4 setups using the secondary micro-HDMI port).
  for connector in \
    /sys/class/drm/card*-HDMI-A-2/status \
    /sys/class/drm/card*-HDMI-A-1/status; do
    [[ -r "$connector" ]] || continue
    if [[ "$(cat "$connector" 2>/dev/null | tr '[:upper:]' '[:lower:]')" == "connected" ]]; then
      case "$connector" in
        *HDMI-A-2/status) printf '%s' "alsa/plughw:CARD=vc4hdmi1,DEV=0"; return 0 ;;
        *HDMI-A-1/status) printf '%s' "alsa/plughw:CARD=vc4hdmi0,DEV=0"; return 0 ;;
      esac
    fi
  done

  # Fallback that mirrors the known-working manual command from field testing.
  printf '%s' "alsa/plughw:CARD=vc4hdmi1,DEV=0"
}

detect_drm_mode() {
  local modes_output preferred
  preferred=""

  if command -v modetest >/dev/null 2>&1; then
    modes_output="$(modetest -M vc4 -c 2>/dev/null || true)"
    if [[ -n "$modes_output" ]]; then
      preferred="$(printf '%s\n' "$modes_output" | grep -Eo "[0-9]{3,4}x[0-9]{3,4}@[0-9]{2,3}(\.[0-9]+)?" | head -n 1 || true)"
      if printf '%s\n' "$modes_output" | grep -Eq "1920x1080@60(\.00)?"; then
        preferred="1920x1080@60"
      fi
    fi
  fi

  if [[ -z "$preferred" ]]; then
    preferred="$(for f in /sys/class/drm/card*-HDMI-A-*/modes; do
      [[ -r "$f" ]] || continue
      if head -n 30 "$f" | grep -Eq '^1920x1080$'; then
        echo "1920x1080@60"
        break
      fi
      head -n 1 "$f" | sed -n '1p' | awk '{print $1 "@60"}'
      break
    done)"
  fi

  printf '%s' "$preferred"
}

write_env_file() {
  local drm_mode audio_device
  drm_mode="$(detect_drm_mode)"
  audio_device="$(detect_audio_device)"

  sudo mkdir -p "$(dirname "$ENV_FILE")"
  {
    echo "# Managed by install.sh"
    echo "# Override values here if needed, then restart rpi-random-player.service"
    if [[ -n "$drm_mode" ]]; then
      echo "MPV_DRM_MODE=$drm_mode"
    else
      echo "# MPV_DRM_MODE=1920x1080@60"
    fi
    echo "AUDIO_DEVICE=$audio_device"
  } | sudo tee "$ENV_FILE" >/dev/null

  if [[ -n "$drm_mode" ]]; then
    echo "Configured MPV_DRM_MODE=$drm_mode in $ENV_FILE"
  else
    echo "Could not auto-detect a DRM mode; leaving MPV_DRM_MODE unset in $ENV_FILE"
  fi
  echo "Configured AUDIO_DEVICE=$audio_device in $ENV_FILE"
}

if [[ -z "$REPO_URL" ]]; then
  echo "REPO_URL is empty; set it to a valid git URL."
  exit 1
fi

sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get install -y mpv ffmpeg python3 python3-pip python3-evdev git libdrm-tests

if [[ -d "$INSTALL_DIR/.git" ]]; then
  sudo git -C "$INSTALL_DIR" pull --ff-only
else
  sudo rm -rf "$INSTALL_DIR"
  sudo git clone "$REPO_URL" "$INSTALL_DIR"
fi

sudo python3 -m pip install --break-system-packages -r "$INSTALL_DIR/requirements.txt"
write_env_file

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
