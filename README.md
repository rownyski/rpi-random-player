# Raspberry Pi Standalone Random Video Player

Standalone random video playback daemon for Raspberry Pi OS Lite with `mpv`.

## Features

- Recursively scans mounted USB drives (`/media`, `/run/media`, `/mnt`) for `.mp4` and `.mkv` files.
- If no USB mount is detected, the daemon attempts to auto-mount `/dev/sd*` partitions (for example `/dev/sda1`) under `/mnt/usb` before scanning.
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
- Randomly selects from discovered `.mp4`/`.mkv` files (with immediate-repeat protection when 2+ videos exist).
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

If you see `Unit rpi-random-player.service could not be found`, run the installer again and then reload systemd:

```bash
REPO_URL=https://github.com/rownyski/rpi-random-player.git bash install.sh
sudo systemctl daemon-reload
sudo systemctl enable --now rpi-random-player.service
```

Compatibility note: installer also drops `player.service` for older docs, but the canonical unit name is `rpi-random-player.service`.


## Command-line debugging (step-by-step)

If service is running but keyboard presses do nothing, use this sequence:

1. Confirm service is active:

```bash
sudo systemctl status rpi-random-player.service --no-pager
```

2. Run keyboard diagnosis directly from CLI (outside systemd):

```bash
cd /opt/rpi-random-player
sudo python3 player.py --debug --diagnose-keyboard --diagnose-seconds 30
```

Expected: you should see `EVENT ... keycode=KEY_S` and `EVENT ... keycode=KEY_E` logs when pressing keys.


If you see `pw.conf: can't load config client.conf` from `mpv`, this is usually a PipeWire warning and playback can still work. The player forces ALSA output (`--ao=alsa`) and auto-detects the connected HDMI port for `--audio-device` (vc4hdmi0/vc4hdmi1). You can override manually with `AUDIO_DEVICE=alsa/plughw:CARD=vc4hdmi1,DEV=0`.

On START (`S`), the player refreshes the file list from USB and picks a random file.
On auto-next (natural end), it reuses the preloaded list for fast transitions.

If you suspect duplicate selections, watch for `Starting playback: ...` logs; the player avoids immediate repeats whenever at least 2 videos are available.

3. Run the player in foreground with debug logs:

```bash
cd /opt/rpi-random-player
sudo systemctl stop rpi-random-player.service
sudo python3 player.py --debug
```

Press `S` / `E` and verify logs for:
- keyboard event detection,
- mount discovery,
- candidate discovery and random selection,
- mpv start/stop messages.

4. Restore service mode:

```bash
sudo systemctl restart rpi-random-player.service
sudo journalctl -u rpi-random-player.service -f
```

## Notes

- No GUI libraries are required.
- Designed for Raspberry Pi 400 (dev) and Raspberry Pi 4 Model B (target).
- Future input migration to GPIO can replace keyboard handlers in `player.py`.
