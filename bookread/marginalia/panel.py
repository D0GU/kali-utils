"""The panel beside the page. Everything slow happens on a thread, so the reader never waits for the model."""

from __future__ import annotations

import curses
import json
import os
import sqlite3
import textwrap
import threading
from pathlib import Path

from kutil.tui import LineEdit, put

from .. import config
from .ask import NOT_YET, ask
from .memory import Character, Memory, alias_form
from .model import CLAUDE_MODEL, ClaudeModel, Model, ModelError
from .prepare import Preparer, split_passages

MIN_PANEL = 30
MAX_PANEL = 50
MIN_SIDE_BY_SIDE = 64   # narrower than this, the panel takes the whole window
RECENT_WORDS = 120
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def model_from(settings: dict) -> Model | ClaudeModel | None:
    """The model chosen by `provider`: "claude", "llamacpp", or off, the default, so that nothing is sent anywhere
    until it's asked for. The key may be written in the settings, or read from a JSON file so that it lives in one
    place only."""
    provider = settings.get("provider", "off")
    if provider not in ("claude", "llamacpp"):
        return None
    key = settings.get("model_key", "")
    source = settings.get("model_key_file")
    if source and not key:
        try:
            value = json.loads(Path(source).expanduser().read_text())
            for part in str(settings.get("model_key_path", "api_key")).split("."):
                value = value[part]
            key = value if isinstance(value, str) else ""
        except (OSError, ValueError, KeyError, TypeError):
            key = ""
    if not key:
        return None
    if provider == "claude":
        return ClaudeModel(key, settings.get("model_name", CLAUDE_MODEL), settings.get("model_url"))
    return Model(settings.get("model_url", "http://127.0.0.1:9931/v1"), key, settings.get("model_name", "qwen3.5-4b"))


class Marginalia:
    """One open book's panel. Nothing is written until it's first opened on that book."""

    def __init__(self, session):
        self.session = session
        self.key = session.key
        cfg = config.load()
        settings = cfg.get("marginalia") or {}
        self.enabled = settings.get("enabled", True) is not False
        self.model = model_from(settings) if self.enabled else None
        self.no_model = ("No key for the model. See docs/bookread.md." if settings.get("provider") in ("claude", "llamacpp")
                         else "The model is off for now. See docs/bookread.md.")
        self.memory = Memory(settings.get("database") and os.path.expanduser(settings["database"])
                             or os.path.join(os.path.dirname(cfg["database"]), "marginalia.db")) if self.enabled else None
        self.is_open = False
        self.focused = False
        self.preparer: Preparer | None = None
        self.started = bool(self.memory and self.memory.has_passages(self.key))
        self.pending: int | None = None     # a place jumped to, past what the characters know
        self.people: list[Character] = []
        self.selected = 0
        self.who: Character | None = None
        self.line = LineEdit()
        self.scroll = 0
        self.asking: threading.Thread | None = None
        self.waiting_question = ""
        self.notice = ""                    # the latest reply that isn't kept in memory
        self.behind = False                 # there is more to read for notes, so the panel has news coming
        self.tick = 0
        if self.started:
            self._start_preparing()

    # ---------------------------------------------------------------- the lock

    def _current(self) -> tuple[int, int]:
        s = self.session
        return s.layout.start_word(s.page), s.layout.end_word(s.page)

    def sync(self) -> None:
        """The page changed. Turning pages moves the lock with you and going back moves it back, but a jump
        ahead waits for you to say so."""
        if not self.started or self.session.layout is None:
            return
        start, end = self._current()
        lock = self.memory.lock(self.key)
        if start > lock:
            self.pending = end
        else:
            self.pending = None
            if end != lock:
                self.memory.set_lock(self.key, end)
        if self.who:
            self.scroll = 0

    def _begin(self) -> None:
        """The first time on this book: split it into passages, which needs no model."""
        _, end = self._current()
        s = self.session
        self.memory.open_book(self.key, s.title, s.book.words, end)
        if not self.memory.has_passages(self.key):
            self.memory.add_passages(self.key, split_passages(s.book))
        self.started = True
        self._start_preparing()

    def _start_preparing(self) -> None:
        if self.model and self.preparer is None:
            skip = frozenset(alias_form(a) for a in getattr(self.session, "authors", []))
            self.preparer = Preparer(self.memory, self.model, self.key, skip)
            self.preparer.start()

    def close(self) -> None:
        if self.preparer:
            self.preparer.stop()

    # ---------------------------------------------------------------- keys

    def toggle(self) -> None:
        if not self.enabled:
            return
        if self.is_open and not self.focused:
            self.focused = True
            return
        self.is_open = not self.is_open
        self.focused = self.is_open
        if self.is_open:
            if not self.started:
                self._begin()
            self.sync()
            self.refresh_people()

    def refresh_people(self) -> None:
        if self.started:
            self.people = self.memory.characters(self.key, self.memory.lock(self.key))
            self.selected = min(self.selected, max(0, len(self.people) - 1))

    def handle(self, key, narrow: bool) -> None:
        if self.pending is not None:
            if key in ("y", "Y"):
                self.memory.set_lock(self.key, self.pending)
                self.pending = None
                self.refresh_people()
            elif key in ("n", "N", "ESC"):
                self.is_open = self.focused = False
            return
        if key == "TAB":
            if narrow:
                self.is_open = self.focused = False
            else:
                self.focused = False
        elif self.who is None:
            self._handle_list(key)
        else:
            self._handle_chat(key)

    def _handle_list(self, key) -> None:
        self.refresh_people()
        if key in ("ESC", "q", "m"):
            self.is_open = self.focused = False
        elif key in ("UP", "k"):
            self.selected = max(0, self.selected - 1)
        elif key in ("DOWN", "j"):
            self.selected = min(max(0, len(self.people) - 1), self.selected + 1)
        elif key == "ENTER" and self.people:
            self.who = self.people[self.selected]
            self.line.set("")
            self.scroll = self.notice_clear()

    def notice_clear(self) -> int:
        self.notice = ""
        return 0

    def _handle_chat(self, key) -> None:
        if key == "ESC":
            self.who = None
            self.refresh_people()
        elif key in ("PGUP", "UP"):
            self.scroll += 3
        elif key in ("PGDN", "DOWN"):
            self.scroll = max(0, self.scroll - 3)
        elif key == "ENTER":
            self._send()
        else:
            self.line.handle(key)

    def _send(self) -> None:
        question = self.line.text.strip()
        if not question or self.asking:
            return
        if self.model is None:
            self.notice = self.no_model
            return
        self.line.set("")
        self.notice = ""
        self.scroll = 0
        self.waiting_question = question
        lock = self.memory.lock(self.key)
        who, recent = self.who, self._recent(lock)
        self.asking = threading.Thread(target=self._ask, args=(who, question, lock, recent), daemon=True)
        self.asking.start()

    def _recent(self, lock: int) -> str:
        """The words just before the lock, which may not have reached a whole passage yet."""
        blocks, text = self.session.book.blocks, []
        left = RECENT_WORDS
        for block in reversed(blocks):
            if block.word >= lock or block.starts is None or not len(block.starts):
                continue
            last = min(len(block.starts), lock - block.word)
            first = max(0, last - left)
            text.insert(0, block.text[block.starts[first]:block.ends[last - 1]])
            left -= last - first
            if left <= 0:
                break
        return " ".join(text)

    def _ask(self, who: Character, question: str, lock: int, recent: str) -> None:
        try:
            reply = ask(self.memory, self.model, self.key, who, question, lock, recent)
            if reply.kept and reply.text != NOT_YET:
                self.memory.add_talk(who.id, lock, question, reply.text, reply.sources)
            else:
                self.notice = f"{who.name}: {reply.text}"
        except ModelError as e:
            self.notice = str(e)
        except (sqlite3.Error, OSError) as e:
            self.notice = f"Can't reach the memory: {e}"
        self.waiting_question = ""
        self.asking = None

    # ---------------------------------------------------------------- drawing

    @property
    def animating(self) -> bool:
        return self.is_open and (self.asking is not None or self.behind)

    @staticmethod
    def width_for(cols: int) -> int:
        return 0 if cols < MIN_SIDE_BY_SIDE else max(MIN_PANEL, min(MAX_PANEL, cols * 2 // 5))

    def draw(self, scr, y: int, x: int, w: int, h: int, normal: int, dim: int) -> None:
        self.tick += 1
        self.refresh_people()
        self.behind = bool(self.preparer and not self.preparer.problem and self.pending is None and
                           self.memory.prepared(self.key) < self.memory.passage_count(self.key, self.memory.lock(self.key)))
        put(scr, y, x, "MARGINALIA" if self.who is None else self.who.name, normal | curses.A_BOLD, width=w)
        if self.pending is not None:
            self._draw_pending(scr, y + 2, x, w, normal)
        elif self.who is None:
            self._draw_list(scr, y + 2, x, w, h - 3, normal, dim)
        else:
            self._draw_chat(scr, y + 2, x, w, h - 2, normal, dim)
        if self.focused:
            hint = "Enter talk · Tab page · Esc close" if self.who is None else "Enter send · Tab page · Esc back"
        else:
            hint = "m back to the panel"
        put(scr, y + h - 1, x, hint, dim, width=w)

    def _draw_pending(self, scr, y: int, x: int, w: int, normal: int) -> None:
        text = "You've jumped ahead. They only know what came before. Let them catch up? They will learn what you skipped. (y/n)"
        for i, line in enumerate(textwrap.wrap(text, w)):
            put(scr, y + i, x, line, normal, width=w)

    def _status(self) -> str:
        if not self.model:
            return self.no_model
        if self.preparer and self.preparer.problem:
            return self.preparer.problem
        done, total = self.memory.prepared(self.key), self.memory.passage_count(self.key, self.memory.lock(self.key))
        return f"Reading: {min(done, total)} of {total} passages so far" if done < total else ""

    def _draw_list(self, scr, y: int, x: int, w: int, h: int, normal: int, dim: int) -> None:
        status = self._status()
        if not self.people:
            put(scr, y, x, "Nobody you've met yet.", normal, width=w)
        top = max(0, self.selected - h + 2)
        for i, person in enumerate(self.people[top:top + h - 1]):
            selected = top + i == self.selected and self.focused
            put(scr, y + i, x, f" {person.name} ", normal | curses.A_REVERSE if selected else normal, width=w)
        if status:
            put(scr, y + h - 1, x, status, dim, width=w)

    def _draw_chat(self, scr, y: int, x: int, w: int, h: int, normal: int, dim: int) -> None:
        lock = self.memory.lock(self.key)
        lines: list[tuple[str, int]] = []

        def add(text: str, attr: int) -> None:
            lines.extend((line, attr) for line in textwrap.wrap(text, w) or [""])

        for row in self.memory.talk(self.who.id, lock, 20):
            add(f"You: {row['question']}", dim)
            add(row["answer"], normal)
            if row["sources"]:
                add(f"from {row['sources']}", dim)
            lines.append(("", 0))
        if self.waiting_question:
            add(f"You: {self.waiting_question}", dim)
            add(f"{SPINNER[self.tick % len(SPINNER)]} thinking, then checking", dim)
        if self.notice:
            add(self.notice, normal)
        room = max(1, h - 3)
        end = max(0, len(lines) - self.scroll)
        for i, (text, attr) in enumerate(lines[max(0, end - room):end]):
            put(scr, y + i, x, text, attr, width=w)
        self._draw_input(scr, y + h - 3, x, w, normal)

    def _draw_input(self, scr, y: int, x: int, w: int, normal: int) -> None:
        room = max(1, w - 3)
        chars, pos = self.line.chars, self.line.pos
        first = max(0, pos - room + 1)
        put(scr, y, x, "> " + "".join(chars[first:first + room]), normal, width=w)
        if self.focused:
            at = chars[pos] if pos < len(chars) else " "
            put(scr, y, x + 2 + pos - first, at, normal | curses.A_REVERSE, width=1)
