"""Curses interface for kvctl (same look and widgets as trctl and plexsort).

Tabs: 1 Inbox · 2 Books · 3 Kavita · 4 History. Opening an inbox entry reads each book's own metadata, tops it up
online, and shows a review screen. Nothing moves until `a` is pressed there. `r` on the Books tab opens the reader.
"""

from __future__ import annotations

import copy
import curses
import locale
import os
import sqlite3
import time

from kutil import fmt
from kutil.tui import Col, Field, Form, LineEdit, ListState, Screen, Theme, choose, pad, put, read_key, set_cursor

import bookread.config
import bookread.reader
from bookread.store import Store

from . import config, engine
from .calibre import BookInfo
from .client import LIBRARY_TYPES, Kavita, KavitaError
from .db import DB
from .metadata import book_ext
from .providers import Lookup

TABS = ["Inbox", "Books", "Kavita", "History"]

HELP = """\
EVERYWHERE
  1 2 3 4  Tab      switch tabs: Inbox, Books, Kavita, History
  S                 settings (the API key is set with `kvctl setup`)
  ?                 this help              q   back / quit

INBOX  (finished torrents with books, and watched folders)
  Enter             review how it would be imported (checks books online)
  o                 import any other folder or file (Tab completes paths)
  r                 refresh

REVIEW  (nothing is moved until you press a)
  e                 edit the book's title, authors, series and so on
  f                 find it online; a match you pick may replace the title and author
  Space             skip / include a book
  a                 apply: move books, write metadata, clean up, scan Kavita
  Status column:    ✓ ok   ! warning   ✗ must be fixed   - skipped
  Applying deletes skipped books and other files in the source, after showing them.
  A folder opened with o, or a torrent that also holds videos or music, keeps its other files.

BOOKS  (what is in the Books folder)
  Enter             details                /  search
  r                 read the book in bookread
  e                 edit metadata; folder and file names follow the title and first author
  M                 merge authors: shows the plan first, a applies it
  s                 group series: shows which books Kavita would split up, a applies it

KAVITA
  Enter             list a library's series
  s  F              scan the library, or force a full rescan
  A                 scan every library
  n  e  x           new library, edit name and folders, delete (book files stay on disk)
  r                 refresh

HISTORY
  Enter             list what an operation changed
  u                 undo it (optionally re-add the torrent)"""


# --------------------------------------------------------------------------
# Book metadata form, used by the review screen and the Books tab
# --------------------------------------------------------------------------

def book_fields(info: BookInfo) -> list[Field]:
    return [Field("title", "Title", "text", info.title),
            Field("authors", "Authors", "text", "; ".join(info.authors), hint="separate several people with ;"),
            Field("series", "Series", "text", info.series),
            Field("series_index", "Number in series", "float", info.series_index),
            Field("language", "Language", "text", info.language),
            Field("publisher", "Publisher", "text", info.publisher),
            Field("isbn", "ISBN", "text", info.isbn)]


def edit_form(app, title: str, info: BookInfo) -> BookInfo | None:
    """Ask for new metadata. Returns a changed copy of `info`, or None if cancelled or unchanged."""
    def validate(v):
        if not v["title"]:
            return "A book needs a title"
        if not engine.split_authors([v["authors"]]):
            return "A book needs an author"
        return None
    fields = book_fields(info)
    form = Form(title, fields, ok="Save", validate=validate)
    values = form.run(app)
    if values is None or not form.changes():
        return None
    new = copy.deepcopy(info)
    new.title, new.series, new.language = values["title"], values["series"], values["language"]
    new.publisher, new.isbn, new.series_index = values["publisher"], values["isbn"], values["series_index"]
    new.authors = engine.split_authors([values["authors"]])
    return new


def formats(files: list[str]) -> str:
    return ",".join(sorted({book_ext(f).lstrip(".") for f in files}))


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------

class Review:
    def __init__(self, app: "App", plan: engine.Plan, errors: list[str]):
        self.app, self.plan = app, plan
        self.state = ListState()
        if errors:
            app.flash("Online check: " + errors[0], "info")
        self.check()

    def check(self) -> None:
        engine.check_plan(self.plan, self.app.cfg, self.app.db, self.app.books)

    def current(self) -> engine.Book | None:
        return self.plan.books[self.state.index] if self.plan.books else None

    def hints(self) -> str:
        return "e edit  f find online  Space skip  a apply  Esc back (keeps your changes)  ? help"

    def draw(self, y0: int, height: int, w: int) -> None:
        app, plan, th = self.app, self.plan, self.app.theme
        active = [b for b in plan.books if not b.skip]
        put(app.scr, y0, 0, f" {plan.name}", th("bold"))
        count = f"{len(active)} of {len(plan.books)} book(s) to import "
        put(app.scr, y0, max(0, w - len(count)), count, th("dim"))
        detail = self.detail_lines()
        table_h = max(3, height - len(detail) - 2)
        cols = [Col("status", "", 1), Col("id", "ID", 4, ">", 2), Col("title", "Title", None), Col("author", "Author", 24, "<", 3),
                Col("series", "Series", 20, "<", 1), Col("type", "Type", 9, "<", 4), Col("from", "Data from", 14, "<", 5)]

        def cell(key, b: engine.Book):
            if key == "status":
                if b.skip:
                    return "-", th("dim")
                return ("✗", th("red", "bold")) if b.error else ("!", th("yellow")) if b.warning else ("✓", th("green"))
            dim = th("dim") if b.skip else 0
            if key == "id":
                return (str(b.book_id) if b.dest else ""), th("dim")
            if key == "title":
                return b.info.title or "(no title)", dim or th("bold")
            if key == "author":
                return ", ".join(b.info.authors) or "?", dim
            if key == "series":
                num = f" #{b.info.series_index:g}" if b.info.series_index is not None else ""
                return (b.info.series + num) if b.info.series else "", dim
            if key == "type":
                return formats(b.files), dim
            if key == "from":
                return (f"✓ {b.verified}" if b.verified else b.source), dim
            return "", 0
        app.draw_table(y0 + 1, table_h, w, cols, plan.books, cell, self.state)
        y = y0 + 2 + table_h
        for text, attr in detail:
            put(app.scr, y, 1, text, attr, width=w - 2)
            y += 1

    def detail_lines(self) -> list[tuple[str, int]]:
        th, cfg, plan = self.app.theme, self.app.cfg, self.plan
        out: list[tuple[str, int]] = []
        b = self.current()
        if b:
            out.append((", ".join(b.files), th("dim")))
            out.append((f"→ {os.path.relpath(b.dest, cfg['books_dir'])}" if b.dest else "Not imported", th("cyan") if b.dest else th("dim")))
            if b.error:
                out.append((f"✗ {b.error}", th("red", "bold")))
            elif b.warning:
                out.append((f"! {b.warning}", th("yellow")))
        out += [(f"✗ {e}", th("red", "bold")) for e in plan.global_errors]
        out += [(f"Also adds {f.book.info.title} to {f.series}, as #{f.index:g} (already in the library)", th("yellow")) for f in plan.series_partners]
        delete = engine.files_to_delete(plan, cfg)
        if delete:
            out.append((f"Applying deletes {len(delete)} file(s) from the source: " + ", ".join(delete[:4]) + (" …" if len(delete) > 4 else ""), th("dim")))
        elif plan.leftovers:
            out.append((f"{len(plan.leftovers)} other file(s) stay in the source", th("dim")))
        return out

    def handle(self, key) -> None:
        app = self.app
        if key in ("ESC", "q"):
            app.review = None
        elif key == "?":
            app.show_help()
        elif app.nav(self.state, key, len(self.plan.books)):
            pass
        elif key == " " and self.current():
            b = self.current()
            b.skip = not b.skip
            self.check()
        elif key == "e" and self.current():
            b = self.current()
            new = edit_form(app, f"Edit: {b.info.title or b.files[0]}", b.info)
            if new:
                b.info = new
                self.check()
        elif key == "f" and self.current():
            self.find_online(self.current())
        elif key == "a":
            self.apply()

    def find_online(self, b: engine.Book) -> None:
        app = self.app
        if app.offline:
            return app.flash("Online lookups are off", "info")
        form = Form("Find online (Open Library)", [Field("title", "Title", "text", b.info.title),
                                                  Field("author", "Author", "text", b.info.authors[0] if b.info.authors else "")],
                    ok="Search")
        values = form.run(app)
        if not values or not values["title"]:
            return
        results = app.run_task("Searching Open Library", lambda log: app.lookup.search(values["title"], [values["author"]] if values["author"] else []))
        if not results:
            why = app.lookup.errors[0] if app.lookup.errors else "no results (self-published books are often missing)"
            return app.flash(f"Nothing found: {why}", "info")
        results = results[:9]
        options = [(str(i + 1), m.label(), "") for i, m in enumerate(results)]
        picked = choose(app, "Use this match? It replaces the title and author", [f"For: {b.info.title or b.files[0]}"], options)
        if picked:
            engine.apply_match(b, results[int(picked) - 1], overwrite=True)
            self.check()

    def apply(self) -> None:
        app, plan, cfg = self.app, self.plan, self.app.cfg
        self.check()
        if not engine.plan_ok(plan):
            return app.flash("Fix the ✗ problems first: " + "; ".join(plan.global_errors + [b.error for b in plan.books if b.error])[:120], "error")
        active = [b for b in plan.books if not b.skip]
        if not active:
            return app.flash("Every book is skipped; nothing to import", "info")
        delete = engine.files_to_delete(plan, cfg)
        lines = [f"Import {len(active)} book(s) into {cfg['books_dir']}"]
        lines += [f"Add {f.book.info.title} to {f.series}, as #{f.index:g}" for f in plan.series_partners]
        skipped = {r for b in plan.books if b.skip for r in b.files}
        if skipped & set(delete):
            lines.append(f"Delete {len(skipped & set(delete))} skipped book file(s)")
        others = [r for r in delete if r not in skipped]
        if others:
            lines.append(f"Delete {len(others)} other file(s): " + ", ".join(others[:3]) + (" …" if len(others) > 3 else ""))
        if engine.other_media(plan):
            lines.append(f"Keep the torrent and {len(engine.other_media(plan))} video/music file(s)")
        elif plan.torrent:
            lines.append("Remove the torrent from Transmission" if engine.will_remove_torrent(plan, cfg) else "Keep the torrent, stopped")
        options = [("a", "Import", "green")]
        if delete or engine.will_remove_torrent(plan, cfg):
            options.append(("k", "Import, but delete nothing" + (" and keep the torrent" if plan.torrent else ""), "yellow"))
        options.append(("c", "Cancel", ""))
        picked = choose(app, "Apply", lines, options)
        if picked not in ("a", "k"):
            return
        keep = picked == "k"
        res = app.run_task("Importing", lambda log: engine.apply_plan(
            plan, cfg, app.db, app.transmission, app.kavita_for_scan(), None if app.offline else app.lookup,
            remove_torrent=False if keep else None, delete_leftovers=False if keep else None, log=log))
        app.plans.pop(plan.root, None)
        app.review = None
        app.refresh_local()
        if res.errors:
            app.flash(f"Imported {res.imported}, then: {res.errors[0]}", "error")
        else:
            app.flash(f"Imported {res.imported} book(s) (operation {res.op_id})" + (f"; {res.scanned}" if res.scanned else ""))


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

class App(Screen):
    def __init__(self, scr, cfg: dict, db: DB, transmission, offline: bool = False):
        self.scr, self.cfg, self.db, self.transmission = scr, cfg, db, transmission
        self.forced_offline = offline
        self.offline = offline or not cfg.get("online", True)
        self.theme = Theme()
        self.kavita = Kavita(cfg["url"], cfg["api_key"])
        self.lookup = Lookup(db)
        self.tab = 0
        self.lists = {name: ListState() for name in ("inbox", "books", "kavita", "series", "history")}
        self.sources: list[engine.Source] = []
        self.inbox_error = ""
        self.books: list[engine.LibBook] = []
        self.search = ""
        self.editing: LineEdit | None = None
        self.libs: list[dict] = []
        self.kavita_error = ""
        self.series_lib: dict | None = None
        self.series: list[dict] = []
        self.ops: list = []
        self.plans: dict[str, tuple[engine.Plan, list[str]]] = {}
        self.review: Review | None = None
        self.message: tuple[str, int, float] | None = None
        self.running = True

    # -- data ------------------------------------------------------------------

    def kavita_for_scan(self) -> Kavita | None:
        return self.kavita if self.cfg["api_key"] else None

    def refresh_local(self) -> None:
        self.sources, self.inbox_error = engine.list_sources(self.cfg, self.transmission)
        self.books = engine.library_books(self.cfg)
        self.ops = self.db.operations()

    def refresh_kavita(self) -> None:
        self.kavita_error = ""
        if not self.cfg["api_key"]:
            self.libs, self.kavita_error = [], "No API key yet: quit and run `kvctl setup`."
            return
        try:
            self.libs = self.kavita.libraries()
        except KavitaError as e:
            self.libs, self.kavita_error = [], str(e)

    def refresh_all(self) -> None:
        self.refresh_local()
        self.refresh_kavita()

    def flash(self, text: str, kind: str = "ok") -> None:
        attr = {"ok": self.theme("green"), "error": self.theme("alert"), "info": self.theme("cyan")}[kind]
        self.message = (text, attr, time.monotonic() + (10 if kind == "error" else 5))

    def visible_books(self) -> list[engine.LibBook]:
        return [b for b in self.books if engine.book_matches(b, self.search)]

    # -- drawing ---------------------------------------------------------------

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 60:
            put(scr, 0, 0, "Terminal too small for kvctl - enlarge it (min 60×12).")
            return
        self.draw_header(w)
        if self.review:
            self.review.draw(1, h - 2, w)
        elif self.tab == 0:
            self.draw_inbox(1, h - 2, w)
        elif self.tab == 1:
            self.draw_books(1, h - 2, w)
        elif self.tab == 2:
            self.draw_kavita(1, h - 2, w)
        else:
            self.draw_history(1, h - 2, w)
        self.draw_footer(h - 1, w)

    def draw_header(self, w: int) -> None:
        th = self.theme
        put(self.scr, 0, 0, " " * w, th("bar"))
        put(self.scr, 0, 0, " kvctl ", th("bar", "bold"))
        x = 8
        labels = [f"Inbox ({len(self.sources)})", f"Books ({len(self.books)})", f"Kavita ({len(self.libs)})", "History"]
        for i, label in enumerate(labels):
            text = f" {i + 1} {label} "
            active = i == self.tab and not self.review
            put(self.scr, 0, x, text, th("reverse", "bold") if active else th("bar"))
            x += fmt.text_width(text) + 1
        status = ["Kavita ✗" if self.kavita_error else "Kavita ✓", "Transmission ✓" if self.transmission else "Transmission ✗"]
        if self.offline:
            status.append("offline")
        right = " " + " · ".join(status) + " "
        if x + fmt.text_width(right) < w:
            put(self.scr, 0, w - fmt.text_width(right), right, th("bar"))

    def draw_footer(self, y: int, w: int) -> None:
        if self.message and self.message[2] > time.monotonic():
            put(self.scr, y, 0, pad(" " + self.message[0], w), self.message[1])
            return
        self.message = None
        if self.review:
            hints = self.review.hints()
        elif self.editing:
            hints = "type to search · Enter keep · Esc clear"
        elif self.tab == 0:
            hints = "⏎ review & import  o open folder  r refresh  ⇥ tabs  S settings  ? help  q quit"
        elif self.tab == 1:
            hints = "⏎ details  r read  / search  e edit  M merge authors  s group series  ⇥ tabs  S settings  ? help  q quit"
        elif self.series_lib:
            hints = "Esc back  ? help"
        elif self.tab == 2:
            hints = "⏎ series  s scan  F force  A scan all  n new  e edit  x delete  r refresh  ? help  q quit"
        else:
            hints = "⏎ changes  u undo  ⇥ tabs  ? help  q quit"
        put(self.scr, y, 0, pad(" " + hints, w), self.theme("dim"))

    def draw_inbox(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        info = f" {len(self.sources)} waiting to be imported"
        put(self.scr, y0, 0, info, th("dim"))
        if self.inbox_error:
            put(self.scr, y0, fmt.text_width(info) + 2, self.inbox_error, th("red"))
        if not self.sources:
            where = ", ".join(self.cfg["inbox_dirs"]) or "a watched folder"
            put(self.scr, y0 + 2, 2, f"Nothing to import. Finished torrents with books and items in {where} show up here;", th("dim"))
            put(self.scr, y0 + 3, 2, "press o to import any other folder.", th("dim"))
            return
        cols = [Col("id", "ID", 4, ">"), Col("kind", "Source", 7), Col("books", "Files", 5, ">"), Col("size", "Size", 10, ">", 2),
                Col("done", "Completed", 16, "<", 3), Col("name", "Name", None), Col("note", "", 16, "<", 1)]

        def cell(key, s: engine.Source):
            if key == "id":
                return (str(s.torrent["id"]) if s.torrent else "-"), th("dim")
            if key == "kind":
                return s.kind, th("cyan") if s.kind == "torrent" else th("magenta")
            if key == "books":
                return str(s.books), 0
            if key == "size":
                return fmt.human_size(s.size), 0
            if key == "done":
                return fmt.human_time(s.done_date), th("dim")
            if key == "name":
                return s.name, th("bold") if s.path in self.plans else 0
            if key == "note":
                return ("plan in progress", th("yellow")) if s.path in self.plans else ("", 0)
            return "", 0
        self.draw_table(y0 + 1, height - 1, w, cols, self.sources, cell, self.lists["inbox"])

    def draw_books(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        put(self.scr, y0, 0, " Search: ", th("dim"))
        if self.editing:
            visible, cx = self.editing.render(30)
            put(self.scr, y0, 9, pad(visible, 30), th("underline"))
            set_cursor(True)
            self.scr.move(y0, 9 + cx)
        else:
            set_cursor(False)
            put(self.scr, y0, 9, self.search or "-", th("yellow", "bold") if self.search else th("dim"))
        books = self.visible_books()
        summary = f"{len(books)} book(s) · {fmt.human_size(sum(b.size for b in books))} "
        put(self.scr, y0, max(42, w - fmt.text_width(summary)), summary, th("dim"))
        if not books:
            put(self.scr, y0 + 2, 2, "No books match." if self.search else f"No books in {self.cfg['books_dir']}.", th("dim"))
            return
        cols = [Col("id", "ID", 4, ">"), Col("title", "Title", None), Col("author", "Author", 24, "<", 3), Col("series", "Series", 20, "<", 1),
                Col("type", "Type", 9, "<", 4), Col("size", "Size", 9, ">", 2)]

        def cell(key, b: engine.LibBook):
            if key == "id":
                return str(b.id), th("dim")
            if key == "title":
                return b.info.title, th("bold")
            if key == "author":
                return ", ".join(b.info.authors), 0
            if key == "series":
                num = f" #{b.info.series_index:g}" if b.info.series_index is not None else ""
                return (b.info.series + num) if b.info.series else "", 0
            if key == "type":
                return formats(b.files), 0
            if key == "size":
                return fmt.human_size(b.size), 0
            return "", 0
        self.draw_table(y0 + 1, height - 1, w, cols, books, cell, self.lists["books"])

    def draw_kavita(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        if self.series_lib:
            put(self.scr, y0, 0, f" {self.series_lib.get('name')}: {len(self.series)} series", th("bold"))
            if not self.series:
                put(self.scr, y0 + 2, 2, "No series yet. Press Esc, then s to scan.", th("dim"))
                return
            cols = [Col("id", "ID", 6, ">"), Col("name", "Name", None), Col("pages", "Pages", 7, ">", 1)]

            def cell(key, s):
                return {"id": (str(s.get("id")), th("dim")), "name": (s.get("name") or "?", th("bold")),
                        "pages": (str(s.get("pages") or 0), 0)}.get(key, ("", 0))
            self.draw_table(y0 + 1, height - 1, w, cols, self.series, cell, self.lists["series"])
            return
        put(self.scr, y0, 0, f" {self.cfg['url']}", th("dim"))
        if self.kavita_error:
            put(self.scr, y0 + 2, 2, self.kavita_error, th("red"))
            return
        if not self.libs:
            put(self.scr, y0 + 2, 2, "Kavita has no libraries yet. Press n to create one.", th("dim"))
            return
        cols = [Col("id", "ID", 4, ">"), Col("name", "Name", 24), Col("type", "Type", 12, "<", 2), Col("folders", "Folders", None),
                Col("scan", "Last scan", 16, "<", 1)]

        def cell(key, l):
            if key == "id":
                return str(l.get("id")), th("dim")
            if key == "name":
                return l.get("name") or "?", th("bold")
            if key == "type":
                return LIBRARY_TYPES.get(l.get("type"), "?"), 0
            if key == "folders":
                return ", ".join(l.get("folders") or []), 0
            if key == "scan":
                return (l.get("lastScanned") or "-")[:16].replace("T", " "), th("dim")
            return "", 0
        self.draw_table(y0 + 1, height - 1, w, cols, self.libs, cell, self.lists["kavita"])

    def draw_history(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        if not self.ops:
            put(self.scr, y0 + 1, 2, "Nothing imported yet.", th("dim"))
            return
        cols = [Col("id", "Op", 4, ">"), Col("when", "When", 16, "<", 2), Col("kind", "Kind", 13), Col("status", "Status", 8),
                Col("books", "Books", 5, ">", 1), Col("name", "Source", None)]

        def cell(key, o):
            if key == "id":
                return str(o["id"]), th("dim")
            if key == "when":
                return o["created"][:16], th("dim")
            if key == "kind":
                return o["kind"], 0
            if key == "status":
                return o["status"], {"undone": th("dim"), "failed": th("red")}.get(o["status"], th("green"))
            if key == "books":
                return str(o["books"] or 0), 0
            if key == "name":
                return o["source_name"] or "", 0
            return "", 0
        self.draw_table(y0, height, w, cols, self.ops, cell, self.lists["history"])

    # -- input -----------------------------------------------------------------

    def run(self, start_source: str | None = None) -> int:
        set_cursor(False)
        self.scr.keypad(True)
        self.scr.timeout(150)
        try:
            self.run_task("Loading", lambda log: (log("Checking Kavita, Transmission and the library…"), self.refresh_all()))
        except Exception as e:
            self.flash(f"Load failed: {e}", "error")
        if start_source:
            self.open_start(start_source)
        while self.running:
            self.draw()
            self.scr.refresh()
            key = read_key(self.scr)
            if key is not None:
                try:
                    self.handle(key)
                except (engine.PlanError, KavitaError) as e:
                    self.flash(str(e), "error")
        return 0

    def open_start(self, arg: str) -> None:
        hits = [s for s in self.sources if arg in (s.path, s.name)
                or (s.torrent and arg in (s.torrent["hashString"], str(s.torrent["id"])))]
        try:
            if hits:
                self.open_source(hits[0])
            elif os.path.exists(os.path.expanduser(arg)):
                self.open_source(engine.source_for_path(arg))
            else:
                self.flash(f"{arg}: not in the inbox and not a path", "error")
        except engine.PlanError as e:
            self.flash(str(e), "error")

    def nav(self, state: ListState, key, n: int) -> bool:
        if state.navigate(key, max(1, self.scr.getmaxyx()[0] - 6)):
            state.index = min(state.index, max(0, n - 1))
            return True
        return False

    def show_help(self) -> None:
        self.show_text(HELP)

    def handle(self, key) -> None:
        if key == "RESIZE":
            return
        if self.review:
            return self.review.handle(key)
        if self.editing:
            return self.handle_search(key)
        if self.series_lib:
            if key in ("ESC", "q"):
                self.series_lib = None
            elif key == "?":
                self.show_help()
            else:
                self.nav(self.lists["series"], key, len(self.series))
            return
        if key == "?":
            return self.show_help()
        if key == "S":
            return self.settings()
        if key in ("q", "C-c"):
            self.running = False
            return
        if key in ("1", "2", "3", "4"):
            self.tab = int(key) - 1
            return
        if key in ("TAB", "BTAB"):
            self.tab = (self.tab + (1 if key == "TAB" else -1)) % len(TABS)
            return
        (self.handle_inbox, self.handle_books, self.handle_kavita, self.handle_history)[self.tab](key)

    def handle_search(self, key) -> None:
        if key == "ESC":
            self.search, self.editing = "", None
        elif key == "ENTER":
            self.search, self.editing = self.editing.text, None
        else:
            self.editing.handle(key)
            self.search = self.editing.text
            self.lists["books"].index = 0
            return
        set_cursor(False)

    # -- inbox -----------------------------------------------------------------

    def handle_inbox(self, key) -> None:
        state = self.lists["inbox"]
        if self.nav(state, key, len(self.sources)):
            return
        if key == "r":
            self.run_task("Refreshing", lambda log: self.refresh_all())
        elif key == "o":
            values = Form("Import a folder or file", [Field("path", "Path", "path", "")], ok="Open",
                          note="Its other files stay where they are").run(self)
            if values and values["path"]:
                self.open_source(engine.source_for_path(values["path"]))
        elif key == "ENTER" and self.sources:
            self.open_source(self.sources[min(state.index, len(self.sources) - 1)])

    def open_source(self, src: engine.Source) -> None:
        if src.path in self.plans:
            self.review = Review(self, self.plans[src.path][0], [])
            return
        if not src.books:
            return self.flash(f"No books in {src.path}", "info")

        def build(log):
            log(f"Reading {src.books} file(s)…")
            self.books = engine.library_books(self.cfg)
            plan = engine.make_plan(src, self.cfg, self.books)
            errors = []
            if not self.offline:
                log("Checking Open Library…")
                errors = engine.auto_match(plan, self.lookup, lambda n, total, title: log(f"{n}/{total}  {title}"))
            return plan, errors
        plan, errors = self.run_task(f"Reading {src.name[:60]}", build)
        self.plans[src.path] = (plan, errors)
        self.review = Review(self, plan, errors)

    # -- books -----------------------------------------------------------------

    def handle_books(self, key) -> None:
        books = self.visible_books()
        state = self.lists["books"]
        if self.nav(state, key, len(books)):
            return
        if key == "/":
            self.editing = LineEdit(self.search)
        elif key == "ESC" and self.search:
            self.search = ""
        elif key == "M":
            self.merge_authors()
        elif key == "s":
            self.group_series()
        elif books:
            b = books[min(state.index, len(books) - 1)]
            if key == "ENTER":
                if self.show_text(self.book_text(b), "Book - e edit, any other key closes") == "e":
                    self.edit_book(b)
            elif key == "e":
                self.edit_book(b)
            elif key == "r":
                self.read(b)

    def read(self, b: engine.LibBook) -> None:
        try:
            store = Store(bookread.config.load()["database"])
            try:
                problem = bookread.reader.read_book(self, store, engine.book_to_read(b), b.info.title)
            finally:
                store.close()
        except (OSError, sqlite3.Error) as e:
            problem = f"Can't open the reading database: {e}"
        if problem:
            self.flash(problem, "error")

    def book_text(self, b: engine.LibBook) -> str:
        i = b.info
        rows = [("Title", i.title), ("Authors", ", ".join(i.authors)),
                ("Series", f"{i.series}" + (f" #{i.series_index:g}" if i.series_index is not None else "") if i.series else ""),
                ("Language", i.language), ("Publisher", i.publisher), ("ISBN", i.isbn), ("Published", i.pubdate[:10]),
                ("Id", str(b.id)), ("Size", fmt.human_size(b.size))]
        lines = [f"  {k + ':':11}{v}" for k, v in rows if v]
        lines += ["", "Folder", f"  {os.path.relpath(b.path, self.cfg['books_dir'])}", "", "Files", *(f"  {f}" for f in b.files)]
        if not b.has_opf:
            lines += ["", "No metadata.opf: this comes from the folder names. Editing writes one."]
        if i.description:
            lines += ["", "Description", f"  {i.description[:300]}"]
        return "\n".join(lines)

    def edit_book(self, b: engine.LibBook) -> None:
        new = edit_form(self, f"Edit: {b.info.title}", b.info)
        if not new:
            return
        res = self.run_task("Saving", lambda log: engine.edit_book(b, new, self.cfg, self.db, self.kavita_for_scan()))
        self.refresh_local()
        if res.errors:
            self.flash(f"Could not save: {res.errors[0]}", "error")
        else:
            self.flash(f"Saved (operation {res.op_id})")

    def merge_authors(self) -> None:
        renames = self.run_task("Reading authors", lambda log: engine.author_merge_plan(self.cfg, self.books))
        if not renames:
            return self.flash("Every author is already 'First Last', with one folder and one spelling each", "info")
        lines = []
        for r in renames:
            lines.append(r.book.info.title)
            if r.new_author != r.book.info.authors[0]:
                lines.append(f"  {r.book.info.authors[0]} -> {r.new_author}")
            if r.new_path != r.book.path:
                lines.append(f"  → {os.path.relpath(r.new_path, self.cfg['books_dir'])}")
            if r.epub_authors:
                lines.append(f"  inside the EPUB: {'; '.join(r.epub_before)} -> {'; '.join(next(iter(r.epub_authors.values())))}")
            lines.append("")
        if self.show_text("\n".join(lines).rstrip(), f"Merge authors: {len(renames)} book(s) - a apply, any other key cancels") != "a":
            return
        res = self.run_task("Renaming", lambda log: engine.apply_renames(renames, self.cfg, self.db, self.kavita_for_scan(), log=log))
        self.refresh_local()
        if res.errors:
            self.flash(f"Renamed {res.imported}; {res.errors[0]}", "error")
        else:
            self.flash(f"Renamed {res.imported} book(s) (operation {res.op_id})")

    def group_series(self) -> None:
        fixes = engine.series_fix_plan(self.cfg, self.books)
        if not fixes:
            return self.flash("No books need grouping: Kavita can keep every series together", "info")
        lines, current = [], None
        for f in fixes:
            if f.series != current:
                lines += ["", f.series] if current else [f.series]
                current = f.series
            lines.append(f"  #{f.index:<4g} {f.book.info.title}" + ("  (number guessed)" if f.number_guessed else ""))
        if self.show_text("\n".join(lines), f"Group series: {len(fixes)} book(s) - a apply, any other key cancels") != "a":
            return
        res = self.run_task("Grouping", lambda log: engine.apply_series(fixes, self.cfg, self.db, self.kavita_for_scan(), log=log))
        self.refresh_local()
        if res.errors:
            self.flash(f"Grouped {res.imported}; {res.errors[0]}", "error")
        else:
            self.flash(f"Grouped {res.imported} book(s) (operation {res.op_id})")

    # -- Kavita ----------------------------------------------------------------

    def handle_kavita(self, key) -> None:
        state = self.lists["kavita"]
        if self.nav(state, key, len(self.libs)):
            return
        if key == "r":
            self.run_task("Refreshing", lambda log: self.refresh_kavita())
        elif key == "A":
            self.run_task("Asking Kavita to scan", lambda log: self.kavita.scan_all())
            self.flash("Kavita is scanning every library")
        elif key == "n":
            self.new_library()
        elif self.libs:
            lib = self.libs[min(state.index, len(self.libs) - 1)]
            if key in ("s", "F"):
                self.run_task("Asking Kavita to scan", lambda log: self.kavita.scan(lib["id"], key == "F"))
                self.flash(f"Kavita is scanning {lib['name']}" + (" (forced)" if key == "F" else ""))
            elif key == "e":
                self.edit_library(lib)
            elif key == "x":
                self.delete_library(lib)
            elif key == "ENTER":
                self.series = self.run_task("Loading series", lambda log: self.kavita.all_series(lib["id"]))
                self.series_lib = lib
                self.lists["series"] = ListState()

    def new_library(self) -> None:
        def validate(v):
            return None if v["name"] and v["folder"] else "A library needs a name and a folder"
        values = Form("New Kavita library (Book type)", [Field("name", "Name", "text", "Books"),
                                                        Field("folder", "Folder", "path", self.cfg["books_dir"])],
                      ok="Create", validate=validate).run(self)
        if values:
            folder = os.path.abspath(os.path.expanduser(values["folder"]))
            self.run_task("Creating", lambda log: self.kavita.create_library(values["name"], [folder]))
            self.run_task("Refreshing", lambda log: self.refresh_kavita())
            self.flash(f"Created {values['name']}")

    def edit_library(self, lib: dict) -> None:
        def validate(v):
            return None if v["name"] and v["folders"] else "A library needs a name and a folder"
        form = Form(f"Edit {lib['name']}", [Field("name", "Name", "text", lib.get("name") or ""),
                                            Field("folders", "Folders", "text", ", ".join(lib.get("folders") or []),
                                                  hint="separate several with commas")],
                    ok="Save", validate=validate)
        values = form.run(self)
        if values and form.changes():
            folders = [os.path.abspath(os.path.expanduser(f.strip())) for f in values["folders"].split(",") if f.strip()]
            self.run_task("Saving", lambda log: self.kavita.update_library(lib, values["name"], folders))
            self.run_task("Refreshing", lambda log: self.refresh_kavita())
            self.flash("Saved")

    def delete_library(self, lib: dict) -> None:
        picked = choose(self, f"Delete {lib['name']} from Kavita?",
                        ["Book files stay on disk. Kavita forgets the series and reading progress."],
                        [("n", "Keep it", ""), ("y", "Delete from Kavita", "red")], danger=True)
        if picked == "y":
            self.run_task("Deleting", lambda log: self.kavita.delete_library(lib["id"]))
            self.run_task("Refreshing", lambda log: self.refresh_kavita())
            self.flash(f"Deleted {lib['name']} from Kavita; files are untouched")

    # -- history ---------------------------------------------------------------

    def handle_history(self, key) -> None:
        state = self.lists["history"]
        if self.nav(state, key, len(self.ops)) or not self.ops:
            return
        op = self.ops[min(state.index, len(self.ops) - 1)]
        if key == "ENTER":
            self.show_changes(op)
        elif key == "u":
            self.undo(op)

    def show_changes(self, op) -> None:
        lines = [f"{op['kind']}: {op['source_name'] or ''}", f"  {op['status']}  {op['created']}"]
        if op["message"]:
            lines.append(f"  {op['message']}")
        lines.append("")
        for c in self.db.changes(op["id"]):
            mark = " (undone)" if c["status"] == "undone" else ""
            lines.append(f"  {c['kind']}{mark}: {os.path.relpath(c['dst'], self.cfg['books_dir'])}")
        self.show_text("\n".join(lines))

    def undo(self, op) -> None:
        if op["status"] == "undone":
            return self.flash("Already undone", "info")
        lines = [f"Undo operation {op['id']}: {op['kind']} of {op['source_name'] or ''}?"]
        if op["leftovers_deleted"]:
            lines.append("Leftover files it deleted can't be restored.")
        options = [("u", "Undo", "yellow")]
        if op["torrent_removed"] and op["magnet"] and self.transmission:
            options.append(("r", "Undo and re-add the torrent", "yellow"))
        options.append(("c", "Cancel", ""))
        picked = choose(self, "Undo", lines, options)
        if picked not in ("u", "r"):
            return
        done, errors = self.run_task("Undoing", lambda log: engine.undo_operation(
            op["id"], self.cfg, self.db, self.transmission, self.kavita_for_scan(), picked == "r"))
        self.plans.clear()
        self.refresh_local()
        if errors:
            self.flash(f"Undid {done} change(s), then: {errors[0]}", "error")
        else:
            self.flash(f"Undid {done} change(s)")

    # -- settings --------------------------------------------------------------

    def settings(self) -> None:
        c = self.cfg
        libs = [(0, "automatic")] + [(l["id"], l.get("name") or "?") for l in self.libs]
        if c["library_id"] not in [v for v, _ in libs]:
            libs.append((c["library_id"], f"library {c['library_id']}"))
        fields = [Field("url", "Kavita address", "text", c["url"]),
                  Field("books_dir", "Books folder", "path", c["books_dir"]),
                  Field("inbox_dirs", "Watched folders", "text", ", ".join(c["inbox_dirs"]), hint="separate several with commas"),
                  Field("library_id", "Library to scan", "choice", c["library_id"], libs, hint="automatic: the one holding the Books folder"),
                  Field("online", "Check books online", "bool", c["online"]),
                  Field("scan_after_import", "Scan after changes", "bool", c["scan_after_import"]),
                  Field("remove_torrent", "Remove torrents", "bool", c["remove_torrent"]),
                  Field("delete_leftovers", "Delete leftovers", "bool", c["delete_leftovers"]),
                  Field("min_free_gb", "Keep free (GB)", "int", c["min_free_gb"])]

        def validate(v):
            return None if v["url"] and v["books_dir"] else "The address and Books folder can't be empty"
        form = Form("Settings", fields, ok="Save", validate=validate, note=f"Saved to {config.config_path()}")
        values = form.run(self)
        if not values or not form.changes():
            return
        c.update(url=values["url"], online=values["online"], scan_after_import=values["scan_after_import"],
                 remove_torrent=values["remove_torrent"], delete_leftovers=values["delete_leftovers"],
                 library_id=values["library_id"], min_free_gb=values["min_free_gb"] or 0,
                 books_dir=os.path.abspath(os.path.expanduser(values["books_dir"])),
                 inbox_dirs=[os.path.abspath(os.path.expanduser(d.strip())) for d in values["inbox_dirs"].split(",") if d.strip()])
        config.save(c)
        self.kavita.url = c["url"].rstrip("/")
        self.offline = self.forced_offline or not c["online"]
        self.plans.clear()
        self.run_task("Refreshing", lambda log: self.refresh_all())
        self.flash("Settings saved")


def run(cfg: dict, db: DB, transmission, start_source: str | None = None, offline: bool = False) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")

    def main(scr):
        return App(scr, cfg, db, transmission, offline).run(start_source)
    return curses.wrapper(main)
