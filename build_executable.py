from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def build(target: str, *, cli: bool = False) -> None:
    current = platform.system().lower()
    required = "darwin" if target == "mac" else "windows"
    if current != required:
        raise SystemExit(
            f"The {target} executable must be built on {required}; current system is {current}."
        )

    entry = ("mac_client.py" if target == "mac" else "win_server.py") if cli else "launcher.py"
    name = "CrossKey-Mac" if target == "mac" else "CrossKey-Windows"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--name",
        name,
        "--collect-submodules",
        "pynput",
    ]
    if not cli:
        command.append("--windowed")
    if target == "mac":
        command.extend(["--hidden-import", "Quartz", "--hidden-import", "AppKit"])
    command.append(str(ROOT / entry))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a native CrossKey executable.")
    parser.add_argument("target", choices=("mac", "windows"))
    parser.add_argument("--cli", action="store_true", help="Build the legacy command-line entry point.")
    args = parser.parse_args()
    build(args.target, cli=args.cli)


if __name__ == "__main__":
    main()
