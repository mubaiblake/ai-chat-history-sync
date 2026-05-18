#!/usr/bin/env python3
"""Install the ChatGPT web auto-sync receiver as a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


DEFAULT_LABEL = "com.chatgpt-exporter-auto-sync.receiver"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Install the local ChatGPT auto-sync receiver.")
    parser.add_argument("--archive-root", type=Path, default=script_dir, help="Where inbox/raw/state/markdow_history should be stored.")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="LaunchAgent label.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Receiver host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Receiver port.")
    parser.add_argument("--python", default=sys.executable or "/usr/bin/python3", help="Python interpreter to run the receiver.")
    parser.add_argument("--no-load", action="store_true", help="Only write the plist; do not load it with launchctl.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_path = Path(__file__).resolve().with_name("sync_ai_chats.py")
    archive_root = args.archive_root.expanduser().resolve()
    state_dir = archive_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": args.label,
        "ProgramArguments": [
            args.python,
            str(script_path),
            "receive-chatgpt",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--archive-root",
            str(archive_root),
            "--sync-after-receive",
        ],
        "WorkingDirectory": str(script_path.parent.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(state_dir / "receiver.out.log"),
        "StandardErrorPath": str(state_dir / "receiver.err.log"),
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

    service = f"gui/{os.getuid()}/{args.label}"
    run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], check=False)
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)])
    run(["launchctl", "kickstart", "-k", service], check=False)
    print(f"Started {service}")
    print(f"Health check: http://{args.host}:{args.port}/ping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
