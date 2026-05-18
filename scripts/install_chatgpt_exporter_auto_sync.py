#!/usr/bin/env python3
"""Initialize or update the chatgpt-exporter-auto-sync submodule."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


DEFAULT_URL = "https://github.com/mubaiblake/chatgpt-exporter-auto-sync.git"
DEFAULT_PATH = Path("vendor/chatgpt-exporter-auto-sync")


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the ChatGPT auto-sync companion submodule.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Git URL for chatgpt-exporter-auto-sync.")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="Submodule path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.path / ".git").exists() or (args.path / "package.json").exists():
        run(["git", "submodule", "update", "--init", "--recursive", str(args.path)])
        return 0
    args.path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "submodule", "add", args.url, str(args.path)])
    run(["git", "submodule", "update", "--init", "--recursive", str(args.path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
