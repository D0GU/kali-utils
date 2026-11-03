"""plexsort command line: `plexsort` opens the curses UI; subcommands are scriptable."""

from __future__ import annotations

import argparse
import json
import os
import sys

from trctl import fmt
from trctl.config import ConfigError, load_settings
from trctl.fmt import paint
from trctl.rpc import Client, RPCError

from . import __version__, config, engine, naming
from .db import DB
from .parse import Plan
from .providers import Matcher, apply_match


class CLIError(Exception):
    pass


def connect(args):
    """Transmission client, or None if it isn't reachable (folders can still be sorted)."""
    try:
        s = load_settings(args.url, args.user)
        client = Client(s.url, s.username, s.password, s.timeout)
        client.session_get(["version"])
        return client, s
    except (RPCError, ConfigError) as e:
        if not args.quiet_transmission:
            print(paint(f"plexsort: Transmission unavailable ({e}); only folders can be sorted", "yellow"),
                  file=sys.stderr)
        return None, None


def find_source(arg: str, cfg: dict, client) -> engine.Source:
    sources, _, _ = engine.list_sources(cfg, client)
    path = os.path.abspath(os.path.expanduser(arg))
    for s in sources:
        if s.path == path:
            return s
    if os.path.exists(path):
        return engine.source_for_path(path)
    low = arg.lower()
    hits = [s for s in sources if s.torrent and (str(s.torrent["id"]) == arg or s.torrent["hashString"].startswith(low)
                                                 or low in s.name.lower())]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise CLIError("several completed torrents match:\n" + "\n".join(f"  {s.torrent['id']:>4}  {s.name}" for s in hits))
    raise CLIError(f"{arg!r} is not a path or a completed torrent (see `plexsort inbox`)")


def build(source, cfg, db, online: bool) -> tuple[Plan, list[str]]:
    plan = engine.make_plan(source)
    errors = []
    if online:
        errors = engine.auto_match(plan, Matcher(cfg, db))
    return plan, errors


def print_plan(plan: Plan, cfg: dict) -> engine.Check:
    check = engine.check_plan(plan, cfg)
    print(paint(f"{plan.name}", "bold"), paint(f"({plan.root})", "dim"))
    for g in plan.groups():
        ids = " ".join(f"{k}:{v}" for k, v in g.ids.items() if k in ("tvdb", "tmdb", "imdb"))
        status = paint(f"✓ {g.verified}", "green") if g.verified else paint("? not verified", "yellow")
        lib = {"show": "TV", "movie": "Movie", "music": "Music"}[g.category]
        label = naming.display_folder(g.category, g.title, g.year, {}, g.artist, False)
        print(f"\n{paint(lib, 'cyan')}  {paint(label, 'bold')}  {status}  {paint(ids, 'dim')}")
        pl = next((p for p in check.placements if p.key == g.key), None)
        if pl:
            print(paint(f"  → {pl.root}  ({fmt.human_size(pl.need)}; {pl.reason})", "red" if not pl.fits else "dim"))
        for it in g.items:
            dst = os.path.relpath(check.dests[id(it)], naming.root_of(check.dests[id(it)], g.category, cfg))
            flag = ""
            if id(it) in check.errors:
                flag = paint(f"  ✗ {check.errors[id(it)]}", "red")
            elif id(it) in check.warnings:
                flag = paint(f"  ! {check.warnings[id(it)]}", "yellow")
            subs = paint(f" +{len(it.subs)} sub", "dim") if it.subs else ""
            subs += paint(f" +{len(it.art)} artwork/log", "dim") if it.art else ""
            print(f"  {paint(it.src, 'dim')}\n    → {dst}{subs}{flag}")
    skipped = plan.skipped()
    if skipped or plan.leftovers:
        from .tui import media_counts
        print(paint(f"\nNot moved: {media_counts(skipped)} skipped, {len(plan.leftovers)} other file(s) "
                    f"({fmt.human_size(sum(s for _, s in plan.leftovers))})", "dim"))
    for e in check.global_errors:
        print(paint(f"✗ {e}", "red"))
    return check


def cmd_inbox(args, cfg, db) -> int:
    client, _ = connect(args)
    sources, downloading, error = engine.list_sources(cfg, client)
    if error:
        print(paint(error, "red"), file=sys.stderr)
    if args.json:
        print(json.dumps([{"kind": s.kind, "name": s.name, "path": s.path, "size": s.size,
                           "torrent_id": s.torrent["id"] if s.torrent else None} for s in sources], indent=2))
        return 0
    if not sources:
        print("Nothing to sort." + (f" ({downloading} torrent(s) still downloading)" if downloading else ""))
        return 0
    rows = [[str(s.torrent["id"]) if s.torrent else "-", s.kind, fmt.human_size(s.size),
             fmt.human_time(s.done_date), (s.name, "" if s.exists else "red")] for s in sources]
    print(fmt.table(["ID", "Source", "Size", "Completed", "Name"], rows, ">>>><", flex=4))
    if downloading:
        print(paint(f"{downloading} torrent(s) still downloading", "dim"))
    return 0


def cmd_plan(args, cfg, db) -> int:
    client, _ = connect(args)
    plan, errors = build(find_source(args.source, cfg, client), cfg, db, not args.offline)
    for e in errors:
        print(paint(f"lookup: {e}", "yellow"), file=sys.stderr)
    if args.json:
        check = engine.check_plan(plan, cfg)
        out = [{"src": it.src, "kind": it.kind, "category": it.category, "title": it.title, "year": it.year,
                "season": it.season, "episode": it.episode, "ep_title": it.ep_title, "ids": it.ids,
                "verified": it.verified, "dst": check.dests.get(id(it)), "error": check.errors.get(id(it))}
               for it in plan.items]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    check = print_plan(plan, cfg)
    return 0 if check.ok else 1


def cmd_apply(args, cfg, db) -> int:
    client, _ = connect(args)
    source = find_source(args.source, cfg, client)
    plan, errors = build(source, cfg, db, not args.offline)
    for e in errors:
        print(paint(f"lookup: {e}", "yellow"), file=sys.stderr)
    check = print_plan(plan, cfg)
    if not check.ok:
        raise CLIError("fix the problems above (use the UI: `plexsort`) before applying")
    unverified = [g for g in plan.groups() if not g.verified]
    if unverified and not args.allow_unverified:
        raise CLIError(f"{len(unverified)} title(s) not verified online - review them in the UI, "
                       "or pass --allow-unverified")
    if not args.yes:
        if not sys.stdin.isatty():
            raise CLIError("refusing to move files without confirmation; pass -y")
        if input("\nMove these files into the Plex library? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    result = engine.apply_plan(plan, cfg, db, client, remove_torrent=not args.keep_torrent,
                               delete_leftovers=not args.keep_leftovers, log=lambda m: print(paint(m, "dim")))
    for e in result.errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Moved {result.moved} file(s) (operation #{result.op_id}; undo with `plexsort undo {result.op_id}`)")
    return 1 if result.errors else 0


def cmd_library(args, cfg, db) -> int:
    category = "movie" if args.movies else "show" if args.shows else "music" if args.music else None
    rows = db.titles(category, args.search)
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return 0
    if not rows:
        print("Library is empty." + ("" if category or args.search else " Sort something, or run `plexsort scan`."))
        return 0
    table = []
    for r in rows:
        if r["category"] == "show":
            what = f"{r['episodes']} ep / {r['seasons']} season{'s' if r['seasons'] != 1 else ''}"
        elif r["category"] == "music":
            what = f"{r['tracks']} track{'s' if r['tracks'] != 1 else ''}"
        else:
            what = f"{r['videos']} file{'s' if r['videos'] != 1 else ''}"
        miss = (f" ({r['missing']} missing)", "red") if r["missing"] else ""
        name = naming.title_name(r["title"], r["year"])
        if r["category"] == "music":
            name = f"{r['artist'] or 'Unknown Artist'} – {name}"
        table.append([str(r["id"]), {"show": "TV", "movie": "Movie", "music": "Music"}[r["category"]],
                      name, what + (miss[0] if miss else ""),
                      fmt.human_size(r["size"]), r["provider"] or "-", (r["last_added"] or "")[:10]])
    print(fmt.table(["ID", "Type", "Title", "Content", "Size", "Source", "Added"], table, "><<<><<", flex=2))
    c = db.counts()
    print(paint(f"{c['movie']} movies · {c['show']} shows ({c['episodes']} episodes) · {c['music']} albums "
                f"({c['tracks']} tracks) · {fmt.human_size(c['size'])}", "dim"))
    return 0


def cmd_show(args, cfg, db) -> int:
    t = db.title(args.title_id)
    if not t:
        raise CLIError(f"no title {args.title_id}")
    print(paint(naming.title_name(t["title"], t["year"]), "bold"), paint(t["path"], "dim"))
    print(paint(f"ids: {t['ids']}  verified by: {t['provider'] or '-'}", "dim"))
    for f in db.title_files(args.title_id):
        flag = paint(" (missing)", "red") if f["status"] == "missing" else ""
        print(f"  {os.path.relpath(f['dst'], t['path'])}{flag}")
    return 0


def cmd_history(args, cfg, db) -> int:
    ops = db.operations()
    if not ops:
        print("Nothing sorted yet.")
        return 0
    rows = [[str(o["id"]), o["created"][:16], (o["status"], {"undone": "dim", "failed": "red"}.get(o["status"], "green")),
             str(o["present"] or 0) + "/" + str(o["files"]), fmt.human_size(o["size"]), o["source_name"] or ""]
            for o in ops]
    print(fmt.table(["Op", "When", "Status", "Files", "Size", "Source"], rows, ">><>><", flex=5))
    return 0


def cmd_undo(args, cfg, db) -> int:
    client, _ = connect(args) if args.readd else (None, None)
    op = db.operation(args.op_id)
    if not op:
        raise CLIError(f"no operation {args.op_id}")
    if not args.yes:
        if not sys.stdin.isatty():
            raise CLIError("pass -y to confirm")
        if input(f"Move {op['source_name']!r} files back to {op['source_root']}? [y/N] ").strip().lower() != "y":
            return 1
    restored, errors = engine.undo_operation(args.op_id, cfg, db, client, args.readd)
    for e in errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Restored {restored} file(s).")
    return 1 if errors else 0


def cmd_scan(args, cfg, db) -> int:
    added, missing = engine.scan_library(cfg, db)
    print(f"Indexed {added} new file(s); {missing} file(s) missing from disk.")
    return 0


def cmd_search(args, cfg, db) -> int:
    m = Matcher(cfg, db)
    category = "music" if args.music else "movie" if args.movie else "show"
    results = m.search(args.query, category, args.year, args.anime, artist=args.artist)
    for e in m.errors:
        print(paint(e, "yellow"), file=sys.stderr)
    for r in results[:10]:
        print(f"{r.score:.2f}  {r.provider:11} {r.category:5}  {paint(r.label(), 'bold')}  "
              f"{paint(json.dumps({k: v for k, v in r.ids.items() if k in ('tvdb', 'tmdb', 'imdb')}), 'dim')}  {r.info[:60]}")
    return 0


def cmd_config(args, cfg, db) -> int:
    if args.action in ("add", "remove"):
        if args.key not in config.LIST_KEYS:
            raise CLIError(f"{args.key!r} is not a folder list (one of: {', '.join(sorted(config.LIST_KEYS))})")
        path = os.path.abspath(os.path.expanduser(args.value))
        if args.action == "add":
            if path not in cfg[args.key]:
                cfg[args.key].append(path)
        elif path in cfg[args.key]:
            cfg[args.key].remove(path)
        else:
            raise CLIError(f"{path} is not in {args.key}")
        print(f"{args.key} = {cfg[args.key]}\nSaved {config.save(cfg)}")
        return 0
    if args.action == "set":
        if args.key not in config.DEFAULTS:
            raise CLIError(f"unknown key {args.key!r} (one of: {', '.join(config.DEFAULTS)})")
        cfg[args.key] = config.parse_value(args.key, args.value)
        print(f"Saved {config.save(cfg)}")
        return 0
    from .folders_ui import drive_text
    for k in config.DEFAULTS:
        v = cfg.get(k)
        if k == "tmdb_api_key" and v:
            v = v[:4] + "…" + v[-4:]
        if k in config.LIST_KEYS:
            print(f"{k:18} {'-' if not v else ''}")
            for path in v:
                print(f"  {path}  {paint(drive_text(path)[0], 'dim')}")
            continue
        print(f"{k:18} {v}")
    print(paint(f"\n{config.config_path()}", "dim"))
    return 0


def cmd_setup(args, cfg, db) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError("setup needs a terminal; or use `plexsort config add movies_dirs /path`")
    from . import tui
    code = tui.run(cfg, db, None, setup_only=True)
    if code == 0:
        print(f"Saved {config.config_path()}")
        for key in ("inbox_dirs", "movies_dirs", "tv_dirs", "music_dirs"):
            print(f"  {key:12} {', '.join(cfg[key]) or '-'}")
    return code


def cmd_ui(args, cfg, db) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError("the interactive UI needs a terminal; see `plexsort -h` for commands")
    client, settings = connect(args)
    from . import tui
    return tui.run(cfg, db, client, settings, start_source=args.source, offline=args.offline)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plexsort", description="Sort completed torrents (movies, TV, anime, music) into a Plex library.")
    p.add_argument("--version", action="version", version=f"plexsort {__version__}")
    p.add_argument("--url", help="Transmission RPC URL (default: trctl's config)")
    p.add_argument("--user", help="Transmission RPC username")
    p.add_argument("--offline", action="store_true", help="don't look titles up online")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(func=func)
        return sp

    sp = add("ui", cmd_ui, "Interactive curses interface (default)")
    sp.add_argument("source", nargs="?", help="open this torrent/folder for review straight away")
    sp = add("inbox", cmd_inbox, "List completed torrents and folders waiting to be sorted", ["ls"])
    sp.add_argument("--json", action="store_true")
    sp = add("plan", cmd_plan, "Show how a torrent/folder would be sorted (changes nothing)")
    sp.add_argument("source", help="torrent id/name/hash or a path")
    sp.add_argument("--json", action="store_true")
    sp = add("apply", cmd_apply, "Sort a torrent/folder into the library")
    sp.add_argument("source")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp.add_argument("--keep-torrent", action="store_true", help="don't remove the torrent from Transmission")
    sp.add_argument("--keep-leftovers", action="store_true", help="don't delete leftover junk files")
    sp.add_argument("--allow-unverified", action="store_true", help="sort titles that couldn't be verified online")
    sp = add("library", cmd_library, "List the sorted library", ["lib"])
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-m", "--movies", action="store_true")
    g.add_argument("-s", "--shows", action="store_true")
    g.add_argument("-M", "--music", action="store_true")
    sp.add_argument("-q", "--search")
    sp.add_argument("--json", action="store_true")
    sp = add("show", cmd_show, "List the files of a library title")
    sp.add_argument("title_id", type=int)
    add("history", cmd_history, "List sort operations")
    sp = add("undo", cmd_undo, "Move an operation's files back where they came from")
    sp.add_argument("op_id", type=int)
    sp.add_argument("--readd", action="store_true", help="also re-add the torrent to Transmission")
    sp.add_argument("-y", "--yes", action="store_true")
    add("scan", cmd_scan, "Index files already in the library folders; detect deleted ones")
    sp = add("search", cmd_search, "Search the online providers")
    sp.add_argument("query")
    sp.add_argument("--movie", action="store_true")
    sp.add_argument("--music", action="store_true", help="search albums (MusicBrainz); see --artist")
    sp.add_argument("--artist", help="album artist, for --music")
    sp.add_argument("--anime", action="store_true")
    sp.add_argument("--year", type=int)
    add("setup", cmd_setup, "Choose library folders (several per library/drive) and watched folders")
    sp = add("config", cmd_config, "Show settings; `config set KEY VALUE`; `config add|remove movies_dirs PATH`")
    sp.add_argument("action", nargs="?", choices=["show", "set", "add", "remove"], default="show")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(argv + ["ui"])
    args.quiet_transmission = args.command in ("ui",)
    fmt.paint.enabled = args.color == "always" or (args.color == "auto" and sys.stdout.isatty()
                                                   and not os.environ.get("NO_COLOR"))
    try:
        cfg = config.load()
        if args.offline:
            cfg["online"] = False
        if args.command == "config" and args.action != "show" and not (args.key and args.value is not None):
            raise CLIError(f"usage: plexsort config {args.action} KEY VALUE")
        db = DB(cfg["database"])
        # make sure all library folders exist so they can be added to Plex before the first sort
        for category in ("movie", "show", "music"):
            for root in naming.library_roots(category, cfg):
                try:
                    os.makedirs(root, exist_ok=True)
                except OSError as e:
                    print(paint(f"plexsort: can't create {e.filename}: {e.strerror}", "yellow"), file=sys.stderr)
        if not cfg["online"]:
            args.offline = True
        return args.func(args, cfg, db) or 0
    except (CLIError, engine.SortError) as e:
        print(f"plexsort: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
