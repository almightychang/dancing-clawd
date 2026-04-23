#!/usr/bin/env bash
# dancing-claude installer.
#
# Copies hook.py / dance.py / mascot_frames.py into ~/.claude/dancing-claude
# (if not already there) and idempotently wires the hook into
# ~/.claude/settings.json. Safe to run multiple times.
#
# Usage:
#   ./install.sh            # install / update
#   ./install.sh --uninstall # remove hooks from settings.json (keeps files)

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${HOME}/.claude/dancing-claude"
SETTINGS="${HOME}/.claude/settings.json"
HOOK_CMD="python3 ${DEST_DIR}/hook.py"
FILES=(hook.py dance.py mascot_frames.py)

have() { command -v "$1" >/dev/null 2>&1; }

# --- preflight -------------------------------------------------------------
have python3 || { echo "error: python3 not found on PATH" >&2; exit 1; }

# --- uninstall path --------------------------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ ! -f "$SETTINGS" ]]; then
    echo "no settings.json at $SETTINGS — nothing to do"
    exit 0
  fi
  python3 - "$SETTINGS" "$HOOK_CMD" <<'PY'
import json, sys, pathlib, shutil, time
settings_path, hook_cmd = sys.argv[1], sys.argv[2]
p = pathlib.Path(settings_path)
data = json.loads(p.read_text())
hooks = data.get("hooks") or {}
changed = False
for event, groups in list(hooks.items()):
    new_groups = []
    for g in groups:
        g_hooks = [h for h in g.get("hooks", []) if h.get("command") != hook_cmd]
        if g_hooks:
            new_groups.append({**g, "hooks": g_hooks})
        else:
            changed = True
    if new_groups != groups:
        hooks[event] = new_groups
        changed = True
    if not hooks[event]:
        hooks.pop(event)
        changed = True
if changed:
    backup = p.with_suffix(f".json.bak.{int(time.time())}")
    shutil.copy2(p, backup)
    data["hooks"] = hooks
    if not data["hooks"]:
        data.pop("hooks")
    p.write_text(json.dumps(data, indent=2) + "\n")
    print(f"removed dancing-claude hooks (backup: {backup.name})")
else:
    print("no dancing-claude hooks found in settings.json")
PY
  echo "files at ${DEST_DIR} left in place — delete manually if you want them gone"
  exit 0
fi

# --- copy files ------------------------------------------------------------
mkdir -p "$DEST_DIR"
if [[ "$SRC_DIR" != "$DEST_DIR" ]]; then
  for f in "${FILES[@]}"; do
    if [[ ! -f "$SRC_DIR/$f" ]]; then
      echo "error: missing source file $SRC_DIR/$f" >&2
      exit 1
    fi
    install -m 0644 "$SRC_DIR/$f" "$DEST_DIR/$f"
  done
  echo "copied ${#FILES[@]} files → $DEST_DIR"
else
  echo "source == destination ($DEST_DIR), skipping copy"
fi
chmod +x "$DEST_DIR/hook.py" "$DEST_DIR/dance.py"

# --- merge hook config -----------------------------------------------------
python3 - "$SETTINGS" "$HOOK_CMD" <<'PY'
import json, sys, pathlib, shutil, time

settings_path, hook_cmd = sys.argv[1], sys.argv[2]
p = pathlib.Path(settings_path)
p.parent.mkdir(parents=True, exist_ok=True)

if p.exists():
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        print(f"error: {settings_path} is not valid JSON ({e})", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"error: {settings_path} top-level is not an object", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

EVENTS = {
    "UserPromptSubmit": False,  # no matcher
    "PreToolUse": True,         # matcher: ".*"
    "PostToolUse": True,
    "Notification": False,
    "Stop": False,
    "SubagentStop": False,
}

hooks = data.setdefault("hooks", {})
added = []
for event, with_matcher in EVENTS.items():
    groups = hooks.setdefault(event, [])
    already = any(
        any(h.get("command") == hook_cmd for h in g.get("hooks", []))
        for g in groups
    )
    if already:
        continue
    entry = {"hooks": [{"type": "command", "command": hook_cmd}]}
    if with_matcher:
        entry = {"matcher": ".*", **entry}
    groups.append(entry)
    added.append(event)

if added:
    if p.exists():
        backup = p.with_suffix(f".json.bak.{int(time.time())}")
        shutil.copy2(p, backup)
        print(f"backup saved: {backup.name}")
    p.write_text(json.dumps(data, indent=2) + "\n")
    print("wired hooks for: " + ", ".join(added))
else:
    print("hooks already wired — settings.json unchanged")
PY

cat <<EOF

done.

next steps:
  1. open a separate terminal and run:
       python3 ${DEST_DIR}/dance.py
  2. use Claude Code as usual — the mascots will react to hook events.

uninstall:  ${SRC_DIR}/install.sh --uninstall
EOF
