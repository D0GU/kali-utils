"""kvctl command line: `kvctl` opens the curses UI; subcommands are scriptable."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys

from kutil import fmt
from kutil.fmt import paint
from trctl.config import ConfigError, load_settings
from trctl.rpc import Client, RPCError

from . import __version__, calibre, config, engine
from .client import BOOK_LIBRARY, LIBRARY_TYPES, AuthError, Kavita, KavitaError
from .db import DB
from .providers import Lookup


class CLIError(Exception):
    pass


def connect_transmission(quiet: bool = False):
    """Transmission client, or None if it isn't reachable (folders can still be imported)."""
    try:
        s = load_settings()
        client = Client(s.url, s.username, s.password, s.timeout)
        client.session_get(["version"])
        return client
    except (RPCError, ConfigError) as e:
        if not quiet:
            print(paint(f"kvctl: Transmission unavailable ({e}); only folders can be imported", "yellow"), file=sys.stderr)
        return None


def kavita_client(cfg: dict) -> Kavita:
    return Kavita(cfg["url"], cfg["api_key"])


def optional_kavita(cfg: dict) -> Kavita | None:
    """For scanning after a change. Without a key the change still happens; Kavita's folder watching may pick it up."""
    return kavita_client(cfg) if cfg["api_key"] else None


def confirm(args, question: str) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise CLIError("refusing to change anything without confirmation; pass -y")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def find_library(client: Kavita, arg: str) -> dict:
    libs = client.libraries()
    hits = [l for l in libs if str(l.get("id")) == arg] or [l for l in libs if (l.get("name") or "").lower() == arg.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise CLIError(f"several libraries are called {arg!r}; use the id")
    raise CLIError(f"no Kavita library {arg!r} (see `kvctl libraries`)")


def find_source(arg: str, cfg: dict, client) -> engine.Source:
    sources, _ = engine.list_sources(cfg, client)
    path = os.path.abspath(os.path.expanduser(arg))
    for s in sources:
        if s.path == path:
            return s
    if os.path.exists(path):
        return engine.source_for_path(path)
    low = arg.lower()
    hits = [s for s in sources if (s.torrent and (str(s.torrent["id"]) == arg or s.torrent["hashString"].startswith(low)))
            or low in s.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise CLIError("several sources match:\n" + "\n".join(f"  {s.kind:8} {s.name}" for s in hits))
    raise CLIError(f"{arg!r} is not a path, or a source listed by `kvctl inbox`")


def build(source, cfg, db, offline: bool) -> tuple[engine.Plan, list[str], Lookup | None]:
    library = engine.library_books(cfg)
    plan = engine.make_plan(source, cfg, library)
    lookup, errors = None, []
    if not offline:
        lookup = Lookup(db)
        errors = engine.auto_match(plan, lookup)
    engine.check_plan(plan, cfg, db, library)
    return plan, errors, lookup


def print_plan(plan: engine.Plan, cfg: dict, args=None) -> None:
    print(paint(plan.name, "bold"), paint(f"({plan.root})", "dim"))
    for b in plan.books:
        who = ", ".join(b.info.authors) or "?"
        if b.skip:
            status = paint("skip", "dim")
        elif b.error:
            status = paint("✗", "red")
        elif b.warning:
            status = paint("!", "yellow")
        else:
            status = paint("✓", "green")
        series = f" [{b.info.series}{f' #{b.info.series_index:g}' if b.info.series_index is not None else ''}]" if b.info.series else ""
        print(f"\n{status} {paint(b.info.title or '(no title)', 'bold')} - {who}{series}")
        print(paint(f"    from {b.source}" + (f", checked with {b.verified}" if b.verified else "")
                    + f"; {', '.join(b.files)}", "dim"))
        if b.dest:
            print(f"    → {os.path.relpath(b.dest, cfg['books_dir'])}")
        if b.error:
            print(paint(f"    ✗ {b.error}", "red"))
        elif b.warning:
            print(paint(f"    ! {b.warning}", "yellow"))
    for e in plan.global_errors:
        print(paint(f"✗ {e}", "red"))
    if plan.series_partners:
        print()
    for f in plan.series_partners:
        print(paint(f"Also adds {f.book.info.title} to {f.series}, as #{f.index:g} (already in the library).", "yellow"))
    delete = engine.files_to_delete(plan, cfg, False if getattr(args, "keep_leftovers", False) else None)
    keep_torrent = getattr(args, "keep_torrent", False)
    remove = engine.will_remove_torrent(plan, cfg, False if keep_torrent else (True if getattr(args, "remove_torrent", False) else None))
    print()
    if engine.other_media(plan):
        print(paint(f"Holds {len(engine.other_media(plan))} other file(s): those stay where they are.", "yellow"))
    skipped = {f for b in plan.books if b.skip for f in b.files}
    for b in engine.kept_duplicates(plan):
        print(paint(f"Keeps {', '.join(b.files)} in the source: not imported, and the library lacks that format.", "yellow"))
    delete_books = [f for f in delete if f in skipped]
    delete_others = [f for f in delete if f not in skipped]
    if delete_books:
        print(paint(f"Moves {len(delete_books)} skipped book file(s) to the holding folder: ", "yellow") + paint(", ".join(delete_books[:6]) + (" …" if len(delete_books) > 6 else ""), "dim"))
    if delete_others:
        print(paint(f"Moves {len(delete_others)} other file(s) to the holding folder: ", "yellow") + paint(", ".join(delete_others[:6]) + (" …" if len(delete_others) > 6 else ""), "dim"))
    elif plan.leftovers:
        print(paint(f"Leaves {len(plan.leftovers)} other file(s) in place.", "dim"))
    if plan.torrent:
        print(paint("Removes the torrent from Transmission (data kept)." if remove else "Keeps the torrent, stopped.", "dim"))


def plan_json(plan: engine.Plan, cfg: dict) -> str:
    return json.dumps({"name": plan.name, "root": plan.root, "errors": plan.global_errors,
                       "delete": engine.files_to_delete(plan, cfg), "remove_torrent": engine.will_remove_torrent(plan, cfg),
                       "series_partners": [{"id": f.book.id, "title": f.book.info.title, "series": f.series, "index": f.index}
                                           for f in plan.series_partners],
                       "books": [{"title": b.info.title, "authors": b.info.authors, "series": b.info.series,
                                  "series_index": b.info.series_index, "files": b.files, "skip": b.skip, "dest": b.dest,
                                  "source": b.source, "verified": b.verified, "duplicate_of": b.duplicate_of,
                                  "error": b.error, "warning": b.warning} for b in plan.books]}, indent=2, ensure_ascii=False)


# -- import ---------------------------------------------------------------------

def cmd_inbox(args, cfg, db) -> int:
    client = connect_transmission(args.json)
    sources, error = engine.list_sources(cfg, client)
    if error:
        print(paint(error, "red"), file=sys.stderr)
    if args.json:
        print(json.dumps([{"kind": s.kind, "name": s.name, "path": s.path, "books": s.books, "size": s.size,
                           "torrent_id": s.torrent["id"] if s.torrent else None} for s in sources], indent=2, ensure_ascii=False))
        return 0
    if not sources:
        print("Nothing to import. Finished torrents with books and anything in "
              f"{', '.join(cfg['inbox_dirs']) or 'a watched folder'} show up here.")
        return 0
    rows = [[str(s.torrent["id"]) if s.torrent else "-", s.kind, str(s.books), fmt.human_size(s.size), s.name] for s in sources]
    print(fmt.table(["ID", "Source", "Files", "Size", "Name"], rows, "><>><", flex=4))
    return 0


def cmd_plan(args, cfg, db) -> int:
    client = connect_transmission(args.json)
    plan, errors, _ = build(find_source(args.source, cfg, client), cfg, db, args.offline)
    for e in errors:
        print(paint(f"lookup: {e}", "yellow"), file=sys.stderr)
    if args.json:
        print(plan_json(plan, cfg))
    else:
        print_plan(plan, cfg)
    return 0 if engine.plan_ok(plan) else 1


def cmd_import(args, cfg, db) -> int:
    client = connect_transmission()
    plan, errors, lookup = build(find_source(args.source, cfg, client), cfg, db, args.offline)
    for e in errors:
        print(paint(f"lookup: {e}", "yellow"), file=sys.stderr)
    print_plan(plan, cfg, args)
    if not engine.plan_ok(plan):
        raise CLIError("fix the problems above (use the UI: `kvctl`) before importing")
    if not confirm(args, "\nImport these books?"):
        print("Aborted.")
        return 1
    remove = False if args.keep_torrent else (True if args.remove_torrent else None)
    res = engine.apply_plan(plan, cfg, db, client, optional_kavita(cfg), lookup, remove_torrent=remove,
                            delete_leftovers=False if args.keep_leftovers else None,
                            log=lambda m: print(paint(m, "dim")))
    for e in res.errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Imported {res.imported} book(s) (operation #{res.op_id}; undo with `kvctl undo {res.op_id}`)")
    return 1 if res.errors else 0


# -- books on disk ----------------------------------------------------------------

def cmd_books(args, cfg, db) -> int:
    books = engine.library_books(cfg)
    if args.search:
        books = [b for b in books if engine.book_matches(b, args.search)]
    if args.author:
        key = calibre.author_key(args.author)
        books = [b for b in books if any(calibre.author_key(a) == key for a in b.info.authors)]
    if args.json:
        print(json.dumps([{"id": b.id, "title": b.info.title, "authors": b.info.authors, "series": b.info.series,
                           "series_index": b.info.series_index, "path": b.path, "files": b.files, "size": b.size,
                           "has_opf": b.has_opf} for b in books], indent=2, ensure_ascii=False))
        return 0
    if not books:
        print("No books found.")
        return 0
    rows = [[str(b.id), b.info.title, ", ".join(b.info.authors), b.info.series,
             ",".join(sorted({os.path.splitext(f)[1][1:] for f in b.files})), fmt.human_size(b.size)] for b in books]
    print(fmt.table(["ID", "Title", "Author", "Series", "Type", "Size"], rows, "><<<<>", flex=1))
    print(paint(f"{len(books)} book(s) · {fmt.human_size(sum(b.size for b in books))}", "dim"))
    return 0


def cmd_authors(args, cfg, db) -> int:
    books = engine.library_books(cfg)
    counts: dict[str, int] = {}
    folders: dict[str, set] = {}
    for b in books:
        for a in b.info.authors[:1]:
            counts[a] = counts.get(a, 0) + 1
            folders.setdefault(a, set()).add(b.author_dir)
    rows = []
    for a in sorted(counts, key=lambda n: calibre.author_sort(calibre.first_last(n)).lower()):
        note = "" if a == calibre.first_last(a) else "not First Last"
        rows.append([a, str(counts[a]), ", ".join(sorted(folders[a])), (note, "yellow")])
    if not rows:
        print("No books found.")
        return 0
    print(fmt.table(["Author", "Books", "Folder", ""], rows, "<><<", flex=2))
    return 0


def print_renames(renames: list[engine.Rename], cfg: dict) -> None:
    for r in renames:
        print(paint(r.book.info.title, "bold"))
        if r.new_author != r.book.info.authors[0]:
            print(f"    author: {r.book.info.authors[0]} -> {paint(r.new_author, 'green')}")
        if r.new_path != r.book.path:
            print(paint(f"    {os.path.relpath(r.book.path, cfg['books_dir'])}", "dim"))
            print(f"    → {os.path.relpath(r.new_path, cfg['books_dir'])}")
        if r.epub_authors:
            print(f"    inside the EPUB: {'; '.join(r.epub_before)} -> {paint('; '.join(next(iter(r.epub_authors.values()))), 'green')}")


def cmd_merge_authors(args, cfg, db) -> int:
    renames = engine.author_merge_plan(cfg)
    if not renames:
        print("Every author is already 'First Last', with one folder and one spelling each.")
        return 0
    print_renames(renames, cfg)
    print(paint(f"\n{len(renames)} book(s) would be renamed.", "bold"))
    if not args.apply:
        print("Nothing changed. Run `kvctl merge-authors --apply` to do it.")
        return 0
    if not confirm(args, "Rename these books?"):
        print("Aborted.")
        return 1
    res = engine.apply_renames(renames, cfg, db, optional_kavita(cfg), log=lambda m: print(paint(m, "dim")))
    for e in res.errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Renamed {res.imported} book(s) (operation #{res.op_id}; undo with `kvctl undo {res.op_id}`)")
    if res.scanned:
        print(res.scanned)
    return 1 if res.errors else 0


def cmd_fix_series(args, cfg, db) -> int:
    fixes = engine.series_fix_plan(cfg)
    if not fixes:
        print("No books need grouping: Kavita can keep every series together.")
        return 0
    current = None
    for f in fixes:
        if f.series != current:
            current = f.series
            print(paint(f.series, "bold"))
        print(f"    #{f.index:<4g} {f.book.info.title}" + (paint("  (number guessed)", "yellow") if f.number_guessed else ""))
    print(paint(f"\n{len(fixes)} book(s) would be grouped.", "bold"))
    if not args.apply:
        print("Nothing changed. Run `kvctl fix-series --apply` to do it.")
        return 0
    if not confirm(args, "Write these series into the books?"):
        print("Aborted.")
        return 1
    res = engine.apply_series(fixes, cfg, db, optional_kavita(cfg), log=lambda m: print(paint(m, "dim")))
    for e in res.errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Grouped {res.imported} book(s) (operation #{res.op_id}; undo with `kvctl undo {res.op_id}`)")
    if res.scanned:
        print(res.scanned)
    return 1 if res.errors else 0


def cmd_history(args, cfg, db) -> int:
    ops = db.operations()
    if not ops:
        print("Nothing imported yet.")
        return 0
    rows = [[str(o["id"]), o["created"][:16], o["kind"], (o["status"], {"undone": "dim", "failed": "red"}.get(o["status"], "green")),
             str(o["books"] or 0), o["source_name"] or ""] for o in ops]
    print(fmt.table(["Op", "When", "Kind", "Status", "Books", "Source"], rows, "><<<><", flex=5))
    return 0


def cmd_undo(args, cfg, db) -> int:
    op = db.operation(args.op_id)
    if not op:
        raise CLIError(f"no operation {args.op_id}")
    if op["leftovers_deleted"]:
        print(paint("This older import deleted its leftover files, and those can't be restored.", "yellow"))
    if not confirm(args, f"Undo operation {args.op_id} ({op['kind']}: {op['source_name']})?"):
        return 1
    client = connect_transmission() if args.readd else None
    done, errors = engine.undo_operation(args.op_id, cfg, db, client, optional_kavita(cfg), args.readd)
    for e in errors:
        print(paint(f"✗ {e}", "red"), file=sys.stderr)
    print(f"Undid {done} change(s).")
    return 1 if errors else 0


# -- Kavita ---------------------------------------------------------------------

def library_rows(libs: list[dict]) -> list[list]:
    return [[str(l.get("id")), l.get("name") or "", LIBRARY_TYPES.get(l.get("type"), "?"), ", ".join(l.get("folders") or []),
             (l.get("lastScanned") or "-")[:16].replace("T", " ")] for l in libs]


def cmd_libraries(args, cfg, db) -> int:
    libs = kavita_client(cfg).libraries()
    if args.json:
        print(json.dumps(libs, indent=2, ensure_ascii=False))
        return 0
    if not libs:
        print("Kavita has no libraries yet. Create one with `kvctl library create NAME FOLDER`.")
        return 0
    print(fmt.table(["ID", "Name", "Type", "Folders", "Last scan"], library_rows(libs), "><<<<", flex=3))
    return 0


def cmd_library(args, cfg, db) -> int:
    client = kavita_client(cfg)
    if args.action == "create":
        folders = [os.path.abspath(os.path.expanduser(f)) for f in args.folders]
        client.create_library(args.name, folders, BOOK_LIBRARY)
        print(f"Created {args.name!r} ({', '.join(folders)})")
        return 0
    lib = find_library(client, args.library)
    if args.action == "rename":
        client.update_library(lib, name=args.name)
        print(f"Renamed {lib['name']!r} to {args.name!r}")
    elif args.action == "folders":
        folders = list(lib.get("folders") or [])
        for f in args.add or []:
            path = os.path.abspath(os.path.expanduser(f))
            if path not in folders:
                folders.append(path)
        for f in args.remove or []:
            path = os.path.abspath(os.path.expanduser(f))
            if path not in folders:
                raise CLIError(f"{path} is not in {lib['name']!r}")
            folders.remove(path)
        if not folders:
            raise CLIError("a library needs at least one folder")
        if args.add or args.remove:
            client.update_library(lib, folders=folders)
        print("\n".join(folders))
    elif args.action == "delete":
        if not confirm(args, f"Delete library {lib['name']!r} from Kavita? Book files stay, reading progress is lost."):
            return 1
        client.delete_library(lib["id"])
        print(f"Deleted {lib['name']!r} from Kavita; the files on disk are untouched.")
    elif args.action == "scan":
        client.scan(lib["id"], args.force)
        print(f"Kavita is scanning {lib['name']!r}" + (" (forced)" if args.force else ""))
    return 0


def cmd_scan_all(args, cfg, db) -> int:
    kavita_client(cfg).scan_all(args.force)
    print("Kavita is scanning every library" + (" (forced)" if args.force else ""))
    return 0


def cmd_series(args, cfg, db) -> int:
    client = kavita_client(cfg)
    lib = find_library(client, args.library) if args.library else None
    series = client.all_series(lib["id"] if lib else None)
    if args.json:
        print(json.dumps(series, indent=2, ensure_ascii=False))
        return 0
    if not series:
        print("No series.")
        return 0
    rows = [[str(s.get("id")), s.get("name") or "", str(s.get("pages") or 0), s.get("libraryName") or ""] for s in series]
    print(fmt.table(["ID", "Name", "Pages", "Library"], rows, "><><", flex=1))
    return 0


def cmd_search(args, cfg, db) -> int:
    if args.online:
        lookup = Lookup(db)
        results = lookup.search(args.query, [args.author] if args.author else [])
        for e in lookup.errors:
            print(paint(e, "yellow"), file=sys.stderr)
        if args.json:
            print(json.dumps([vars(m) for m in results], indent=2, ensure_ascii=False))
            return 0
        if not results:
            print("No matches.")
        for m in results[:10]:
            print(f"{m.score:.2f}  {paint(m.label(), 'bold')}  {paint(m.isbn or m.key, 'dim')}")
        return 0
    found = kavita_client(cfg).search(args.query)
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
        return 0
    series = found.get("series") or []
    people = found.get("persons") or []
    if not series and not people:
        print("No matches in Kavita.")
    for s in series:
        print(f"{paint(s.get('name') or '?', 'bold')}  {paint(s.get('libraryName') or '', 'dim')}")
    for person in people:
        print(f"{person.get('name') or '?'}  {paint('person', 'dim')}")
    return 0


def cmd_status(args, cfg, db) -> int:
    ok = paint("✓", "green")
    bad = paint("✗", "red")
    client = kavita_client(cfg)
    code = 0
    try:
        client.healthy()
        print(f"{ok} Kavita at {cfg['url']}")
    except KavitaError as e:
        print(f"{bad} {e}")
        code = 1
    if code == 0:
        try:
            info = client.info()
            libs = client.libraries()
            print(f"{ok} API key works; Kavita {info.get('kavitaVersion', '?')}; {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}")
            target = next((l for l in libs if l.get("id") == cfg["library_id"]), None) or client.library_for_folder(cfg["books_dir"])
            if target:
                print(f"{ok} Imports scan library {target['name']!r}")
            else:
                print(f"{bad} No Kavita library contains {cfg['books_dir']}")
        except AuthError as e:
            print(f"{bad} {e}")
            code = 1
        except KavitaError as e:
            print(f"{bad} {e}")
            code = 1
    books = engine.library_books(cfg)
    print(f"{ok if os.path.isdir(cfg['books_dir']) else bad} Books folder {cfg['books_dir']}: {len(books)} books, next id {engine.next_id(cfg, db, books)}")
    for d in cfg["inbox_dirs"]:
        print(f"{ok if os.path.isdir(d) else bad} Watched folder {d}" + ("" if os.path.isdir(d) else " is missing (`kvctl setup` creates it)"))
    print(f"{ok if connect_transmission(True) else bad} Transmission")
    return code


# -- settings -------------------------------------------------------------------

def cmd_setup(args, cfg, db) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError("setup needs a terminal, so the API key is never passed on a command line")
    print("In Kavita, open your user settings, then API Key / OPDS, and copy the key. It needs an admin account.")
    url = input(f"Kavita address [{cfg['url']}]: ").strip() or cfg["url"]
    prompt = "API key (hidden; Enter keeps the saved one): " if cfg["api_key"] else "API key (hidden): "
    key = getpass.getpass(prompt).strip() or cfg["api_key"]
    if not key:
        raise CLIError("no API key entered; nothing saved")
    client = Kavita(url, key)
    libs = client.libraries()     # a bad key or address stops here, before anything is saved
    print(paint(f"✓ Connected: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}", "green"))
    cfg["url"], cfg["api_key"] = url, key
    path = config.save(cfg)
    print(f"Saved {path} (only you can read it)")
    for d in cfg["inbox_dirs"]:
        try:
            os.makedirs(d, exist_ok=True)
            print(f"Watched folder: {d}")
        except OSError as e:
            print(paint(f"Can't create {d}: {e.strerror}", "yellow"), file=sys.stderr)
    if not client.library_for_folder(cfg["books_dir"]):
        print(paint(f"No Kavita library contains {cfg['books_dir']} yet; "
                    f"`kvctl library create Books {cfg['books_dir']}` adds one.", "yellow"))
    return 0


def cmd_config(args, cfg, db) -> int:
    if args.action == "set":
        if not args.key or args.value is None:
            raise CLIError("usage: kvctl config set KEY VALUE")
        if args.key == "api_key":
            raise CLIError("use `kvctl setup` for the API key, so it stays out of your shell history")
        if args.key not in config.DEFAULTS:
            raise CLIError(f"unknown key {args.key!r} (one of: {', '.join(config.DEFAULTS)})")
        try:
            cfg[args.key] = config.parse_value(args.key, args.value)
        except ValueError:
            raise CLIError(f"{args.key} must be a whole number") from None
        print(f"Saved {config.save(cfg)}")
        return 0
    for k in config.DEFAULTS:
        v = cfg.get(k)
        if k == "api_key":
            v = "set" if v else "not set (run `kvctl setup`)"
        elif k in config.LIST_KEYS:
            v = ", ".join(v) or "-"
        print(f"{k:18} {v}")
    print(paint(f"\n{config.config_path()}", "dim"))
    return 0


def cmd_ui(args, cfg, db) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError("the interactive UI needs a terminal; see `kvctl -h` for commands")
    from . import tui
    return tui.run(cfg, db, connect_transmission(True), start_source=args.source, offline=args.offline)


def cmd_read(args, cfg, db) -> int:
    """Hand over to bookread: this library's matching book if there's one, otherwise its menu of this library."""
    path, title, query = None, "", ""
    if args.book:
        books = engine.library_books(cfg)
        hits = [b for b in books if str(b.id) == args.book] or [b for b in books if engine.book_matches(b, args.book)]
        if not hits:
            raise CLIError(f"no book matches {args.book!r} (see `kvctl books`)")
        if len(hits) == 1:
            path, title = engine.book_to_read(hits[0]), hits[0].info.title
        else:
            query = args.book
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CLIError("reading needs a terminal")
    import bookread.config
    import bookread.reader
    from bookread.store import Store
    try:
        store = Store(bookread.config.load()["database"])
    except (OSError, sqlite3.Error) as e:
        raise CLIError(f"can't open the reading database: {e}") from None
    try:
        problem = bookread.reader.run(store, cfg["books_dir"], query, path, title)
    finally:
        store.close()
    if problem:
        raise CLIError(problem)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kvctl", description="Manage a Kavita server and import books in Calibre's folder layout.")
    p.add_argument("--version", action="version", version=f"kvctl {__version__}")
    p.add_argument("--url", help="Kavita address (default: from the config)")
    p.add_argument("--offline", action="store_true", help="don't look books up online")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help):
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    sp = add("ui", cmd_ui, "Interactive curses interface (default)")
    sp.add_argument("source", nargs="?", help="open this torrent or folder for review straight away")
    sp = add("read", cmd_read, "Read a book in the terminal; with no book, choose from a list")
    sp.add_argument("book", nargs="?", help="a library id, or words from the title, author or series")
    add("setup", cmd_setup, "Enter the Kavita address and API key, test them, and create the watched folder")
    add("status", cmd_status, "Check Kavita, the API key, the folders and Transmission")
    sp = add("libraries", cmd_libraries, "List Kavita libraries")
    sp.add_argument("--json", action="store_true")

    sp = add("library", cmd_library, "Create, rename, re-folder, delete or scan a Kavita library")
    actions = sp.add_subparsers(dest="action", metavar="ACTION", required=True)
    a = actions.add_parser("create", help="create a Book library")
    a.add_argument("name")
    a.add_argument("folders", nargs="+", metavar="FOLDER")
    a = actions.add_parser("rename", help="rename a library")
    a.add_argument("library", help="id or name")
    a.add_argument("name")
    a = actions.add_parser("folders", help="show or change a library's folders")
    a.add_argument("library", help="id or name")
    a.add_argument("--add", action="append", metavar="FOLDER")
    a.add_argument("--remove", action="append", metavar="FOLDER")
    a = actions.add_parser("delete", help="delete a library from Kavita (files stay on disk)")
    a.add_argument("library", help="id or name")
    a.add_argument("-y", "--yes", action="store_true")
    a = actions.add_parser("scan", help="scan a library now")
    a.add_argument("library", help="id or name")
    a.add_argument("--force", action="store_true", help="rescan unchanged files too")

    sp = add("scan-all", cmd_scan_all, "Scan every Kavita library")
    sp.add_argument("--force", action="store_true")
    sp = add("series", cmd_series, "List series in Kavita")
    sp.add_argument("library", nargs="?", help="id or name")
    sp.add_argument("--json", action="store_true")
    sp = add("search", cmd_search, "Search Kavita, or Open Library with --online")
    sp.add_argument("query")
    sp.add_argument("--online", action="store_true", help="search Open Library instead")
    sp.add_argument("--author", help="narrow an --online search")
    sp.add_argument("--json", action="store_true")
    sp = add("inbox", cmd_inbox, "List finished torrents and watched-folder items that hold books")
    sp.add_argument("--json", action="store_true")
    sp = add("plan", cmd_plan, "Show how a torrent or folder would be imported (changes nothing)")
    sp.add_argument("source", help="torrent id, hash or name, an inbox name, or a path")
    sp.add_argument("--json", action="store_true")
    sp = add("import", cmd_import, "Import a torrent or folder into the Books folder")
    sp.add_argument("source", help="torrent id, hash or name, an inbox name, or a path")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--keep-torrent", action="store_true", help="don't remove the torrent from Transmission")
    g.add_argument("--remove-torrent", action="store_true", help="remove it even if it holds other files")
    sp.add_argument("--keep-leftovers", action="store_true", help="leave other files in the source")
    sp = add("books", cmd_books, "List books in the Books folder")
    sp.add_argument("-q", "--search", help="words in the title, author or series")
    sp.add_argument("--author", help="only this author, in any name order")
    sp.add_argument("--json", action="store_true")
    add("authors", cmd_authors, "List authors and their folders")
    sp = add("merge-authors", cmd_merge_authors, "Show or apply the plan to make every author 'First Last' with one folder")
    sp.add_argument("--apply", action="store_true", help="rename the books after showing the plan")
    sp.add_argument("-y", "--yes", action="store_true")
    sp = add("fix-series", cmd_fix_series, "Show or apply the plan to group books Kavita would show as separate series")
    sp.add_argument("--apply", action="store_true", help="write the series into the books after showing the plan")
    sp.add_argument("-y", "--yes", action="store_true")
    add("history", cmd_history, "List imports, merges and edits")
    sp = add("undo", cmd_undo, "Reverse an import, merge or edit")
    sp.add_argument("op_id", type=int)
    sp.add_argument("--readd", action="store_true", help="also re-add the torrent to Transmission")
    sp.add_argument("-y", "--yes", action="store_true")
    sp = add("config", cmd_config, "Show settings, or `config set KEY VALUE`")
    sp.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(argv + ["ui"])
    fmt.paint.enabled = args.color == "always" or (args.color == "auto" and sys.stdout.isatty()
                                                   and not os.environ.get("NO_COLOR"))
    try:
        cfg = config.load()
        if args.url:
            cfg["url"] = args.url
        args.offline = args.offline or not cfg["online"]
        db = DB(cfg["database"])
        return args.func(args, cfg, db) or 0
    except (CLIError, engine.PlanError, KavitaError, config.tomllib.TOMLDecodeError) as e:
        print(f"kvctl: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
