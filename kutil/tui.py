"""Curses building blocks shared by the kali-utils tools: keys, drawing helpers, line editor, forms and dialogs."""

from __future__ import annotations

import curses
import glob
import os
from dataclasses import dataclass, field

from . import fmt

# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

KEYMAP = {
    curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
    curses.KEY_HOME: "HOME", curses.KEY_END: "END", curses.KEY_PPAGE: "PGUP", curses.KEY_NPAGE: "PGDN",
    curses.KEY_ENTER: "ENTER", curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_DC: "DELETE",
    curses.KEY_BTAB: "BTAB", curses.KEY_RESIZE: "RESIZE", curses.KEY_MOUSE: "MOUSE",
    curses.KEY_F1: "?", curses.KEY_SR: "S-UP", curses.KEY_SF: "S-DOWN",
}
# terminfo names some terminals use for Shift+arrows instead of KEY_SR/KEY_SF
SHIFT_NAMES = {b"kUP": "S-UP", b"kDN": "S-DOWN", b"KEY_SR": "S-UP", b"KEY_SF": "S-DOWN"}
FAST_STEP = 5  # rows moved by Shift+Up/Down (or K/J)
CHARMAP = {"\n": "ENTER", "\r": "ENTER", "\t": "TAB", "\x1b": "ESC", "\x7f": "BACKSPACE",
           "\b": "BACKSPACE"}


def read_key(win):
    try:
        k = win.get_wch()
    except curses.error:
        return None
    if isinstance(k, int):
        if k in KEYMAP:
            return KEYMAP[k]
        try:
            return SHIFT_NAMES.get(curses.keyname(k))
        except (ValueError, curses.error):
            return None
    if k in CHARMAP:
        return CHARMAP[k]
    if len(k) == 1 and ord(k) < 32:
        return "C-" + chr(ord(k) + 96)
    return k


def pad(text: str, width: int, align: str = "<") -> str:
    if width <= 0:
        return ""
    text = fmt.truncate(text, width)
    space = " " * (width - fmt.text_width(text))
    return space + text if align == ">" else text + space


def put(win, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    avail = w - x if width is None else min(width, w - x)
    if fmt.text_width(text) > avail:
        text = fmt.truncate(text, avail)
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass  # writing into the bottom-right cell raises after drawing


def set_cursor(visible: bool) -> None:
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def complete_path(text: str) -> tuple[str, list[str]]:
    """Shell-style completion of a local filesystem path."""
    home = os.path.expanduser("~")
    expanded = os.path.expanduser(text)
    matches = sorted(glob.glob(glob.escape(expanded) + "*"))
    if not matches:
        return text, []
    if len(matches) == 1:
        new = matches[0] + ("/" if os.path.isdir(matches[0]) else "")
    else:
        new = os.path.commonprefix(matches)
    if text.startswith("~") and new.startswith(home):
        new = "~" + new[len(home):]
    names = [os.path.basename(m) + ("/" if os.path.isdir(m) else "") for m in matches]
    return new, names


class Theme:
    def __init__(self):
        self.map = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE,
                    "underline": curses.A_UNDERLINE}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            colors = [("red", curses.COLOR_RED), ("green", curses.COLOR_GREEN),
                      ("yellow", curses.COLOR_YELLOW), ("blue", curses.COLOR_BLUE),
                      ("magenta", curses.COLOR_MAGENTA), ("cyan", curses.COLOR_CYAN)]
            for i, (name, fg) in enumerate(colors, start=1):
                curses.init_pair(i, fg, bg)
                self.map[name] = curses.color_pair(i)
            curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)
            self.map["bar"] = curses.color_pair(7)
            curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_RED)
            self.map["alert"] = curses.color_pair(8) | curses.A_BOLD
        else:
            self.map["bar"] = curses.A_REVERSE
            self.map["alert"] = curses.A_REVERSE | curses.A_BOLD

    def __call__(self, *names) -> int:
        attr = 0
        for n in names:
            attr |= self.map.get(n, 0)
        return attr


class LineEdit:
    """A single-line text editor with emacs-ish keys."""

    def __init__(self, text: str = ""):
        self.chars = list(text)
        self.pos = len(self.chars)
        self.scroll = 0

    @property
    def text(self) -> str:
        return "".join(self.chars)

    def set(self, text: str) -> None:
        self.chars = list(text)
        self.pos = len(self.chars)

    def handle(self, key) -> bool:
        if key == "LEFT" or key == "C-b":
            self.pos = max(0, self.pos - 1)
        elif key == "RIGHT" or key == "C-f":
            self.pos = min(len(self.chars), self.pos + 1)
        elif key in ("HOME", "C-a"):
            self.pos = 0
        elif key in ("END", "C-e"):
            self.pos = len(self.chars)
        elif key == "BACKSPACE":
            if self.pos:
                del self.chars[self.pos - 1]
                self.pos -= 1
        elif key in ("DELETE", "C-d"):
            if self.pos < len(self.chars):
                del self.chars[self.pos]
        elif key == "C-u":
            del self.chars[:self.pos]
            self.pos = 0
        elif key == "C-k":
            del self.chars[self.pos:]
        elif key == "C-w":
            i = self.pos
            while i and self.chars[i - 1] == " ":
                i -= 1
            while i and self.chars[i - 1] not in " /":
                i -= 1
            del self.chars[i:self.pos]
            self.pos = i
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.chars.insert(self.pos, key)
            self.pos += 1
        else:
            return False
        return True

    def render(self, width: int) -> tuple[str, int]:
        """Visible slice of the text and the cursor column within it."""
        width = max(1, width)
        if self.pos < self.scroll:
            self.scroll = self.pos
        while fmt.text_width("".join(self.chars[self.scroll:self.pos])) > width - 1:
            self.scroll += 1
        out, w = [], 0
        for c in self.chars[self.scroll:]:
            cw = fmt.text_width(c)
            if w + cw > width:
                break
            out.append(c)
            w += cw
        return "".join(out), fmt.text_width("".join(self.chars[self.scroll:self.pos]))


@dataclass
class ListState:
    index: int = 0
    top: int = 0

    def clamp(self, n: int, height: int) -> None:
        height = max(1, height)
        self.index = max(0, min(self.index, n - 1))
        if self.index < self.top:
            self.top = self.index
        if self.index >= self.top + height:
            self.top = self.index - height + 1
        self.top = max(0, min(self.top, max(0, n - height)))

    def navigate(self, key, page: int) -> bool:
        moves = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PGUP": -page, "PGDN": page,
                 "S-UP": -FAST_STEP, "K": -FAST_STEP, "S-DOWN": FAST_STEP, "J": FAST_STEP}
        if key in moves:
            self.index += moves[key]
        elif key in ("HOME", "g"):
            self.index = 0
        elif key in ("END", "G"):
            self.index = 10 ** 9
        else:
            return False
        self.index = max(0, self.index)
        return True


@dataclass
class Col:
    key: str
    title: str
    width: int | None  # None = flexible
    align: str = "<"
    drop: int = 0      # 0 = never dropped; higher numbers are dropped first on narrow screens


def layout(cols: list[Col], width: int, min_flex: int = 20) -> tuple[list[Col], int]:
    active = list(cols)

    def flex_width():
        return width - sum(c.width for c in active if c.width) - (len(active) - 1)

    while flex_width() < min_flex and any(c.drop for c in active):
        active.remove(max((c for c in active if c.drop), key=lambda c: c.drop))
    return active, max(6, flex_width())


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

class FormError(Exception):
    pass


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"   # text, path, int, float, bool, choice
    value: object = ""
    choices: list = field(default_factory=list)  # [(value, label)] for kind=choice
    hint: str = ""

    def __post_init__(self):
        self.initial = self.value
        if self.kind in ("text", "path", "int", "float"):
            self.edit = LineEdit("" if self.value is None else
                                 (f"{self.value:g}" if isinstance(self.value, float) else str(self.value)))

    def parsed(self):
        if self.kind in ("bool", "choice"):
            return self.value
        text = self.edit.text.strip()
        if self.kind == "int":
            if not text:
                return None
            try:
                return int(text)
            except ValueError:
                raise FormError(f"{self.label}: enter a whole number (or leave it blank)") from None
        if self.kind == "float":
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                raise FormError(f"{self.label}: enter a number (or leave it blank)") from None
        return text

    def changed(self) -> bool:
        value = self.parsed()
        if self.kind in ("int", "float") and self.initial is not None and value is not None:
            return float(value) != float(self.initial)
        if self.kind in ("text", "path"):
            return value != ("" if self.initial is None else str(self.initial))
        return value != self.initial

    def cycle(self, step: int) -> None:
        if self.kind == "bool":
            self.value = not self.value
        elif self.kind == "choice":
            values = [v for v, _ in self.choices]
            i = values.index(self.value) if self.value in values else 0
            self.value = values[(i + step) % len(values)]


class Form:
    def __init__(self, title: str, fields: list[Field], ok: str = "OK", note: str = "",
                 validate=None, danger: bool = False, intro: list | None = None):
        self.title, self.fields, self.ok, self.note = title, fields, ok, note
        self.validate = validate
        self.danger = danger
        self.intro = intro or []   # lines (str or (str, style...)) shown above the fields

    def run(self, app: "Screen") -> dict | None:
        focus = 0
        message, message_attr = self.note, app.theme("dim")
        nfields = len(self.fields)
        while True:
            app.draw()
            cursor = self._draw(app, focus, message, message_attr)
            if cursor:
                set_cursor(True)
                app.scr.move(*cursor)
            else:
                set_cursor(False)
            app.scr.refresh()
            key = read_key(app.scr)
            if key is None or key == "RESIZE":
                continue
            current = self.fields[focus] if focus < nfields else None
            if key not in ("TAB",):
                message, message_attr = self.note, app.theme("dim")
            if key == "ESC":
                set_cursor(False)
                return None
            if key in ("UP", "BTAB"):
                focus = (focus - 1) % (nfields + 2)
            elif key == "DOWN" or (key == "TAB" and not (current and current.kind == "path")):
                focus = (focus + 1) % (nfields + 2)
            elif key == "TAB" and current:
                new, names = complete_path(current.edit.text)
                current.edit.set(new)
                if len(names) > 1:
                    message, message_attr = "  ".join(names), app.theme("cyan")
                elif not names:
                    message, message_attr = "no matches", app.theme("dim")
            elif key == "ENTER" or (key == " " and focus >= nfields):
                if focus == nfields + 1:
                    set_cursor(False)
                    return None
                try:
                    values = {f.key: f.parsed() for f in self.fields}
                    err = self.validate(values) if self.validate else None
                    if err:
                        raise FormError(err)
                except FormError as e:
                    message, message_attr = str(e), app.theme("red", "bold")
                    continue
                set_cursor(False)
                return values
            elif focus >= nfields:
                if key in ("LEFT", "RIGHT"):
                    focus = nfields if focus == nfields + 1 else nfields + 1
            elif current.kind in ("bool", "choice"):
                if key in (" ", "RIGHT", "l"):
                    current.cycle(1)
                elif key in ("LEFT", "h"):
                    current.cycle(-1)
            else:
                current.edit.handle(key)

    def changes(self) -> dict:
        return {f.key: f.parsed() for f in self.fields if f.changed()}

    def _draw(self, app: "Screen", focus: int, message: str, message_attr: int):
        scr, th = app.scr, app.theme
        h, w = scr.getmaxyx()
        label_w = max(fmt.text_width(f.label) for f in self.fields) + 2
        intro = [l if isinstance(l, tuple) else (l,) for l in self.intro][:max(0, h - len(self.fields) - 9)]
        width = min(w - 2, max(64, label_w + 40, fmt.text_width(self.title) + 6,
                               *(fmt.text_width(l[0]) + 6 for l in intro)))
        skip = len(intro) + 1 if intro else 0
        height = len(self.fields) + 6 + skip
        y0, x0 = max(0, (h - height) // 2), max(0, (w - width) // 2)
        border = th("red") if self.danger else th("cyan")
        app.box(y0, x0, height, width, self.title, border)
        for i, (text, *styles) in enumerate(intro):
            put(scr, y0 + 2 + i, x0 + 3, text, th(*styles), width=width - 6)
        cursor = None
        input_x = x0 + 2 + label_w
        input_w = width - label_w - 4
        for i, f in enumerate(self.fields):
            y = y0 + 2 + skip + i
            focused = i == focus
            put(scr, y, x0 + 2, pad(f.label, label_w - 1, ">"), th("bold") if focused else th("dim"))
            if f.kind == "bool":
                text = "[x] yes" if f.value else "[ ] no"
                put(scr, y, input_x, text, th("reverse") if focused else 0)
            elif f.kind == "choice":
                label = dict(f.choices).get(f.value, str(f.value))
                put(scr, y, input_x, f"‹ {label} ›", th("reverse") if focused else 0)
            else:
                if focused:
                    visible, cx = f.edit.render(input_w)
                    cursor = (y, input_x + cx)
                else:
                    visible = f.edit.text
                attr = th("reverse") if focused else th("underline")
                put(scr, y, input_x, pad(visible, input_w), attr)
        # message / hint line
        hint = self.fields[focus].hint if focus < len(self.fields) and message == self.note else ""
        put(scr, y0 + height - 3, x0 + 2, pad(hint or message, width - 4),
            th("dim") if hint else message_attr)
        # buttons
        ok = f"[ {self.ok} ]"
        cancel = "[ Cancel ]"
        bx = x0 + width - fmt.text_width(ok) - fmt.text_width(cancel) - 4
        put(scr, y0 + height - 2, bx, ok,
            th("reverse", "bold") if focus == len(self.fields) else th("red" if self.danger else "bold"))
        put(scr, y0 + height - 2, bx + fmt.text_width(ok) + 1, cancel,
            th("reverse") if focus == len(self.fields) + 1 else 0)
        return cursor


def choose(app: "Screen", title: str, lines: list[str], options: list[tuple[str, str, str]],
           default: int = 0, danger: bool = False):
    """Pick one of `options` [(hotkey, label, style)]. Returns the hotkey or None."""
    index = default
    while True:
        app.draw()
        scr, th = app.scr, app.theme
        h, w = scr.getmaxyx()
        body = lines[:max(1, h - len(options) - 8)]
        width = min(w - 2, max(56, *(fmt.text_width(l) + 6 for l in body),
                               *(fmt.text_width(o[1]) + 12 for o in options)))
        height = len(body) + len(options) + 5
        y0, x0 = max(0, (h - height) // 2), max(0, (w - width) // 2)
        app.box(y0, x0, height, width, title, th("red") if danger else th("cyan"))
        for i, line in enumerate(body):
            put(scr, y0 + 2 + i, x0 + 3, line, width=width - 6)
        for i, (hk, label, style) in enumerate(options):
            y = y0 + 3 + len(body) + i
            attr = th(style) | (th("reverse") if i == index else 0)
            put(scr, y, x0 + 3, pad(f" {hk:>3}  {label}", width - 6), attr)
        set_cursor(False)
        scr.refresh()
        key = read_key(scr)
        if key in (None, "RESIZE"):
            continue
        if key == "ESC" or key == "q":
            return None
        if key in ("UP", "k", "BTAB"):
            index = (index - 1) % len(options)
        elif key in ("DOWN", "j", "TAB"):
            index = (index + 1) % len(options)
        elif key == "ENTER":
            return options[index][0]
        else:
            for hk, _, _ in options:
                if key == hk:
                    return hk


# --------------------------------------------------------------------------
# Base class for full-screen apps
# --------------------------------------------------------------------------

class Screen:
    """What dialogs and tables need from an app. Subclasses set self.scr and self.theme and implement draw()."""

    scr = None
    theme = None

    def draw(self) -> None:
        raise NotImplementedError

    def box(self, y: int, x: int, h: int, w: int, title: str, attr: int) -> None:
        scr = self.scr
        for row in range(h):
            put(scr, y + row, x, " " * w)
        put(scr, y, x, "┌" + "─" * (w - 2) + "┐", attr)
        for row in range(1, h - 1):
            put(scr, y + row, x, "│", attr)
            put(scr, y + row, x + w - 1, "│", attr)
        put(scr, y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)
        put(scr, y, x + 2, f" {fmt.truncate(title, w - 6)} ", attr | self.theme("bold"))

    def draw_table(self, y0: int, height: int, width: int, cols: list[Col], items: list,
                   cell, state: ListState, selected: bool = True, x0: int = 0) -> None:
        """Header + scrolling rows. `cell(key, item)` returns (text, attr) for a column."""
        th = self.theme
        active, flex = layout(cols, width)
        x = x0
        for c in active:
            cw = c.width or flex
            put(self.scr, y0, x, pad(c.title, cw, c.align), th("bold", "underline"))
            x += cw + 1
        body = height - 1
        state.clamp(len(items), body)
        for row in range(body):
            i = state.top + row
            if i >= len(items):
                break
            item = items[i]
            sel = selected and i == state.index
            if sel:
                put(self.scr, y0 + 1 + row, x0, " " * width, th("reverse"))
            x = x0
            for c in active:
                cw = c.width or flex
                text, attr = cell(c.key, item)
                put(self.scr, y0 + 1 + row, x, pad(text, cw, c.align), attr | (th("reverse") if sel else 0))
                x += cw + 1
        if len(items) > body:
            pos = f" {state.index + 1}/{len(items)} "
            put(self.scr, y0, x0 + width - len(pos), pos, th("dim"))
