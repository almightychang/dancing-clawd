#!/usr/bin/env python3
"""Dancing Claude crowd — every session's Claudes random-walk the terminal.

One session = one main Claude. Each Task tool-call spawns a subagent Claude.
All active Claudes wander the terminal at once, each colored by its session's
mood. A speech bubble floats above the most-recently-updated session's main.

Run in a spare terminal while you work:
    python3 ~/.claude/dancing-claude/dance.py
"""

import json
import math
import os
import random
import shutil
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_user_name, prompt_user_name
from mascot_frames import FRAMES_BY_MOOD, FRAME_COLS, FRAME_PX_ROWS, FRAME_ROWS

STATE = Path.home() / ".claude" / "dancing-claude" / "state.json"

TICK_SEC = 0.18
BUBBLE_TTL = 12.0
SESSION_IDLE_TTL = 600.0   # drop sessions silent for 10 min
SESSION_DONE_TTL = 15.0    # linger after Stop, then leave
TRANSIENT_MOOD_TTL = 60.0  # working/tool_running decays to idle

RESET = "\033[0m"
DIM = "\033[2m"
HIDE = "\033[?25l"
SHOW = "\033[?25h"
CLEAR = "\033[2J\033[H"
BUBBLE_COLOR = "\033[33m"

# Body color per mood (256-color index). Palette value 3 is always white (eyes);
# every other nonzero palette value uses the mood body color.
BODY_IDX = {
    "idle": 130,         # muted amber
    "working": 208,      # Claude brand orange
    "tool_running": 214, # brighter orange
    "error": 196,        # red
    "needs_you": 220,    # yellow
    "done": 46,          # green
}
EYE_IDX = 231  # bright white (palette slot 3)
BG_RESET = "\033[49m"

DANCER_W = FRAME_COLS      # 16 pixel cols = 16 terminal cols
DANCER_H = FRAME_ROWS      # 8 terminal rows (16 pixel rows via half-block)


def _color_of(px: int, body_idx: int) -> int:
    """Map palette value to 256-color index. 0 = transparent handled by caller."""
    if px == 3:
        return EYE_IDX
    return body_idx


def render_sprite_cells(frame, body_idx: int):
    """Render a 16×16 pixel frame as half-block terminal cells.

    Returns list of (row_offset, col_offset, ansi_text, visible_width) for each
    contiguous opaque run.  row_offset is 0..7, col_offset is 0..15.
    """
    runs: list[tuple[int, int, str, int]] = []
    for ty in range(FRAME_ROWS):
        py_top = ty * 2
        py_bot = py_top + 1
        run_start_col = None
        run_chars: list[str] = []
        for x in range(FRAME_COLS):
            top = frame[py_top][x]
            bot = frame[py_bot][x] if py_bot < FRAME_PX_ROWS else 0
            if top == 0 and bot == 0:
                if run_start_col is not None:
                    runs.append((ty, run_start_col, "".join(run_chars) + RESET,
                                 len(run_chars)))
                    run_start_col = None
                    run_chars = []
                continue
            if run_start_col is None:
                run_start_col = x
            if top != 0 and bot != 0:
                fg = _color_of(top, body_idx)
                bg = _color_of(bot, body_idx)
                run_chars.append(f"\033[38;5;{fg}m\033[48;5;{bg}m▀")
            elif top != 0:
                fg = _color_of(top, body_idx)
                run_chars.append(f"{BG_RESET}\033[38;5;{fg}m▀")
            else:  # bot != 0
                fg = _color_of(bot, body_idx)
                run_chars.append(f"{BG_RESET}\033[38;5;{fg}m▄")
        if run_start_col is not None:
            runs.append((ty, run_start_col, "".join(run_chars) + RESET,
                         len(run_chars)))
    return runs


def visual_width(s: str) -> int:
    """Terminal display width — CJK wide chars count as 2."""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def wrap_text(text: str, max_w: int) -> list[str]:
    """Greedy word wrap honoring visual width; hard-breaks over-long words."""
    text = (text or "").strip()
    if not text:
        return []
    tokens = text.split(" ")
    lines: list[str] = []
    cur: list[str] = []
    cur_w = 0
    for tok in tokens:
        tw = visual_width(tok)
        if not cur:
            cur = [tok]
            cur_w = tw
        elif cur_w + 1 + tw <= max_w:
            cur.append(tok)
            cur_w += 1 + tw
        else:
            lines.append(" ".join(cur))
            cur = [tok]
            cur_w = tw
    if cur:
        lines.append(" ".join(cur))

    out: list[str] = []
    for line in lines:
        if visual_width(line) <= max_w:
            out.append(line)
            continue
        buf, bw = "", 0
        for ch in line:
            cw = visual_width(ch)
            if bw + cw > max_w and buf:
                out.append(buf)
                buf, bw = ch, cw
            else:
                buf += ch
                bw += cw
        if buf:
            out.append(buf)
    return out


def pose_for(mood: str, tick: int, offset: int):
    """Return a 16×16 pixel frame for the given mood."""
    frames = FRAMES_BY_MOOD.get(mood) or FRAMES_BY_MOOD["working"]
    if mood == "idle":
        return frames[(tick // 4 + offset) % len(frames)]
    return frames[(tick + offset) % len(frames)]


def load_state() -> dict:
    try:
        with open(STATE) as f:
            data = json.load(f)
        if isinstance(data, dict) and "sessions" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"sessions": {}, "last_update_sid": ""}


def effective_mood(session: dict, now: float) -> str:
    mood = session.get("mood", "idle")
    if mood in ("working", "tool_running"):
        if now - session.get("mood_ts", 0) > TRANSIENT_MOOD_TTL:
            return "idle"
    return mood


def term_size() -> tuple[int, int]:
    try:
        ts = shutil.get_terminal_size((80, 24))
        return ts.columns, max(ts.lines, 10)
    except OSError:
        return 80, 24


class Dancer:
    __slots__ = ("sid", "idx", "x", "y", "vx", "vy", "prev_cells", "tick_offset")

    def __init__(self, sid: str, idx: int, w: int, h: int):
        self.sid = sid
        self.idx = idx  # 0 = session main, 1+ = subagent
        self.x = random.uniform(0, max(1, w - DANCER_W))
        self.y = random.uniform(0, max(1, h - DANCER_H - 1))
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(0.35, 0.7)
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed * 0.5  # rows visually taller → slow down y
        self.prev_cells: list[tuple[int, int, int]] = []
        self.tick_offset = random.randint(0, 7)

    def step(self, w: int, h: int, frozen: bool) -> None:
        if frozen:
            return
        if random.random() < 0.05:
            ang = random.uniform(0, 2 * math.pi)
            mag = random.uniform(0.15, 0.45)
            self.vx += math.cos(ang) * mag
            self.vy += math.sin(ang) * mag * 0.5
            s = math.hypot(self.vx, self.vy * 2)
            if s > 1.1:
                self.vx *= 1.0 / s
                self.vy *= 1.0 / s
        self.x += self.vx
        self.y += self.vy
        if self.x < 0:
            self.x = 0
            self.vx = -self.vx
        elif self.x > w - DANCER_W:
            self.x = w - DANCER_W
            self.vx = -self.vx
        if self.y < 0:
            self.y = 0
            self.vy = -self.vy
        elif self.y > h - DANCER_H - 1:
            self.y = h - DANCER_H - 1
            self.vy = -self.vy


def cursor(row: int, col: int) -> str:
    return f"\033[{row};{col}H"


def build_bubble_at(text: str, anchor_col: int, anchor_row: int, w: int, h: int):
    text = (text or "").strip()
    if not text:
        return []
    # Inner text budget. Cap at ~40 visual cols so bubbles don't eat the canvas.
    max_inner = max(10, min(40, w - 6))
    lines = wrap_text(text, max_inner)
    if not lines:
        return []
    inner_w = max(visual_width(line) for line in lines)
    border_len = inner_w + 2  # matches the "| " + line + " |" inner span
    top = " " + "_" * border_len + " "
    bot = " " + "‾" * border_len + " "
    mids = []
    for line in lines:
        pad = inner_w - visual_width(line)
        mids.append("| " + line + " " * pad + " |")
    bubble_w = visual_width(top)  # same as mid and bot
    total_h = 2 + len(mids)  # top + mid(s) + bot

    # Try above first, then below. +1 for tail gap.
    row_top = anchor_row - total_h - 1
    below = False
    if row_top < 1:
        row_top = anchor_row + DANCER_H + 1
        below = True
    if row_top + total_h - 1 > h:
        return []

    left = max(1, anchor_col - bubble_w // 2)
    if left + bubble_w - 1 > w:
        left = max(1, w - bubble_w + 1)

    cells = []
    cells.append((row_top, left, top))
    for i, mid in enumerate(mids):
        cells.append((row_top + 1 + i, left, mid))
    cells.append((row_top + 1 + len(mids), left, bot))
    tail_col = max(left, min(anchor_col, left + bubble_w - 1))
    tail_char = "^" if below else "v"
    tail_row = (row_top - 1) if below else (row_top + total_h)
    if 1 <= tail_row <= h:
        cells.append((tail_row, tail_col, tail_char))
    return cells


def ensure_user_name() -> str:
    """Return the configured user name; prompt for one on first run."""
    name = get_user_name()
    if name:
        return name
    if not sys.stdin.isatty():
        return ""
    print("First run — let's save a name for your mascots' speech bubbles.")
    return prompt_user_name()


def main() -> int:
    ensure_user_name()
    sys.stdout.write(HIDE + CLEAR)
    sys.stdout.flush()

    w, h = term_size()
    dancers: dict[tuple[str, int], Dancer] = {}
    prev_bubble_cells: list[tuple[int, int, int]] = []
    tick = 0
    last_size = (w, h)

    try:
        while True:
            w, h = term_size()
            resized = (w, h) != last_size
            if resized:
                sys.stdout.write(CLEAR)
                prev_bubble_cells = []
                for d in dancers.values():
                    d.prev_cells = []
                    d.x = min(d.x, max(0, w - DANCER_W))
                    d.y = min(d.y, max(0, h - DANCER_H - 1))
                last_size = (w, h)

            state = load_state()
            sessions = state.get("sessions", {}) or {}
            now = time.time()

            active: dict[str, dict] = {}
            for sid, sdata in sessions.items():
                if not isinstance(sdata, dict):
                    continue
                last_seen = float(sdata.get("last_seen", 0) or 0)
                if now - last_seen > SESSION_IDLE_TTL:
                    continue
                if sdata.get("mood") == "done":
                    if now - float(sdata.get("mood_ts", 0) or 0) > SESSION_DONE_TTL:
                        continue
                active[sid] = sdata

            required: set[tuple[str, int]] = set()
            for sid, sdata in active.items():
                required.add((sid, 0))
                for i in range(int(sdata.get("subagent_count", 0) or 0)):
                    required.add((sid, i + 1))

            # Remove obsolete dancers (we'll erase their cells in pass 1).
            removed = [k for k in dancers if k not in required]
            removed_dancers = [dancers.pop(k) for k in removed]

            # Add new dancers for newly-required keys.
            for key in required:
                if key not in dancers:
                    dancers[key] = Dancer(key[0], key[1], w, h)

            # Step each remaining dancer.
            for key, d in dancers.items():
                sdata = active.get(d.sid, {})
                mood = effective_mood(sdata, now)
                d.step(w, h, frozen=(mood == "needs_you"))

            # Compose frame: pass 1 erase, pass 2 draw.
            out_parts: list[str] = []

            for d in removed_dancers:
                for (r, c, width) in d.prev_cells:
                    out_parts.append(cursor(r, c) + " " * width)
            for d in dancers.values():
                for (r, c, width) in d.prev_cells:
                    out_parts.append(cursor(r, c) + " " * width)
            for (r, c, width) in prev_bubble_cells:
                out_parts.append(cursor(r, c) + " " * width)
            prev_bubble_cells = []

            for key, d in dancers.items():
                sdata = active.get(d.sid, {})
                mood = effective_mood(sdata, now)
                body_idx = BODY_IDX.get(mood, BODY_IDX["working"])
                frame = pose_for(mood, tick, d.idx + d.tick_offset)
                x_int, y_int = int(d.x), int(d.y)
                new_cells: list[tuple[int, int, int]] = []
                for (row_off, col_off, ansi_text, vis_w) in render_sprite_cells(frame, body_idx):
                    row = y_int + row_off + 1
                    col = x_int + col_off + 1
                    if row < 1 or row > h or col < 1:
                        continue
                    # Clip visible width to terminal width.
                    if col + vis_w - 1 > w:
                        vis_w = w - col + 1
                        if vis_w <= 0:
                            continue
                        # Can't easily clip an ansi_text mid-run; drop partial runs.
                        # (They'd only appear near right edge during bounce.)
                        continue
                    out_parts.append(cursor(row, col) + ansi_text)
                    new_cells.append((row, col, vis_w))
                d.prev_cells = new_cells

            # Speech bubbles — one per session with a fresh bubble.
            for sid, sdata in active.items():
                bubble_text = sdata.get("bubble", "") or ""
                if not bubble_text:
                    continue
                if (now - float(sdata.get("bubble_ts", 0) or 0)) >= BUBBLE_TTL:
                    continue
                main_key = (sid, 0)
                if main_key not in dancers:
                    continue
                md = dancers[main_key]
                anchor_col = int(md.x) + 1 + DANCER_W // 2
                anchor_row = int(md.y) + 1
                for (r, c, text) in build_bubble_at(bubble_text, anchor_col, anchor_row, w, h):
                    if 1 <= r <= h and 1 <= c <= w:
                        out_parts.append(cursor(r, c) + BUBBLE_COLOR + text + RESET)
                        prev_bubble_cells.append((r, c, visual_width(text)))

            # HUD line at bottom.
            n_sessions = len(active)
            n_dancers = len(dancers)
            hud = f" sessions: {n_sessions}  dancers: {n_dancers} "
            hud = hud[: max(1, w - 1)]
            out_parts.append(cursor(h, 1) + DIM + hud + RESET)
            out_parts.append(cursor(h, len(hud) + 1) + "\033[K")  # clear rest of line

            out_parts.append(cursor(h, 1))
            sys.stdout.write("".join(out_parts))
            sys.stdout.flush()

            tick += 1
            time.sleep(TICK_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW + RESET + CLEAR)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
