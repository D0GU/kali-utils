"""Interactive full-screen interface for trctl, built on curses (ncurses).

Layout: a header with global speeds, a filterable/sortable torrent list, and
a details view with Info / Files / Trackers / Peers tabs. Everything the CLI
can do (add, remove, start/stop, move, rename, file selection, priorities,
per-torrent and daemon settings, queue order, trackers) is available from
here via single-key commands and small dialog forms.

Data is fetched by a background thread so the UI never blocks on the daemon;
actions run on a separate RPC client in the UI thread.
"""

from __future__ import annotations

import curses
import dataclasses
import fnmatch
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from kutil import fmt
from kutil.tui import FAST_STEP, Col, Field, Form, FormError, LineEdit, ListState, Screen, Theme, choose, pad, put, \
    read_key, set_cursor, set_locale

from .cli import (INFO_FIELDS, LIST_FIELDS, SORT_KEYS, STATUS_FILTERS, CLIError, add_source,
                  info_fields, is_complete, status_label)
from .rpc import Client, RPCError

CONFIG_FIELDS = ["id", "name", "downloadLimit", "downloadLimited", "uploadLimit", "uploadLimited",
                 "honorsSessionLimits", "bandwidthPriority", "seedRatioMode", "seedRatioLimit",
                 "seedIdleMode", "seedIdleLimit", "peer-limit", "labels", "downloadDir"]
DETAIL_FIELDS = sorted(set(INFO_FIELDS + CONFIG_FIELDS + ["peers", "trackerList"]))

FILTERS = ["all", "downloading", "seeding", "active", "incomplete", "complete", "stopped",
           "queued", "checking", "error"]
SORTS = ["queue", "name", "progress", "size", "eta", "down", "up", "ratio", "added", "status", "id"]
TABS = ["Info", "Files", "Trackers", "Peers"]
PRIORITIES = {-1: "low", 0: "normal", 1: "high"}

HELP = """\
TORRENT LIST
  ↑ ↓  j k  PgUp PgDn  g G    move the cursor     Shift+↑↓ / K J  move 5 rows
  Enter  →  l                 open details          f   open the Files tab
  Space                       mark / unmark         Ctrl-A  mark all shown
  Tab  Shift-Tab              cycle status filter   /   search names (globs ok)
  o  O                        sort column / reverse order
  Esc                         clear marks, then the search

ACTIONS  (apply to marked torrents, otherwise the one under the cursor)
  a   add a torrent (magnet, URL, info hash, or .torrent file; Tab completes paths)
  p   start / stop                 v   verify data         R   reannounce
  d   remove (choose: keep data or DELETE data)
  m   move data to another folder  n   rename the torrent
  c   torrent settings: speed limits, priority, seeding goals, peers, labels
  [ ] move up / down the queue     { }  move to top / bottom of the queue
  P   sort a finished torrent into Plex (opens plexsort)

DAEMON
  t   toggle turtle (alternative speed) mode
  C   daemon settings: speed limits, download folder, queue, seeding, ...

DETAILS VIEW
  Tab  ← →  1-4               switch tab            Esc  q  Backspace   back
  Files:     Space  want / skip      + -  priority up / down
             w  want all shown       x  skip all shown       /  filter
             n  rename file          N  rename its folder
  Trackers:  a  add    e  edit    d  remove
  The torrent actions above (p v R d m n c) also work here.

IN DIALOGS
  ↑ ↓ Tab  move between fields     Enter  confirm         Esc  cancel
  Space ← →  toggle / change choice  Tab  completes paths in folder/file fields
  Ctrl-U  clear field   Ctrl-W  delete word   Ctrl-A / Ctrl-E  start / end

  ?  this help        q  quit"""


# --------------------------------------------------------------------------
# Background polling
# --------------------------------------------------------------------------

@dataclass
class Snapshot:
    torrents: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    session: dict = field(default_factory=dict)
    free: int | None = None
    detail: dict | None = None
    error: str | None = None
    loaded: bool = False


class Poller(threading.Thread):
    def __init__(self, client: Client, interval: float):
        super().__init__(daemon=True)
        self.client, self.interval = client, interval
        self.snap = Snapshot()
        self.detail_id: int | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._session_due = 0.0

    def refresh(self, session: bool = False) -> None:
        if session:
            self._session_due = 0.0
        self._wake.set()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            self.poll_once()
            self._wake.wait(self.interval)

    def poll_once(self) -> None:
        snap = self.snap
        try:
            torrents = self.client.torrent_get(LIST_FIELDS)
            stats = self.client.session_stats()
            session, free = snap.session, snap.free
            if time.monotonic() >= self._session_due:
                session = self.client.session_get()
                try:
                    free = self.client.free_space(session["download-dir"])["size-bytes"]
                except (RPCError, KeyError):
                    free = None
                self._session_due = time.monotonic() + 15
            detail = None
            did = self.detail_id
            if did is not None:
                found = self.client.torrent_get(DETAIL_FIELDS, [did])
                detail = found[0] if found else {"id": did, "_gone": True}
            self.snap = Snapshot(torrents, stats, session, free, detail, None, True)
        except RPCError as e:
            self.snap = dataclasses.replace(snap, error=str(e))
        except Exception as e:  # never let the poller thread die silently
            self.snap = dataclasses.replace(snap, error=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------

TORRENT_COLS = [
    Col("mark", "", 1),
    Col("id", "ID", 4, ">"),
    Col("name", "Name", None),
    Col("progress", "Progress", 17, "<", 1),
    Col("size", "Size", 10, ">", 5),
    Col("eta", "ETA", 7, ">", 6),
    Col("down", "Down", 11, ">", 4),
    Col("up", "Up", 11, ">", 7),
    Col("ratio", "Ratio", 5, ">", 8),
    Col("status", "Status", 11, "<", 2),
]
FILE_COLS = [
    Col("get", "Get", 3), Col("prio", "Priority", 8, "<", 3), Col("done", "Done", 6, ">", 2),
    Col("size", "Size", 10, ">", 1), Col("path", "Path", None),
]
TRACKER_COLS = [
    Col("tier", "Tier", 4, ">"), Col("announce", "Announce URL", None),
    Col("seeds", "Seeds", 6, ">", 2), Col("leech", "Leech", 6, ">", 3),
    Col("result", "Last announce", 24, "<", 1),
]
PEER_COLS = [
    Col("address", "Address", 40, "<", 5), Col("client", "Client", None),
    Col("progress", "Has", 6, ">", 2), Col("down", "Down", 11, ">"), Col("up", "Up", 11, ">", 1),
    Col("flags", "Flags", 8, "<", 3),
]


class App(Screen):
    def __init__(self, scr, client: Client, poll_client: Client, interval: float, mouse: bool = True):
        self.scr = scr
        self.client = client
        self.poller = Poller(poll_client, interval)
        self.theme = Theme()
        self.view = "list"
        self.filter = "all"
        self.sort = "queue"
        self.reverse = False
        self.search = ""
        self.marked: set[int] = set()
        self.list = ListState()
        self.selected_id: int | None = None
        self.tab = 0
        self.tab_state = {i: ListState() for i in range(len(TABS))}
        self.files_filter = ""
        self.detail_cache: dict | None = None
        self.editing: tuple[str, LineEdit, str] | None = None  # (target, editor, previous)
        self.message: tuple[str, int, float] | None = None
        self.running = True
        self.body_y = 0
        self.mouse = mouse

    # -- data -----------------------------------------------------------------

    @property
    def snap(self) -> Snapshot:
        return self.poller.snap

    def visible(self) -> list[dict]:
        torrents = self.snap.torrents
        if self.filter != "all":
            torrents = [t for t in torrents if STATUS_FILTERS[self.filter](t)]
        if self.search:
            pat = self.search.lower()
            if any(c in pat for c in "*?["):
                torrents = [t for t in torrents if fnmatch.fnmatch(t["name"].lower(), pat)]
            else:
                torrents = [t for t in torrents if pat in t["name"].lower()]
        return sorted(torrents, key=SORT_KEYS[self.sort], reverse=self.reverse)

    def current(self) -> dict | None:
        if self.view == "detail":
            return next((t for t in self.snap.torrents if t["id"] == self.selected_id), None)
        items = self.visible()
        if not items:
            return None
        self._sync_cursor(items)
        return items[self.list.index]

    def targets(self) -> list[dict]:
        if self.view == "list" and self.marked:
            return [t for t in self.snap.torrents if t["id"] in self.marked]
        t = self.current()
        return [t] if t else []

    def _sync_cursor(self, items: list[dict]) -> None:
        ids = [t["id"] for t in items]
        if self.selected_id in ids:
            self.list.index = ids.index(self.selected_id)
        self.list.index = max(0, min(self.list.index, len(items) - 1))
        if items:
            self.selected_id = items[self.list.index]["id"]

    def flash(self, text: str, kind: str = "ok") -> None:
        attr = {"ok": self.theme("green"), "error": self.theme("alert"),
                "info": self.theme("cyan")}[kind]
        self.message = (text, attr, time.monotonic() + (8 if kind == "error" else 4))

    def rpc(self, what: str, func, *args, **kwargs):
        """Run an RPC action, reporting failures in the footer. Returns (ok, result)."""
        try:
            result = func(*args, **kwargs)
        except (RPCError, CLIError) as e:
            self.flash(f"{what} failed: {e}", "error")
            return False, None
        self.poller.refresh()
        return True, result

    # -- drawing --------------------------------------------------------------

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 50:
            put(scr, 0, 0, "Terminal too small for trctl - enlarge it (min 50×10).")
            return
        self.draw_header(w)
        if self.view == "detail":
            self.draw_detail(1, h - 2, w)
        else:
            self.draw_list(1, h - 2, w)
        self.draw_footer(h - 1, w)

    def draw_header(self, w: int) -> None:
        th, snap = self.theme, self.snap
        stats, session = snap.stats, snap.session
        put(self.scr, 0, 0, " " * w, th("bar"))
        parts = [" trctl ",
                 f"↓ {fmt.human_rate(stats.get('downloadSpeed'))}",
                 f"↑ {fmt.human_rate(stats.get('uploadSpeed'))}",
                 f"{stats.get('torrentCount', len(snap.torrents))} torrents, "
                 f"{stats.get('activeTorrentCount', 0)} active"]
        if snap.free is not None:
            parts.append(f"{fmt.human_size(snap.free)} free")
        limits = []
        if session.get("alt-speed-enabled"):
            limits.append(f"TURTLE ↓{session.get('alt-speed-down')} ↑{session.get('alt-speed-up')} KB/s")
        else:
            if session.get("speed-limit-down-enabled"):
                limits.append(f"limit ↓{session.get('speed-limit-down')} KB/s")
            if session.get("speed-limit-up-enabled"):
                limits.append(f"limit ↑{session.get('speed-limit-up')} KB/s")
        parts += limits
        left = " │ ".join(parts)
        put(self.scr, 0, 0, left, th("bar", "bold"))
        if snap.error:
            right = f" ⚠ {snap.error} "
            put(self.scr, 0, max(0, w - fmt.text_width(right)), right, th("alert"))
        elif session:
            right = f"Transmission {session.get('version', '?').split(' ')[0]} "
            if fmt.text_width(left) + fmt.text_width(right) + 2 <= w:
                put(self.scr, 0, w - fmt.text_width(right), right, th("bar"))

    def draw_footer(self, y: int, w: int) -> None:
        th = self.theme
        if self.message and self.message[2] > time.monotonic():
            put(self.scr, y, 0, pad(" " + self.message[0], w), self.message[1])
            return
        self.message = None
        if self.editing:
            hints = "type to filter · Enter keep · Esc clear"
        elif self.view == "list":
            hints = ("⏎ details  ␣ mark  a add  p start/stop  d remove  c torrent settings · C daemon settings  "
                     "m move  n rename  f files  / search  ⇥ filter  o sort  ? help  q quit")
        else:
            hints = {
                0: "⇥ tabs  p start/stop  v verify  c torrent settings · C daemon settings  m move  n rename  "
                   "d remove  Esc back  ? help",
                1: "␣ want/skip  + - priority  w/x want/skip shown  / filter  n rename  N rename folder  Esc back",
                2: "a add tracker  e edit  d remove tracker  R reannounce  ⇥ tabs  Esc back",
                3: "⇥ tabs  Esc back  ? help",
            }[self.tab]
        put(self.scr, y, 0, pad(" " + hints, w), th("dim"))

    def draw_list(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        items = self.visible()
        self._sync_cursor(items)
        total = len(self.snap.torrents)
        # filter bar
        x = 0
        segs = [(" Filter: ", th("dim")), (f"{self.filter.capitalize()} ({len(items)}/{total})", th("bold")),
                ("  Sort: ", th("dim")), (f"{self.sort} {'↓' if self.reverse else '↑'}", th("bold")),
                ("  Search: ", th("dim"))]
        for text, attr in segs:
            put(self.scr, y0, x, text, attr)
            x += fmt.text_width(text)
        if self.editing and self.editing[0] == "search":
            visible, cx = self.editing[1].render(30)
            put(self.scr, y0, x, pad(visible, 30), th("underline"))
            cursor = (y0, x + cx)
            x += 31
        else:
            cursor = None
            text = self.search or "-"
            put(self.scr, y0, x, text, th("yellow", "bold") if self.search else th("dim"))
            x += fmt.text_width(text) + 1
        if self.marked:
            put(self.scr, y0, x + 1, f" {len(self.marked)} marked ", th("yellow", "reverse"))

        self.body_y = y0 + 2
        if not self.snap.loaded:
            msg = f"Connecting… {self.snap.error or ''}"
            put(self.scr, y0 + 3, 2, msg, th("red") if self.snap.error else th("dim"))
        elif not items:
            put(self.scr, y0 + 3, 2, "No torrents match." if total else "No torrents yet - press a to add one.",
                th("dim"))
            put(self.scr, y0 + 1, 0, "")
        else:
            self.draw_table(y0 + 1, height - 1, w, TORRENT_COLS, items, self.torrent_cell, self.list)
            self.selected_id = items[self.list.index]["id"]
        if cursor:
            set_cursor(True)
            self.scr.move(*cursor)
        else:
            set_cursor(False)

    def torrent_cell(self, key: str, t: dict) -> tuple[str, int]:
        th = self.theme
        if key == "mark":
            return ("●" if t["id"] in self.marked else " ", th("yellow", "bold"))
        if key == "id":
            return str(t["id"]), th("dim")
        if key == "name":
            return t["name"], th("bold") if t["id"] in self.marked else 0
        if key == "progress":
            meta = t.get("metadataPercentComplete", 1)
            done = t["percentDone"] if meta >= 1 else meta
            attr = th("green") if is_complete(t) else th("cyan") if t["status"] == 4 else th("dim")
            return f"{fmt.bar(done, 10)} {fmt.percent(done):>6}", attr
        if key == "size":
            return fmt.human_size(t["sizeWhenDone"]), 0
        if key == "eta":
            return ("-" if is_complete(t) else fmt.human_eta(t["eta"])), 0
        if key == "down":
            return fmt.human_rate(t["rateDownload"]), th("cyan") if t["rateDownload"] else th("dim")
        if key == "up":
            return fmt.human_rate(t["rateUpload"]), th("green") if t["rateUpload"] else th("dim")
        if key == "ratio":
            return fmt.human_ratio(t["uploadRatio"]), 0
        if key == "status":
            label, *styles = status_label(t)
            return label, th(*styles)
        return "", 0

    # -- details view -----------------------------------------------------------

    def detail(self) -> dict | None:
        d = self.snap.detail
        if d and d.get("id") == self.selected_id:
            return d
        c = self.detail_cache
        return c if c and c.get("id") == self.selected_id else None

    def file_items(self, d: dict) -> list[dict]:
        prefix = d["name"] + "/"
        items = []
        for i, (f, s) in enumerate(zip(d.get("files", []), d.get("fileStats", []))):
            rel = f["name"][len(prefix):] if f["name"].startswith(prefix) else f["name"]
            items.append({"index": i, "path": rel, "full": f["name"], "length": f["length"],
                          "done": s["bytesCompleted"], "wanted": s["wanted"], "priority": s["priority"]})
        if self.files_filter:
            pat = self.files_filter.lower()
            pat = pat if any(c in pat for c in "*?[") else f"*{pat}*"
            items = [it for it in items if fnmatch.fnmatch(it["path"].lower(), pat)]
        return sorted(items, key=lambda it: it["path"].lower())

    def draw_detail(self, y0: int, height: int, w: int) -> None:
        th = self.theme
        d = self.detail()
        t = self.current()
        name = (d or t or {}).get("name", "…")
        put(self.scr, y0, 1, name, th("bold"))
        if t:
            label, *styles = status_label(t)
            info = f"  {label}  {fmt.percent(t['percentDone'])}  ↓ {fmt.human_rate(t['rateDownload'])}  ↑ {fmt.human_rate(t['rateUpload'])}"
            put(self.scr, y0, min(w - 1, fmt.text_width(name) + 2), info, th(*styles))
        x = 1
        for i, tab in enumerate(TABS):
            text = f" {i + 1} {tab} "
            put(self.scr, y0 + 1, x, text, th("reverse", "bold") if i == self.tab else th("dim"))
            x += fmt.text_width(text) + 1
        cursor = None
        if self.tab == 1 and (self.editing or self.files_filter):
            put(self.scr, y0 + 1, x + 2, "filter: ", th("dim"))
            if self.editing:
                visible, cx = self.editing[1].render(30)
                put(self.scr, y0 + 1, x + 10, pad(visible, 30), th("underline"))
                cursor = (y0 + 1, x + 10 + cx)
            else:
                put(self.scr, y0 + 1, x + 10, self.files_filter, th("yellow", "bold"))
        top = y0 + 3
        body_h = height - 3
        self.body_y = top + 1
        if d is None:
            put(self.scr, top, 2, "Loading…", th("dim"))
        elif self.tab == 0:
            self.draw_info(d, top, body_h, w)
        elif self.tab == 1:
            items = self.file_items(d)
            if not d.get("files"):
                put(self.scr, top, 2, "No file list yet (magnet link still fetching metadata).", th("dim"))
            else:
                self.draw_table(top, body_h, w, FILE_COLS, items, self.file_cell, self.tab_state[1])
                wanted = [f for f, s in zip(d["files"], d["fileStats"]) if s["wanted"]]
                summary = (f" {len(d['files'])} files · {len(wanted)} wanted · "
                           f"{fmt.human_size(sum(f['length'] for f in wanted))} selected ")
                put(self.scr, y0 + height - 1, 0, summary, th("dim"))
        elif self.tab == 2:
            trackers = sorted(d.get("trackerStats", []), key=lambda tr: (tr.get("tier", 0), tr.get("id", 0)))
            if not trackers:
                put(self.scr, top, 2, "No trackers. Press a to add one.", th("dim"))
            else:
                self.draw_table(top, body_h, w, TRACKER_COLS, trackers, self.tracker_cell, self.tab_state[2])
        elif self.tab == 3:
            peers = sorted(d.get("peers", []), key=lambda p: -(p.get("rateToClient", 0) + p.get("rateToPeer", 0)))
            if not peers:
                put(self.scr, top, 2, "No connected peers.", th("dim"))
            else:
                self.draw_table(top, body_h, w, PEER_COLS, peers, self.peer_cell, self.tab_state[3])
        if cursor:
            set_cursor(True)
            self.scr.move(*cursor)
        else:
            set_cursor(False)

    def info_lines(self, d: dict) -> list[tuple]:
        lines = info_fields(d)
        lim = lambda on, v: f"{v} KB/s" if on else "unlimited"
        ratio = {0: "global setting", 1: f"stop at ratio {d.get('seedRatioLimit', 0):g}", 2: "seed forever"}
        idle = {0: "global setting", 1: f"stop after {d.get('seedIdleLimit', 0)} min idle", 2: "never stop"}
        lines += [
            ("", ""),
            ("Speed limits", f"↓ {lim(d.get('downloadLimited'), d.get('downloadLimit'))}   "
                             f"↑ {lim(d.get('uploadLimited'), d.get('uploadLimit'))}   "
                             f"global limits {'apply' if d.get('honorsSessionLimits', True) else 'ignored'}"),
            ("Priority", PRIORITIES.get(d.get("bandwidthPriority", 0), "?")),
            ("Seeding", f"{ratio.get(d.get('seedRatioMode', 0), '?')}; idle: {idle.get(d.get('seedIdleMode', 0), '?')}"),
            ("Peer limit", str(d.get("peer-limit", "?"))),
            ("Magnet", d.get("magnetLink", "")),
        ]
        return lines

    def draw_info(self, d: dict, top: int, height: int, w: int) -> None:
        th = self.theme
        lines = self.info_lines(d)
        state = self.tab_state[0]
        state.top = max(0, min(state.top, len(lines) - height))
        kw = max(len(l[0]) for l in lines) + 2
        for row, (key, value, *styles) in enumerate(lines[state.top:state.top + height]):
            put(self.scr, top + row, 1, (key + ":" if key else "").ljust(kw), th("dim"))
            put(self.scr, top + row, 1 + kw, value, th(*styles), width=w - kw - 2)

    def file_cell(self, key: str, f: dict) -> tuple[str, int]:
        th = self.theme
        dim = 0 if f["wanted"] else th("dim")
        if key == "get":
            return (" ✓ ", th("green")) if f["wanted"] else (" · ", th("dim"))
        if key == "prio":
            p = PRIORITIES.get(f["priority"], "?")
            return p, {"high": th("yellow", "bold"), "low": th("dim")}.get(p, 0) | dim
        if key == "done":
            frac = f["done"] / f["length"] if f["length"] else 1
            return fmt.percent(frac), th("green") if frac >= 1 else dim
        if key == "size":
            return fmt.human_size(f["length"]), dim
        if key == "path":
            return f["path"], dim
        return "", 0

    def tracker_cell(self, key: str, tr: dict) -> tuple[str, int]:
        if key == "tier":
            return str(tr.get("tier", 0)), self.theme("dim")
        if key == "announce":
            return tr.get("announce", tr.get("host", "?")), 0
        if key in ("seeds", "leech"):
            n = tr.get("seederCount" if key == "seeds" else "leecherCount", -1)
            return (str(n) if n >= 0 else "?"), 0
        if key == "result":
            ok = tr.get("lastAnnounceSucceeded", True)
            return tr.get("lastAnnounceResult") or "-", 0 if ok else self.theme("red")
        return "", 0

    def peer_cell(self, key: str, p: dict) -> tuple[str, int]:
        th = self.theme
        if key == "address":
            return p.get("address", "?"), 0
        if key == "client":
            return p.get("clientName", "") or "?", 0
        if key == "progress":
            return fmt.percent(p.get("progress", 0)), 0
        if key == "down":
            r = p.get("rateToClient", 0)
            return fmt.human_rate(r), th("cyan") if r else th("dim")
        if key == "up":
            r = p.get("rateToPeer", 0)
            return fmt.human_rate(r), th("green") if r else th("dim")
        if key == "flags":
            return p.get("flagStr", ""), th("dim")
        return "", 0

    # -- main loop ----------------------------------------------------------------

    def run(self) -> int:
        set_cursor(False)
        self.scr.keypad(True)
        self.scr.timeout(250)
        if self.mouse:
            mask = (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED
                    | getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0))
            curses.mousemask(mask)
            curses.mouseinterval(250)
        self.poller.start()
        try:
            while self.running:
                self.draw()
                self.scr.refresh()
                key = read_key(self.scr)
                if key is not None:
                    self.handle(key)
        except KeyboardInterrupt:
            pass
        finally:
            self.poller.stop()
        return 0

    def page(self) -> int:
        return max(1, self.scr.getmaxyx()[0] - 6)

    def handle(self, key) -> None:
        if key == "RESIZE":
            return
        if key == "MOUSE":
            self.handle_mouse()
            return
        if self.editing:
            self.handle_edit(key)
            return
        if key == "?":
            self.show_help()
        elif key == "C-c":
            self.running = False
        elif key == "t":
            self.toggle_turtle()
        elif key == "C":
            self.session_settings()
        elif self.view == "list":
            self.handle_list(key)
        else:
            self.handle_detail(key)

    def handle_edit(self, key) -> None:
        target, editor, previous = self.editing
        if key == "ENTER":
            self.editing = None
        elif key == "ESC":
            self.editing = None
            value = ""
            if target == "search":
                self.search = value
            else:
                self.files_filter = value
            return
        else:
            editor.handle(key)
        if target == "search":
            self.search = editor.text
        else:
            self.files_filter = editor.text
            self.tab_state[1].index = 0

    def handle_mouse(self) -> None:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        state = self.list if self.view == "list" else self.tab_state[self.tab]
        if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
            state.index -= 3
        elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            state.index += 3
        elif bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            row = my - self.body_y
            if row >= 0:
                state.index = state.top + row
                if self.view == "list" and bstate & curses.BUTTON1_DOUBLE_CLICKED:
                    self.open_detail(0)
        state.index = max(0, state.index)
        if self.view == "list":
            items = self.visible()
            if items:
                self.list.index = min(self.list.index, len(items) - 1)
                self.selected_id = items[self.list.index]["id"]

    def handle_list(self, key) -> None:
        items = self.visible()
        if self.list.navigate(key, self.page()):
            if items:
                self.list.index = min(self.list.index, len(items) - 1)
                self.selected_id = items[self.list.index]["id"]
            return
        if key == "q":
            self.running = False
        elif key in ("ENTER", "RIGHT", "l"):
            self.open_detail(0)
        elif key == "f":
            self.open_detail(1)
        elif key == " ":
            t = self.current()
            if t:
                self.marked ^= {t["id"]}
                self.list.navigate("DOWN", 1)
                if items:
                    self.list.index = min(self.list.index, len(items) - 1)
                    self.selected_id = items[self.list.index]["id"]
        elif key == "C-a":
            ids = {t["id"] for t in items}
            self.marked = set() if ids <= self.marked else self.marked | ids
        elif key == "ESC":
            if self.marked:
                self.marked.clear()
            else:
                self.search = ""
        elif key in ("TAB", "BTAB"):
            step = 1 if key == "TAB" else -1
            self.filter = FILTERS[(FILTERS.index(self.filter) + step) % len(FILTERS)]
        elif key == "o":
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        elif key == "O":
            self.reverse = not self.reverse
        elif key == "/":
            self.editing = ("search", LineEdit(self.search), self.search)
        elif key == "a":
            self.add_torrent()
        elif key == "P":
            self.send_to_plex()
        else:
            self.torrent_action(key)

    def send_to_plex(self) -> None:
        """Open the selected finished torrent in plexsort's review screen."""
        t = self.current()
        if not t:
            return
        if not is_complete(t):
            self.flash("Only finished torrents can be sorted into Plex", "info")
            return
        exe = shutil.which("plexsort")
        if not exe:
            self.flash("plexsort isn't on your PATH (run kali-utils' install.sh)", "error")
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call([exe, "ui", t["hashString"]])
        finally:
            curses.reset_prog_mode()
            self.scr.refresh()
            self.poller.refresh()

    def handle_detail(self, key) -> None:
        if key == "ESC" and self.tab == 1 and self.files_filter:
            self.files_filter = ""
            return
        if key in ("ESC", "q", "BACKSPACE"):
            self.close_detail()
            return
        if key in ("TAB", "RIGHT"):
            self.tab = (self.tab + 1) % len(TABS)
            return
        if key in ("BTAB", "LEFT"):
            self.tab = (self.tab - 1) % len(TABS)
            return
        if key in ("1", "2", "3", "4"):
            self.tab = int(key) - 1
            return
        d = self.detail()
        if d is None:
            return  # still loading; don't let keys fall through to the wrong action
        if d.get("_gone"):
            self.close_detail()
            self.flash("Torrent no longer exists", "info")
            return
        state = self.tab_state[self.tab]
        if self.tab == 0 and key in ("UP", "k", "DOWN", "j", "PGUP", "PGDN", "HOME", "g", "END", "G",
                                     "S-UP", "S-DOWN", "K", "J"):
            delta = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PGUP": -self.page(), "PGDN": self.page(),
                     "S-UP": -FAST_STEP, "K": -FAST_STEP, "S-DOWN": FAST_STEP, "J": FAST_STEP,
                     "HOME": -10 ** 6, "g": -10 ** 6, "END": 10 ** 6, "G": 10 ** 6}[key]
            state.top = max(0, state.top + delta)
            return
        if self.tab != 0 and state.navigate(key, self.page()):
            return
        if self.tab == 1 and self.handle_files(key, d):
            return
        if self.tab == 2 and self.handle_trackers(key, d):
            return
        if key == "a":
            self.add_torrent()
        else:
            self.torrent_action(key)

    def open_detail(self, tab: int) -> None:
        t = self.current()
        if not t:
            return
        self.selected_id = t["id"]
        self.view = "detail"
        self.tab = tab
        self.files_filter = ""
        self.tab_state = {i: ListState() for i in range(len(TABS))}
        self.poller.detail_id = t["id"]
        try:  # load synchronously so the view is usable immediately
            found = self.client.torrent_get(DETAIL_FIELDS, [t["id"]])
            self.detail_cache = found[0] if found else None
        except RPCError:
            self.detail_cache = None
        self.poller.refresh()

    def close_detail(self) -> None:
        self.view = "list"
        self.editing = None
        self.poller.detail_id = None

    def show_help(self) -> None:
        self.show_text(HELP)

    # -- torrent actions ------------------------------------------------------------

    def torrent_action(self, key) -> None:
        actions = {"p": self.start_stop, "v": self.verify, "R": self.reannounce,
                   "d": self.remove, "DELETE": self.remove, "m": self.move, "n": self.rename,
                   "c": self.configure, "[": lambda: self.queue("up"), "]": lambda: self.queue("down"),
                   "{": lambda: self.queue("top"), "}": lambda: self.queue("bottom")}
        if key in actions:
            if not self.targets():
                self.flash("No torrent selected", "info")
                return
            actions[key]()

    @staticmethod
    def plural(items: list, word: str = "torrent") -> str:
        return f"{len(items)} {word}{'s' if len(items) != 1 else ''}"

    def describe(self, ts: list[dict]) -> str:
        return f"“{ts[0]['name']}”" if len(ts) == 1 else self.plural(ts)

    def start_stop(self) -> None:
        ts = self.targets()
        first = self.current() if self.current() in ts else ts[0]
        ids = [t["id"] for t in ts]
        if first["status"] == 0:
            ok, _ = self.rpc("Start", self.client.torrent_action, "start", ids)
            if ok:
                self.flash(f"Started {self.describe(ts)}")
        else:
            ok, _ = self.rpc("Stop", self.client.torrent_action, "stop", ids)
            if ok:
                self.flash(f"Stopped {self.describe(ts)}")

    def verify(self) -> None:
        ts = self.targets()
        ok, _ = self.rpc("Verify", self.client.torrent_action, "verify", [t["id"] for t in ts])
        if ok:
            self.flash(f"Verifying {self.describe(ts)}")

    def reannounce(self) -> None:
        ts = self.targets()
        ok, _ = self.rpc("Reannounce", self.client.torrent_action, "reannounce", [t["id"] for t in ts])
        if ok:
            self.flash(f"Asked trackers for more peers for {self.describe(ts)}")

    def queue(self, where: str) -> None:
        ts = self.targets()
        ok, _ = self.rpc("Queue move", self.client.queue_move, where, [t["id"] for t in ts])
        if ok:
            hint = "" if self.sort == "queue" else " (sort by queue with o to see it)"
            self.flash(f"Moved {self.describe(ts)} {where} in the queue{hint}")

    def remove(self) -> None:
        ts = self.targets()
        lines = [f"Remove {self.plural(ts)} from Transmission?", ""]
        lines += [f"  • {t['name']}" for t in ts[:8]]
        if len(ts) > 8:
            lines.append(f"  … and {len(ts) - 8} more")
        size = sum(t["sizeWhenDone"] - t["leftUntilDone"] for t in ts)
        options = [("k", "Remove, keep downloaded files", "bold"),
                   ("D", f"Remove AND DELETE {fmt.human_size(size)} of data from disk", "red"),
                   ("Esc", "Cancel", "dim")]
        choice = choose(self, "Remove torrents", lines, options, default=2, danger=True)
        if choice not in ("k", "D"):
            return
        ids = [t["id"] for t in ts]
        ok, _ = self.rpc("Remove", self.client.torrent_remove, ids, delete_data=choice == "D")
        if ok:
            self.marked -= set(ids)
            if self.view == "detail":
                self.close_detail()
            self.flash(f"Removed {self.describe(ts)}" + (" and deleted its data" if choice == "D" else ""))

    def move(self) -> None:
        ts = self.targets()
        dirs = {t["downloadDir"] for t in ts}
        form = Form(f"Move {self.describe(ts)}", [
            Field("dest", "Destination folder", "path", dirs.pop() if len(dirs) == 1 else "",
                  hint="absolute path on the daemon's machine · Tab completes"),
            Field("move", "Move the files", "bool", True,
                  hint="off = files are already there, just update the location"),
        ], ok="Move", validate=lambda v: None if v["dest"].startswith(("/", "~"))
                     else "Destination must be an absolute path")
        values = form.run(self)
        if values:
            dest = os.path.expanduser(values["dest"])
            ok, _ = self.rpc("Move", self.client.set_location, [t["id"] for t in ts], dest, values["move"])
            if ok:
                self.flash(f"{'Moving' if values['move'] else 'Relocated'} {self.describe(ts)} → {dest}")

    def rename(self) -> None:
        ts = self.targets()
        if len(ts) != 1:
            self.flash("Rename works on a single torrent - clear marks with Esc", "info")
            return
        t = ts[0]
        form = Form("Rename torrent", [Field("name", "New name", "text", t["name"],
                                             hint="renames the top-level file/folder on disk too")],
                    ok="Rename", validate=lambda v: "Name can't be empty or contain '/'"
                    if not v["name"] or "/" in v["name"] else None)
        values = form.run(self)
        if values and values["name"] != t["name"]:
            ok, _ = self.rpc("Rename", self.client.rename_path, t["id"], t["name"], values["name"])
            if ok:
                self.flash(f"Renamed to “{values['name']}”")

    def configure(self) -> None:
        ts = self.targets()
        try:
            cfgs = self.client.torrent_get(CONFIG_FIELDS, [t["id"] for t in ts])
        except RPCError as e:
            self.flash(f"Could not load settings: {e}", "error")
            return
        if not cfgs:
            return
        c = cfgs[0]
        title = f"Settings: {c['name']}" if len(cfgs) == 1 else \
            f"Settings for {len(cfgs)} torrents (showing “{fmt.truncate(c['name'], 30)}”; only changed fields apply)"
        fields = [
            Field("down", "Download limit (KB/s)", "int", c["downloadLimit"] if c["downloadLimited"] else None,
                  hint="blank = unlimited"),
            Field("up", "Upload limit (KB/s)", "int", c["uploadLimit"] if c["uploadLimited"] else None,
                  hint="blank = unlimited"),
            Field("honors", "Obey global limits", "bool", c["honorsSessionLimits"],
                  hint="whether the daemon-wide speed limits also apply"),
            Field("prio", "Bandwidth priority", "choice", c["bandwidthPriority"],
                  [(-1, "low"), (0, "normal"), (1, "high")]),
            Field("ratio_mode", "Stop seeding", "choice", c["seedRatioMode"],
                  [(0, "use global setting"), (1, "at ratio below"), (2, "never (seed forever)")]),
            Field("ratio", "  ratio", "float", c["seedRatioLimit"], hint="used when 'Stop seeding' = at ratio"),
            Field("idle_mode", "Stop when idle", "choice", c["seedIdleMode"],
                  [(0, "use global setting"), (1, "after minutes below"), (2, "never")]),
            Field("idle", "  idle minutes", "int", c["seedIdleLimit"]),
            Field("peers", "Max peers", "int", c.get("peer-limit")),
            Field("labels", "Labels", "text", ", ".join(c.get("labels") or []), hint="comma-separated"),
        ]
        form = Form(title, fields, ok="Save")
        if form.run(self) is None:
            return
        try:
            changed = form.changes()
        except FormError as e:
            self.flash(str(e), "error")
            return
        args: dict = {}
        if "down" in changed:
            args["downloadLimited"] = changed["down"] is not None
            if changed["down"] is not None:
                args["downloadLimit"] = changed["down"]
        if "up" in changed:
            args["uploadLimited"] = changed["up"] is not None
            if changed["up"] is not None:
                args["uploadLimit"] = changed["up"]
        simple = {"honors": "honorsSessionLimits", "prio": "bandwidthPriority",
                  "ratio_mode": "seedRatioMode", "idle_mode": "seedIdleMode"}
        for k, rpc_key in simple.items():
            if k in changed:
                args[rpc_key] = changed[k]
        if changed.get("ratio") is not None:
            args["seedRatioLimit"] = changed["ratio"]
        if changed.get("idle") is not None:
            args["seedIdleLimit"] = changed["idle"]
        if changed.get("peers") is not None:
            args["peer-limit"] = changed["peers"]
        if "labels" in changed:
            args["labels"] = [l.strip() for l in changed["labels"].split(",") if l.strip()]
        if not args:
            self.flash("No changes", "info")
            return
        ok, _ = self.rpc("Save settings", self.client.torrent_set, [x["id"] for x in cfgs], **args)
        if ok:
            self.flash(f"Updated settings for {self.describe(cfgs)}")

    def add_torrent(self) -> None:
        session = self.snap.session
        form = Form("Add torrent", [
            Field("source", "Magnet / URL / file", "path", "",
                  hint="magnet link, http(s) URL, 40-char info hash, or local .torrent path (Tab completes)"),
            Field("dir", "Download to", "path", session.get("download-dir", ""),
                  hint="folder on the daemon's machine · Tab completes"),
            Field("paused", "Add paused", "bool", not session.get("start-added-torrents", True)),
            Field("labels", "Labels", "text", "", hint="optional, comma-separated"),
        ], ok="Add", validate=lambda v: None if v["source"] else "Enter a magnet link, URL, hash or file")
        values = form.run(self)
        if not values:
            return
        labels = [l.strip() for l in values["labels"].split(",") if l.strip()]
        ok, res = self.rpc("Add", add_source, self.client, values["source"],
                           download_dir=os.path.expanduser(values["dir"]) or None,
                           paused=values["paused"], labels=labels)
        if ok:
            status, t = res
            if status == "added":
                self.flash(f"Added “{t.get('name', values['source'])}”")
                if self.view == "list":
                    self.selected_id = t.get("id", self.selected_id)
            else:
                self.flash(f"Already in Transmission: “{t.get('name')}”", "info")

    def toggle_turtle(self) -> None:
        on = not self.snap.session.get("alt-speed-enabled", False)
        ok, _ = self.rpc("Turtle mode", self.client.session_set, {"alt-speed-enabled": on})
        if ok:
            self.poller.refresh(session=True)
            self.flash(f"Turtle mode {'ON' if on else 'off'}", "info")

    def session_settings(self) -> None:
        try:
            s = self.client.session_get()
        except RPCError as e:
            self.flash(f"Could not load daemon settings: {e}", "error")
            return
        fields = [
            Field("down", "Download limit (KB/s)", "int",
                  s.get("speed-limit-down") if s.get("speed-limit-down-enabled") else None, hint="blank = unlimited"),
            Field("up", "Upload limit (KB/s)", "int",
                  s.get("speed-limit-up") if s.get("speed-limit-up-enabled") else None, hint="blank = unlimited"),
            Field("turtle", "Turtle mode", "bool", s.get("alt-speed-enabled", False)),
            Field("alt_down", "  turtle download (KB/s)", "int", s.get("alt-speed-down")),
            Field("alt_up", "  turtle upload (KB/s)", "int", s.get("alt-speed-up")),
            Field("dir", "Default download folder", "path", s.get("download-dir", ""),
                  hint="must be writable by the daemon (AppArmor allows /mnt/data/torrents/**)"),
            Field("start", "Start added torrents", "bool", s.get("start-added-torrents", True)),
            Field("queue", "Max active downloads", "int",
                  s.get("download-queue-size") if s.get("download-queue-enabled") else None,
                  hint="blank = no download queue"),
            Field("ratio", "Stop seeding at ratio", "float",
                  s.get("seedRatioLimit") if s.get("seedRatioLimited") else None, hint="blank = seed forever"),
            Field("peers", "Global peer limit", "int", s.get("peer-limit-global")),
        ]
        form = Form(f"Daemon settings (Transmission {s.get('version', '?')})", fields, ok="Save")
        if form.run(self) is None:
            return
        try:
            changed = form.changes()
        except FormError as e:
            self.flash(str(e), "error")
            return
        args: dict = {}
        for key, enabled, value in (("down", "speed-limit-down-enabled", "speed-limit-down"),
                                    ("up", "speed-limit-up-enabled", "speed-limit-up"),
                                    ("queue", "download-queue-enabled", "download-queue-size"),
                                    ("ratio", "seedRatioLimited", "seedRatioLimit")):
            if key in changed:
                args[enabled] = changed[key] is not None
                if changed[key] is not None:
                    args[value] = changed[key]
        for key, rpc_key in (("turtle", "alt-speed-enabled"), ("start", "start-added-torrents"),
                             ("alt_down", "alt-speed-down"), ("alt_up", "alt-speed-up"),
                             ("peers", "peer-limit-global")):
            if key in changed and changed[key] is not None:
                args[rpc_key] = changed[key]
        if "dir" in changed and changed["dir"]:
            args["download-dir"] = os.path.expanduser(changed["dir"])
        if not args:
            self.flash("No changes", "info")
            return
        ok, _ = self.rpc("Save daemon settings", self.client.session_set, args)
        if ok:
            self.poller.refresh(session=True)
            self.flash("Daemon settings saved")

    # -- files tab ----------------------------------------------------------------

    def handle_files(self, key, d: dict) -> bool:
        items = self.file_items(d)
        state = self.tab_state[1]
        if key == "/":
            self.editing = ("files", LineEdit(self.files_filter), self.files_filter)
            return True
        if key == "ESC" and self.files_filter:
            self.files_filter = ""
            return True
        if not items:
            return key in (" ", "+", "=", "-", "w", "x", "n", "N")
        state.index = min(state.index, len(items) - 1)
        f = items[state.index]
        tid = [d["id"]]
        if key == " ":
            want = not f["wanted"]
            ok, _ = self.rpc("Change file", self.client.torrent_set, tid,
                             **{"files-wanted" if want else "files-unwanted": [f["index"]]})
            if ok:
                d["fileStats"][f["index"]]["wanted"] = want  # optimistic, until the next poll
                state.index = min(state.index + 1, len(items) - 1)
            return True
        if key in ("+", "=", "-"):
            new = max(-1, min(1, f["priority"] + (-1 if key == "-" else 1)))
            if new != f["priority"]:
                rpc_key = {1: "priority-high", 0: "priority-normal", -1: "priority-low"}[new]
                ok, _ = self.rpc("Set priority", self.client.torrent_set, tid, **{rpc_key: [f["index"]]})
                if ok:
                    d["fileStats"][f["index"]]["priority"] = new
            return True
        if key in ("w", "x"):
            idx = [it["index"] for it in items]
            want = key == "w"
            ok, _ = self.rpc("Change files", self.client.torrent_set, tid,
                             **{"files-wanted" if want else "files-unwanted": idx})
            if ok:
                for i in idx:
                    d["fileStats"][i]["wanted"] = want
                self.flash(f"{'Wanted' if want else 'Skipped'} {self.plural(idx, 'file')}")
            return True
        if key == "n":
            old = f["full"].rsplit("/", 1)[-1]
            values = Form("Rename file", [Field("name", "New name", "text", old)], ok="Rename",
                          validate=lambda v: "Name can't be empty or contain '/'"
                          if not v["name"] or "/" in v["name"] else None).run(self)
            if values and values["name"] != old:
                ok, _ = self.rpc("Rename", self.client.rename_path, d["id"], f["full"], values["name"])
                if ok:
                    self.flash(f"Renamed to “{values['name']}”")
            return True
        if key == "N":
            if "/" not in f["full"]:
                self.flash("This file is not inside a folder", "info")
                return True
            folder = f["full"].rsplit("/", 1)[0]
            old = folder.rsplit("/", 1)[-1]
            values = Form("Rename folder", [Field("name", "New folder name", "text", old,
                                                  hint=f"renames “{folder}”")], ok="Rename",
                          validate=lambda v: "Name can't be empty or contain '/'"
                          if not v["name"] or "/" in v["name"] else None).run(self)
            if values and values["name"] != old:
                ok, _ = self.rpc("Rename", self.client.rename_path, d["id"], folder, values["name"])
                if ok:
                    self.flash(f"Renamed folder to “{values['name']}”")
            return True
        return False

    # -- trackers tab --------------------------------------------------------------

    def handle_trackers(self, key, d: dict) -> bool:
        if key not in ("a", "e", "d", "DELETE"):
            return False
        if "trackerList" not in d:
            self.flash("Editing trackers needs Transmission 4.0 or newer", "error")
            return True
        tiers = [[u for u in block.split("\n") if u.strip()]
                 for block in d["trackerList"].split("\n\n") if block.strip()]
        trackers = sorted(d.get("trackerStats", []), key=lambda tr: (tr.get("tier", 0), tr.get("id", 0)))
        state = self.tab_state[2]
        current = trackers[min(state.index, len(trackers) - 1)]["announce"] if trackers else None
        valid = lambda v: None if v["url"].startswith(("http://", "https://", "udp://", "wss://")) \
            else "Tracker URL must start with http://, https://, udp:// or wss://"
        if key == "a":
            values = Form("Add tracker", [Field("url", "Announce URL", "text", "",
                                                hint="added as a new tier")], ok="Add", validate=valid).run(self)
            if not values:
                return True
            tiers.append([values["url"]])
            verb = "Added"
        elif current is None:
            return True
        elif key == "e":
            values = Form("Edit tracker", [Field("url", "Announce URL", "text", current)], ok="Save",
                          validate=valid).run(self)
            if not values or values["url"] == current:
                return True
            tiers = [[values["url"] if u == current else u for u in tier] for tier in tiers]
            verb = "Updated"
        else:
            if choose(self, "Remove tracker", [current], [("y", "Remove this tracker", "red"),
                                                          ("Esc", "Cancel", "dim")], default=1) != "y":
                return True
            tiers = [[u for u in tier if u != current] for tier in tiers]
            verb = "Removed"
        new_list = "\n\n".join("\n".join(tier) for tier in tiers if tier)
        ok, _ = self.rpc("Update trackers", self.client.torrent_set, [d["id"]], trackerList=new_list)
        if ok:
            self.flash(f"{verb} tracker")
        return True


def run(settings, interval: float = 2.0, mouse: bool = True) -> int:
    """Start the interactive UI. Raises RPCError early if the daemon is unreachable."""
    def make_client():
        return Client(settings.url, settings.username, settings.password, settings.timeout)

    make_client().session_get(["version"])  # fail fast, before taking over the terminal
    os.environ.setdefault("ESCDELAY", "25")
    set_locale()
    return curses.wrapper(lambda scr: App(scr, make_client(), make_client(), interval, mouse).run())
