#!/usr/bin/env python3
"""Standalone random USB video player for Raspberry Pi."""

from __future__ import annotations

import argparse
import json
import logging
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
    "--ao=alsa",
]

logger = logging.getLogger("rpi_random_player")

CACHE_FILE_PATH = Path("/var/cache/rpi-random-player/classification-cache.json")


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    bucket: str


class RandomVideoPlayer:
    def __init__(self, debug: bool = False) -> None:
        self.mpv_process: Optional[subprocess.Popen] = None
        self.auto_restart = False
        self.running = True
        self.debug = debug
        self.classification_cache: dict[Path, tuple[int, int, str]] = {}
        self._load_classification_cache()

    def run(self) -> None:
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
            self.start_random_video(force_restart=False)

    def start_random_video(self, force_restart: bool) -> None:
        if self.mpv_process and force_restart:
            logger.info("Force restart requested; stopping current video first.")
            self.stop_playback()

        logger.info("Scanning USB mounts for playable videos...")
        candidates = self.scan_video_candidates()
        if not candidates:
            self.auto_restart = False
            logger.warning("No playable videos found on mounted USB storage.")
            return

        selected = self.select_video_from_candidates(candidates)
        if not selected:
            self.auto_restart = False
            logger.warning("No playable videos found on mounted USB storage.")
            return

        logger.info("Starting playback: %s [%s]", selected.path, selected.bucket)
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
            logger.debug("Stop requested, but no active mpv process.")
            return

        process = self.mpv_process
        self.mpv_process = None
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            logger.info("Sent SIGTERM to mpv process group pid=%s", process.pid)
        except ProcessLookupError:
            logger.debug("mpv process already exited before SIGTERM.")

    def scan_video_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        mounts = self._discover_usb_mounts()
        logger.info("Detected mounts for scanning: %s", ", ".join(str(m) for m in mounts) if mounts else "<none>")
        for mount in mounts:
            for path in mount.rglob("*"):
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                    candidates.append(path)

        logger.info("Found %d candidate video files", len(candidates))
        return candidates

    def select_video_from_candidates(self, candidates: list[Path]) -> Optional[VideoInfo]:
        videos_from_cache: list[VideoInfo] = []
        uncached: list[Path] = []

        for path in candidates:
            bucket = self._cached_bucket_for(path)
            if bucket is None:
                uncached.append(path)
            else:
                videos_from_cache.append(VideoInfo(path=path, bucket=bucket))

        logger.info("Selection pool: cached=%d uncached=%d", len(videos_from_cache), len(uncached))

        if videos_from_cache:
            safe = sum(video.bucket == "safe" for video in videos_from_cache)
            medium = sum(video.bucket == "medium" for video in videos_from_cache)
            risky = sum(video.bucket == "risky" for video in videos_from_cache)
            logger.info("Cached classification totals: safe=%d medium=%d risky=%d", safe, medium, risky)
            selected = self.select_video(videos_from_cache)
            if selected:
                return selected

        # First-run fast path: classify only one random uncached file to keep START latency low.
        if uncached:
            selected_path = random.choice(uncached)
            logger.info("No cached classifications available; probing one file: %s", selected_path)
            bucket = self.classify_video(selected_path)
            self._update_cache(selected_path, bucket)
            return VideoInfo(path=selected_path, bucket=bucket)

        return None

    def _cached_bucket_for(self, path: Path) -> Optional[str]:
        try:
            stat = path.stat()
        except OSError:
            return None

        cached = self.classification_cache.get(path)
        if not cached:
            return None

        mtime_ns, size, bucket = cached
        if mtime_ns == stat.st_mtime_ns and size == stat.st_size:
            return bucket

        return None

    def _update_cache(self, path: Path, bucket: str) -> None:
        try:
            stat = path.stat()
        except OSError:
            return

        self.classification_cache[path] = (stat.st_mtime_ns, stat.st_size, bucket)
        self._save_classification_cache()

    def _load_classification_cache(self) -> None:
        if not CACHE_FILE_PATH.exists():
            return

        try:
            payload = json.loads(CACHE_FILE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to read classification cache: %s", exc)
            return

        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        loaded = 0
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            mtime_ns = value.get("mtime_ns")
            size = value.get("size")
            bucket = value.get("bucket")
            if not isinstance(mtime_ns, int) or not isinstance(size, int) or not isinstance(bucket, str):
                continue
            self.classification_cache[Path(key)] = (mtime_ns, size, bucket)
            loaded += 1

        if loaded:
            logger.info("Loaded %d cached classifications", loaded)

    def _save_classification_cache(self) -> None:
        try:
            CACHE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "entries": {
                    str(path): {
                        "mtime_ns": mtime_ns,
                        "size": size,
                        "bucket": bucket,
                    }
                    for path, (mtime_ns, size, bucket) in self.classification_cache.items()
                }
            }
            CACHE_FILE_PATH.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to persist classification cache: %s", exc)

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
        except Exception as exc:
            logger.debug("ffprobe failed for %s: %s. Defaulting to safe.", video_path, exc)
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
