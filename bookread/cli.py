"""`bookread`: a menu of books, or straight into the book you name. `--list` is for scripts."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tomllib

from kutil import fmt
from kutil.fmt import paint

from . import __version__, config, library
from .pace import Pace
from .store import Store


class CLIError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookread", description="Read e-books in the terminal, with progress and a prediction of the time left.",
                                epilog="With no BOOK, shows a menu of the books in the folder.")
    p.add_argument("book", nargs="?", metavar="BOOK", help="an EPUB file, a folder to list, or words from a title, author or series")
    p.add_argument("--dir", metavar="FOLDER", help="the folder of books (default: from the config)")
    p.add_argument("--list", action="store_true", help="print the books and your progress instead of opening anything")
    p.add_argument("--json", action="store_true", help="with --list, print JSON")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--version", action="version", version=f"bookread {__version__}")
    return p


def print_list(entries: list, store: Store, as_json: bool) -> None:
    from .reader import progress_label
    progress, pace, lengths = store.reading_all(), Pace.load(store), store.lengths()
    if as_json:
        out = []
        for e in entries:
            row = progress.get(e.key)
            words = row["words"] - row["position"] if row else lengths.get(e.key)
            out.append({"title": e.title, "authors": e.authors, "series": e.series, "series_index": e.series_index, "path": e.path,
                        "readable": e.readable, "percent": row["percent"] if row else None,
                        "seconds_left": 0 if row and row["percent"] >= 1 else round(pace.seconds_for(words)) if words is not None else None})
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not entries:
        print("No books found.")
        return
    rows = []
    for e in entries:
        read, left = progress_label(progress.get(e.key), pace, lengths.get(e.key)) if e.readable else (e.format, "")
        rows.append([e.title, ", ".join(e.authors), read, left])
    print(fmt.table(["Title", "Author", "Read", "Left"], rows, "<<>>", flex=0))


def run(args) -> int:
    cfg = config.load()
    folder = os.path.abspath(os.path.expanduser(args.dir)) if args.dir else cfg["books_dir"]
    path, query = None, ""
    if args.book:
        given = os.path.abspath(os.path.expanduser(args.book))
        if os.path.isfile(given):
            path = given
        elif os.path.isdir(given):
            folder = given
        else:
            query = args.book
    store = Store(cfg["database"])
    try:
        if args.list:
            entries = [library.read_entry(path)] if path else [e for e in library.scan(folder) if library.matches(e, query)]
            print_list(entries, store, args.json)
            return 0
        if path and not library.is_readable(path):
            raise CLIError(f"the reader opens EPUB books; {os.path.basename(path)} isn't one")
        if query:
            hits = [e for e in library.scan(folder) if library.matches(e, query)]
            if not hits:
                raise CLIError(f"no book matches {query!r} in {folder}")
            if len(hits) == 1:
                path = hits[0].path
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise CLIError("reading needs a terminal; `bookread --list` prints the books")
        from . import reader
        problem = reader.run(store, folder, query, path)
        if problem:
            raise CLIError(problem)
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")   # a path may not be valid text, and the terminal may not be UTF-8
    fmt.paint.enabled = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    try:
        return run(args)
    except BrokenPipeError:
        return 0
    except (CLIError, tomllib.TOMLDecodeError, OSError, sqlite3.Error) as e:
        print(f"bookread: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
