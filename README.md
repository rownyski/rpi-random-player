# Raspberry Pi Standalone Random Video Player

Standalone random video playback daemon for Raspberry Pi OS Lite with `mpv`.

## Features

- Recursively scans mounted USB drives (`/media`, `/run/media`, `/mnt`) for `.mp4` and `.mkv` files.
- Classifies videos with `ffprobe` into `safe`, `medium`, and `risky` playback buckets.
- Keyboard controls in development mode:
  - `S`: start random playback (or restart with another random video if already playing).
  - `E`: stop playback immediately.
- Automatically starts another random video when playback ends naturally.
- Runs as a `systemd` service on Raspberry Pi OS Lite.

## Project layout

```text
rpi-random-player/
├── install.sh
├── player.py
├── requirements.txt
├── player.service
├── README.md
└── config/
    └── mpv.conf
```

## Dependencies

Install on Raspberry Pi:

- `mpv`
- `ffmpeg` (for `ffprobe`)
- `python3`
- `python3-pip`
- `python3-evdev`
- `git`

## Installation

### Option A: Clone and run installer

```bash
git clone https://github.com/rownyski/rpi-random-player.git
cd rpi-random-player
REPO_URL=https://github.com/rownyski/rpi-random-player.git bash install.sh
```

### Option B: One-line remote installer

```bash
curl -sSL https://github.com/rownyski/rpi-random-player/raw/main/install.sh | REPO_URL=https://github.com/rownyski/rpi-random-player.git bash
```

## Runtime behavior

- On each `S` press, USB storage is re-scanned.
- Selection probabilities:
  - If `safe` exists: 80% from `safe`, 20% from `medium` (if any).
  - `risky` is chosen only when `safe` and `medium` are unavailable.
- `STOP` (`E`) sends `SIGTERM` to `mpv` immediately.
- Fullscreen playback uses:

```text
--fullscreen --hwdec=drm --no-terminal --quiet --no-osc --no-osd-bar
```

## Service management

```bash
sudo systemctl status rpi-random-player.service
sudo systemctl restart rpi-random-player.service
sudo journalctl -u rpi-random-player.service -f
```

## Notes

- No GUI libraries are required.
- Designed for Raspberry Pi 400 (dev) and Raspberry Pi 4 Model B (target).
- Future input migration to GPIO can replace keyboard handlers in `player.py`.
