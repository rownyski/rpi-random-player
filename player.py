#!/usr/bin/env python3
"""Standalone random USB video player for Raspberry Pi."""

from __future__ import annotations

import json
import os
import random
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import evdev
from evdev import ecodes

VIDEO_EXTENSIONS = {".mp4", ".mkv"}
USB_SCAN_ROOTS = [Path("/media"), Path("/run/media"), Path("/mnt")]
FFPROBE_TIMEOUT_SECONDS = 4
POLL_INTERVAL_SECONDS = 0.05

MPV_BASE_CMD = [
    "mpv",
    "--fullscreen",
    "--hwdec=drm",
    "--no-terminal",
    "--quiet",
    "--no-osc",
    "--no-osd-bar",
]


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    bucket: str


class RandomVideoPlayer:
    def __init__(self) -> None:
        self.mpv_process: Optional[subprocess.Popen] = None
        self.auto_restart = False
        self.running = True

    def run(self) -> None:
        keyboards = self._open_keyboard_devices()
        if not keyboards:
            print("No keyboard input devices found. Retrying until one appears.")

        try:
            while self.running:
                if not keyboards:
                    time.sleep(1)
                    keyboards = self._open_keyboard_devices()
                else:
                    ready, _, _ = select.select(keyboards, [], [], POLL_INTERVAL_SECONDS)
                    for device in ready:
                        for event in device.read():
                            self._handle_event(event)

                self._check_playback_exit()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_playback()
            for keyboard in keyboards:
                keyboard.close()

    def _open_keyboard_devices(self) -> list[evdev.InputDevice]:
        devices: list[evdev.InputDevice] = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities().get(ecodes.EV_KEY, [])
                keycodes = {code for code in caps if isinstance(code, int)}
                if ecodes.KEY_S in keycodes and ecodes.KEY_E in keycodes:
                    dev.grab()
                    devices.append(dev)
            except OSError:
                continue
        return devices

    def _handle_event(self, event: evdev.InputEvent) -> None:
        if event.type != ecodes.EV_KEY:
            return

        key_event = evdev.categorize(event)
        if key_event.keystate != key_event.key_down:
            return

        if key_event.scancode == ecodes.KEY_S:
            self.start_random_video(force_restart=True)
        elif key_event.scancode == ecodes.KEY_E:
            self.stop_playback()

    def _check_playback_exit(self) -> None:
        if not self.mpv_process:
            return

        ret_code = self.mpv_process.poll()
        if ret_code is None:
            return

        self.mpv_process = None
        if self.auto_restart:
            self.start_random_video(force_restart=False)

    def start_random_video(self, force_restart: bool) -> None:
        if self.mpv_process and force_restart:
            self.stop_playback()

        videos = self.scan_and_classify_videos()
        selected = self.select_video(videos)
        if not selected:
            self.auto_restart = False
            print("No playable videos found on mounted USB storage.")
            return

        self.auto_restart = True
        self.mpv_process = subprocess.Popen(
            [*MPV_BASE_CMD, str(selected.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    def stop_playback(self) -> None:
        self.auto_restart = False
        if not self.mpv_process:
            return

        process = self.mpv_process
        self.mpv_process = None
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return

    def scan_and_classify_videos(self) -> list[VideoInfo]:
        candidates: list[Path] = []
        for mount in self._discover_usb_mounts():
            for path in mount.rglob("*"):
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                    candidates.append(path)

        return [VideoInfo(path=path, bucket=self.classify_video(path)) for path in candidates]

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

    def classify_video(self, video_path: Path) -> str:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt",
            "-of",
            "json",
            str(video_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=True,
                text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
            data = json.loads(result.stdout)
            stream = data.get("streams", [{}])[0]
        except Exception:
            return "safe"

        codec = (stream.get("codec_name") or "").lower()
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        pix_fmt = (stream.get("pix_fmt") or "").lower()

        max_dim = max(width, height)
        is_4k = max_dim > 1920
        is_10bit = "10" in pix_fmt

        if codec == "h264" and not is_4k:
            return "safe"
        if codec in {"hevc", "h265"} and not is_4k and not is_10bit:
            return "safe"
        if codec in {"hevc", "h265"} and is_4k and not is_10bit:
            return "medium"
        if codec in {"hevc", "h265"} and is_4k and is_10bit:
            return "risky"
        return "safe"

    def select_video(self, videos: list[VideoInfo]) -> Optional[VideoInfo]:
        if not videos:
            return None

        safe = [video for video in videos if video.bucket == "safe"]
        medium = [video for video in videos if video.bucket == "medium"]
        risky = [video for video in videos if video.bucket == "risky"]

        if safe:
            if medium and random.random() > 0.8:
                return random.choice(medium)
            return random.choice(safe)

        if medium:
            return random.choice(medium)

        if risky:
            return random.choice(risky)

        return random.choice(videos)


def main() -> None:
    player = RandomVideoPlayer()
    player.run()


if __name__ == "__main__":
    main()
