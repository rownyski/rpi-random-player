#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rownyski/rpi-random-player.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/rpi-random-player}"
SERVICE_NAME="rpi-random-player.service"
LEGACY_SERVICE_NAME="player.service"
ENV_FILE="/etc/default/rpi-random-player"

print_available_modes() {
  echo "Inspecting available HDMI modes..."
  if command -v modetest >/dev/null 2>&1; then
    modetest -M vc4 -c | sed -n '1,260p' || true
  else
    for f in /sys/class/drm/card*-HDMI-A-*/modes; do
      [[ -r "$f" ]] || continue
      echo "== $f =="
      head -n 30 "$f" || true
    done
  fi
}

is_valid_drm_mode() {
  [[ "$1" =~ ^[0-9]{3,4}x[0-9]{3,4}@[0-9]{2,3}(\.[0-9]+)?$ ]]
}

clear_forced_audio_override() {
  local dropin
  for dropin in \
    "/etc/systemd/system/$SERVICE_NAME.d/override.conf" \
    "/etc/systemd/system/$LEGACY_SERVICE_NAME.d/override.conf"; do
    [[ -f "$dropin" ]] || continue
    if sudo grep -q '^Environment=AUDIO_DEVICE=' "$dropin"; then
      sudo sed -i '/^Environment=AUDIO_DEVICE=/d' "$dropin"
      echo "Removed forced AUDIO_DEVICE override from $dropin"
    fi
  done
}

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
  local preferred mode_file
  preferred=""

  # Allow explicit installer override.
  if [[ -n "${MPV_DRM_MODE:-}" ]]; then
    if is_valid_drm_mode "${MPV_DRM_MODE}"; then
      printf '%s' "${MPV_DRM_MODE}"
      return 0
    fi
    echo "Ignoring invalid MPV_DRM_MODE override: ${MPV_DRM_MODE}" >&2
  fi

  # Prefer the connected HDMI connector(s) only.
  for mode_file in /sys/class/drm/card*-HDMI-A-*/modes; do
    [[ -r "$mode_file" ]] || continue
    status_file="${mode_file%/modes}/status"
    if [[ -r "$status_file" ]]; then
      status="$(tr '[:upper:]' '[:lower:]' <"$status_file" 2>/dev/null || true)"
      [[ "$status" == "connected" ]] || continue
    fi

    # Force a known-good deterministic mode when available.
    if head -n 60 "$mode_file" | grep -Eq '^1920x1080$|^1920x1080p60$'; then
      preferred="1920x1080@60"
      break
    fi
  done

  if [[ -z "$preferred" ]]; then
    # Fallback to any connector listing 1080p.
    for mode_file in /sys/class/drm/card*-HDMI-A-*/modes; do
      [[ -r "$mode_file" ]] || continue
      if head -n 60 "$mode_file" | grep -Eq '^1920x1080$|^1920x1080p60$'; then
        preferred="1920x1080@60"
        break
      fi
    done
  fi

  if [[ -z "$preferred" ]]; then
    # Final deterministic fallback.
    preferred="1920x1080@60"
  fi

  printf '%s' "$preferred"
}

write_env_file() {
  local drm_mode video_sync forced_audio_device
  drm_mode="$(detect_drm_mode)"
  video_sync="${MPV_VIDEO_SYNC:-audio}"
  forced_audio_device="${AUDIO_DEVICE:-}"

  sudo mkdir -p "$(dirname "$ENV_FILE")"
  {
    echo "# Managed by install.sh"
    echo "# Override values here if needed, then restart rpi-random-player.service"
    echo "MPV_VIDEO_SYNC=$video_sync"
    echo "MPV_DRM_MODE=$drm_mode"
    if [[ -n "$forced_audio_device" ]]; then
      echo "AUDIO_DEVICE=$forced_audio_device"
    else
      echo "# AUDIO_DEVICE=alsa/plughw:CARD=vc4hdmi0,DEV=0"
    fi
  } | sudo tee "$ENV_FILE" >/dev/null

  echo "Configured MPV_VIDEO_SYNC=$video_sync in $ENV_FILE"
  echo "Configured MPV_DRM_MODE=$drm_mode in $ENV_FILE"
  if [[ -n "$forced_audio_device" ]]; then
    echo "Configured AUDIO_DEVICE=$forced_audio_device in $ENV_FILE"
  else
    echo "AUDIO_DEVICE left unset in $ENV_FILE (player auto-detects connected HDMI port)."
  fi
}

if [[ -z "$REPO_URL" ]]; then
  echo "REPO_URL is empty; set it to a valid git URL."
  exit 1
fi

sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get install -y mpv ffmpeg python3 python3-pip python3-evdev git libdrm-tests
print_available_modes
sudo git config --global --add safe.directory "$INSTALL_DIR" >/dev/null 2>&1 || true

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
clear_forced_audio_override
sudo systemctl daemon-reload
if sudo systemctl list-unit-files | grep -q "^$LEGACY_SERVICE_NAME"; then
  sudo systemctl disable "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
  sudo systemctl stop "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
fi
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Installation complete. Service status:"
sudo systemctl --no-pager status "$SERVICE_NAME" || true
