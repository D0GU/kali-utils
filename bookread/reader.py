"""The reader and the menu of books.

`Session` holds everything about reading that can be tested without a terminal: the page, the saved place and the
speed samples. `Reader` and `Menu` draw it. Another program can open a book on its own screen with `read_book`. A curses program can't set the terminal's font size, so text size is the
terminal's own zoom; the page lays itself out again when the window's size in cells changes.
"""

from __future__ import annotations

import curses
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import NamedTuple

from kutil import fmt
from kutil.tui import Col, LineEdit, ListState, Screen, Theme, pad, put, read_key, set_cursor, set_locale

from . import ebook, library
from .ebook import cell_width
from .pace import DEFAULT_WPM, Pace, human_duration
from .pages import Layout

DEFAULT_WIDTH = 72
MIN_TEXT = 20
MIN_SIDE = 2            # cells kept clear either side, however wide the text is set
WIDTH_STEP = 4          # two cells a side per key press
MAX_MARGIN_Y = 8
MIN_PAGE_ROWS = 3
MIN_COLS, MIN_ROWS = 24, 6
A_ITALIC = getattr(curses, "A_ITALIC", 0)

HELP = """\
TURNING PAGES
  Space → ↓ PgDn    next page
  ← ↑ PgUp b        previous page
  Home  End         start and end of the book
  c                 contents

THE CHARACTERS
  m                 Marginalia: talk to the people you've met so far
  Tab               between the panel and the page

HOW IT LOOKS
  t  T              next and previous colour theme
  [  ]              smaller and bigger side margins
  {{  }}              smaller and bigger top and bottom margins

TEXT SIZE
  Ctrl+=  Ctrl+-    your terminal's own zoom (Ctrl+0 resets it in Tabby)
                    The page lays itself out again and keeps your place.

LEAVING
  q  Esc            close the book; your place is saved on every page turn

READING SPEED
  {speed}
  Time left is the words still to read at this speed."""


# --------------------------------------------------------------------------
# Settings and themes
# --------------------------------------------------------------------------

class ReaderTheme(NamedTuple):
    key: str
    label: str
    fg: int
    bg: int
    heading: int
    status: int


TERMINAL = ReaderTheme("terminal", "Terminal", -1, -1, -1, -1)
# xterm's 256 colours are fixed whatever scheme the terminal uses, so these look the same everywhere.
THEMES = [TERMINAL,
          ReaderTheme("paper", "Paper", 235, 255, 16, 243),
          ReaderTheme("sepia", "Sepia", 94, 230, 52, 95),
          ReaderTheme("dark", "Dark", 252, 235, 255, 245),
          ReaderTheme("black", "Black", 250, 16, 255, 243),
          ReaderTheme("solarized", "Solarized", 245, 234, 136, 242)]
BASIC_THEMES = [TERMINAL,
                ReaderTheme("paper", "Paper", curses.COLOR_BLACK, curses.COLOR_WHITE, curses.COLOR_BLACK, curses.COLOR_BLACK),
                ReaderTheme("black", "Black", curses.COLOR_WHITE, curses.COLOR_BLACK, curses.COLOR_WHITE, curses.COLOR_WHITE)]


def available_themes() -> list[ReaderTheme]:
    if os.environ.get("NO_COLOR") or not curses.has_colors():
        return [TERMINAL]
    return THEMES if curses.COLORS >= 256 else BASIC_THEMES


@dataclass
class Prefs:
    theme: str = TERMINAL.key
    width: int = DEFAULT_WIDTH      # the line length, so margins stay sensible when the window changes size
    margin_y: int = 1

    @classmethod
    def load(cls, db) -> "Prefs":
        prefs = cls(db.meta_get("reader_theme", TERMINAL.key))
        if prefs.theme not in {t.key for t in THEMES}:
            prefs.theme = TERMINAL.key
        try:
            prefs.width = max(MIN_TEXT, int(db.meta_get("reader_width", DEFAULT_WIDTH)))
        except ValueError:
            pass
        try:
            prefs.margin_y = max(0, min(MAX_MARGIN_Y, int(db.meta_get("reader_margin_y", 1))))
        except ValueError:
            pass
        return prefs

    def save(self, db) -> None:
        db.meta_set("reader_theme", self.theme)
        db.meta_set("reader_width", self.width)
        db.meta_set("reader_margin_y", self.margin_y)

    def change_side_margin(self, step: int, cols: int) -> None:
        """Grow (+1) or shrink (-1) the side margins, starting from what's on screen now."""
        most = max(MIN_TEXT, cols - 2 * MIN_SIDE)
        current = max(MIN_TEXT, min(self.width, most))
        wanted = max(MIN_TEXT, min(most, current - step * WIDTH_STEP))
        if wanted != current:   # otherwise a window too small to change anything would cut the width used in larger ones
            self.width = wanted

    def change_top_margin(self, step: int) -> None:
        self.margin_y = max(0, min(MAX_MARGIN_Y, self.margin_y + step))

    def next_theme(self, step: int, themes: list[ReaderTheme]) -> None:
        keys = [t.key for t in themes]
        at = keys.index(self.theme) if self.theme in keys else 0
        self.theme = keys[(at + step) % len(keys)]


class View(NamedTuple):
    width: int
    rows: int
    x: int
    y: int


def view(cols: int, rows: int, prefs: Prefs) -> View:
    """Where the text goes in a window this size. One row is kept for the status line."""
    width = max(1, min(max(prefs.width, MIN_TEXT), cols - 2 * MIN_SIDE))
    margin = prefs.margin_y
    if rows - 1 - 2 * margin < MIN_PAGE_ROWS:
        margin = max(0, (rows - 1 - MIN_PAGE_ROWS) // 2)
    return View(width, rows - 1 - 2 * margin, (cols - width) // 2, margin)


def _open_marginalia(session):
    """The reader works just the same without it, whatever goes wrong here."""
    try:
        from .marginalia.panel import Marginalia

        margin = Marginalia(session)
        return margin if margin.enabled else None
    except (ImportError, OSError, ValueError, sqlite3.Error):
        return None


# --------------------------------------------------------------------------
# A reading session
# --------------------------------------------------------------------------

class Session:
    def __init__(self, book: ebook.Book, key: str, title: str, db, clock=time.monotonic):
        self.book, self.key, self.title, self.db, self.clock = book, key, title, db, clock
        self.authors: list[str] = []    # for Marginalia, so that a title page doesn't make the author a character
        self.pace = Pace.load(db)
        row = db.reading_get(key)
        self.finished_before = bool(row and row["percent"] >= 1)
        self.finished = self.finished_before
        self.problem: str | None = None     # a save that failed; reading carries on regardless
        self.position = min(row["position"], book.words - 1) if row else 0   # stays fixed while the window changes
        self.layout: Layout | None = None
        self.page = 0
        self.furthest = -1              # the furthest page start shown since the last jump
        self.shown_at: float | None = None      # set while the page on screen is being timed

    def set_view(self, width: int, rows: int) -> None:
        if self.layout and (self.layout.width, self.layout.rows) == (width, rows):
            return
        first = self.layout is None
        self.layout = Layout(self.book, width, rows)
        self.page = self.layout.page_of(self.position)
        if first:
            self._arrived()
        else:
            self.shown_at = None

    def _arrived(self) -> None:
        """A page has appeared. Time it only if it hasn't been read already."""
        start = self.layout.start_word(self.page)
        self.shown_at = self.clock() if start > self.furthest else None
        self.furthest = max(self.furthest, start)
        self._save_place()

    def _save_place(self) -> None:
        # A page on screen hasn't been read yet, so only turning past the last one finishes a book.
        percent = 1.0 if self.finished else min(self.fraction(), 0.999)
        try:
            self.db.reading_save(self.key, self.position, self.book.words, percent)
        except sqlite3.Error as e:
            self.problem = str(e)

    def take_problem(self) -> str | None:
        problem, self.problem = self.problem, None
        return problem

    def _measure(self) -> None:
        if self.shown_at is None:
            return
        words = self.layout.end_word(self.page) - self.layout.start_word(self.page)
        if self.pace.record(words, self.clock() - self.shown_at):
            try:
                self.pace.save(self.db)
            except sqlite3.Error as e:
                self.problem = str(e)
        self.shown_at = None

    def _show(self, page: int) -> None:
        self.page = page
        self.finished = self.finished and page == self.layout.pages - 1
        self.position = self.layout.start_word(page)
        self._arrived()

    def next_page(self) -> bool:
        """False if this was already the last page."""
        self._measure()
        if self.page + 1 >= self.layout.pages:
            self.finished = True
            self._save_place()
            return False
        self._show(self.page + 1)
        return True

    def prev_page(self) -> None:
        if self.page > 0:
            self._show(self.page - 1)

    def go_to(self, word: int) -> None:
        self.furthest = -1
        self._show(self.layout.page_of(word))

    def pause(self) -> None:
        """The reader is doing something else, so this page's time says nothing about reading speed."""
        self.shown_at = None

    def fraction(self) -> float:
        return self.layout.end_word(self.page) / self.book.words

    def seconds_left(self) -> float:
        return self.pace.seconds_for(self.book.words - self.position)

    def seconds_left_in_chapter(self) -> float:
        return self.pace.seconds_for(self.book.chapter_end(self.position) - self.position)

    def status(self, cols: int) -> tuple[str, str]:
        """The two ends of the status line, using only what fits."""
        room = cols - 2
        place = f"p. {self.page + 1}/{self.layout.pages} · {int(self.fraction() * 100)}%"
        total = human_duration(self.seconds_left())
        chapter = self.book.chapter_at(self.position)
        options = [f"{total} left"]
        if self.book.chapters:
            options.insert(0, f"{human_duration(self.seconds_left_in_chapter())} chapter · {total} book")
        right = next((o for o in options if fmt.text_width(place) + 2 + fmt.text_width(o) <= room), "")
        gap = 2 + fmt.text_width(right) if right else 0
        name_room = room - gap - fmt.text_width(place) - 3
        name = chapter.title if chapter else self.title
        if name and name_room >= 8:
            place = f"{fmt.truncate(name, name_room)} · {place}"
        return place, right


# --------------------------------------------------------------------------
# Choosing a book
# --------------------------------------------------------------------------

def progress_label(row, pace: Pace, length: int | None = None) -> tuple[str, str]:
    """(how far, time left) for the list of books. `length` is the words in a book not opened yet, if known."""
    if row is None:
        return "-", human_duration(pace.seconds_for(length)) if length is not None else "-"
    if row["percent"] >= 1:
        return "done", ""
    return f"{int(row['percent'] * 100)}%", human_duration(pace.seconds_for(row["words"] - row["position"]))


def shelf_order(books: list, progress: dict) -> list:
    """Books being read, latest first, then the rest as they were."""
    def latest(b):
        row = progress.get(b.key)
        return -row["updated"] if row else 0
    return sorted(books, key=latest)


# --------------------------------------------------------------------------
# Curses: the reader
# --------------------------------------------------------------------------

class ReaderStyle:
    """Colours for one theme, answering to the names kutil's dialogs ask for."""

    BODY, HEADING, STATUS = 21, 22, 23   # kutil.tui.Theme uses pairs 1 to 8

    def __init__(self, theme: ReaderTheme):
        self.theme = theme
        if theme.fg < 0:
            self.body, self.heading, self.status = 0, curses.A_BOLD, curses.A_DIM
            return
        curses.init_pair(self.BODY, theme.fg, theme.bg)
        curses.init_pair(self.HEADING, theme.heading, theme.bg)
        curses.init_pair(self.STATUS, theme.status, theme.bg)
        self.body = curses.color_pair(self.BODY)
        self.heading = curses.color_pair(self.HEADING) | curses.A_BOLD
        self.status = curses.color_pair(self.STATUS)

    def __call__(self, *names) -> int:
        attr = 0
        for name in names:
            attr |= {"bold": curses.A_BOLD, "reverse": curses.A_REVERSE, "underline": curses.A_UNDERLINE,
                     "dim": self.status}.get(name, self.heading if name else 0)
        return attr


def _lone_escape(scr) -> bool:
    """True if Esc itself was pressed, rather than the start of a key sequence this terminal description doesn't know."""
    scr.timeout(0)
    try:
        scr.get_wch()
    except curses.error:
        return True
    finally:
        scr.timeout(150)
    curses.flushinp()
    return False


def _write(scr, y: int, x: int, text: str, attr: int) -> None:
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass    # the bottom-right cell raises after drawing


class Reader(Screen):
    def __init__(self, scr, session: Session):
        self.scr, self.session, self.db = scr, session, session.db
        self.prefs = Prefs.load(self.db)
        self.themes = available_themes()
        self.message: tuple[str, float] | None = None
        self.running = True
        self.too_small = False
        self.margin = _open_marginalia(session)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = next((t for t in self.themes if t.key == self.prefs.theme), self.themes[0])
        self.theme = ReaderStyle(theme)
        self.scr.bkgd(" ", self.theme.body)

    def flash(self, text: str) -> None:
        self.message = (text, time.monotonic() + 3)

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        rows, cols = scr.getmaxyx()
        self.too_small = cols < MIN_COLS or rows < MIN_ROWS
        if self.too_small:
            put(scr, 0, 0, f"Too small to read in ({MIN_COLS}×{MIN_ROWS}).")
            return
        m = self.margin
        panel = m.width_for(cols) if m and m.is_open else 0
        v = view(cols - (panel + 1 if panel else 0), rows, self.prefs)
        s = self.session
        s.set_view(v.width, v.rows)
        if m and m.is_open and not panel:       # too narrow for both, so the panel has the window
            m.draw(scr, 0, 1, cols - 2, rows - 1, self.theme.body, self.theme.status)
            self.draw_status(rows - 1, cols)
            return
        blocks = s.book.blocks
        for i, line in enumerate(s.layout.page_lines(s.page)):
            x = v.x + line.x
            base = 0
            if line.block >= 0 and blocks[line.block].kind in ("h", "break"):
                base = self.theme.heading if blocks[line.block].kind == "h" else self.theme.status
            for text, style in s.layout.segments(line):
                attr = base | (curses.A_BOLD if style & ebook.BOLD else 0) | (A_ITALIC if style & ebook.ITALIC else 0)
                _write(scr, v.y + i, x, text, attr)
                x += cell_width(text)
        if panel:
            for y in range(rows - 1):
                put(scr, y, cols - panel - 1, "│", self.theme.status)
            m.draw(scr, 0, cols - panel, panel - 1, rows - 1, self.theme.body, self.theme.status)
        self.draw_status(rows - 1, cols)

    def draw_status(self, y: int, cols: int) -> None:
        if self.message and self.message[1] <= time.monotonic():
            self.message = None
        if self.message:
            put(self.scr, y, 1, self.message[0], self.theme.status, width=cols - 2)
            return
        left, right = self.session.status(cols)
        put(self.scr, y, 1, left, self.theme.status)
        if right:
            put(self.scr, y, cols - 1 - fmt.text_width(right), right, self.theme.status)

    def run(self) -> None:
        set_cursor(False)
        self.scr.keypad(True)
        if self.session.finished_before:
            self.flash("You've reached the end of this one. Home starts it again.")
        elif not self.db.meta_get("reader_help_seen"):
            self.flash("? keys and settings")
        try:
            while self.running:
                self.draw()
                self.scr.refresh()
                # Only wake up to clear a message, or while the panel has news coming. Asked after drawing,
                # because drawing is what finds out.
                self.scr.timeout(200 if self.message or (self.margin and self.margin.animating) else -1)
                key = read_key(self.scr)
                if key is not None:
                    self.handle(key)
        finally:
            if self.margin:
                self.margin.close()
            self.scr.bkgd(" ", 0)
            self.scr.timeout(150)

    def handle(self, key) -> None:
        before = self.session.position
        try:
            self.act(key)
            if self.margin and self.session.position != before:
                self.margin.sync()
        except (sqlite3.Error, OSError) as e:       # the panel's memory failing must never cost a page
            self.flash(f"Marginalia: {e}")
        if key in ("UP", "DOWN") and not (self.margin and self.margin.focused):
            curses.flushinp()       # a terminal turns one scroll-wheel notch into several arrows at once
        problem = self.session.take_problem()
        if problem:
            self.flash(f"Can't save your place: {problem}")

    def act(self, key) -> None:
        s = self.session
        m = self.margin
        if m and m.focused and key != "RESIZE" and s.layout is not None and not self.too_small:
            if key != "ESC" or _lone_escape(self.scr):
                s.pause()
                m.handle(key, m.width_for(self.scr.getmaxyx()[1]) == 0)
            return
        if m and key == "TAB" and m.is_open:
            m.focused = True
        elif m and key == "m" and s.layout is not None and not self.too_small:
            s.pause()
            m.toggle()
        elif key == "q" or (key == "ESC" and _lone_escape(self.scr)):
            self.running = False
        elif key in ("RESIZE", "ESC") or s.layout is None or self.too_small:
            return
        elif key in (" ", "RIGHT", "DOWN", "PGDN", "ENTER", "j", "l", "n"):
            if not s.next_page():
                self.flash("End of the book")
        elif key in ("LEFT", "UP", "PGUP", "BACKSPACE", "k", "h", "b", "p"):
            s.prev_page()
        elif key in ("HOME", "g"):
            s.go_to(0)
        elif key in ("END", "G"):
            s.go_to(s.book.words)
        elif key in ("t", "T"):
            s.pause()
            self.prefs.next_theme(1 if key == "t" else -1, self.themes)
            self.apply_theme()
            self.save_prefs()
            self.flash(f"Theme: {self.theme.theme.label}")
        elif key in ("[", "]"):
            self.side_margin(1 if key == "]" else -1)
        elif key in ("{", "}"):
            self.top_margin(1 if key == "}" else -1)
        elif key == "c":
            self.contents()
        elif key == "?":
            self.help()

    def save_prefs(self) -> None:
        try:
            self.prefs.save(self.db)
        except sqlite3.Error as e:
            self.session.problem = str(e)

    def side_margin(self, step: int) -> None:
        cols = self.scr.getmaxyx()[1]
        before = view(cols, MIN_ROWS, self.prefs).width
        self.prefs.change_side_margin(step, cols)
        self.save_prefs()
        v = view(cols, MIN_ROWS, self.prefs)
        if v.width == before:
            return self.flash("Side margins can't go further in this window")
        self.flash(f"Side margins {v.x} · lines {v.width} wide")

    def top_margin(self, step: int) -> None:
        self.prefs.change_top_margin(step)
        self.save_prefs()
        self.flash(f"Top and bottom margins {self.prefs.margin_y}")

    def help(self) -> None:
        self.session.pause()
        try:
            self.db.meta_set("reader_help_seen", 1)
        except sqlite3.Error:
            pass                # the hint simply shows again next time
        pace = self.session.pace
        if pace.samples:
            speed = f"{pace.wpm:.0f} words a minute, learned from {pace.samples} page turns (started at {DEFAULT_WPM:.0f})"
        else:
            speed = f"{pace.wpm:.0f} words a minute, the global average, until you've turned some pages"
        self.show_text(HELP.format(speed=speed))

    def contents(self) -> None:
        s = self.session
        chapters = s.book.chapters
        if not chapters:
            return self.flash("This book has no contents list")
        s.pause()
        current = s.book.chapter_at(s.position)
        state = ListState(chapters.index(current) if current else 0)
        while True:
            self.draw()
            self._draw_contents(chapters, state)
            self.scr.refresh()
            key = read_key(self.scr)
            if key in (None, "RESIZE"):
                continue
            if key in ("ESC", "q", "c"):
                return
            if key == "ENTER":
                return s.go_to(chapters[state.index].word)
            if state.navigate(key, max(1, self.scr.getmaxyx()[0] - 6)):
                state.index = min(state.index, len(chapters) - 1)

    def _draw_contents(self, chapters: list, state: ListState) -> None:
        rows, cols = self.scr.getmaxyx()
        height = min(rows - 2, len(chapters) + 2)
        width = min(cols - 2, 70)
        y0, x0 = max(0, (rows - height) // 2), max(0, (cols - width) // 2)
        self.box(y0, x0, height, width, "Contents - Enter go, Esc close", self.theme.status)
        body = height - 2
        state.clamp(len(chapters), body)
        words = self.session.book.words
        for row in range(body):
            i = state.top + row
            if i >= len(chapters):
                break
            c = chapters[i]
            attr = curses.A_REVERSE if i == state.index else 0
            where = f"{int(c.word / words * 100):>3}%"
            line = pad("  " * min(c.depth, 4) + c.title, width - 9) + " " + where
            put(self.scr, y0 + 1 + row, x0 + 2, line, attr)


# --------------------------------------------------------------------------
# Curses: the list of books
# --------------------------------------------------------------------------

def read_book(app: Screen, store, path: str, title: str = "") -> str | None:
    """Open a book on `app`'s screen and read until it's closed. Returns why it couldn't be opened, if it couldn't."""
    if not library.is_readable(path):
        return f"The reader opens EPUB books; this one is {os.path.splitext(path)[1].lstrip('.') or 'something else'}"
    entry = library.read_entry(path)
    title = title or entry.title
    try:
        loaded = app.run_task(f"Opening {title[:60]}", lambda log: ebook.load(path))
    except ebook.EbookError as e:
        return f"{title}: {e}"
    session = Session(loaded, library.book_key(path), title, store)
    session.authors = entry.authors
    Reader(app.scr, session).run()
    return None


class Menu(Screen):
    def __init__(self, scr, store, folder: str, query: str = ""):
        self.scr, self.db, self.folder = scr, store, folder
        self.theme = Theme()
        self.search = query
        self.editing: LineEdit | None = None
        self.state = ListState()
        self.books: list = []
        self.progress: dict = {}
        self.lengths: dict[str, int] = {}
        self.counted: list[tuple[str, int]] = []    # found by the counter, waiting to be saved on this thread
        self.reading = threading.Event()
        self.pace = Pace.load(store)
        self.message: tuple[str, float] | None = None
        self.running = True

    def refresh_progress(self) -> None:
        self.progress = self.db.reading_all()
        self.pace = Pace.load(self.db)
        self.books = shelf_order(self.books, self.progress)

    def count_lengths(self) -> None:
        """Runs on its own thread, a book at a time, so unopened books gain a time left without holding up the menu.
        It waits while a book is open, so it never slows the reader."""
        known = set(self.lengths) | set(self.progress)
        for entry in [e for e in self.books if e.readable and e.key not in known]:
            while self.reading.is_set():
                time.sleep(0.2)
            words = ebook.count_words(entry.path)      # never raises, so nothing can print over the screen
            if words:
                self.counted.append((entry.key, words))

    def save_counted(self) -> None:
        while self.counted:
            key, words = self.counted.pop()
            self.lengths[key] = words
            self.db.length_save(key, words)

    def visible(self) -> list:
        return [b for b in self.books if library.matches(b, self.search)]

    def draw(self) -> None:
        scr, th = self.scr, self.theme
        scr.erase()
        h, w = scr.getmaxyx()
        if h < MIN_ROWS or w < MIN_COLS:
            put(scr, 0, 0, f"Too small ({MIN_COLS}×{MIN_ROWS}).")
            return
        put(scr, 0, 0, " " * w, th("bar"))
        put(scr, 0, 0, " bookread ", th("bar", "bold"))
        right = f" {self.pace.wpm:.0f} words a minute "
        put(scr, 0, max(13, w - fmt.text_width(right)), right, th("bar"))
        put(scr, 1, 0, " Search: ", th("dim"))
        if self.editing:
            field = min(30, w - 10)
            visible, cx = self.editing.render(field)
            put(scr, 1, 9, pad(visible, field), th("underline"))
            set_cursor(True)
            scr.move(1, 9 + cx)
        else:
            set_cursor(False)
            put(scr, 1, 9, self.search or "-", th("yellow", "bold") if self.search else th("dim"))
        books = self.visible()
        count = f"{len(books)} book{'' if len(books) == 1 else 's'} "
        put(scr, 1, max(42, w - fmt.text_width(count)), count, th("dim"))
        if books:
            self.draw_table(2, h - 3, w, self.columns(), books, self.cell, self.state)
        else:
            put(scr, 3, 2, "No books match." if self.search else f"No books in {self.folder}. Choose another with `bookread --dir FOLDER`.", th("dim"))
        self.draw_footer(h - 1, w)

    @staticmethod
    def columns() -> list[Col]:
        # Narrow windows lose the series, then the author, never your progress. The last column leaves room for the
        # position in the list that draw_table prints at the end of the heading row, until titles need that room too.
        return [Col("title", "Title", None), Col("author", "Author", 24, "<", 2), Col("series", "Series", 20, "<", 3),
                Col("read", "Read", 5, ">"), Col("left", "Left", 8, ">"), Col("room", "", 7, "<", 1)]

    def cell(self, key, b):
        th = self.theme
        dim = th("dim") if not b.readable else 0
        if key == "title":
            return b.title, dim or th("bold")
        if key == "author":
            return ", ".join(b.authors), dim
        if key == "series":
            num = f" #{b.series_index:g}" if b.series_index is not None else ""
            return (b.series + num) if b.series else "", dim
        read, left = progress_label(self.progress.get(b.key), self.pace, self.lengths.get(b.key)) if b.readable else (b.format, "")
        if key == "room":
            return "", 0
        return (read, dim) if key == "read" else (left, dim or th("dim"))

    def draw_footer(self, y: int, w: int) -> None:
        if self.message and self.message[1] > time.monotonic():
            put(self.scr, y, 0, pad(" " + self.message[0], w), self.theme("alert"))
            return
        self.message = None
        hints = "type to search · Enter keep · Esc clear" if self.editing else "⏎ read  / search  q quit"
        put(self.scr, y, 0, pad(" " + hints, w), self.theme("dim"))

    def run(self) -> None:
        set_cursor(False)
        self.scr.keypad(True)
        self.scr.timeout(150)

        def load(log):
            log(f"Reading {self.folder}…")
            return library.scan(self.folder)
        self.books = self.run_task("Loading", load)
        self.refresh_progress()
        self.lengths = self.db.lengths()
        threading.Thread(target=self.count_lengths, daemon=True).start()
        while self.running:
            self.save_counted()
            self.draw()
            self.scr.refresh()
            key = read_key(self.scr)
            if key is not None:
                self.handle(key)

    def handle(self, key) -> None:
        if key == "RESIZE":
            return
        if self.editing:
            return self.handle_search(key)
        books = self.visible()
        if self.state.navigate(key, max(1, self.scr.getmaxyx()[0] - 5)):
            self.state.index = min(self.state.index, max(0, len(books) - 1))
        elif key == "/":
            self.editing = LineEdit(self.search)
        elif key == "ESC" and self.search:
            self.search = ""
            self.state.index = 0
        elif key == "q" or (key == "ESC" and _lone_escape(self.scr)):
            self.running = False
        elif key == "ENTER" and books:
            self.open(books[min(self.state.index, len(books) - 1)])

    def handle_search(self, key) -> None:
        if key == "ESC":
            self.search, self.editing = "", None
        elif key == "ENTER":
            self.search, self.editing = self.editing.text, None
        else:
            self.editing.handle(key)
            self.search = self.editing.text
            self.state.index = 0
            return
        set_cursor(False)
        self.state.index = 0

    def open(self, book) -> None:
        self.reading.set()
        try:
            problem = read_book(self, self.db, book.path, book.title)
        finally:
            self.reading.clear()
        if problem:
            self.message = (problem, time.monotonic() + 8)
            return
        self.refresh_progress()
        self.search = ""
        self.state = ListState()    # the book just read is now at the top


def run(store, folder: str, query: str = "", path: str | None = None, title: str = "") -> str | None:
    """The menu of books, or straight into the file at `path`. Returns a problem to report once the terminal is back."""
    os.environ.setdefault("ESCDELAY", "25")
    set_locale()

    def main(scr):
        menu = Menu(scr, store, folder, query)
        if path:
            scr.timeout(150)            # run_task polls for keys while it waits
            menu.books = [library.read_entry(path)]     # what shows behind the "Opening" box
            return read_book(menu, store, path, title)
        return menu.run()
    return curses.wrapper(main)
