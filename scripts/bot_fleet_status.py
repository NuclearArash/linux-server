#!/usr/bin/env python3
"""Render registered bot process status lines for startup notifications."""

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path


BOT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _is_process_running(pid_file: Path) -> tuple[bool, int | None]:
    try:
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False, None

    try:
        pid = int(raw_pid)
    except ValueError:
        return False, None
    if pid <= 0:
        return False, None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, pid
    return True, pid


def render_bot_status_lines(registry_path: Path, pid_directory: Path) -> list[str]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or not isinstance(registry.get("bots"), list):
        raise ValueError("bot registry must contain a bots list")

    lines = []
    for bot in registry["bots"]:
        if not isinstance(bot, dict):
            raise ValueError("bot registry entries must be objects")
        bot_id = bot.get("id")
        if not isinstance(bot_id, str) or not BOT_ID_PATTERN.fullmatch(bot_id):
            raise ValueError("bot registry entry has an invalid id")

        raw_name = bot.get("name")
        name = " ".join(raw_name.split()) if isinstance(raw_name, str) else ""
        safe_name = html.escape(name or bot_id, quote=True)
        running, pid = _is_process_running(pid_directory / f"bot-{bot_id}.pid")
        if running:
            lines.append(f"🟢 {safe_name}: RUNNING (PID {pid})")
        else:
            lines.append(f"🔴 {safe_name}: STOPPED")

    return lines or ["No bots configured"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--pid-directory", type=Path, default=Path("/tmp"))
    args = parser.parse_args()

    try:
        lines = render_bot_status_lines(args.registry, args.pid_directory)
    except (OSError, ValueError, TypeError) as error:
        print(f"Unable to read bot fleet status: {error}", file=sys.stderr)
        return 1

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
