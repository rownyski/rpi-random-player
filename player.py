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
USB_AUTOMOUNT_POINT = Path(os.environ.get("USB_AUTOMOUNT_POINT", "/mnt/usb"))
PREFERRED_USB_MOUNT_POINT = Path(os.environ.get("USB_MOUNT_POINT", "/mnt/usb"))
USB_DEVICE_GLOB = "sd*[0-9]"
SUPPORTED_USB_FILESYSTEMS = {"vfat", "exfat", "ntfs", "ext4", "ext3", "ext2"}
SCAN_TIMEOUT_SECONDS = 20.0
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
    "--vo=gpu",
    "--gpu-context=drm",
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

    def _resolve_drm_mode_arg(self) -> list[str]:
        drm_mode = os.environ.get("MPV_DRM_MODE", "").strip()
        if not drm_mode:
            return []

        logger.info("Using forced DRM mode from MPV_DRM_MODE=%s", drm_mode)
        return [f"--drm-mode={drm_mode}"]

    def _resolve_video_sync_arg(self) -> list[str]:
        # Default to audio-locked sync; this matched known-good behavior on target Pi4 setup.
        video_sync = os.environ.get("MPV_VIDEO_SYNC", "audio").strip()
        if not video_sync:
            logger.info("MPV_VIDEO_SYNC is empty; using mpv default video sync behavior.")
            return []

        logger.info("Using mpv video sync mode from MPV_VIDEO_SYNC=%s", video_sync)
        return [f"--video-sync={video_sync}"]

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
            [
                *MPV_BASE_CMD,
                *self._resolve_video_sync_arg(),
                *self._resolve_drm_mode_arg(),
                *self._resolve_audio_device_arg(),
                str(selected),
            ],
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
        deadline = started + SCAN_TIMEOUT_SECONDS

        mounts = self._discover_usb_mounts()
        if not mounts:
            logger.info("No USB mounts detected; attempting auto-mount.")
            self._attempt_usb_automount()
            mounts = self._discover_usb_mounts()

        candidates = self._scan_videos_from_mounts(mounts, deadline, reason)

        # Handle stale/empty mount points: re-attempt mount and scan once more.
        if not candidates:
            logger.info("No videos found in detected mounts; retrying mount+scan once.")
            self._attempt_usb_automount()
            mounts = self._discover_usb_mounts()
            candidates = self._scan_videos_from_mounts(mounts, deadline, f"{reason}-retry")

        self.video_candidates = candidates
        elapsed = time.monotonic() - started
        logger.info("Found %d candidate video files in %.2fs", len(self.video_candidates), elapsed)

    def _collect_videos_under_mount(self, mount: Path, deadline: float) -> list[Path]:
        candidates: list[Path] = []
        stack = [mount]

        while stack:
            if time.monotonic() >= deadline:
                logger.warning("Stopping scan of %s due to timeout.", mount)
                break

            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        entry_path = Path(entry.path)
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry_path)
                            elif entry.is_file(follow_symlinks=False) and entry_path.suffix.lower() in VIDEO_EXTENSIONS:
                                candidates.append(entry_path)
                        except OSError as exc:
                            logger.debug("Skipping entry %s due to read/stat error: %s", entry_path, exc)
            except OSError as exc:
                logger.debug("Skipping directory %s due to scan error: %s", current, exc)

        return candidates

    def _scan_videos_from_mounts(self, mounts: list[Path], deadline: float, reason: str) -> list[Path]:
        candidates: list[Path] = []
        logger.info(
            "Refreshing video list (%s). Detected mounts: %s",
            reason,
            ", ".join(str(m) for m in mounts) if mounts else "<none>",
        )

        for mount in mounts:
            if time.monotonic() >= deadline:
                logger.warning(
                    "USB scan timeout reached after %.1fs; using partial results.",
                    SCAN_TIMEOUT_SECONDS,
                )
                break

            if not os.path.ismount(mount):
                logger.warning("Skipping non-mounted path listed as USB mount: %s", mount)
                continue

            mount_candidates = self._collect_videos_under_mount(mount, deadline)
            logger.info("Scanned mount %s -> %d video file(s)", mount, len(mount_candidates))
            candidates.extend(mount_candidates)

        return candidates


    def _discover_usb_mounts(self) -> list[Path]:
        mounts_by_source: dict[str, list[Path]] = {}
        proc_mounts = Path("/proc/mounts")
        if not proc_mounts.exists():
            return []

        for line in proc_mounts.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue

            source, mountpoint, fs_type = parts[:3]
            mount_path = Path(mountpoint)
            if not source.startswith("/dev/sd"):
                continue
            if fs_type not in SUPPORTED_USB_FILESYSTEMS:
                continue
            if not any(str(mount_path).startswith(str(root)) for root in USB_SCAN_ROOTS):
                continue
            if not mount_path.exists() or not os.path.ismount(mount_path):
                continue

            mounts_by_source.setdefault(source, []).append(mount_path)

        selected_mounts: list[Path] = []
        for source, mountpoints in mounts_by_source.items():
            unique = sorted(set(mountpoints))
            preferred = next((m for m in unique if m == PREFERRED_USB_MOUNT_POINT), None)
            selected = preferred or unique[0]
            if len(unique) > 1:
                logger.info(
                    "Device %s mounted at multiple paths (%s); using %s",
                    source,
                    ", ".join(str(m) for m in unique),
                    selected,
                )
            selected_mounts.append(selected)

        return sorted(selected_mounts)

    def _attempt_usb_automount(self) -> None:
        mounted_sources: set[str] = set()
        proc_mounts = Path("/proc/mounts")
        if not proc_mounts.exists():
            return []

        for line in proc_mounts.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue

            source, mountpoint, fs_type = parts[:3]
            mount_path = Path(mountpoint)
            if not source.startswith("/dev/sd"):
                continue
            if fs_type not in SUPPORTED_USB_FILESYSTEMS:
                continue
            if not any(str(mount_path).startswith(str(root)) for root in USB_SCAN_ROOTS):
                continue
            if not mount_path.exists() or not os.path.ismount(mount_path):
                continue

            mounts.add(mount_path)

        return sorted(mounts)

    def _attempt_usb_automount(self) -> None:
        mounted_sources: set[str] = set()
        proc_mounts = Path("/proc/mounts")
        if proc_mounts.exists():
            for line in proc_mounts.read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    mounted_sources.add(parts[0])

        devices = sorted(Path("/dev").glob(USB_DEVICE_GLOB))
        if not devices:
            logger.debug("No /dev/%s device nodes found for auto-mount attempt.", USB_DEVICE_GLOB)
            return

        mounted_any = False
        for device in devices:
            source = str(device)
            if source in mounted_sources:
                continue

            target = USB_AUTOMOUNT_POINT if not mounted_any else Path(f"{USB_AUTOMOUNT_POINT}-{device.name}")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create mount directory %s: %s", target, exc)
                continue

            result = subprocess.run(
                ["mount", source, str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                logger.info("Mounted USB partition %s at %s", source, target)
                mounted_any = True
                continue

            logger.warning(
                "Auto-mount failed for %s -> %s (code=%s): %s",
                source,
                target,
                result.returncode,
                (result.stderr or result.stdout).strip() or "no output",
            )

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

    while True:
        player = RandomVideoPlayer(debug=args.debug)
        try:
            player.run()
        except Exception:
            logger.exception("Unhandled player error; restarting player loop in 2s.")
            time.sleep(2)
            continue

        logger.warning("Player loop exited unexpectedly; restarting in 2s.")
        time.sleep(2)


if __name__ == "__main__":
    main()
