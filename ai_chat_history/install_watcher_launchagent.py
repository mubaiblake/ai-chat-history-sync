#!/usr/bin/env python3
"""Install the multi-source chat-history watcher as a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys


DEFAULT_LABEL = "com.ai-chat-history-sync.watcher"
DEFAULT_ARCHIVE_ROOT = Path.home() / "AI Chat History"


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the AI chat-history watcher.")
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT, help="Where inbox/raw/state/markdow_history should be stored.")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="LaunchAgent label.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")
    parser.add_argument("--python", default=sys.executable or "/usr/bin/python3", help="Python interpreter.")
    parser.add_argument("--move-web-exports", action="store_true", help="Also scan Downloads for web exports.")
    parser.add_argument("--no-load", action="store_true", help="Only write the plist; do not load it with launchctl.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_path = Path(__file__).resolve().with_name("sync_ai_chats.py")
    archive_root = args.archive_root.expanduser().resolve()
    (archive_root / "state").mkdir(parents=True, exist_ok=True)

    program_arguments = [
        args.python,
        str(script_path),
        "watch",
        "--archive-root",
        str(archive_root),
        "--interval",
        str(args.interval),
    ]
    if args.move_web_exports:
        program_arguments.append("--move-web-exports")

    plist = {
        "Label": args.label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(script_path.parent.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(archive_root / "state" / "watcher.out.log"),
        "StandardErrorPath": str(archive_root / "state" / "watcher.err.log"),
    }

    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{args.label}.plist"
    plist_path.write_bytes(plistlib.dumps(plist, sort_keys=False))

    print(f"Wrote {plist_path}")
    print(f"Archive root: {archive_root}")

    if args.no_load:
        print("Skipped launchctl load.")
        return 0

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{args.label}"
    run(["launchctl", "bootout", domain, str(plist_path)], check=False)
    run(["launchctl", "bootstrap", domain, str(plist_path)])
    run(["launchctl", "kickstart", "-k", service], check=False)
    print(f"Started {service}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
