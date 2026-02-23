#!/usr/bin/env python3
"""Standalone random USB video player for Raspberry Pi."""

from __future__ import annotations

import argparse
import logging
import os
import random
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import evdev
from evdev import ecodes

VIDEO_EXTENSIONS = {".mp4", ".mkv"}
USB_SCAN_ROOTS = [Path("/media"), Path("/run/media"), Path("/mnt")]
POLL_INTERVAL_SECONDS = 0.05
HDMI_STATUS_TO_AUDIO_DEVICE = {
    Path("/sys/class/drm/card1-HDMI-A-1/status"): "alsa/plughw:CARD=vc4hdmi0,DEV=0",
    Path("/sys/class/drm/card1-HDMI-A-2/status"): "alsa/plughw:CARD=vc4hdmi1,DEV=0",
    Path("/sys/class/drm/card0-HDMI-A-1/status"): "alsa/plughw:CARD=vc4hdmi0,DEV=0",
    Path("/sys/class/drm/card0-HDMI-A-2/status"): "alsa/plughw:CARD=vc4hdmi1,DEV=0",
}

MPV_BASE_CMD = [
    "mpv",
    "--fullscreen",
    "--hwdec=drm",
    "--no-terminal",
    "--quiet",
    "--no-osc",
    "--no-osd-bar",
    "--ao=alsa",
]

logger = logging.getLogger("rpi_random_player")


class RandomVideoPlayer:
    def __init__(self, debug: bool = False) -> None:
        self.mpv_process: Optional[subprocess.Popen] = None
        self.auto_restart = False
        self.running = True
        self.debug = debug
        self.video_candidates: list[Path] = []
        self.last_played: Optional[Path] = None

    def run(self) -> None:
        # Preload list once on startup for fast first START.
        self.refresh_candidates(reason="startup")

        keyboards = self._open_keyboard_devices()
        if not keyboards:
            logger.warning("No keyboard input devices found. Retrying until one appears.")

        try:
            while self.running:
                if not keyboards:
                    time.sleep(1)
                    keyboards = self._open_keyboard_devices()
                    if keyboards:
                        logger.info("Keyboard device(s) detected: %s", ", ".join(dev.path for dev in keyboards))
                else:
                    ready, _, _ = select.select(keyboards, [], [], POLL_INTERVAL_SECONDS)
                    for device in ready:
                        try:
                            events = device.read()
                        except OSError as exc:
                            logger.warning("Read failed for %s (%s); reopening devices", device.path, exc)
                            keyboards = self._open_keyboard_devices()
                            break

                        for event in events:
                            self._handle_event(device, event)

                self._check_playback_exit()
        except KeyboardInterrupt:
            logger.info("Interrupted by keyboard, shutting down.")
        finally:
            self.stop_playback()
            for keyboard in keyboards:
                keyboard.close()

    def _open_keyboard_devices(self) -> list[evdev.InputDevice]:
        devices: list[evdev.InputDevice] = []
        discovered: list[str] = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities().get(ecodes.EV_KEY, [])
                keycodes = {code for code in caps if isinstance(code, int)}
                if ecodes.KEY_S in keycodes or ecodes.KEY_E in keycodes:
                    discovered.append(f"{dev.path}:{dev.name}")
                    try:
                        dev.grab()
                        logger.debug("Grabbed input device %s (%s)", dev.path, dev.name)
                    except OSError as exc:
                        logger.debug("Could not grab %s (%s): %s. Continuing without grab.", dev.path, dev.name, exc)
                    devices.append(dev)
            except OSError as exc:
                logger.debug("Skipping unreadable input device %s: %s", path, exc)
                continue

        if discovered:
            logger.info("Discovered keyboard-capable devices: %s", ", ".join(discovered))
        return devices

    def _handle_event(self, device: evdev.InputDevice, event: evdev.InputEvent) -> None:
        if event.type != ecodes.EV_KEY:
            return

        key_event = evdev.categorize(event)
        if key_event.keystate != key_event.key_down:
            return

        logger.debug(
            "Key event from %s (%s): code=%s scancode=%s",
            device.path,
            device.name,
            getattr(key_event, "keycode", "unknown"),
            key_event.scancode,
        )

        if key_event.scancode == ecodes.KEY_S:
            logger.info("START requested from keyboard (S).")
            self.start_random_video(force_restart=True)
        elif key_event.scancode == ecodes.KEY_E:
            logger.info("STOP requested from keyboard (E).")
            self.stop_playback()

    def _check_playback_exit(self) -> None:
        if not self.mpv_process:
            return

        ret_code = self.mpv_process.poll()
        if ret_code is None:
            return

        logger.info("mpv exited with code %s", ret_code)
        self.mpv_process = None
        if self.auto_restart:
            logger.info("Auto-restart enabled; selecting next random video.")
            # Do not rescan on auto-next to keep transitions fast.
            self.start_random_video(force_restart=False, rescan=False)

    def _resolve_audio_device_arg(self) -> list[str]:
        # Allow explicit override if needed.
        forced = os.environ.get("AUDIO_DEVICE", "").strip()
        if forced:
            logger.info("Using forced audio device from AUDIO_DEVICE=%s", forced)
            return [f"--audio-device={forced}"]

        # Auto-detect connected HDMI connector and map to ALSA card.
        for status_path, audio_device in HDMI_STATUS_TO_AUDIO_DEVICE.items():
            try:
                status = status_path.read_text(encoding="utf-8", errors="ignore").strip().lower()
            except OSError:
                continue
            if status == "connected":
                logger.info("Detected active HDMI connector %s -> %s", status_path.parent.name, audio_device)
                return [f"--audio-device={audio_device}"]

        logger.info("No active HDMI connector detected; using ALSA default audio routing.")
        return []

    def start_random_video(self, force_restart: bool, rescan: bool = True) -> None:
        if self.mpv_process and force_restart:
            logger.info("Force restart requested; stopping current video first.")
            self.stop_playback()

        if rescan:
            self.refresh_candidates(reason="start-key")

        selected = self.select_video(self.video_candidates)
        if not selected:
            self.auto_restart = False
            logger.warning("No playable videos found on mounted USB storage.")
            return

        logger.info("Starting playback: %s", selected)
        self.last_played = selected
        self.auto_restart = True
        self.mpv_process = subprocess.Popen(
            [*MPV_BASE_CMD, *self._resolve_audio_device_arg(), str(selected)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    def stop_playback(self) -> None:
        self.auto_restart = False
        if not self.mpv_process:
            logger.debug("Stop requested, but no active mpv process.")
            return

        process = self.mpv_process
        self.mpv_process = None
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            logger.info("Sent SIGTERM to mpv process group pid=%s", process.pid)
        except ProcessLookupError:
            logger.debug("mpv process already exited before SIGTERM.")

    def refresh_candidates(self, reason: str) -> None:
        started = time.monotonic()
        candidates: list[Path] = []
        mounts = self._discover_usb_mounts()
        logger.info("Refreshing video list (%s). Detected mounts: %s", reason, ", ".join(str(m) for m in mounts) if mounts else "<none>")
        for mount in mounts:
            for path in mount.rglob("*"):
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                    candidates.append(path)

        self.video_candidates = candidates
        elapsed = time.monotonic() - started
        logger.info("Found %d candidate video files in %.2fs", len(self.video_candidates), elapsed)

    def _discover_usb_mounts(self) -> list[Path]:
        mounts: set[Path] = set()
        proc_mounts = Path("/proc/mounts")
        if proc_mounts.exists():
            for line in proc_mounts.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                source, mountpoint, fs_type = parts[:3]
                if not source.startswith("/dev/sd"):
                    continue
                if fs_type in {"vfat", "exfat", "ntfs", "ext4", "ext3", "ext2"}:
                    mounts.add(Path(mountpoint))

        for root in USB_SCAN_ROOTS:
            if not root.exists():
                continue
            for entry in root.iterdir():
                if entry.is_dir():
                    mounts.add(entry)

        return sorted(mounts)

    def select_video(self, videos: list[Path]) -> Optional[Path]:
        if not videos:
            return None

        # Avoid immediate repeat when possible.
        if self.last_played and len(videos) > 1:
            pool = [video for video in videos if video != self.last_played]
            if pool:
                return random.choice(pool)

        return random.choice(videos)


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def diagnose_keyboard(duration: int) -> int:
    logger.info("Running keyboard diagnosis for %ss", duration)
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            keycodes = {code for code in caps if isinstance(code, int)}
            if ecodes.KEY_S in keycodes or ecodes.KEY_E in keycodes:
                logger.info("Found candidate input device: %s (%s)", dev.path, dev.name)
                devices.append(dev)
        except OSError as exc:
            logger.debug("Skipping unreadable input device %s: %s", path, exc)

    if not devices:
        logger.error("No keyboard input devices with S/E keys found.")
        return 1

    deadline = time.time() + duration
    logger.info("Press S and E now. Logging key-down events...")
    while time.time() < deadline:
        ready, _, _ = select.select(devices, [], [], 0.2)
        for device in ready:
            for event in device.read():
                if event.type != ecodes.EV_KEY:
                    continue
                key_event = evdev.categorize(event)
                if key_event.keystate == key_event.key_down:
                    logger.info(
                        "EVENT %s (%s): keycode=%s scancode=%s",
                        device.path,
                        device.name,
                        getattr(key_event, "keycode", "unknown"),
                        key_event.scancode,
                    )

    for dev in devices:
        dev.close()
    logger.info("Keyboard diagnosis finished.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Random USB video player")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument(
        "--diagnose-keyboard",
        action="store_true",
        help="List keyboard devices and print S/E key events for a short period",
    )
    parser.add_argument(
        "--diagnose-seconds",
        type=int,
        default=20,
        help="Seconds to listen during --diagnose-keyboard (default: 20)",
    )
    args = parser.parse_args()

    configure_logging(args.debug)

    if args.diagnose_keyboard:
        raise SystemExit(diagnose_keyboard(args.diagnose_seconds))

    player = RandomVideoPlayer(debug=args.debug)
    player.run()


if __name__ == "__main__":
    main()
