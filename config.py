"""Dancing-clawd config — user name and other persistent prefs.

Stored at ~/.claude/dancing-claude/config.json. Read by hook.py (per event)
and by dance.py (at startup). Writes are atomic via tmp + replace.
"""

import json
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "dancing-claude" / "config.json"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_user_name() -> str:
    return (load_config().get("user_name") or "").strip()


def set_user_name(name: str) -> None:
    config = load_config()
    config["user_name"] = name.strip()
    save_config(config)


def prompt_user_name() -> str:
    """Interactively ask for a name on stdin and persist it. Blocks until a
    non-empty name is given or stdin closes (in which case returns '')."""
    while True:
        try:
            name = input("What's your name? ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if name:
            set_user_name(name)
            return name
        print("(please enter a non-empty name)")
