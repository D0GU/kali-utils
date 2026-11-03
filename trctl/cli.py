"""Command-line interface for trctl."""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, fmt
from .config import (DEFAULT_URL, ConfigError, _run_password_command, config_path, load_settings,
                     normalize_url, read_config_file, write_config)
from .fmt import paint
from .rpc import AuthError, Client, RPCError


class CLIError(Exception):
    pass


# --------------------------------------------------------------------------
# Torrent state helpers
# --------------------------------------------------------------------------

STATUS_NAMES = {0: "stopped", 1: "verify-wait", 2: "verifying", 3: "queued",
                4: "downloading", 5: "seed-wait", 6: "seeding"}

LIST_FIELDS = ["id", "name", "status", "error", "errorString", "percentDone",
               "metadataPercentComplete", "sizeWhenDone", "leftUntilDone", "totalSize",
               "rateDownload", "rateUpload", "eta", "uploadRatio", "downloadDir",
               "addedDate", "doneDate", "hashString", "peersConnected", "labels",
               "queuePosition", "recheckProgress"]

STATUS_FILTERS = {
    "downloading": lambda t: t["status"] == 4,
    "seeding": lambda t: t["status"] == 6,
    "stopped": lambda t: t["status"] == 0,
    "queued": lambda t: t["status"] in (3, 5),
    "checking": lambda t: t["status"] in (1, 2),
    "active": lambda t: t["rateDownload"] > 0 or t["rateUpload"] > 0 or t["status"] == 2,
    "complete": lambda t: is_complete(t),
    "incomplete": lambda t: not is_complete(t),
    "error": lambda t: t["error"] != 0,
}
STATUS_ALIASES = {"paused": "stopped", "done": "complete", "finished": "complete",
                  "verifying": "checking", "down": "downloading", "up": "seeding"}

SORT_KEYS = {
    "id": lambda t: t["id"],
    "name": lambda t: t["name"].lower(),
    "progress": lambda t: t["percentDone"],
    "size": lambda t: t["sizeWhenDone"],
    "ratio": lambda t: t["uploadRatio"],
    "added": lambda t: t["addedDate"],
    "done": lambda t: t["doneDate"] or 0,
    "eta": lambda t: t["eta"] if t["eta"] >= 0 else float("inf"),
    "down": lambda t: t["rateDownload"],
    "up": lambda t: t["rateUpload"],
    "status": lambda t: (t["status"], t["name"].lower()),
    "queue": lambda t: t["queuePosition"],
}


def is_complete(t: dict) -> bool:
    return t.get("metadataPercentComplete", 1) >= 1 and t["leftUntilDone"] == 0


def status_label(t: dict) -> tuple:
    """Return (label, style...) for a torrent's state."""
    if t["error"] == 3:
        return ("error", "red", "bold")
    if t["status"] == 4 and t.get("metadataPercentComplete", 1) < 1:
        return ("metadata", "magenta")
    if t["status"] == 0:
        return ("finished", "green") if is_complete(t) else ("stopped", "dim")
    if t["status"] == 2:
        return (f"verify {t.get('recheckProgress', 0) * 100:.0f}%", "yellow")
    label = STATUS_NAMES.get(t["status"], str(t["status"]))
    if t["error"]:  # tracker warning/error while otherwise running
        label += "*"
    style = {"downloading": "cyan", "seeding": "green", "queued": "yellow",
             "seed-wait": "yellow", "verify-wait": "yellow"}
    return (label, style.get(STATUS_NAMES.get(t["status"], ""), ""))


def have_bytes(t: dict) -> int:
    return max(0, t["sizeWhenDone"] - t["leftUntilDone"])


# --------------------------------------------------------------------------
# Selector resolution: ids, ranges, hashes, names, globs
# --------------------------------------------------------------------------

def resolve(client: Client, selectors: list[str], *, single: bool = False) -> list[dict]:
    """Turn user selectors into a list of torrents ({id, name, hashString}).

    A selector may be: `all`, an id (`7`), a range (`3-9`), comma lists
    (`1,4,7-9`), a hash or hash prefix (6+ hex chars), a glob on the name
    (`*ubuntu*`), or a case-insensitive name substring.
    """
    if not selectors:
        raise CLIError("no torrent given (use an id, name, hash, or `all`)")
    torrents = client.torrent_get(["id", "name", "hashString"])
    by_id = {t["id"]: t for t in torrents}
    chosen: dict[int, dict] = {}

    parts: list[str] = []
    for s in selectors:
        parts.extend(p for p in (s.split(",") if re.fullmatch(r"[\d,\-\s]+", s) else [s]) if p.strip())

    for raw in parts:
        s = raw.strip()
        if s.lower() == "all":
            chosen.update(by_id)
            continue
        if m := re.fullmatch(r"(\d+)-(\d+)", s):
            lo, hi = int(m[1]), int(m[2])
            hits = [by_id[i] for i in range(lo, hi + 1) if i in by_id]
            if not hits:
                raise CLIError(f"no torrents with ids in range {s}")
        elif s.isdigit() and (int(s) in by_id or len(s) < 6):
            if int(s) not in by_id:
                raise CLIError(f"no torrent with id {s}")
            hits = [by_id[int(s)]]
        else:
            low = s.lower()
            hits = []
            if re.fullmatch(r"[0-9a-f]{6,40}", low):
                hits = [t for t in torrents if t["hashString"].startswith(low)]
            if not hits and any(c in s for c in "*?["):
                hits = [t for t in torrents if fnmatch.fnmatch(t["name"].lower(), low)]
            elif not hits:
                hits = [t for t in torrents if low in t["name"].lower()]
            if not hits:
                raise CLIError(f"no torrent matches {s!r}")
        for t in hits:
            chosen[t["id"]] = t

    result = sorted(chosen.values(), key=lambda t: t["id"])
    if single and len(result) != 1:
        names = "\n".join(f"  {t['id']:>4}  {t['name']}" for t in result)
        raise CLIError(f"this command needs exactly one torrent, but the selector matched "
                       f"{len(result)}:\n{names}")
    return result


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise CLIError("refusing to continue without confirmation; pass -y/--yes")
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def describe(torrents: list[dict], limit: int = 10) -> str:
    lines = [f"  {t['id']:>4}  {t['name']}" for t in torrents[:limit]]
    if len(torrents) > limit:
        lines.append(f"  ... and {len(torrents) - limit} more")
    return "\n".join(lines)


def print_json(data) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


# --------------------------------------------------------------------------
# list / watch rendering
# --------------------------------------------------------------------------

def torrent_rows(torrents: list[dict], with_bar: bool = False, bar_width: int = 16) -> list[list]:
    rows = []
    for t in torrents:
        done = t["percentDone"] if t.get("metadataPercentComplete", 1) >= 1 else 0
        pct_style = "green" if is_complete(t) else ""
        progress = (f"{fmt.bar(done, bar_width)} {fmt.percent(done):>6}" if with_bar
                    else fmt.percent(done))
        rows.append([
            str(t["id"]),
            (progress, pct_style),
            f"{fmt.human_size(have_bytes(t))} / {fmt.human_size(t['sizeWhenDone'])}",
            fmt.human_eta(t["eta"]) if not is_complete(t) else "-",
            (fmt.human_rate(t["rateDownload"]), "cyan" if t["rateDownload"] else "dim"),
            (fmt.human_rate(t["rateUpload"]), "green" if t["rateUpload"] else "dim"),
            fmt.human_ratio(t["uploadRatio"]),
            status_label(t),
            t["name"],
        ])
    return rows


TORRENT_HEADERS = ["ID", "Done", "Have / Size", "ETA", "Down", "Up", "Ratio", "Status", "Name"]
TORRENT_ALIGNS = ">>>>>>><<"


def summary_line(torrents: list[dict]) -> str:
    have = sum(have_bytes(t) for t in torrents)
    size = sum(t["sizeWhenDone"] for t in torrents)
    down = sum(t["rateDownload"] for t in torrents)
    up = sum(t["rateUpload"] for t in torrents)
    n = len(torrents)
    return paint(f"{n} torrent{'s' if n != 1 else ''} · {fmt.human_size(have)} of "
                 f"{fmt.human_size(size)} · ↓ {fmt.human_rate(down)}  ↑ {fmt.human_rate(up)}",
                 "dim")


def parse_status_filter(value: str) -> list[str]:
    names = []
    for raw in value.split(","):
        name = STATUS_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if name not in STATUS_FILTERS:
            raise argparse.ArgumentTypeError(
                f"unknown status {raw!r} (choose from {', '.join(sorted(STATUS_FILTERS))})")
        names.append(name)
    return names


def cmd_list(client: Client, args) -> int:
    torrents = client.torrent_get(LIST_FIELDS)
    if args.status:
        torrents = [t for t in torrents if any(STATUS_FILTERS[s](t) for s in args.status)]
    if args.name:
        pat = args.name.lower()
        globbing = any(c in pat for c in "*?[")
        torrents = [t for t in torrents
                    if (fnmatch.fnmatch(t["name"].lower(), pat) if globbing else pat in t["name"].lower())]
    if args.dir:
        d = args.dir.rstrip("/")
        torrents = [t for t in torrents if t["downloadDir"].rstrip("/") == d]
    if args.label:
        torrents = [t for t in torrents if args.label in (t.get("labels") or [])]
    torrents.sort(key=SORT_KEYS[args.sort], reverse=args.reverse)
    if args.limit:
        torrents = torrents[:args.limit]

    if args.json:
        print_json(torrents)
        return 0
    if args.ids_only:
        for t in torrents:
            print(t["id"])
        return 0
    if not torrents:
        print("No torrents.")
        return 0
    print(fmt.table(TORRENT_HEADERS, torrent_rows(torrents), TORRENT_ALIGNS, flex=8))
    print(summary_line(torrents))
    return 0


def cmd_watch(client: Client, args) -> int:
    if not (args.plain or args.count or args.selectors) and sys.stdin.isatty() and sys.stdout.isatty():
        from . import tui
        return tui.run(args.settings, interval=args.interval, mouse=not args.no_mouse)
    ids = [t["id"] for t in resolve(client, args.selectors)] if args.selectors else None
    tty = sys.stdout.isatty()
    seen_incomplete: set[int] = set()
    free_cache: tuple[float, str] = (0.0, "")
    if tty:
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        while True:
            torrents = client.torrent_get(LIST_FIELDS, ids)
            for t in torrents:
                if not is_complete(t):
                    seen_incomplete.add(t["id"])
            if ids is None and not args.all:
                torrents = [t for t in torrents
                            if not is_complete(t) or t["id"] in seen_incomplete
                            or t["status"] in (1, 2)]
            torrents.sort(key=SORT_KEYS[args.sort], reverse=args.reverse)
            stats = client.session_stats()

            now = time.monotonic()
            if now - free_cache[0] > 30:
                try:
                    ddir = client.session_get(["download-dir"])["download-dir"]
                    free = client.free_space(ddir)["size-bytes"]
                    free_cache = (now, f" · {fmt.human_size(free)} free")
                except RPCError:
                    free_cache = (now, "")

            header = (f"{paint('trctl watch', 'bold')}  {time.strftime('%H:%M:%S')} · "
                      f"↓ {paint(fmt.human_rate(stats.get('downloadSpeed')), 'cyan')}  "
                      f"↑ {paint(fmt.human_rate(stats.get('uploadSpeed')), 'green')} · "
                      f"{stats.get('activeTorrentCount', 0)} active / "
                      f"{stats.get('torrentCount', 0)} total{free_cache[1]}")
            hint = paint(f"refresh {args.interval:g}s · Ctrl-C to quit"
                         + ("" if args.all or ids else " · showing incomplete/active (use --all)"),
                         "dim")
            if torrents:
                body = fmt.table(TORRENT_HEADERS, torrent_rows(torrents, with_bar=True),
                                 TORRENT_ALIGNS, flex=8)
            else:
                body = "Nothing downloading."
            frame = f"{header}\n{hint}\n\n{body}\n"
            if tty:
                sys.stdout.write("\x1b[H\x1b[2J" + frame)
                sys.stdout.flush()
            else:
                print(frame, flush=True)
            if args.count:
                args.count -= 1
                if args.count == 0:
                    return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if tty:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()


def cmd_wait(client: Client, args) -> int:
    targets = resolve(client, args.selectors)
    ids = [t["id"] for t in targets]
    started = time.monotonic()
    warned_stopped: set[int] = set()
    tty = sys.stderr.isatty()
    while True:
        torrents = client.torrent_get(LIST_FIELDS, ids)
        if len(torrents) < len(ids):
            gone = set(ids) - {t["id"] for t in torrents}
            raise CLIError(f"torrent(s) {', '.join(map(str, sorted(gone)))} were removed while waiting")
        pending = [t for t in torrents if not is_complete(t)]
        if not pending:
            if tty:
                sys.stderr.write("\r\x1b[K")
            n = len(torrents)
            print(f"Done: {n} torrent{'s' if n != 1 else ''} complete.")
            return 0
        failed = [t for t in pending if t["error"] == 3]
        if failed and not args.keep_going:
            if tty:
                sys.stderr.write("\r\x1b[K")
            for t in failed:
                print(f"trctl: error: {t['name']}: {t['errorString']}", file=sys.stderr)
            return 1
        for t in pending:
            if t["status"] == 0 and t["id"] not in warned_stopped:
                warned_stopped.add(t["id"])
                if tty:
                    sys.stderr.write("\r\x1b[K")
                print(f"trctl: warning: {t['name']!r} is stopped and won't finish until started",
                      file=sys.stderr)
        if args.timeout and time.monotonic() - started > args.timeout:
            if tty:
                sys.stderr.write("\r\x1b[K")
            print(f"trctl: timed out with {len(pending)} torrent(s) still incomplete",
                  file=sys.stderr)
            return 124
        if tty:
            size = sum(t["sizeWhenDone"] for t in torrents)
            have = sum(have_bytes(t) for t in torrents)
            rate = sum(t["rateDownload"] for t in torrents)
            frac = have / size if size else 0
            etas = [t["eta"] for t in pending if t["eta"] >= 0]
            eta = fmt.human_eta(max(etas)) if etas and len(etas) == len(pending) else "-"
            sys.stderr.write(f"\r\x1b[K{fmt.bar(frac, 24)} {fmt.percent(frac):>6}  "
                             f"{len(torrents) - len(pending)}/{len(torrents)} done  "
                             f"↓ {fmt.human_rate(rate)}  ETA {eta}")
            sys.stderr.flush()
        time.sleep(args.interval)


# --------------------------------------------------------------------------
# info / stats
# --------------------------------------------------------------------------

INFO_FIELDS = LIST_FIELDS + [
    "totalSize", "downloadedEver", "uploadedEver", "corruptEver", "activityDate",
    "peersSendingToUs", "peersGettingFromUs", "comment", "creator", "dateCreated",
    "isPrivate", "pieceCount", "pieceSize", "magnetLink", "trackerStats", "files",
    "fileStats", "secondsDownloading", "secondsSeeding", "webseedsSendingToUs",
]


def info_fields(t: dict) -> list[tuple]:
    """Key/value rows describing a torrent: (key, text, style...)."""
    label = status_label(t)
    wanted = sum(1 for s in t["fileStats"] if s["wanted"])
    fields = [
        ("Name", t["name"], "bold"),
        ("ID", str(t["id"])),
        ("Hash", t["hashString"]),
        ("Status", f"{label[0]}  ({fmt.percent(t['percentDone'])})", *label[1:]),
        ("Location", os.path.join(t["downloadDir"], t["name"])),
        ("Size", f"{fmt.human_size(have_bytes(t))} of {fmt.human_size(t['sizeWhenDone'])} selected"
                 f"  (torrent total {fmt.human_size(t['totalSize'])})"),
        ("Files", f"{len(t['files'])} ({wanted} wanted)"),
        ("Transferred", f"↓ {fmt.human_size(t['downloadedEver'])}  ↑ {fmt.human_size(t['uploadedEver'])}"
                        f"  ratio {fmt.human_ratio(t['uploadRatio'])}"
                        + (f"  ({fmt.human_size(t['corruptEver'])} corrupt)" if t["corruptEver"] else "")),
        ("Speed", f"↓ {fmt.human_rate(t['rateDownload'])}  ↑ {fmt.human_rate(t['rateUpload'])}"),
        ("ETA", fmt.human_eta(t["eta"])),
        ("Peers", f"{t['peersConnected']} connected, {t['peersSendingToUs']} sending to us, "
                  f"{t['peersGettingFromUs']} downloading from us"),
        ("Added", fmt.human_time(t["addedDate"])),
        ("Completed", fmt.human_time(t["doneDate"])),
        ("Last activity", fmt.human_time(t["activityDate"])),
        ("Pieces", f"{t['pieceCount']} × {fmt.human_size(t['pieceSize'])}"),
        ("Private", "yes" if t["isPrivate"] else "no"),
    ]
    if t.get("labels"):
        fields.append(("Labels", ", ".join(t["labels"])))
    if t["error"]:
        kind = {1: "tracker warning", 2: "tracker error", 3: "local error"}.get(t["error"], "error")
        fields.append(("Error", f"{kind}: {t['errorString']}", "red"))
    if t.get("comment"):
        fields.append(("Comment", t["comment"]))
    if t.get("creator"):
        fields.append(("Created by", t["creator"]))
    return fields


def cmd_info(client: Client, args) -> int:
    ids = [t["id"] for t in resolve(client, args.selectors)]
    torrents = client.torrent_get(INFO_FIELDS, ids)
    if args.json:
        print_json(torrents)
        return 0
    for n, t in enumerate(torrents):
        if n:
            print()
        fields = info_fields(t)
        width = max(len(f[0]) for f in fields) + 1
        for key, value, *styles in fields:
            print(f"{paint((key + ':').ljust(width), 'dim')} {paint(value, *styles)}")
        if t["trackerStats"]:
            print(paint("Trackers:", "dim"))
            for tr in t["trackerStats"]:
                seeds = tr.get("seederCount", -1)
                peers = tr.get("leecherCount", -1)
                counts = f"{seeds if seeds >= 0 else '?'} seeders / {peers if peers >= 0 else '?'} leechers"
                result = tr.get("lastAnnounceResult") or "-"
                print(f"  tier {tr.get('tier', 0)}  {tr.get('host', tr.get('announce'))}  {counts}  ({result})")
        if args.magnet:
            print(f"{paint('Magnet:'.ljust(width), 'dim')} {t['magnetLink']}")
    return 0


def cmd_stats(client: Client, args) -> int:
    session = client.session_get()
    stats = client.session_stats()
    free = {}
    for key in ("download-dir", "incomplete-dir"):
        path = session.get(key)
        if path and path not in free and (key == "download-dir" or session.get("incomplete-dir-enabled")):
            try:
                free[path] = client.free_space(path)
            except RPCError as e:
                free[path] = {"error": str(e)}
    if args.json:
        print_json({"session": session, "stats": stats, "free-space": free})
        return 0

    def limit(kind):
        alt = session.get("alt-speed-enabled")
        if alt:
            return f"{session.get(f'alt-speed-{kind}')} KB/s (turtle mode)"
        if session.get(f"speed-limit-{kind}-enabled"):
            return f"{session.get(f'speed-limit-{kind}')} KB/s"
        return "unlimited"

    cur, cum = stats.get("current-stats", {}), stats.get("cumulative-stats", {})
    lines = [
        ("Daemon", f"Transmission {session.get('version', '?')} (RPC v{session.get('rpc-version', '?')})"),
        ("Torrents", f"{stats.get('torrentCount', 0)} total, {stats.get('activeTorrentCount', 0)} active, "
                     f"{stats.get('pausedTorrentCount', 0)} paused"),
        ("Speed", f"↓ {fmt.human_rate(stats.get('downloadSpeed'))}  ↑ {fmt.human_rate(stats.get('uploadSpeed'))}"),
        ("Limits", f"↓ {limit('down')}  ↑ {limit('up')}"),
        ("This session", f"↓ {fmt.human_size(cur.get('downloadedBytes'))}  ↑ {fmt.human_size(cur.get('uploadedBytes'))}"
                         f"  over {fmt.human_eta(cur.get('secondsActive'))}"),
        ("All time", f"↓ {fmt.human_size(cum.get('downloadedBytes'))}  ↑ {fmt.human_size(cum.get('uploadedBytes'))}"
                     f"  over {fmt.human_eta(cum.get('secondsActive'))}  ({cum.get('sessionCount', '?')} sessions)"),
        ("Download dir", session.get("download-dir", "?")),
    ]
    if session.get("incomplete-dir-enabled"):
        lines.append(("Incomplete dir", session.get("incomplete-dir")))
    for path, info in free.items():
        if "error" in info:
            lines.append((f"Free ({path})", info["error"]))
        else:
            total = info.get("total_size")
            extra = f" of {fmt.human_size(total)}" if total else ""
            lines.append((f"Free space", f"{fmt.human_size(info['size-bytes'])}{extra}  ({path})"))
    width = max(len(k) for k, _ in lines) + 1
    for key, value in lines:
        print(f"{paint((key + ':').ljust(width), 'dim')} {value}")
    return 0


# --------------------------------------------------------------------------
# add / start / stop / remove
# --------------------------------------------------------------------------

def add_source(client: Client, source: str, *, download_dir: str | None = None,
               paused: bool = False, labels: list[str] | None = None) -> tuple[str, dict]:
    """Add a torrent from a .torrent path, magnet link, URL, or bare info hash."""
    source = source.strip()
    kwargs = dict(download_dir=download_dir or None, paused=paused, labels=labels or None)
    path = Path(source).expanduser()
    if re.fullmatch(r"[0-9a-fA-F]{40}", source):
        return client.torrent_add(filename=f"magnet:?xt=urn:btih:{source}", **kwargs)
    if source.startswith(("magnet:", "http://", "https://")):
        return client.torrent_add(filename=source, **kwargs)
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError as e:
            raise CLIError(f"{source}: {e.strerror}") from None
        return client.torrent_add(metainfo=data, **kwargs)
    raise CLIError(f"{source}: not a file, magnet link, URL, or info hash")


def cmd_add(client: Client, args) -> int:
    failures = 0
    labels = [l for l in (args.label or []) if l]
    for source in args.sources:
        try:
            status, t = add_source(client, source, download_dir=args.dir, paused=args.paused,
                                   labels=labels)
        except (RPCError, CLIError) as e:
            print(f"trctl: error: {e}", file=sys.stderr)
            failures += 1
            continue
        verb = paint("Added", "green") if status == "added" else paint("Already present", "yellow")
        print(f"{verb}: [{t.get('id')}] {t.get('name', source)}")
        path = Path(source).expanduser()
        if args.remove_file and status == "added" and path.is_file() and path.suffix == ".torrent":
            path.unlink()
    return 1 if failures else 0


def simple_action(action: str, past: str):
    def run(client: Client, args) -> int:
        targets = resolve(client, args.selectors)
        client.torrent_action("start-now" if action == "start" and args.now else action,
                              [t["id"] for t in targets])
        for t in targets:
            print(f"{past}: [{t['id']}] {t['name']}")
        return 0
    return run


def cmd_remove(client: Client, args) -> int:
    targets = resolve(client, args.selectors)
    what = "Remove torrent AND DELETE DATA for" if args.delete else "Remove (keeping data)"
    print(f"{what} {len(targets)} torrent{'s' if len(targets) != 1 else ''}:\n{describe(targets)}")
    if not confirm("Continue?", args.yes):
        print("Aborted.")
        return 1
    client.torrent_remove([t["id"] for t in targets], delete_data=args.delete)
    print(f"Removed {len(targets)} torrent{'s' if len(targets) != 1 else ''}"
          + (" and deleted their data." if args.delete else "; data kept on disk."))
    return 0


# --------------------------------------------------------------------------
# File management: files / move / rename
# --------------------------------------------------------------------------

PRIORITY_NAMES = {-1: "low", 0: "normal", 1: "high"}


def parse_file_spec(spec: str, paths: list[str]) -> list[int]:
    """Parse `0,3-5,*.nfo,all` into file indices. Globs match the relative path
    or the basename, case-insensitively."""
    result: set[int] = set()
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if part.lower() == "all":
            result.update(range(len(paths)))
        elif m := re.fullmatch(r"(\d+)-(\d+)", part):
            lo, hi = int(m[1]), int(m[2])
            if hi >= len(paths) or lo > hi:
                raise CLIError(f"file range {part} is out of bounds (0-{len(paths) - 1})")
            result.update(range(lo, hi + 1))
        elif part.isdigit():
            if int(part) >= len(paths):
                raise CLIError(f"file index {part} is out of bounds (0-{len(paths) - 1})")
            result.add(int(part))
        else:
            pat = part.lower()
            if not any(c in pat for c in "*?["):
                pat = f"*{pat}*"
            hits = [i for i, p in enumerate(paths)
                    if fnmatch.fnmatch(p.lower(), pat) or fnmatch.fnmatch(p.lower().rsplit("/", 1)[-1], pat)]
            if not hits:
                raise CLIError(f"no file matches {part!r}")
            result.update(hits)
    return sorted(result)


def relative_paths(t: dict) -> list[str]:
    prefix = t["name"] + "/"
    return [f["name"][len(prefix):] if f["name"].startswith(prefix) else f["name"] for f in t["files"]]


def torrent_path(t: dict, rel: str) -> str:
    """Map a path relative to the torrent root to what torrent-rename-path expects."""
    single = len(t["files"]) == 1 and t["files"][0]["name"] == t["name"]
    return rel if single else f"{t['name']}/{rel}"


def cmd_files(client: Client, args) -> int:
    (target,) = resolve(client, args.selectors, single=True)
    fields = ["id", "name", "files", "fileStats", "downloadDir"]
    t = client.torrent_get(fields, [target["id"]])[0]
    if not t["files"]:
        raise CLIError(f"{t['name']}: file list not available yet (magnet still fetching metadata?)")
    paths = relative_paths(t)

    changes: dict[str, list[int]] = {}
    for opt, key in (("want", "files_wanted"), ("skip", "files_unwanted"), ("high", "priority_high"),
                     ("normal", "priority_normal"), ("low", "priority_low")):
        spec = getattr(args, opt)
        if spec:
            changes[key] = parse_file_spec(",".join(spec), paths)
    if args.only:
        only = set(parse_file_spec(",".join(args.only), paths))
        rest = [i for i in range(len(paths)) if i not in only]
        changes["files_wanted"] = sorted(only | set(changes.get("files_wanted", [])))
        if rest or "files_unwanted" in changes:
            changes["files_unwanted"] = sorted(set(rest) | set(changes.get("files_unwanted", [])))
    if changes:
        overlap = set(changes.get("files_wanted", [])) & set(changes.get("files_unwanted", []))
        if overlap:
            raise CLIError(f"file(s) {', '.join(map(str, sorted(overlap)))} are both wanted and skipped")
        client.torrent_set([t["id"]], **changes)
        summary = ", ".join(f"{k.replace('_', ' ')}: {len(v)}" for k, v in changes.items())
        print(paint(f"Updated {t['name']} ({summary})", "green"))
        t = client.torrent_get(fields, [t["id"]])[0]

    if args.json:
        print_json([{"index": i, "path": f["name"], "size": f["length"],
                     "completed": s["bytesCompleted"], "wanted": s["wanted"],
                     "priority": PRIORITY_NAMES.get(s["priority"], s["priority"])}
                    for i, (f, s) in enumerate(zip(t["files"], t["fileStats"]))])
        return 0

    rows = []
    filt = args.filter.lower() if args.filter else None
    for i, (f, s, p) in enumerate(zip(t["files"], t["fileStats"], paths)):
        if filt and not fnmatch.fnmatch(p.lower(), filt if any(c in filt for c in "*?[") else f"*{filt}*"):
            continue
        frac = s["bytesCompleted"] / f["length"] if f["length"] else 1.0
        prio = PRIORITY_NAMES.get(s["priority"], str(s["priority"]))
        rows.append([
            str(i),
            (fmt.percent(frac), "green" if frac >= 1 else ""),
            fmt.human_size(f["length"]),
            ("yes", "") if s["wanted"] else ("skip", "dim"),
            (prio, {"high": "yellow", "low": "dim"}.get(prio, "")),
            (p, "") if s["wanted"] else (p, "dim"),
        ])
    print(paint(f"{t['name']}  ({os.path.join(t['downloadDir'], t['name'])})", "bold"))
    if not rows:
        print("No matching files.")
        return 0
    print(fmt.table(["#", "Done", "Size", "Get", "Priority", "Path"], rows, ">>><<<", flex=5))
    wanted = [(f, s) for f, s in zip(t["files"], t["fileStats"]) if s["wanted"]]
    print(paint(f"{len(t['files'])} files · {len(wanted)} wanted · "
                f"{fmt.human_size(sum(f['length'] for f, _ in wanted))} selected of "
                f"{fmt.human_size(sum(f['length'] for f in t['files']))}", "dim"))
    return 0


def cmd_move(client: Client, args) -> int:
    targets = resolve(client, args.selectors)
    dest = args.destination
    if not dest.startswith("/"):
        raise CLIError("destination must be an absolute path on the daemon's machine")
    verb = "Move data of" if not args.no_move else "Point (without moving data)"
    print(f"{verb} {len(targets)} torrent{'s' if len(targets) != 1 else ''} to {dest}:\n{describe(targets)}")
    if len(targets) > 1 and not confirm("Continue?", args.yes):
        print("Aborted.")
        return 1
    client.set_location([t["id"] for t in targets], dest, move=not args.no_move)
    print("Requested. The daemon moves data in the background; check `trctl info` for errors.")
    return 0


def cmd_rename(client: Client, args) -> int:
    (target,) = resolve(client, [args.torrent], single=True)
    if "/" in args.new_name:
        raise CLIError("new name must be a single path component (no '/')")
    if args.path:
        t = client.torrent_get(["id", "name", "files"], [target["id"]])[0]
        paths = relative_paths(t)
        old = args.path.strip("/")
        if old not in paths and not any(p.startswith(old + "/") for p in paths):
            # allow index shorthand
            if old.isdigit() and int(old) < len(paths):
                old = paths[int(old)]
            else:
                raise CLIError(f"{args.path!r} is not a file or folder in this torrent "
                               f"(see `trctl files {target['id']}`)")
        full = torrent_path(t, old)
    else:
        full = target["name"]
        old = target["name"]
    res = client.rename_path(target["id"], full, args.new_name)
    print(f"Renamed {old!r} -> {res.get('name', args.new_name)!r} in [{target['id']}] {target['name']}")
    return 0


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def cmd_setup(args) -> int:
    existing = read_config_file()
    path = config_path()
    print(f"Configuring trctl ({path})")

    def ask(prompt, default):
        if args.non_interactive:
            return default
        value = input(f"{prompt} [{default or ''}]: ").strip()
        return value or default

    url = normalize_url(args.url or ask("RPC URL", existing.get("url", DEFAULT_URL)))
    username = args.user or ask("Username", existing.get("username", ""))
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif args.non_interactive:
        password = existing.get("password")
    else:
        password = getpass.getpass("Password (blank keeps the saved one): ") or existing.get("password")
    use_command = bool(existing.get("password_command")) and not password
    test_password = _run_password_command(existing["password_command"]) if use_command else password

    try:
        session = Client(url, username or None, test_password).session_get(["version"])
        print(paint(f"Connected: Transmission {session.get('version', '?')}", "green"))
    except RPCError as e:
        print(paint(f"Connection test failed: {e}", "red"))
        if args.non_interactive or not confirm("Save anyway?", False):
            return 1

    values = {"url": url, "username": username, "password": None if use_command else password}
    if use_command:
        values["password_command"] = existing["password_command"]
    if "timeout" in existing:
        values["timeout"] = existing["timeout"]
    write_config(values, path)
    print(f"Saved {path} (mode 600)")
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trctl",
        description="Manage a transmission-daemon from the command line.",
        epilog="Torrent selectors: id (7), range (3-9), list (1,4,7), hash prefix, "
               "name substring, glob ('*ubuntu*'), or 'all'. Run `trctl <command> -h` for details.")
    p.add_argument("--version", action="version", version=f"trctl {__version__}")
    p.add_argument("--url", help="RPC URL or host[:port] (default from config, else localhost:9091)")
    p.add_argument("--user", help="RPC username (password from config, $TRCTL_PASSWORD, or prompt)")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--no-color", dest="color", action="store_const", const="never")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(func=func)
        return sp

    def selectors(sp, nargs="+"):
        sp.add_argument("selectors", nargs=nargs, metavar="TORRENT",
                        help="id, range, hash prefix, name, glob, or 'all'")

    sp = add("list", cmd_list, "List torrents (default command)", aliases=["ls"])
    sp.add_argument("-s", "--status", type=parse_status_filter,
                    help="filter: " + ", ".join(sorted(STATUS_FILTERS)) + " (comma-separated = any)")
    sp.add_argument("-n", "--name", help="filter by name substring or glob")
    sp.add_argument("-d", "--dir", help="only torrents whose download dir is DIR")
    sp.add_argument("-l", "--label", help="only torrents with this label")
    sp.add_argument("-S", "--sort", choices=sorted(SORT_KEYS), default="id")
    sp.add_argument("-r", "--reverse", action="store_true")
    sp.add_argument("--limit", type=int, help="show at most N torrents")
    sp.add_argument("--ids", dest="ids_only", action="store_true", help="print only ids (for scripts)")
    sp.add_argument("--json", action="store_true")

    sp = add("info", cmd_info, "Show detailed information about torrents", aliases=["show"])
    selectors(sp)
    sp.add_argument("-m", "--magnet", action="store_true", help="also print the magnet link")
    sp.add_argument("--json", action="store_true")

    sp = add("watch", cmd_watch, "Interactive full-screen manager (plain live view with --plain or when piped)",
             aliases=["ui", "top"])
    selectors(sp, nargs="*")
    sp.add_argument("--plain", action="store_true", help="simple auto-refreshing table instead of the interactive UI")
    sp.add_argument("--no-mouse", action="store_true", help="don't capture the mouse (keeps terminal text selection)")
    sp.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    sp.add_argument("-a", "--all", action="store_true", help="show all torrents, not just active/incomplete")
    sp.add_argument("-S", "--sort", choices=sorted(SORT_KEYS), default="queue")
    sp.add_argument("-r", "--reverse", action="store_true")
    sp.add_argument("-n", "--count", type=int, default=0, help="(plain mode) exit after N refreshes")

    sp = add("wait", cmd_wait, "Block until torrents finish downloading (exit 0 done, 1 error, 124 timeout)")
    selectors(sp)
    sp.add_argument("-i", "--interval", type=float, default=5.0)
    sp.add_argument("-t", "--timeout", type=float, help="give up after N seconds")
    sp.add_argument("-k", "--keep-going", action="store_true", help="don't exit on local errors")

    sp = add("stats", cmd_stats, "Session statistics, speed limits and free space", aliases=["session"])
    sp.add_argument("--json", action="store_true")

    sp = add("add", cmd_add, "Add torrents from .torrent files, magnet links, URLs, or info hashes")
    sp.add_argument("sources", nargs="+", metavar="SOURCE")
    sp.add_argument("-d", "--dir", help="download directory (on the daemon's machine)")
    sp.add_argument("-p", "--paused", action="store_true", help="add without starting")
    sp.add_argument("-l", "--label", action="append", help="label to attach (repeatable)")
    sp.add_argument("--remove-file", action="store_true", help="delete local .torrent file after adding")

    for name, past, help, aliases in (
            ("start", "Started", "Start (resume) torrents", ["resume"]),
            ("stop", "Stopped", "Stop (pause) torrents", ["pause"]),
            ("verify", "Verifying", "Re-check torrent data on disk", ["check"]),
            ("reannounce", "Reannounced", "Ask trackers for more peers now", [])):
        sp = add(name, simple_action(name, past), help, aliases=aliases)
        selectors(sp)
        if name == "start":
            sp.add_argument("--now", action="store_true", help="bypass the download queue")

    sp = add("remove", cmd_remove, "Remove torrents (optionally deleting their data)", aliases=["rm"])
    selectors(sp)
    sp.add_argument("-D", "--delete", action="store_true", help="also delete downloaded data")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    sp = add("files", cmd_files, "List a torrent's files; select/skip files and set priorities")
    selectors(sp)
    spec_help = "files by index, range, glob or name part, comma-separated (e.g. 0,3-5,'*.mkv')"
    sp.add_argument("-w", "--want", action="append", metavar="SPEC", help="download these " + spec_help)
    sp.add_argument("-x", "--skip", action="append", metavar="SPEC", help="don't download these files")
    sp.add_argument("-o", "--only", action="append", metavar="SPEC", help="download only these files, skip the rest")
    sp.add_argument("--high", action="append", metavar="SPEC", help="set high priority")
    sp.add_argument("--normal", action="append", metavar="SPEC", help="set normal priority")
    sp.add_argument("--low", action="append", metavar="SPEC", help="set low priority")
    sp.add_argument("-f", "--filter", help="only display files matching a substring or glob")
    sp.add_argument("--json", action="store_true")

    sp = add("move", cmd_move, "Move torrent data to another directory on the daemon's machine", aliases=["mv"])
    selectors(sp)
    sp.add_argument("destination", metavar="DEST", help="absolute directory path")
    sp.add_argument("--no-move", action="store_true",
                    help="don't move files; just tell the daemon the data is already at DEST")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("rename", cmd_rename, "Rename a torrent's top folder/file, or a path inside it")
    sp.add_argument("torrent", metavar="TORRENT")
    sp.add_argument("new_name", metavar="NEW_NAME")
    sp.add_argument("-p", "--path", help="file/folder inside the torrent to rename (path or file index); "
                                         "default is the torrent's top-level name")

    sp = sub.add_parser("setup", help="Create or update the config file (URL, username, password)")
    sp.set_defaults(func=None)
    sp.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    sp.add_argument("--non-interactive", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(argv + ["list"])

    fmt.paint.enabled = (args.color == "always" or
                         (args.color == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
    try:
        if args.command == "setup":
            return cmd_setup(args)
        settings = load_settings(args.url, args.user)
        args.settings = settings
        client = Client(settings.url, settings.username, settings.password, settings.timeout)
        return args.func(client, args) or 0
    except AuthError as e:
        print(f"trctl: error: {e}", file=sys.stderr)
        return 2
    except (CLIError, RPCError, ConfigError) as e:
        print(f"trctl: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # e.g. `trctl list | head`
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
