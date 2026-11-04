"""Curses interface for plexsort (same look and widgets as trctl's).

Tabs: 1 Inbox (completed torrents) · 2 Library (sorted titles) · 3 History (operations, undo).
Opening an inbox entry builds a plan, verifies it online, and shows the review
screen where every file's destination can be checked and corrected before
anything is moved.
"""

from __future__ import annotations

import curses
import locale
import os
import time

from kutil import fmt
from kutil.tui import Col, Field, Form, FormError, LineEdit, ListState, Screen, Theme, choose, pad, put, read_key, \
    set_cursor

from . import config, engine, naming
from .folders_ui import FoldersDialog
from .db import DB
from .parse import EXTRA_TYPES, Group, Item, Plan, media_counts
from .providers import Match, Matcher, apply_match, similarity

TABS = ["Inbox", "Library", "History"]
LIB_FILTERS = ["all", "movie", "show", "music"]
LIB_NAMES = {"all": "All", "movie": "Movies", "show": "TV shows", "music": "Music"}

HELP = """\
EVERYWHERE
  1 2 3  Tab        switch tabs: Inbox, Library, History
  S                 settings: library & watched folders (several per library/drive), options
  ?                 this help              q   back / quit

INBOX  (completed torrents and folders waiting to be sorted)
  Enter             review how it would be sorted (looks titles up online)
  o                 sort any other folder or file (Tab completes paths)
  r                 refresh

REVIEW  (nothing is moved until you press a)
  Enter  e          edit the title (on a title row) or the file (on a file row)
  f                 find the right match online (TVmaze, Wikidata, TMDB, AniList, MyAnimeList, MusicBrainz)
  Space             skip / include a file (it moves to its section; the cursor stays put)
  Shift+↑↓  K J     move 5 rows at a time      m  mark files    E  edit all marked files
  M                 re-run automatic matching for unverified titles
  l                 choose which library folder / drive a title goes to (default: automatic by free space)
  v                 toggle source column       a  apply: move files, remove torrent, clean up
  Status column:    ✓ ok   ! warning (unverified / unknown episode)   ✗ must be fixed

LIBRARY
  Enter             show the title's files     /  search        Tab  all / movies / TV / music
  f                 fix the match online (renames folder and files)
  e                 edit title / year by hand  u  undo the sort that added it
  x                 forget (remove from database only)     s  scan library folders

HISTORY
  Enter             list the operation's files
  u                 undo: move files back (optionally re-add the torrent)"""


def title_label(row) -> str:
    if row["category"] == "music":
        return f"{row['artist'] or 'Unknown Artist'} – {naming.title_name(row['title'], row['year'])}"
    return naming.title_name(row["title"], row["year"])


class App(Screen):

    def __init__(self, scr, cfg: dict, db: DB, client, offline: bool = False):
        self.scr = scr
        self.cfg = cfg
        self.db = db
        self.client = client
        self.offline = offline or not cfg.get("online", True)
        self.theme = Theme()
        self.tab = 0
        self.lists = {name: ListState() for name in ("inbox", "library", "history", "files", "review")}
        self.sources: list[engine.Source] = []
        self.downloading = 0
        self.inbox_error = ""
        self.lib_rows: list = []
        self.lib_counts = {"movie": 0, "show": 0, "music": 0, "episodes": 0, "tracks": 0, "size": 0}
        self.lib_filter = "all"
        self.lib_search = ""
        self.ops: list = []
        self.detail_title = None          # library title row being viewed
        self.detail_files: list = []
        self.review: Review | None = None
        self.plans: dict[str, Plan] = {}  # cached plans by source path
        self.editing: LineEdit | None = None
        self.message: tuple[str, int, float] | None = None
        self.running = True
        self.setup_only = False
        self.matcher = Matcher(cfg, db)

    # -- data ------------------------------------------------------------------

    def refresh_inbox(self) -> None:
        self.sources, self.downloading, self.inbox_error = engine.list_sources(self.cfg, self.client)

    def refresh_library(self) -> None:
        cat = None if self.lib_filter == "all" else self.lib_filter
        self.lib_rows = self.db.titles(cat, self.lib_search or None)
        self.lib_counts = self.db.counts()
        if self.detail_title is not None:
            self.detail_title = self.db.title(self.detail_title["id"])
            self.detail_files = self.db.title_files(self.detail_title["id"]) if self.detail_title else []

    def refresh_history(self) -> None:
        self.ops = self.db.operations()

    def refresh_all(self) -> None:
        self.refresh_inbox()
        self.refresh_library()
        self.refresh_history()

    def flash(self, text: str, kind: str = "ok") -> None:
        attr = {"ok": self.theme("green"), "error": self.theme("alert"), "info": self.theme("cyan")}[kind]
        self.message = (text, attr, time.monotonic() + (10 if kind == "error" else 5))

    # -- drawing -------------------------------------------------------------------

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 60:
            put(scr, 0, 0, "Terminal too small for plexsort - enlarge it (min 60×12).")
            return
        self.draw_header(w)
        if self.review:
            self.review.draw(1, h - 2, w)
        elif self.tab == 0:
            self.draw_inbox(1, h - 2, w)
        elif self.tab == 1:
            self.draw_library(1, h - 2, w)
        else:
            self.draw_history(1, h - 2, w)
        self.draw_footer(h - 1, w)

    def draw_header(self, w: int) -> None:
        th = self.theme
        put(self.scr, 0, 0, " " * w, th("bar"))
        x = 0
        put(self.scr, 0, x, " plexsort ", th("bar", "bold"))
        x += 11
        c = self.lib_counts
        labels = [f"Inbox ({len(self.sources)})",
                  f"Library ({c['movie']} movies · {c['show']} shows · {c.get('music', 0)} albums)", "History"]
        for i, label in enumerate(labels):
            text = f" {i + 1} {label} "
            active = i == self.tab and not self.review
            put(self.scr, 0, x, text, th("reverse", "bold") if active else th("bar"))
            x += fmt.text_width(text) + 1
        status = []
        status.append("Transmission ✓" if self.client else "Transmission ✗")
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
            hints = "⏎ review & sort  o open folder  r refresh  ⇥ tabs  S settings  ? help  q quit"
        elif self.tab == 1 and self.detail_title is not None:
            hints = "f fix match  e edit title  u undo its sort  Esc back  ? help"
        elif self.tab == 1:
            hints = "⏎ files  / search  ⇥ all/movies/TV/music  f fix match  e edit  u undo  x forget  s scan  ? help"
        else:
            hints = "⏎ files  u undo (move files back)  ⇥ tabs  ? help  q quit"
        put(self.scr, y, 0, pad(" " + hints, w), self.theme("dim"))

    def draw_inbox(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        info = f" {len(self.sources)} waiting to be sorted"
        if self.downloading:
            info += f" · {self.downloading} torrent(s) still downloading"
        put(self.scr, y0, 0, info, th("dim"))
        if self.inbox_error:
            put(self.scr, y0, fmt.text_width(info) + 2, self.inbox_error, th("red"))
        if not self.sources:
            put(self.scr, y0 + 2, 2, "Nothing to sort. Completed torrents show up here; press o to sort any folder.",
                th("dim"))
            return
        cols = [Col("id", "ID", 4, ">"), Col("kind", "Source", 7), Col("size", "Size", 10, ">", 2),
                Col("done", "Completed", 16, "<", 3), Col("name", "Name", None), Col("note", "", 22, "<", 1)]

        def cell(key, s: engine.Source):
            if key == "id":
                return (str(s.torrent["id"]) if s.torrent else "-"), th("dim")
            if key == "kind":
                return s.kind, th("cyan") if s.kind == "torrent" else th("magenta")
            if key == "size":
                return fmt.human_size(s.size), 0
            if key == "done":
                return fmt.human_time(s.done_date), th("dim")
            if key == "name":
                return s.name, th("bold") if s.path in self.plans else 0
            if key == "note":
                if not s.exists:
                    return s.note, th("red")
                return ("plan in progress", th("yellow")) if s.path in self.plans else ("", 0)
            return "", 0
        self.draw_table(y0 + 1, height - 1, w, cols, self.sources, cell, self.lists["inbox"])

    def lib_visible(self) -> list:
        return self.lib_rows

    def draw_library(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        if self.detail_title is not None:
            return self.draw_title_detail(y0, height, w)
        x = 0
        for text, attr in ((" Show: ", th("dim")), (LIB_NAMES[self.lib_filter], th("bold")), ("   Search: ", th("dim"))):
            put(self.scr, y0, x, text, attr)
            x += fmt.text_width(text)
        if self.editing:
            visible, cx = self.editing.render(30)
            put(self.scr, y0, x, pad(visible, 30), th("underline"))
            set_cursor(True)
            cursor = (y0, x + cx)
        else:
            cursor = None
            put(self.scr, y0, x, self.lib_search or "-", th("yellow", "bold") if self.lib_search else th("dim"))
        c = self.lib_counts
        summary = (f"{c['movie']} movies · {c['show']} shows ({c['episodes']} ep) · {c.get('music', 0)} albums "
                   f"({c.get('tracks', 0)} tracks) · {fmt.human_size(c['size'])} ")
        put(self.scr, y0, max(x + 32, w - fmt.text_width(summary)), summary, th("dim"))
        rows = self.lib_visible()
        if not rows:
            put(self.scr, y0 + 2, 2, "Nothing here yet. Sort something from the Inbox, or press s to scan "
                                     "existing library folders.", th("dim"))
        else:
            cols = [Col("type", "Type", 5), Col("title", "Title", None), Col("content", "Content", 20, "<", 3),
                    Col("size", "Size", 10, ">", 2), Col("verified", "Matched via", 12, "<", 4),
                    Col("where", "Location", 24, "<", 6), Col("added", "Added", 10, "<", 5)]

            def cell(key, r):
                if key == "type":
                    return {"show": ("TV", th("cyan")), "music": ("Music", th("green"))}.get(
                        r["category"], ("Movie", th("magenta")))
                if key == "title":
                    return title_label(r), th("bold")
                if key == "content":
                    if r["missing"]:
                        return f"{r['missing']} file(s) missing", th("red")
                    if r["category"] == "music":
                        return f"{r['tracks'] or 0} track{'s' if r['tracks'] != 1 else ''}" + (
                            f" · {r['discs']} discs" if (r["discs"] or 0) > 1 else ""), 0
                    if r["category"] == "show":
                        return f"{r['episodes'] or 0} ep · {r['seasons'] or 0} season{'s' if r['seasons'] != 1 else ''}", 0
                    return f"{r['videos'] or 0} file{'s' if r['videos'] != 1 else ''}", 0
                if key == "size":
                    return fmt.human_size(r["size"]), 0
                if key == "verified":
                    return (r["provider"] or "-"), th("green") if r["provider"] not in ("", "scan", "manual") else th("dim")
                if key == "added":
                    return (r["last_added"] or "")[:10], th("dim")
                if key == "where":
                    return naming.root_of(r["path"], r["category"], self.cfg), th("dim")
                return "", 0
            self.draw_table(y0 + 1, height - 1, w, cols, rows, cell, self.lists["library"])
        if cursor:
            self.scr.move(*cursor)
        else:
            set_cursor(False)

    def draw_title_detail(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        t = self.detail_title
        put(self.scr, y0, 1, title_label(t), th("bold"))
        put(self.scr, y0 + 1, 1, f"{t['path']}   ids: {t['ids']}   via {t['provider'] or '-'}", th("dim"))
        cols = [Col("code", "What", 12), Col("path", "File", None), Col("size", "Size", 10, ">", 1),
                Col("status", "Status", 8, "<", 2)]

        def cell(key, f):
            if key == "code":
                if f["kind"] == "track":
                    return (f"{f['season']}-" if f["season"] else "") + (f"{f['episode']:02d}" if f["episode"] else "track"), th("green")
                if f["kind"] == "episode":
                    return naming.episode_code(f["season"] or 0, f["episode"] or 0, f["episode_end"]), th("cyan")
                if f["kind"] == "subtitle":
                    return f"sub {f['lang'] or ''}".strip(), th("dim")
                return f["kind"] + (f" {f['extra_type']}" if f["kind"] == "extra" and f["extra_type"] else ""), 0
            if key == "path":
                return os.path.relpath(f["dst"], t["path"]), 0 if f["status"] == "present" else th("red")
            if key == "size":
                return fmt.human_size(f["size"]), 0
            if key == "status":
                return f["status"], th("green") if f["status"] == "present" else th("red")
            return "", 0
        self.draw_table(y0 + 3, height - 3, w, cols, self.detail_files, cell, self.lists["files"])

    def draw_history(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        if not self.ops:
            put(self.scr, y0 + 1, 2, "Nothing sorted yet.", th("dim"))
            return
        cols = [Col("id", "Op", 4, ">"), Col("when", "When", 16), Col("status", "Status", 8),
                Col("files", "Files", 9, ">", 2), Col("size", "Size", 10, ">", 3), Col("source", "Source", None),
                Col("torrent", "Torrent", 9, "<", 1)]

        def cell(key, o):
            if key == "id":
                return str(o["id"]), th("dim")
            if key == "when":
                return o["created"][:16], 0
            if key == "status":
                return o["status"], {"applied": th("green"), "undone": th("dim"), "failed": th("red")}.get(o["status"], 0)
            if key == "files":
                return f"{o['present'] or 0}/{o['files']}", 0
            if key == "size":
                return fmt.human_size(o["size"]), 0
            if key == "source":
                return o["source_name"] or "", 0
            if key == "torrent":
                return ("removed" if o["torrent_removed"] else "-"), th("dim")
            return "", 0
        self.draw_table(y0, height, w, cols, self.ops, cell, self.lists["history"])

    # -- main loop -------------------------------------------------------------------

    def run(self, start_source: str | None = None) -> int:
        set_cursor(False)
        self.scr.keypad(True)
        self.scr.timeout(150)
        if self.setup_only or not config.exists():
            saved = self.folders(first_run=not config.exists())
            if self.setup_only:
                return 0 if saved else 1
            if not config.exists():
                config.save(self.cfg)  # don't ask again; defaults were accepted
        try:
            self.run_task("Loading", lambda log: (log("Checking Transmission and the library…"), self.refresh_all()))
        except Exception as e:
            self.flash(f"Load failed: {e}", "error")
        if start_source:
            match = [s for s in self.sources if start_source in (s.path, s.name) or
                     (s.torrent and start_source in (s.torrent["hashString"], str(s.torrent["id"])))]
            src = match[0] if match else (engine.source_for_path(start_source) if os.path.exists(start_source) else None)
            if src:
                self.open_source(src)
        while self.running:
            self.draw()
            self.scr.refresh()
            key = read_key(self.scr)
            if key is not None:
                try:
                    self.handle(key)
                except engine.SortError as e:
                    self.flash(str(e), "error")
        return 0

    def handle(self, key) -> None:
        if key == "RESIZE":
            return
        if self.review:
            self.review.handle(key)
            return
        if self.editing:
            return self.handle_search_edit(key)
        if key == "?":
            return self.show_help()
        if key == "S":
            return self.settings()
        if key == "C-c":
            self.running = False
            return
        if key in ("1", "2", "3") and self.detail_title is None:
            self.tab = int(key) - 1
            return
        if key == "q" and self.detail_title is None:
            self.running = False
            return
        if self.tab == 0:
            self.handle_inbox(key)
        elif self.tab == 1:
            self.handle_library(key)
        else:
            self.handle_history(key)

    def nav(self, state: ListState, key, n: int) -> bool:
        if state.navigate(key, max(1, self.scr.getmaxyx()[0] - 6)):
            state.index = min(state.index, max(0, n - 1))
            return True
        return False

    def show_help(self) -> None:
        self.show_text(HELP)

    # -- inbox ---------------------------------------------------------------------------

    def handle_inbox(self, key) -> None:
        state = self.lists["inbox"]
        if self.nav(state, key, len(self.sources)):
            return
        if key in ("TAB", "BTAB"):
            self.tab = (self.tab + (1 if key == "TAB" else -1)) % 3
        elif key == "r":
            self.run_task("Refreshing", lambda log: self.refresh_all())
        elif key == "o":
            values = Form("Sort a folder or file", [
                Field("path", "Path", "path", os.path.dirname(self.sources[0].path) + "/" if self.sources else "",
                      hint="any folder, video or audio file · Tab completes")], ok="Open",
                validate=lambda v: None if os.path.exists(os.path.expanduser(v["path"])) else "No such file or folder"
            ).run(self)
            if values:
                self.open_source(engine.source_for_path(values["path"]))
        elif key == "ENTER" and self.sources:
            src = self.sources[min(state.index, len(self.sources) - 1)]
            if not src.exists:
                self.flash(f"{src.path} not found on disk", "error")
                return
            self.open_source(src)

    def open_source(self, src: engine.Source) -> None:
        plan = self.plans.get(src.path)
        if plan is None:
            def build(log):
                log(f"Reading {src.path}")
                p = engine.make_plan(src)
                log(f"{media_counts(p.items)}, {len(p.leftovers)} other file(s) · {len(p.groups())} title(s)")
                errors = []
                if not self.offline:
                    errors = engine.auto_match(p, self.matcher,
                                               progress=lambda n, total, t: log(f"[{n}/{total}] looked up “{t}”"))
                return p, errors
            plan, errors = self.run_task(f"Analysing {src.name[:60]}", build)
            self.plans[src.path] = plan
            if errors:
                self.flash("Lookup problems: " + "; ".join(errors)[:200], "error")
        self.review = Review(self, plan, src)

    # -- library ---------------------------------------------------------------------------

    def handle_search_edit(self, key) -> None:
        if key == "ENTER":
            self.editing = None
        elif key == "ESC":
            self.editing = None
            self.lib_search = ""
        else:
            self.editing.handle(key)
            self.lib_search = self.editing.text
        self.lists["library"].index = 0
        self.refresh_library()

    def handle_library(self, key) -> None:
        if self.detail_title is not None:
            return self.handle_title_detail(key)
        rows = self.lib_visible()
        state = self.lists["library"]
        if self.nav(state, key, len(rows)):
            return
        current = rows[min(state.index, len(rows) - 1)] if rows else None
        if key in ("TAB", "BTAB"):
            self.lib_filter = LIB_FILTERS[(LIB_FILTERS.index(self.lib_filter) + (1 if key == "TAB" else -1)) % 3]
            state.index = 0
            self.refresh_library()
        elif key == "/":
            self.editing = LineEdit(self.lib_search)
        elif key == "ESC":
            self.lib_search = ""
            self.refresh_library()
        elif key == "s":
            added, missing = self.run_task("Scanning library folders", lambda log: engine.scan_library(self.cfg, self.db))
            self.refresh_library()
            self.flash(f"Indexed {added} new file(s); {missing} missing")
        elif current is None:
            return
        elif key == "ENTER":
            self.detail_title = current
            self.lists["files"] = ListState()
            self.refresh_library()
        else:
            self.title_action(key, current)

    def handle_title_detail(self, key) -> None:
        if key in ("ESC", "q", "BACKSPACE", "LEFT"):
            self.detail_title = None
            self.refresh_library()
            return
        if self.nav(self.lists["files"], key, len(self.detail_files)):
            return
        self.title_action(key, self.detail_title)

    def title_action(self, key, t) -> None:
        if key == "f":
            match = MatchDialog(self, t["title"], t["year"], t["category"], bool(t["anime"]),
                                artist=t["artist"] or "").run()
            if match:
                if (match.category == "music") != (t["category"] == "music"):
                    self.flash("Can't turn a video title into music (or back)", "error")
                    return
                self.do_retitle(t, match.title, match.year, match.ids, match.provider, match.category,
                                match.artist if match.category == "music" else None)
        elif key == "e":
            music = t["category"] == "music"
            fields = [Field("title", "Album" if music else "Title", "text", t["title"]),
                      Field("year", "Year", "int", t["year"])]
            if music:
                fields.insert(0, Field("artist", "Album artist", "text", t["artist"] or ""))
            else:
                fields.append(Field("category", "Library", "choice", t["category"],
                                    [("movie", "Movies"), ("show", "TV Shows")]))
            values = Form(f"Edit “{t['title']}”", fields, ok="Rename", note="Renames the folder and every file on disk",
                          validate=lambda v: None if v["title"] else "Title can't be empty").run(self)
            if values:
                self.do_retitle(t, values["title"], values["year"], {}, "manual", values.get("category", "music"),
                                values.get("artist"))
        elif key == "u":
            files = self.db.title_files(t["id"])
            ops = sorted({f["operation_id"] for f in files if f["operation_id"]})
            if not ops:
                self.flash("This title was indexed by a scan, not sorted - nothing to undo", "info")
                return
            self.undo_op(ops[-1])
        elif key == "x":
            if choose(self, "Forget title", [f"Remove “{t['title']}” from the database?",
                                             "Files on disk are not touched."],
                      [("y", "Forget it", "red"), ("Esc", "Cancel", "dim")], default=1) == "y":
                self.db.forget_title(t["id"])
                self.detail_title = None
                self.refresh_library()
                self.flash("Forgotten")

    def do_retitle(self, t, title, year, ids, provider, category, artist=None) -> None:
        moved, errors = self.run_task("Renaming", lambda log: engine.retitle(
            t["id"], title, year, ids, provider, self.cfg, self.db, category, artist))
        self.detail_title = None
        self.refresh_library()
        if errors:
            self.flash("; ".join(errors)[:200], "error")
        else:
            self.flash(f"Renamed to “{naming.title_name(title, year)}” ({moved} file(s) moved)")

    # -- history -------------------------------------------------------------------------------

    def handle_history(self, key) -> None:
        state = self.lists["history"]
        if self.nav(state, key, len(self.ops)):
            return
        if key in ("TAB", "BTAB"):
            self.tab = (self.tab + (1 if key == "TAB" else -1)) % 3
            return
        if not self.ops:
            return
        op = self.ops[min(state.index, len(self.ops) - 1)]
        if key == "u":
            self.undo_op(op["id"])
        elif key == "ENTER":
            files = self.db.operation_files(op["id"])
            lines = [f"{'✓' if f['status'] == 'present' else '·'} {f['dst']}" for f in files] or ["(no files)"]
            if op["message"]:
                lines = [op["message"], ""] + lines
            choose(self, f"Operation #{op['id']}: {op['source_name']}", lines, [("Esc", "Close", "dim")])

    def undo_op(self, op_id: int) -> None:
        op = self.db.operation(op_id)
        if op["status"] == "undone":
            self.flash("Already undone", "info")
            return
        options = [("u", "Move the files back", "bold")]
        if op["magnet"] and op["torrent_removed"] and self.client:
            options.append(("t", "Move the files back and re-add the torrent to Transmission", "bold"))
        options.append(("Esc", "Cancel", "dim"))
        lines = [f"Undo sorting “{op['source_name']}” (#{op_id})?", f"Files go back to {op['source_root']}"]
        if op["leftovers_deleted"]:
            lines.append("Deleted leftover files (txt/nfo/samples) can't be restored.")
        choice = choose(self, "Undo", lines, options, default=len(options) - 1)
        if choice not in ("u", "t"):
            return
        restored, errors = self.run_task("Undoing", lambda log: engine.undo_operation(
            op_id, self.cfg, self.db, self.client, readd_torrent=choice == "t", log=log))
        self.refresh_all()
        if errors:
            self.flash(f"Restored {restored}; problems: " + "; ".join(errors)[:150], "error")
        else:
            self.flash(f"Restored {restored} file(s)" + (" and re-added the torrent" if choice == "t" else ""))

    # -- settings ---------------------------------------------------------------------------------

    def settings(self) -> None:
        choice = choose(self, "Settings", [str(config.config_path())], [
            ("f", "Library & watched folders (several per library, one per drive is fine)", "bold"),
            ("o", "Options: lookups, TMDB key, cleanup, free-space reserve", "bold"),
            ("Esc", "Close", "dim")])
        if choice == "f":
            self.folders()
        elif choice == "o":
            self.options()

    def folders(self, first_run: bool = False) -> bool:
        if not FoldersDialog(self, self.cfg, first_run).run():
            return False
        config.save(self.cfg)
        self.plans.clear()
        self.run_task("Refreshing", lambda log: self.refresh_all())
        self.flash("Folders saved")
        return True

    def options(self) -> None:
        c = self.cfg
        form = Form("Options", [
            Field("tmdb_api_key", "TMDB API key", "text", c.get("tmdb_api_key", ""),
                  hint="optional (free at themoviedb.org/settings/api); Wikidata works without a key"),
            Field("id_tags", "Add {tvdb-…} ids to folders", "bool", c["id_tags"],
                  hint="makes Plex match exactly"),
            Field("remove_torrent", "Remove torrent after sorting", "bool", c["remove_torrent"]),
            Field("delete_leftovers", "Delete leftover junk", "bool", c["delete_leftovers"],
                  hint=".txt/.nfo/samples left in the torrent folder"),
            Field("online", "Look titles up online", "bool", c.get("online", True)),
            Field("download_cover_art", "Download missing album covers", "bool", c.get("download_cover_art", True),
                  hint="from the Cover Art Archive, for albums matched on MusicBrainz"),
            Field("min_free_gb", "Keep free on each drive (GB)", "int", c.get("min_free_gb", 2),
                  hint="sorting never fills a drive beyond this"),
        ], ok="Save", note=str(config.config_path()),
            validate=lambda v: None if (v["min_free_gb"] or 0) >= 0 else "Must be 0 or more")
        values = form.run(self)
        if not values:
            return
        values["min_free_gb"] = values["min_free_gb"] or 0
        c.update(values)
        config.save(c)
        self.offline = not c["online"]
        self.matcher = Matcher(c, self.db)
        self.plans.clear()
        self.flash("Settings saved")


# --------------------------------------------------------------------------
# Online match search dialog
# --------------------------------------------------------------------------

class MatchDialog:
    def __init__(self, app: App, query: str, year, category: str, anime: bool, artist: str = ""):
        self.app = app
        if category == "music" and artist and artist not in ("Unknown Artist",):
            query = f"{artist} - {query}"
        self.query = LineEdit(query)
        self.year = LineEdit(str(year) if year else "")
        self.category = category
        self.anime = anime
        self.provider = "All"
        self.results: list[Match] = []
        self.state = ListState()
        self.focus = 0  # 0 query, 1 year, 2 type, 3 source, 4 results
        self.message = ""

    def providers(self) -> list[str]:
        names = ["All"] + [n for n, _ in self.app.matcher.sources(self.category, self.anime)]
        return names

    def search(self) -> None:
        q = self.query.text.strip()
        if not q:
            return
        year = int(self.year.text) if self.year.text.strip().isdigit() else None
        prov = None if self.provider == "All" else self.provider
        m = self.app.matcher
        artist = None
        if self.category == "music" and " - " in q:
            artist, q = (x.strip() for x in q.split(" - ", 1))
        self.results = self.app.run_task("Searching", lambda log: m.search(q, self.category, year, self.anime, prov,
                                                                           artist=artist))
        self.state = ListState()
        self.focus = 4 if self.results else 0
        errs = "; ".join(m.errors)
        self.message = (f"{len(self.results)} result(s)" if self.results else "No results - try a shorter or "
                        "original-language title") + (f" · {errs}" if errs else "")

    def run(self) -> Match | None:
        if self.query.text:
            self.search()
        while True:
            self.app.draw()
            cursor = self.draw()
            if cursor:
                set_cursor(True)
                self.app.scr.move(*cursor)
            else:
                set_cursor(False)
            self.app.scr.refresh()
            key = read_key(self.app.scr)
            if key in (None, "RESIZE"):
                continue
            if key == "ESC":
                set_cursor(False)
                return None
            if key == "TAB":
                self.focus = (self.focus + 1) % 5
            elif key == "BTAB":
                self.focus = (self.focus - 1) % 5
            elif self.focus == 4:
                if key == "UP" and self.state.index == 0:
                    self.focus = 0
                elif self.state.navigate(key, 5):
                    self.state.index = min(self.state.index, max(0, len(self.results) - 1))
                elif key == "ENTER" and self.results:
                    set_cursor(False)
                    return self.results[self.state.index]
                elif key == "i":
                    manual = self.manual()
                    if manual:
                        return manual
            elif key == "ENTER":
                self.search()
            elif key == "DOWN":
                self.focus = 4 if self.results else self.focus
            elif self.focus == 2 and key in (" ", "LEFT", "RIGHT"):
                order = ["show", "movie", "music"]
                self.category = order[(order.index(self.category) + (-1 if key == "LEFT" else 1)) % 3]
                self.provider = "All"
            elif self.focus == 3 and key in (" ", "LEFT", "RIGHT"):
                names = self.providers()
                i = names.index(self.provider) if self.provider in names else 0
                self.provider = names[(i + (-1 if key == "LEFT" else 1)) % len(names)]
            elif self.focus == 0:
                self.query.handle(key)
            elif self.focus == 1 and (key in ("BACKSPACE", "DELETE", "LEFT", "RIGHT", "HOME", "END", "C-u")
                                      or (isinstance(key, str) and key.isdigit())):
                self.year.handle(key)

    def manual(self) -> Match | None:
        values = Form("Enter ids by hand", [
            Field("title", "Title", "text", self.query.text), Field("year", "Year", "int",
                                                                    int(self.year.text) if self.year.text.isdigit() else None),
            Field("tvdb", "TVDB id", "int", None), Field("tmdb", "TMDB id", "int", None),
            Field("imdb", "IMDb id", "text", "", hint="tt1234567"),
        ], ok="Use", validate=lambda v: None if v["title"] else "Title can't be empty").run(self.app)
        if not values:
            return None
        ids = {k: values[k] for k in ("tvdb", "tmdb", "imdb") if values[k]}
        artist = ""
        title = values["title"]
        if self.category == "music" and " - " in title:
            artist, title = (x.strip() for x in title.split(" - ", 1))
        return Match("manual", "", self.category, title, values["year"], ids, "entered by hand", artist=artist)

    def draw(self):
        app, th = self.app, self.app.theme
        scr = app.scr
        h, w = scr.getmaxyx()
        width = min(w - 2, 120)
        height = min(h - 2, 22)
        y0, x0 = max(0, (h - height) // 2), max(0, (w - width) // 2)
        app.box(y0, x0, height, width, "Find the right title online", th("cyan"))
        cursor = None
        y = y0 + 2
        x = x0 + 2
        put(scr, y, x, "Search:", th("bold") if self.focus == 0 else th("dim"))
        qw = max(20, width - 62)
        visible, cx = self.query.render(qw)
        put(scr, y, x + 8, pad(visible, qw), th("reverse") if self.focus == 0 else th("underline"))
        if self.focus == 0:
            cursor = (y, x + 8 + cx)
        x += 8 + qw + 2
        put(scr, y, x, "Year:", th("bold") if self.focus == 1 else th("dim"))
        visible, cx = self.year.render(5)
        put(scr, y, x + 6, pad(visible, 5), th("reverse") if self.focus == 1 else th("underline"))
        if self.focus == 1:
            cursor = (y, x + 6 + cx)
        x += 13
        put(scr, y, x, f"‹ {dict(show='TV show', movie='Movie', music='Album')[self.category]} ›",
            th("reverse") if self.focus == 2 else 0)
        x += 13
        put(scr, y, x, f"‹ {self.provider} ›", th("reverse") if self.focus == 3 else 0)
        put(scr, y0 + 3, x0 + 2, pad(self.message, width - 4), th("dim"))
        cols = [Col("score", "Score", 5, ">"), Col("prov", "Source", 11), Col("type", "Type", 5),
                Col("title", "Title", None), Col("ids", "Plex id", 16, "<", 2), Col("info", "Info", 30, "<", 1)]

        def cell(key, m: Match):
            if key == "score":
                return f"{m.score:.2f}", th("green") if m.score >= 0.85 else th("yellow") if m.score >= 0.6 else th("dim")
            if key == "prov":
                return m.provider, th("cyan")
            if key == "type":
                return {"show": "TV", "movie": "Movie", "music": "Album"}[m.category], 0
            if key == "title":
                return m.label(), th("bold")
            if key == "ids":
                if m.category == "music":
                    return f"{m.tracks or '?'} tracks", 0
                tag = naming.id_tag(m.category, m.ids).strip()
                return tag or "-", th("dim") if not tag else 0
            if key == "info":
                return m.info, th("dim")
            return "", 0

        if self.results:
            app.draw_table(y0 + 4, height - 7, width - 2, cols, self.results, cell, self.state,
                           selected=self.focus == 4, x0=x0 + 1)
        put(scr, y0 + height - 2, x0 + 2,
            pad("Enter search/choose · Tab next field · ↓ results · ←→ type/source · i ids by hand · "
                "albums: “Artist - Album” · Esc cancel",
                width - 4), th("dim"))
        return cursor


# --------------------------------------------------------------------------
# Review screen
# --------------------------------------------------------------------------

class Review:
    def __init__(self, app: App, plan: Plan, source: engine.Source):
        self.app = app
        self.plan = plan
        self.source = source
        self.state = ListState()
        self.marked: set[int] = set()
        self.show_source = True
        self.rows: list[tuple] = []
        self.check: engine.Check | None = None
        self.recheck()

    # -- model -----------------------------------------------------------------

    def recheck(self, keep=None) -> None:
        self.check = engine.check_plan(self.plan, self.app.cfg)
        rows: list[tuple] = []
        for g in self.plan.groups():
            rows.append(("group", g))
            rows.extend(("item", it, g) for it in g.items)
        skipped = self.plan.skipped()
        if skipped:
            rows.append(("section", f"Skipped ({media_counts(skipped)}) - press Space to include"))
            rows.extend(("item", it, None) for it in skipped)
        if self.plan.leftovers:
            size = sum(s for _, s in self.plan.leftovers)
            rows.append(("section", f"Other files ({len(self.plan.leftovers)}, {fmt.human_size(size)}) - not moved"))
            rows.extend(("leftover", rel, size) for rel, size in self.plan.leftovers[:300])
        self.rows = rows
        if keep is not None:
            for i, r in enumerate(rows):
                if (r[0] == "item" and r[1] is keep) or (r[0] == "group" and isinstance(keep, tuple) and r[1].key == keep):
                    self.state.index = i
                    break

    def placement(self, g: Group):
        return next((p for p in (self.check.placements if self.check else []) if p.key == g.key), None)

    def current(self):
        if not self.rows:
            return None
        self.state.index = max(0, min(self.state.index, len(self.rows) - 1))
        return self.rows[self.state.index]

    def group_of(self, row) -> Group | None:
        if row is None:
            return None
        if row[0] == "group":
            return row[1]
        if row[0] == "item" and row[2] is not None:
            return row[2]
        return None

    # -- drawing --------------------------------------------------------------------

    def hints(self) -> str:
        return ("⏎/e edit  f find match  l location  ␣ skip/include  m mark  E edit marked  M auto-match  "
                "v source col  a APPLY  Esc back  ? help")

    def draw(self, y0: int, height: int, w: int) -> None:
        th = self.app.theme
        scr = self.app.scr
        plan, check = self.plan, self.check
        groups = plan.groups()
        put(scr, y0, 1, plan.name, th("bold"))
        tracks = sum(1 for g in groups for i in g.items if i.kind == "track")
        videos = sum(len(g.items) for g in groups) - tracks
        subs = sum(len(i.subs) for g in groups for i in g.items)
        unverified = sum(1 for g in groups if not g.verified)
        errors = len(check.errors) + len(check.global_errors)
        parts = [f"{len(groups)} title(s)"] + [x for x in (f"{videos} video(s)" if videos else "",
                                                             f"{tracks} track(s)" if tracks else "",
                                                             f"{subs} subtitle(s)" if subs else "") if x] + [
                 f"{len(plan.skipped())} skipped", f"{len(plan.leftovers)} other file(s)"]
        put(scr, y0 + 1, 1, " · ".join(parts), th("dim"))
        x = fmt.text_width(" · ".join(parts)) + 3
        if errors:
            text = f"✗ {errors} problem(s) to fix" + (f": {check.global_errors[0]}" if check.global_errors else "")
            put(scr, y0 + 1, x, text, th("red", "bold"))
            x += fmt.text_width(text) + 2
        if unverified:
            put(scr, y0 + 1, x, f"! {unverified} title(s) not verified (press f)", th("yellow", "bold"))
        elif not errors:
            put(scr, y0 + 1, x, "✓ ready - press a to sort", th("green", "bold"))

        top = y0 + 3
        body = height - 3
        self.state.clamp(len(self.rows), body)
        src_w = (w - 16) * 2 // 5 if self.show_source else 0
        dst_w = w - 16 - src_w - (1 if self.show_source else 0)
        put(scr, top - 1, 0, pad("  What", 12) + " " + (pad("Source file", src_w) + " " if self.show_source else "")
            + pad("→ Destination (inside the title folder)", dst_w) + " OK", th("bold", "underline"))
        for row_i in range(body):
            i = self.state.top + row_i
            if i >= len(self.rows):
                break
            row = self.rows[i]
            y = top + row_i
            sel = i == self.state.index
            rev = th("reverse") if sel else 0
            if sel:
                put(scr, y, 0, " " * w, rev)
            kind = row[0]
            if kind == "group":
                g: Group = row[1]
                lib = {"show": "TV", "movie": "Movie", "music": "Music"}[g.category]
                eps = sum(1 for it in g.items if it.kind == "episode" and it.season != 0)
                sp = sum(1 for it in g.items if it.kind == "episode" and it.season == 0)
                ex = sum(1 for it in g.items if it.kind == "extra")
                tr = sum(1 for it in g.items if it.kind == "track")
                art = sum(len(it.art) for it in g.items)
                counts = ", ".join(x for x in (f"{eps} episode(s)" if eps else "", f"{sp} special(s)" if sp else "",
                                               f"{ex} extra(s)" if ex else "",
                                               f"{sum(1 for it in g.items if it.kind == 'movie')} file(s)"
                                               if g.category == "movie" else "",
                                               f"{tr} track(s)" if tr else "", f"{art} artwork/log file(s)" if art else "")
                                   if x)
                folder = naming.display_folder(g.category, g.title, g.year, g.ids, g.artist,
                                               self.app.cfg.get("id_tags", True))
                status = (f"✓ {g.verified}", th("green", "bold")) if g.verified else ("? not verified - press f", th("yellow", "bold"))
                pl = self.placement(g)
                if pl and not pl.fits:
                    where = ("  ✗ no room on any drive", th("red", "bold"))
                elif pl and (len(pl.candidates) > 1 or pl.manual):
                    where = (f"  → {pl.root}", th("magenta"))
                else:
                    where = ("", 0)
                x = 0
                for text, attr in ((f"▾ {lib:5} ", th("cyan", "bold")), (folder, th("bold")), ("  ", 0), status,
                                   where, (f"  · {counts}", th("dim"))):
                    put(scr, y, x, text, attr | rev, width=w - x)
                    x += fmt.text_width(text)
            elif kind == "section":
                put(scr, y, 0, f"── {row[1]} ", th("dim", "bold") | rev)
            elif kind == "leftover":
                put(scr, y, 0, pad("    other", 12) + " " + row[1], th("dim") | rev, width=w)
            else:
                it: Item = row[1]
                mark = "●" if id(it) in self.marked else " "
                if it.kind == "episode":
                    what = naming.episode_code(it.season if it.season is not None else 1, it.episode or 0, it.episode_end)
                elif it.kind == "track":
                    what = (f"♪ {it.disc}-" if it.disc else "♪ ") + (f"{it.track:02d}" if it.track is not None else "--")
                elif it.kind == "extra":
                    what = f"extra"
                elif it.kind == "movie":
                    what = f"movie{f' pt{it.part}' if it.part else ''}"
                else:
                    what = "skip"
                x = 0
                put(scr, y, x, f" {mark}", th("yellow", "bold") | rev)
                put(scr, y, 2, pad(what, 10), (th("cyan") if it.kind == "episode" else th("dim") if it.kind == "skip"
                                               else th("green") if it.kind == "track" else th("magenta")) | rev)
                x = 13
                if self.show_source:
                    put(scr, y, x, pad(it.src, src_w), th("dim") | rev)
                    x += src_w + 1
                if it.kind == "skip":
                    dst_text, attr = f"(not moved){' - ' + it.note if it.note else ''}", th("dim")
                else:
                    dst = check.dests.get(id(it), "")
                    root = naming.title_root(dst, it.category, self.app.cfg) if dst else ""
                    dst_text = os.path.relpath(dst, root) if dst and root else dst
                    if it.subs:
                        dst_text += f"  +{len(it.subs)} sub ({','.join(s.lang or '?' for s in it.subs)})"
                    if it.art:
                        dst_text += f"  +{len(it.art)} artwork/log"
                    attr = 0
                put(scr, y, x, pad(dst_text, dst_w), attr | rev)
                if id(it) in check.errors:
                    flag, fattr = "✗", th("red", "bold")
                elif id(it) in check.warnings:
                    flag, fattr = "!", th("yellow", "bold")
                elif it.kind == "skip":
                    flag, fattr = "·", th("dim")
                else:
                    flag, fattr = "✓", th("green")
                put(scr, y, w - 2, flag, fattr | rev)
        # explain the selected row's problem on the last line
        row = self.current()
        if row and row[0] == "group" and (pl := self.placement(row[1])):
            c = pl.chosen()
            free = f" · {fmt.human_size(c.free)} free on that drive" if c else ""
            put(scr, y0 + height - 1, 0, pad(f" → {pl.root or '?'}  ({fmt.human_size(pl.need)}: {pl.reason}{free})"
                                             f"  · l to change", w),
                th("red") if not pl.fits else th("dim"))
        if row and row[0] == "item":
            it = row[1]
            msg = self.check.errors.get(id(it)) or self.check.warnings.get(id(it))
            info = f"{it.src}  ·  {fmt.human_size(it.size)}  ·  guessed from: {it.note or '-'}"
            if msg:
                put(scr, y0 + height - 1, 0, pad(f" {msg}  ·  {info}", w),
                    th("red") if id(it) in self.check.errors else th("yellow"))
            else:
                put(scr, y0 + height - 1, 0, pad(f" {info}", w), th("dim"))

    # -- keys ------------------------------------------------------------------------

    def handle(self, key) -> None:
        app = self.app
        if key == "?":
            return app.show_help()
        if key in ("ESC", "q"):
            app.review = None
            return
        if self.state.navigate(key, max(1, app.scr.getmaxyx()[0] - 8)):
            self.state.index = min(self.state.index, len(self.rows) - 1)
            return
        row = self.current()
        if key == "v":
            self.show_source = not self.show_source
        elif key in ("ENTER", "e") and row:
            if row[0] == "group":
                self.edit_group(row[1])
            elif row[0] == "item":
                self.edit_items([row[1]])
        elif key == "f":
            g = self.group_of(row)
            if g:
                self.find_match(g)
            else:
                app.flash("Put the cursor on a title or one of its files", "info")
        elif key == " " and row and row[0] in ("item", "group"):
            items = [row[1]] if row[0] == "item" else row[1].items
            for it in items:
                if it.kind == "skip":
                    it.kind = "track" if it.category == "music" else \
                        "episode" if it.category == "show" and it.episode is not None else \
                        "movie" if it.category == "movie" else "extra"
                    it.note = (it.note or "") + " (included by you)"
                else:
                    it.kind = "skip"
            # the file moves to its section, but the cursor stays on this row (now the next file)
            index = self.state.index
            self.recheck()
            self.state.index = min(index, len(self.rows) - 1)
        elif key == "m" and row and row[0] == "item":
            self.marked ^= {id(row[1])}
            self.state.navigate("DOWN", 1)
        elif key == "E":
            items = [it for it in self.plan.items if id(it) in self.marked]
            if not items:
                app.flash("Mark files with m first", "info")
            else:
                self.edit_items(items)
        elif key == "M":
            if app.offline:
                app.flash("Online lookups are off (Settings)", "info")
                return
            errors = app.run_task("Matching online", lambda log: engine.auto_match(
                self.plan, app.matcher, progress=lambda n, t, title: log(f"[{n}/{t}] {title}")))
            self.recheck()
            unverified = sum(1 for g in self.plan.groups() if not g.verified)
            app.flash(f"{unverified} title(s) still unverified" + (f" · {'; '.join(errors)[:100]}" if errors else ""),
                      "info" if unverified else "ok")
        elif key == "a":
            self.apply()
        elif key == "l":
            g = self.group_of(row)
            if g:
                self.choose_location(g)

    def choose_location(self, g: Group) -> None:
        pl = self.placement(g)
        if not pl or not pl.candidates:
            self.app.flash("No library folder configured for this type (Settings → folders)", "error")
            return
        options = [("0", "Automatic (by free space)", "bold")]
        for n, c in enumerate(pl.candidates[:9], start=1):
            notes = [f"{fmt.human_size(c.free)} free"]
            if c.existing:
                notes.append("already has this title")
            if c.cost == 0:
                notes.append("same drive as download")
            if not c.fits:
                notes.append("NO ROOM")
            options.append((str(n), f"{c.root}  ({', '.join(notes)})", "red" if not c.fits else ""))
        options.append(("Esc", "Cancel", "dim"))
        choice = choose(self.app, f"Where should “{pl.label}” go?",
                        [f"Needs {fmt.human_size(pl.need)} · now: {pl.root} ({pl.reason})"], options)
        if choice is None or choice == "Esc":
            return
        for it in g.items:
            if choice == "0":
                it.root, it.root_manual = "", False
            else:
                it.root, it.root_manual = pl.candidates[int(choice) - 1].root, True
        self.recheck(keep=g.items[0].group_key())

    def find_match(self, g: Group) -> None:
        app = self.app
        match = MatchDialog(app, g.title, g.year, g.category, g.anime, artist=g.artist).run()
        if not match:
            return
        if (match.category == "music") != (g.category == "music"):
            app.flash("That result is a different kind of title (music vs video)", "error")
            return
        if match.category == "music" and g.artist and g.artist != "Unknown Artist" \
                and similarity(g.artist, match.artist) < 0.5:
            if choose(app, "Different artist", [f"Your files say: {g.artist}", f"This release is by: {match.artist}",
                                                "", "The files will be filed under that artist."],
                      [("y", f"Use {match.artist} anyway", "red"), ("Esc", "Cancel", "dim")], default=1) != "y":
                return
        items = list(g.items)
        app.run_task("Applying match", lambda log: (log("Fetching episode list…"),
                                                     apply_match(items, match, app.matcher)))
        self.recheck(keep=items[0])
        app.flash(f"Matched “{match.label()}” via {match.provider}")

    def edit_group(self, g: Group) -> None:
        if g.category == "music":
            return self.edit_album(g)
        values = Form(f"Title: {g.title}", [
            Field("category", "Library", "choice", g.category, [("show", "TV Shows"), ("movie", "Movies")]),
            Field("title", "Title", "text", g.title),
            Field("year", "Year", "int", g.year),
            Field("tvdb", "TVDB id", "int", g.ids.get("tvdb")),
            Field("tmdb", "TMDB id", "int", g.ids.get("tmdb")),
            Field("imdb", "IMDb id", "text", g.ids.get("imdb", "")),
        ], ok="Save", note="Applies to every file of this title. Use f to search online instead.",
            validate=lambda v: None if v["title"] else "Title can't be empty").run(self.app)
        if not values:
            return
        ids = {k: values[k] for k in ("tvdb", "tmdb", "imdb") if values[k]}
        changed = (values["title"] != g.title or values["year"] != g.year or ids != {k: v for k, v in g.ids.items()
                                                                                  if k in ("tvdb", "tmdb", "imdb")})
        for it in g.items:
            it.title, it.year = values["title"], values["year"]
            if values["category"] != it.category:
                it.category = values["category"]
                if it.kind == "episode" and it.category == "movie":
                    it.kind = "movie"
                elif it.kind == "movie" and it.category == "show":
                    it.kind, it.season, it.episode = "episode", 1, it.part or 1
            if changed:
                it.ids = dict(ids)
                it.verified = "manual" if ids else ""
        self.recheck(keep=g.items[0].group_key() if g.items else None)

    def edit_album(self, g: Group) -> None:
        values = Form(f"Album: {g.artist} – {g.title}", [
            Field("artist", "Album artist", "text", g.artist, hint="'Various Artists' for compilations"),
            Field("title", "Album", "text", g.title),
            Field("year", "Year", "int", g.year),
            Field("mbid", "MusicBrainz release id", "text", g.ids.get("mbid", ""),
                  hint="optional; use f to search MusicBrainz instead"),
        ], ok="Save", note="Applies to every track of this album.",
            validate=lambda v: None if v["title"] and v["artist"] else "Artist and album are required").run(self.app)
        if not values:
            return
        if values["mbid"] and values["mbid"] != g.ids.get("mbid"):
            from .providers import Match
            match = Match("MusicBrainz", values["mbid"], "music", values["title"], values["year"],
                          {"mbid": values["mbid"]}, artist=values["artist"])
            items = list(g.items)
            self.app.run_task("Fetching tracklist", lambda log: apply_match(items, match, self.app.matcher))
            self.recheck(keep=items[0].group_key())
            return
        changed = (values["title"], values["artist"], values["year"]) != (g.title, g.artist, g.year)
        for it in g.items:
            it.title, it.artist, it.year = values["title"], values["artist"], values["year"]
            if changed:
                it.ids, it.verified = {}, ""
        self.recheck(keep=g.items[0].group_key() if g.items else None)

    def edit_tracks(self, items: list[Item]) -> None:
        one = items[0] if len(items) == 1 else None
        keep = "(unchanged)"

        def val(attr):
            return getattr(one, attr) if one else None
        fields = [
            Field("kind", "Kind", "choice", one.kind if one else keep,
                  ([] if one else [(keep, keep)]) + [("track", "Music track"), ("skip", "Skip (don't move)")]),
            Field("artist", "Album artist", "text", val("artist") or "", hint="" if one else "blank = unchanged"),
            Field("title", "Album", "text", val("title") or ""),
            Field("year", "Year", "int", val("year")),
            Field("disc", "Disc", "int", val("disc"), hint="blank for single-disc albums"),
            Field("track", "Track" if one else "Renumber from", "int", val("track"),
                  hint="" if one else "numbers the marked tracks in file order, starting here"),
            Field("track_title", "Track title", "text", val("track_title") or ""),
            Field("track_artist", "Track artist", "text", val("track_artist") or "",
                  hint="only if different from the album artist"),
        ]
        form = Form(f"Track: {os.path.basename(one.src)}" if one else f"Edit {len(items)} marked tracks", fields,
                    ok="Save", note=f"Source: {one.src}" if one else "Only fields you change are applied")
        values = form.run(self.app)
        if not values:
            return
        if one:
            changed = values
        else:
            try:
                changed = {k: v for k, v in form.changes().items() if v not in (keep, "", None)}
            except FormError as e:
                self.app.flash(str(e), "error")
                return
        before = {id(it): it.group_key() for it in items}
        for n, it in enumerate(sorted(items, key=lambda i: i.src.lower())):
            for k, v in changed.items():
                setattr(it, k, v + n if (k == "track" and not one and v is not None) else v)
            if it.group_key() != before[id(it)]:
                twin = next((o for o in self.plan.items if o is not it and o.group_key() == it.group_key()
                             and o.kind != "skip"), None)
                it.ids = dict(twin.ids) if twin else {}
                it.verified = twin.verified if twin else ""
            it.ep_found = None
        self.marked.clear()
        self.recheck(keep=items[0])

    def edit_items(self, items: list[Item]) -> None:
        if all(it.category == "music" for it in items):
            return self.edit_tracks(items)
        one = items[0] if len(items) == 1 else None
        keep = "(unchanged)"

        def val(attr):
            return getattr(one, attr) if one else None

        fields = [
            Field("kind", "Kind", "choice", one.kind if one else keep,
                  ([] if one else [(keep, keep)]) + [("episode", "TV episode"), ("movie", "Movie"),
                                                     ("extra", "Extra (bonus feature)"), ("skip", "Skip (don't move)")]),
            Field("category", "Library", "choice", one.category if one else keep,
                  ([] if one else [(keep, keep)]) + [("show", "TV Shows"), ("movie", "Movies")],
                  hint="for extras: which kind of title they belong to"),
            Field("title", "Title", "text", val("title") or "", hint="" if one else "blank = unchanged"),
            Field("year", "Year", "int", val("year")),
            Field("season", "Season", "int", val("season"), hint="0 = specials"),
            Field("episode", "Episode" if one else "Renumber from", "int", val("episode"),
                  hint="" if one else "numbers the marked files in order, starting here"),
            Field("episode_end", "Last episode", "int", val("episode_end"), hint="for double episodes, e.g. 1-2"),
            Field("ep_title", "Episode title", "text", val("ep_title") or ""),
            Field("part", "Movie part", "int", val("part"), hint="pt1/pt2 for split movies"),
            Field("extra_type", "Extra type", "choice", one.extra_type if one else keep,
                  ([] if one else [(keep, keep)]) + [(t, t) for t in EXTRA_TYPES]),
            Field("extra_name", "Extra name", "text", val("extra_name") or ""),
        ]
        title = f"File: {os.path.basename(one.src)}" if one else f"Edit {len(items)} marked files"
        form = Form(title, fields, ok="Save", note=f"Source: {one.src}" if one else "Only fields you change are applied")
        values = form.run(self.app)
        if not values:
            return
        if one:
            changed = {f.key: values[f.key] for f in fields}
        else:
            try:
                changed = form.changes()
            except FormError as e:
                self.app.flash(str(e), "error")
                return
            changed = {k: v for k, v in changed.items() if v not in (keep, "", None)}
        before = {id(it): it.group_key() for it in items}
        ordered = sorted(items, key=lambda i: i.src.lower())
        for n, it in enumerate(ordered):
            for k, v in changed.items():
                if k == "episode" and not one:
                    it.episode = v + n
                    it.absolute = False
                elif k in ("season",):
                    it.season = v
                    it.absolute = False
                else:
                    setattr(it, k, v)
            if it.kind == "episode":
                it.category = "show"
                if it.season is None:
                    it.season = 1
            elif it.kind == "movie":
                it.category = "movie"
            if it.group_key() != before[id(it)]:
                # moved to another title: adopt that title's verified ids if it exists
                twin = next((o for o in self.plan.items if o is not it and o.group_key() == it.group_key()
                             and o.kind != "skip"), None)
                it.ids = dict(twin.ids) if twin else {}
                it.verified = twin.verified if twin else ""
            if "episode" in changed or "season" in changed:
                it.ep_found = None
        self.marked.clear()
        self.recheck(keep=items[0])

    def apply(self) -> None:
        app = self.app
        check = engine.check_plan(self.plan, app.cfg)
        self.check = check
        if not check.ok:
            app.flash(f"Fix the {len(check.errors) + len(check.global_errors)} problem(s) marked ✗ first", "error")
            return
        groups = self.plan.groups()
        unverified = [g for g in groups if not g.verified]
        n_videos = sum(len(g.items) for g in groups)
        n_subs = sum(len(i.subs) for g in groups for i in g.items)
        shows = sum(1 for g in groups if g.category == "show")
        albums = sum(1 for g in groups if g.category == "music")
        movies = len(groups) - shows - albums
        junk = len(self.plan.leftovers) + len(self.plan.skipped())
        junk_size = sum(s for _, s in self.plan.leftovers) + sum(i.size for i in self.plan.skipped())
        skipped_media = [i for i in self.plan.skipped() if i.note != "sample"]
        fields = []
        if self.plan.torrent and app.client:
            fields.append(Field("remove", "Remove torrent from Transmission", "bool", app.cfg["remove_torrent"],
                                hint="the data is moved, so seeding stops either way"))
        if junk and os.path.isdir(self.plan.root):
            fields.append(Field("leftovers", f"Delete {junk} leftover file(s) ({fmt.human_size(junk_size)})", "bool",
                                app.cfg["delete_leftovers"] and not skipped_media,
                                hint=(f"includes skipped {media_counts(skipped_media).upper()} - check before enabling"
                                      if skipped_media
                                      else ".txt/.nfo/samples etc. left in the torrent folder")))
        intro = []
        by_root: dict[str, list] = {}
        for pl in check.placements:
            by_root.setdefault(pl.root, []).append(pl)
        for root, pls in by_root.items():
            c = pls[0].chosen()
            used = sum(cand.cost for p in pls if (cand := p.chosen()))
            free_after = (c.free - used) if c else 0
            intro.append((f"→ {root}", "bold"))
            intro.append((f"    {len(pls)} title(s), {fmt.human_size(sum(p.need for p in pls))}"
                          + (f" · {fmt.human_size(free_after)} free afterwards" if used else
                             " · same drive, no extra space used"), "dim"))
        note = f"{shows} show(s), {movies} movie(s), {albums} album(s) · {n_videos} file(s) + {n_subs} subtitle(s)"
        if unverified:
            note = f"⚠ {len(unverified)} title(s) NOT verified online. " + note
        if not fields:
            fields.append(Field("confirm", "Move the files now", "bool", True))
        values = Form("Sort into Plex library", fields, ok="Sort now", note=note, danger=bool(unverified),
                      intro=intro).run(app)
        if not values:
            return
        log_lines: list[str] = []

        def work(log):
            def both(msg):
                log(msg)
                log_lines.append(msg)
            return engine.apply_plan(self.plan, app.cfg, app.db, app.client,
                                     remove_torrent=values.get("remove", False),
                                     delete_leftovers=values.get("leftovers", False), log=both)
        result = app.run_task(f"Sorting {self.plan.name[:60]}", work)
        app.plans.pop(self.source.path, None)
        app.review = None
        app.refresh_all()
        if result.errors:
            choose(app, "Finished with problems", [f"Moved {result.moved} file(s) (operation #{result.op_id})", ""]
                   + result.errors[:12] + ["", "Undo from the History tab if needed."], [("Esc", "Close", "dim")],
                   danger=True)
        else:
            app.tab = 1
            app.flash(f"Sorted {result.moved} file(s) into the library"
                      + (" · torrent removed" if result.torrent_removed else "")
                      + (f" · {result.leftovers_deleted} leftover(s) deleted" if result.leftovers_deleted else ""))


def run(cfg: dict, db: DB, client, settings=None, start_source: str | None = None, offline: bool = False,
        setup_only: bool = False) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")

    def main(scr):
        app = App(scr, cfg, db, client, offline)
        app.setup_only = setup_only
        return app.run(start_source)
    return curses.wrapper(main)
