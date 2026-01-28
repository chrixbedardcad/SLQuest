#!/usr/bin/env python3
"""cambot.py

Minimal Firestorm (Second Life) cambot controller using X11 automation.

Requires (on the Firestorm machine):
  - wmctrl
  - xdotool

Assumptions (current Chrix setup):
  - Firestorm window class: do-not-directly-run-firestorm-bin.do-not-directly-run-firestorm-bin
  - Snapshot hotkey: Ctrl+` (ctrl+grave) saves to disk
  - Snapshot folder: ~/Pictures/cambot
  - HUD listens on public chat channel 1 for waypoints: /1 1, /1 2, /1 3

Usage:
  python3 cambot.py health
  python3 cambot.py snap
  python3 cambot.py seq 1 2 3 --delay 2.0

Notes:
  This is UI automation: it will steal focus and type into the viewer.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional


FIRESTORM_WMCTRL_CLASS = "do-not-directly-run-firestorm-bin.do-not-directly-run-firestorm-bin"
SNAP_DIR = os.path.expanduser("~/Pictures/cambot")
LOCK_PATH = os.path.expanduser("~/.cache/cambot.lock")


class CambotError(RuntimeError):
    pass


def sh(cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run shell command."""
    return subprocess.run(
        cmd,
        shell=True,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def require_tools() -> None:
    for tool in ("wmctrl", "xdotool"):
        if sh(f"command -v {tool}", check=False).returncode != 0:
            raise CambotError(f"Missing dependency: {tool}. Install with: sudo apt-get install -y {tool}")


def focus_firestorm() -> None:
    # Activate by WM_CLASS (robust)
    r = sh(f"wmctrl -xa {FIRESTORM_WMCTRL_CLASS}", check=False, capture=True)
    if r.returncode != 0:
        raise CambotError(
            "Firestorm window not found. Is Firestorm running and on the same X11 DISPLAY?"
        )
    time.sleep(0.2)


def type_text(text: str, delay_ms: int = 10) -> None:
    # Use %q-like quoting by passing through python repr (single quotes). xdotool expects raw string.
    # We shell-escape via shlex.quote for safety.
    import shlex

    sh(f"xdotool type --delay {int(delay_ms)} {shlex.quote(text)}")


def key(keys: str) -> None:
    sh(f"xdotool key --clearmodifiers {keys}")


def say_local(text: str) -> None:
    """Type into the currently-focused chat input and press Enter."""
    type_text(text, delay_ms=10)
    key("Return")


def newest_image(path: str) -> Optional[str]:
    files = glob.glob(os.path.join(path, "*.png")) + glob.glob(os.path.join(path, "*.jpg")) + glob.glob(
        os.path.join(path, "*.jpeg")
    )
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def wait_new_image(folder: str, before: Optional[str], timeout_s: float = 10.0, poll_s: float = 0.2) -> Optional[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        after = newest_image(folder)
        if after and after != before:
            return after
        time.sleep(poll_s)
    return None


def snap_to_disk(folder: str = SNAP_DIR, timeout_s: float = 12.0) -> str:
    os.makedirs(folder, exist_ok=True)
    before = newest_image(folder)
    # Ctrl+` is xdotool "ctrl+grave"
    key("ctrl+grave")
    out = wait_new_image(folder, before, timeout_s=timeout_s)
    if not out:
        raise CambotError(f"No new snapshot detected in {folder} within {timeout_s}s")
    return out


@dataclass
class Health:
    ready: bool
    firestorm_window: bool
    snap_dir_exists: bool
    notes: List[str]


def healthcheck() -> Health:
    notes: List[str] = []
    require_tools()

    snap_dir_exists = os.path.isdir(SNAP_DIR)
    if not snap_dir_exists:
        notes.append(f"Snapshot dir missing (will be created on snap): {SNAP_DIR}")

    # window present?
    r = sh(f"wmctrl -lx | grep -i {FIRESTORM_WMCTRL_CLASS.split('.')[0]}", check=False, capture=True)
    firestorm_window = r.returncode == 0
    if not firestorm_window:
        notes.append("Firestorm window not detected.")

    ready = firestorm_window
    return Health(ready=ready, firestorm_window=firestorm_window, snap_dir_exists=snap_dir_exists, notes=notes)


class Lock:
    def __init__(self, path: str):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # simple lockfile using O_EXCL
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("utf-8"))
            return self
        except FileExistsError:
            raise CambotError(f"Busy (lock exists): {self.path}")

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def sequence(waypoints: List[int], delay_s: float) -> List[str]:
    """Run /1 <wp> in local chat then snap."""
    require_tools()
    with Lock(LOCK_PATH):
        focus_firestorm()
        shots: List[str] = []
        for wp in waypoints:
            say_local(f"/1 {wp}")
            time.sleep(delay_s)
            shots.append(snap_to_disk())
        return shots


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")

    sub.add_parser("snap")

    ap_seq = sub.add_parser("seq")
    ap_seq.add_argument("waypoints", nargs="+", type=int)
    ap_seq.add_argument("--delay", type=float, default=2.0, help="seconds to wait after /1 <n> before snapping")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "health":
            h = healthcheck()
            print(json.dumps({
                "ready": h.ready,
                "firestorm_window": h.firestorm_window,
                "snap_dir": SNAP_DIR,
                "snap_dir_exists": h.snap_dir_exists,
                "notes": h.notes,
            }, indent=2))
            return 0 if h.ready else 2

        if args.cmd == "snap":
            require_tools()
            with Lock(LOCK_PATH):
                focus_firestorm()
                path = snap_to_disk()
                print(json.dumps({"snapshot": path}, indent=2))
            return 0

        if args.cmd == "seq":
            shots = sequence(args.waypoints, delay_s=args.delay)
            print(json.dumps({"snapshots": shots}, indent=2))
            return 0

        raise CambotError("Unknown command")

    except CambotError as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
