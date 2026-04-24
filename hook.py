#!/usr/bin/env python3
"""Dancing Claude hook — per-session state updater.

Multiple Claude Code sessions can fire this hook concurrently. We flock the
state file so writes don't race. State schema:

  {
    "sessions": {
      "<session_id>": {
        "mood": "...", "subagent_count": N,
        "bubble": "...", "bubble_ts": ..., "mood_ts": ..., "last_seen": ...
      }
    },
    "last_update_sid": "<session_id>"
  }

dance.py reads this state file and renders one Claude per session + one per
active subagent, all random-walking the terminal.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_user_name

STATE_DIR = Path.home() / ".claude" / "dancing-claude"
STATE = STATE_DIR / "state.json"
LOCK = STATE_DIR / "state.lock"

DEFAULT_SESSION = {
    "mood": "idle",
    "subagent_count": 0,
    "bubble": "",
    "bubble_ts": 0,
    "mood_ts": 0,
    "last_seen": 0,
}


def load_unlocked() -> dict:
    try:
        with open(STATE) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "sessions" not in data:
            return {"sessions": {}, "last_update_sid": ""}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"sessions": {}, "last_update_sid": ""}


def save_unlocked(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def trunc(s: str, n: int = 48) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_hint(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        return trunc(tool_input.get("description") or tool_input.get("command", ""))
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return trunc(tool_input.get("file_path", ""))
    if tool == "WebFetch":
        return trunc(tool_input.get("url", ""))
    if tool == "WebSearch":
        return trunc(tool_input.get("query", ""))
    if tool in ("Grep", "Glob"):
        return trunc(tool_input.get("pattern", ""))
    if tool == "Task":
        return trunc(tool_input.get("description", "subagent"))
    return ""


def set_mood(session: dict, mood: str, now: float, bubble: str | None = None) -> None:
    session["mood"] = mood
    session["mood_ts"] = now
    session["last_seen"] = now
    if bubble is not None:
        session["bubble"] = bubble
        session["bubble_ts"] = now


def handle(event: dict, state: dict, now: float) -> bool:
    name = event.get("hook_event_name", "")
    sid = event.get("session_id") or "default"

    sessions = state.setdefault("sessions", {})
    session = sessions.get(sid)
    if session is None:
        session = dict(DEFAULT_SESSION)
        sessions[sid] = session
    session["last_seen"] = now

    if name == "UserPromptSubmit":
        prompt = trunc(event.get("prompt", ""), 56)
        user = get_user_name() or "you"
        set_mood(session, "working", now, bubble=f"{user}: {prompt}" if prompt else "thinking…")
    elif name == "PreToolUse":
        tool = event.get("tool_name", "")
        ti = event.get("tool_input", {}) or {}
        hint = tool_hint(tool, ti)
        if tool == "Task":
            session["subagent_count"] = int(session.get("subagent_count", 0) or 0) + 1
            set_mood(session, "working", now, bubble=f"spawning: {hint}" if hint else "spawning subagent")
        else:
            bubble = f"{tool}: {hint}" if hint else tool
            set_mood(session, "tool_running", now, bubble=bubble)
    elif name == "PostToolUse":
        tool = event.get("tool_name", "")
        resp = event.get("tool_response", {}) or {}
        is_error = bool(resp.get("is_error")) if isinstance(resp, dict) else False
        if is_error:
            set_mood(session, "error", now, bubble=f"{tool} failed")
        else:
            set_mood(session, "working", now)
    elif name == "Notification":
        msg = trunc(event.get("message", "needs attention"), 56)
        set_mood(session, "needs_you", now, bubble=msg)
    elif name == "Stop":
        set_mood(session, "done", now, bubble="all done")
    elif name == "SubagentStop":
        session["subagent_count"] = max(0, int(session.get("subagent_count", 0) or 0) - 1)
    else:
        return False

    state["last_update_sid"] = sid
    return True


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        event = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        with open(LOCK, "a") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            state = load_unlocked()
            if handle(event, state, now):
                save_unlocked(state)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
