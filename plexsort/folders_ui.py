"""Curses dialogs for choosing folders: the library/watched-folder manager and a directory browser."""

from __future__ import annotations

import os

from trctl import fmt
from trctl.tui import Field, Form, ListState, choose, pad, put, read_key, set_cursor

from . import space

SECTIONS = [
    ("inbox_dirs", "Watched folders", "Their contents show up in the Inbox. Transmission's download folder is always "
                                      "watched, so this is for extra folders.", False),
    ("movies_dirs", "Movies", "Plex “Movies” library folders. The first is preferred.", True),
    ("tv_dirs", "TV Shows", "Plex “TV Shows” library folders. The first is preferred.", True),
    ("music_dirs", "Music", "Plex “Music” library folders. The first is preferred.", True),
]
LIBRARY_NAME = {"movies_dirs": "Movies", "tv_dirs": "TV Shows", "music_dirs": "Music"}


def mount_point(path: str) -> str:
    p = space.existing_ancestor(path)
    while not os.path.ismount(p):
        p = os.path.dirname(p)
    return p


def short_path(path: str, width: int) -> str:
    """Truncate from the left so the end of the path (the part that differs) stays visible."""
    if fmt.text_width(path) <= width:
        return path
    parts = path.split("/")
    while len(parts) > 1 and fmt.text_width("…/" + "/".join(parts[1:])) > width:
        parts.pop(0)
    tail = "…/" + "/".join(parts[1:]) if len(parts) > 1 else "…" + path[-(width - 1):]
    return tail if fmt.text_width(tail) <= width else "…" + path[-(width - 1):]


def drive_text(path: str) -> tuple[str, str]:
    """('1.4 TiB free of 1.8 TiB on /mnt/data', style)"""
    try:
        _, free, total = space.drive_info(path)
    except OSError as e:
        return f"unavailable: {e.strerror}", "red"
    style = "red" if free < 5 * space.GB else "yellow" if free < 0.1 * total else "green"
    return f"{fmt.human_size(free)} free of {fmt.human_size(total)} on {mount_point(path)}", style


class DirPicker:
    """Browse the filesystem and pick a folder. Tab shows the mounted drives."""

    def __init__(self, app, start: str = "", title: str = "Choose a folder"):
        self.app = app
        self.title = title
        start = os.path.expanduser(start) if start else os.path.expanduser("~")
        self.path = space.existing_ancestor(start)
        self.drives = False
        self.hidden = False
        self.state = ListState()
        self.message = ""

    def entries(self) -> list[tuple]:
        if self.drives:
            rows = [("drive", m, fs, free, total) for m, fs, free, total in space.mounts()]
            return rows + [("dir", os.path.expanduser("~"))]
        rows: list[tuple] = [("select", self.path)]
        if self.path != "/":
            rows.append(("up", os.path.dirname(self.path)))
        try:
            names = sorted(os.listdir(self.path), key=str.lower)
        except OSError as e:
            self.message = f"can't read {self.path}: {e.strerror}"
            names = []
        for n in names:
            full = os.path.join(self.path, n)
            if (self.hidden or not n.startswith(".")) and os.path.isdir(full):
                rows.append(("dir", full))
        return rows

    def go(self, path: str) -> None:
        self.path = path
        self.drives = False
        self.state = ListState()
        self.message = ""

    def run(self) -> str | None:
        while True:
            rows = self.entries()
            self.draw(rows)
            key = read_key(self.app.scr)
            if key in (None, "RESIZE"):
                continue
            if key == "ESC":
                return None
            if key not in ("g", "G") and self.state.navigate(key, 10):  # g = go to path, not "top"
                self.state.index = min(self.state.index, len(rows) - 1)
                continue
            row = rows[min(self.state.index, len(rows) - 1)] if rows else None
            if key in ("ENTER", "RIGHT", "l") and row:
                if row[0] == "select":
                    return row[1]
                if row[0] in ("up", "dir", "drive"):
                    self.go(row[1])
            elif key in ("BACKSPACE", "LEFT", "h") and not self.drives and self.path != "/":
                child = self.path
                self.go(os.path.dirname(self.path))
                rows = self.entries()
                self.state.index = next((i for i, r in enumerate(rows) if r[1] == child), 0)
            elif key in ("TAB", "D"):
                self.drives = not self.drives
                self.state = ListState()
            elif key == ".":
                self.hidden = not self.hidden
            elif key == " " and row and row[0] in ("dir", "drive"):
                return row[1]
            elif key == "n" and not self.drives:
                values = Form("New folder", [Field("name", "Name", "text", "")], ok="Create",
                              note=f"inside {self.path}",
                              validate=lambda v: None if v["name"] and "/" not in v["name"] else "Enter a name (no '/')"
                              ).run(self.app)
                if values:
                    target = os.path.join(self.path, values["name"])
                    try:
                        os.makedirs(target, exist_ok=True)
                        self.go(target)
                    except OSError as e:
                        self.message = f"can't create {target}: {e.strerror}"
            elif key in ("g", "/"):
                values = Form("Go to folder", [Field("path", "Path", "path", self.path + "/", hint="Tab completes")],
                              ok="Go").run(self.app)
                if values:
                    target = os.path.expanduser(values["path"]).rstrip("/") or "/"
                    if os.path.isdir(target):
                        self.go(target)
                    else:
                        self.message = f"{target} is not a folder"

    def draw(self, rows: list[tuple]) -> None:
        app, th, scr = self.app, self.app.theme, self.app.scr
        app.draw()
        h, w = scr.getmaxyx()
        width, height = min(w - 2, 100), min(h - 2, 30)
        y0, x0 = max(0, (h - height) // 2), max(0, (w - width) // 2)
        app.box(y0, x0, height, width, self.title, th("cyan"))
        if self.drives:
            put(scr, y0 + 1, x0 + 2, "Mounted drives", th("bold"), width=width - 4)
        else:
            text, style = drive_text(self.path)
            put(scr, y0 + 1, x0 + 2, self.path, th("bold"), width=width - 4)
            put(scr, y0 + 2, x0 + 2, text, th(style), width=width - 4)
        body_top = y0 + 4
        body_h = height - 7
        self.state.clamp(len(rows), body_h)
        for i in range(body_h):
            n = self.state.top + i
            if n >= len(rows):
                break
            row = rows[n]
            sel = n == self.state.index
            if row[0] == "select":
                text, attr = "✓ Use this folder", th("green", "bold")
            elif row[0] == "up":
                text, attr = "↰ ..  (parent folder)", th("dim")
            elif row[0] == "drive":
                _, mnt, fs, free, total = row
                text, attr = f"⛁ {mnt}   {fmt.human_size(free)} free of {fmt.human_size(total)}  ({fs})", th("cyan")
            else:
                text, attr = "▸ " + (os.path.basename(row[1]) or row[1]) + ("/" if not self.drives else ""), 0
                if self.drives:
                    text = "⌂ " + row[1] + "  (home)"
            put(scr, body_top + i, x0 + 2, pad(" " + text, width - 4), attr | (th("reverse") if sel else 0))
        put(scr, y0 + height - 3, x0 + 2, pad(self.message, width - 4), th("red"))
        put(scr, y0 + height - 2, x0 + 2,
            pad("Enter open/choose · ← up · Space pick highlighted · Tab drives · n new folder · g go to path · "
                ". hidden · Esc cancel", width - 4), th("dim"))
        set_cursor(False)
        scr.refresh()


class FoldersDialog:
    """Edit the watched folders and the library folders (several per library, in order of preference)."""

    def __init__(self, app, cfg: dict, first_run: bool = False):
        self.app = app
        self.cfg = cfg
        self.lists = {key: list(cfg.get(key) or []) for key, *_ in SECTIONS}
        self.original = {k: list(v) for k, v in self.lists.items()}
        self.first_run = first_run
        self.index = 1
        self.top = 0
        self.message = ""

    def rows(self) -> list[tuple]:
        out = []
        for key, title, help_text, _required in SECTIONS:
            out.append(("section", key, title, help_text))
            out += [("folder", key, path, n) for n, path in enumerate(self.lists[key])]
            out.append(("add", key))
        return out

    def run(self) -> bool:
        while True:
            rows = self.rows()
            selectable = [i for i, r in enumerate(rows) if r[0] != "section"]
            if self.index not in selectable:
                self.index = min(selectable, key=lambda i: abs(i - self.index))
            self.draw(rows)
            key = read_key(self.app.scr)
            if key in (None, "RESIZE"):
                continue
            row = rows[self.index]
            pos = selectable.index(self.index)
            if key in ("UP", "k"):
                self.index = selectable[max(0, pos - 1)]
            elif key in ("DOWN", "j"):
                self.index = selectable[min(len(selectable) - 1, pos + 1)]
            elif key in ("S-UP", "S-DOWN"):
                self.index = selectable[max(0, min(len(selectable) - 1, pos + (5 if key == "S-DOWN" else -5)))]
            elif key in ("PGUP", "PGDN"):
                self.index = selectable[max(0, min(len(selectable) - 1, pos + (8 if key == "PGDN" else -8)))]
            elif key in ("a", "ENTER") and row[0] == "add" or key == "a":
                section = row[1]
                start = self.lists[section][-1] if self.lists[section] else "/mnt/data"
                picked = DirPicker(self.app, os.path.dirname(start) if self.lists[section] else start,
                                   f"Add a {dict((k, t) for k, t, *_ in SECTIONS)[section]} folder").run()
                if picked:
                    picked = os.path.abspath(picked)
                    if picked in self.lists[section]:
                        self.message = "That folder is already in the list"
                    else:
                        self.lists[section].append(picked)
                        self.index = self.rows().index(("folder", section, picked, len(self.lists[section]) - 1))
            elif key == "ENTER" and row[0] == "folder":
                picked = DirPicker(self.app, row[2], "Change folder").run()
                if picked:
                    self.lists[row[1]][row[3]] = os.path.abspath(picked)
            elif key in ("d", "DELETE", "x") and row[0] == "folder":
                del self.lists[row[1]][row[3]]
            elif key in ("[", "K", "]", "J") and row[0] == "folder":
                lst, n = self.lists[row[1]], row[3]
                m = n - 1 if key in ("[", "K") else n + 1
                if 0 <= m < len(lst):
                    lst[n], lst[m] = lst[m], lst[n]
                    self.index += m - n
            elif key in ("w", "s", "C-s"):
                if self.save():
                    return True
            elif key in ("ESC", "q"):
                if self.lists == self.original:
                    return False
                choice = choose(self.app, "Unsaved changes", ["Save the folder changes?"],
                                [("s", "Save", "bold"), ("d", "Discard", "red"), ("Esc", "Keep editing", "dim")],
                                default=0)
                if choice == "s" and self.save():
                    return True
                if choice == "d":
                    return False

    def save(self) -> bool:
        missing = [LIBRARY_NAME[k] for k, _t, _h, required in SECTIONS if required and not self.lists[k]]
        if missing:
            self.message = f"Add at least one folder for: {', '.join(missing)}"
            return False
        for key, *_ in SECTIONS:
            for path in self.lists[key]:
                if key == "inbox_dirs" and not os.path.isdir(path):
                    self.message = f"Watched folder {path} doesn't exist"
                    return False
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as e:
                    self.message = f"Can't create {path}: {e.strerror}"
                    return False
        for key, *_ in SECTIONS:
            self.cfg[key] = list(self.lists[key])
        return True

    def draw(self, rows: list[tuple]) -> None:
        app, th, scr = self.app, self.app.theme, self.app.scr
        app.draw()
        h, w = scr.getmaxyx()
        width, height = min(w - 2, 110), h - 2
        y0, x0 = 1, max(0, (w - width) // 2)
        app.box(y0, x0, height, width, "Library & watched folders", th("cyan"))
        y = y0 + 1
        if self.first_run:
            for line in ("Welcome to plexsort. Choose where your Plex libraries live and which folders to watch.",
                         "Each library can use several folders, e.g. one per drive: titles go to the first folder that "
                         "has room (or where the title already is)."):
                put(scr, y, x0 + 2, line, th("bold"), width=width - 4)
                y += 1
            y += 1
        body_top = y
        body_h = y0 + height - 3 - body_top
        if self.index < self.top:
            self.top = self.index
        while self.index >= self.top + body_h:
            self.top += 1
        for i in range(body_h):
            n = self.top + i
            if n >= len(rows):
                break
            row = rows[n]
            sel = n == self.index
            rev = th("reverse") if sel else 0
            yy = body_top + i
            if row[0] == "section":
                put(scr, yy, x0 + 2, row[2], th("cyan", "bold"))
                put(scr, yy, x0 + 4 + fmt.text_width(row[2]), row[3], th("dim"), width=width - 8 - len(row[2]))
            elif row[0] == "add":
                put(scr, yy, x0 + 4, pad("+ add a folder…", 30), th("green") | rev)
            else:
                _, key, path, idx = row
                exists = os.path.isdir(path)
                star = "★ " if idx == 0 and key != "inbox_dirs" else "  "
                suffix = "" if exists else "  (will be created)"
                label = star + short_path(path, width // 2 - 6 - len(suffix)) + suffix
                info, style = drive_text(path)
                put(scr, yy, x0 + 4, pad(label, width // 2 - 2), (th("bold") if idx == 0 else 0) | rev)
                put(scr, yy, x0 + 4 + width // 2 - 1, pad(info, width // 2 - 6), th(style) | rev)
        put(scr, y0 + height - 3, x0 + 2, pad(self.message, width - 4), th("red"))
        put(scr, y0 + height - 2, x0 + 2,
            pad("a add · Enter change · d remove · [ ] move up/down (first = preferred) · w save · Esc cancel",
                width - 4), th("dim"))
        set_cursor(False)
        scr.refresh()
