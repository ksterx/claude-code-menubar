#!/usr/bin/env python3
"""
MenubarCC hook bridge.

Invoked by Claude Code as a hook command. Reads the JSON event from stdin,
consults the MenubarCC config, and plays the appropriate sound via afplay.

Config: ~/Library/Application Support/com.ksterx.MenubarCC/hook-config.json
    {
      "muteAll": false,
      "perEventEnabled": {"Stop": true, "Notification": true, "PermissionRequest": true},
      "soundPaths":      {"Stop": null,  "Notification": null,  "PermissionRequest": null}
    }

Exits 0 in all cases so it never blocks Claude Code's work.
"""

import json
import subprocess
import sys
from pathlib import Path


CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "com.ksterx.MenubarCC"
    / "hook-config.json"
)

# Default sounds shipped with macOS — no bundled assets required.
DEFAULT_SOUNDS: dict[str, str] = {
    "Stop":              "/System/Library/Sounds/Glass.aiff",
    "Notification":      "/System/Library/Sounds/Tink.aiff",
    "PermissionRequest": "/System/Library/Sounds/Funk.aiff",
}

SUPPORTED_EVENTS = set(DEFAULT_SOUNDS.keys())


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_sound_path(event: str, cfg: dict) -> str | None:
    custom = (cfg.get("soundPaths") or {}).get(event)
    if isinstance(custom, str) and custom:
        p = Path(custom).expanduser()
        if p.is_file():
            return str(p)
    default = DEFAULT_SOUNDS.get(event)
    if default and Path(default).is_file():
        return default
    return None


def main() -> None:
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            sys.exit(0)
        payload = json.loads(raw)
        event = payload.get("hook_event_name", "")
        if event not in SUPPORTED_EVENTS:
            sys.exit(0)

        cfg = _load_config()

        if bool(cfg.get("muteAll", False)):
            sys.exit(0)

        per_event = cfg.get("perEventEnabled") or {}
        # Default to enabled when not set so a fresh install still beeps
        if not bool(per_event.get(event, True)):
            sys.exit(0)

        sound_path = _resolve_sound_path(event, cfg)
        if not sound_path:
            sys.exit(0)

        subprocess.Popen(
            ["afplay", sound_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        # Never break Claude Code — fail silently
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
