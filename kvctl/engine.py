"""Inbox, plans, importing, library listing, author merging, editing and undo."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections import defaultdict
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from kutil.fsops import MoveRefused, move_file, permission_hint, remove_empty_dirs
from kutil.text import normalize

from . import calibre, epub, series
from .calibre import BookInfo, author_key, first_last, read_opf, write_opf
from .db import DB
from .metadata import book_ext, is_book, read_book
from .providers import BookMatch, Lookup

TORRENT_FIELDS = ["id", "name", "hashString", "downloadDir", "percentDone", "leftUntilDone", "sizeWhenDone",
                  "doneDate", "status", "magnetLink", "metadataPercentComplete"]
OTHER_MEDIA = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
GB = 1024 ** 3


class PlanError(MoveRefused):
    pass


def split_authors(names: list[str]) -> list[str]:
    """One field often holds several people: 'Kugane Maruyama & so-bin', 'Carlo Zen and Shinobu Shinotsuki'."""
    out: list[str] = []
    for name in names:
        for part in re.split(r"\s*(?:;|&|\band\b)\s*", name):
            part = first_last(part.strip())
            if part and part not in out:
                out.append(part)
    return out


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------

@dataclass
class Source:
    kind: str   # torrent | folder (inside a watched folder) | path (named by hand: its other files are not ours to delete)
    name: str
    path: str
    books: int = 0
    size: int = 0
    done_date: int = 0
    torrent: dict | None = None


def scan_files(root: str) -> list[tuple[str, int]]:
    if os.path.isfile(root):
        return [(os.path.basename(root), os.path.getsize(root))]
    out = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            try:
                if not os.path.islink(p):   # a link could point anywhere; it is neither moved nor deleted
                    out.append((os.path.relpath(p, root), os.lstat(p).st_size))
            except OSError:
                pass
    return out


def _count_books(path: str) -> tuple[int, int]:
    files = scan_files(path)
    return sum(1 for rel, _ in files if is_book(rel)), sum(size for _, size in files)


def list_sources(cfg: dict, transmission=None) -> tuple[list[Source], str]:
    """Finished torrents that contain books, plus everything with books in the watched folders."""
    sources, error, seen = [], "", set()
    if transmission is not None:
        try:
            for t in transmission.torrent_get(TORRENT_FIELDS):
                path = os.path.join(t["downloadDir"], t["name"])
                seen.add(path)
                if t.get("metadataPercentComplete", 1) < 1 or t["leftUntilDone"] != 0 or not os.path.exists(path):
                    continue
                n, size = _count_books(path)
                if n:
                    sources.append(Source("torrent", t["name"], path, n, size, t.get("doneDate") or 0, t))
        except Exception as e:  # RPCError and friends: the watched folders still work
            error = f"Transmission: {e}"
    for d in cfg.get("inbox_dirs") or []:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            if name.startswith(".") or path in seen:
                continue
            n, size = _count_books(path)
            if n:
                sources.append(Source("folder", name, path, n, size, int(os.stat(path).st_mtime)))
    sources.sort(key=lambda s: -s.done_date)
    return sources, error


def source_for_path(path: str) -> Source:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise PlanError(f"{path} does not exist")
    n, size = _count_books(path)
    return Source("path", os.path.basename(path.rstrip("/")), path, n, size)


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------

@dataclass
class LibBook:
    id: int
    path: str
    author_dir: str
    info: BookInfo
    files: list[str]
    size: int
    has_opf: bool = True


def book_matches(book: LibBook, query: str) -> bool:
    """Every word of the query is in the title, an author or the series."""
    text = " ".join([book.info.title, *book.info.authors, book.info.series]).lower()
    return all(word in text for word in query.lower().split())


def book_to_read(book: LibBook) -> str:
    """The file to open in the reader: the EPUB if there's one among the formats."""
    names = sorted(book.files, key=lambda n: not n.lower().endswith((".epub", ".kepub")))
    return os.path.join(book.path, names[0]) if names else book.path


def library_books(cfg: dict) -> list[LibBook]:
    root = cfg["books_dir"]
    books = []
    if not os.path.isdir(root):
        return books
    for a in sorted(os.listdir(root)):
        ap = os.path.join(root, a)
        if a.startswith(".") or not os.path.isdir(ap):
            continue
        for f in sorted(os.listdir(ap)):
            p = os.path.join(ap, f)
            if not os.path.isdir(p):
                continue
            opf = os.path.join(p, "metadata.opf")
            m = re.search(r"\((\d+)\)$", f)
            try:
                info, bid = read_opf(opf)
                has = True
            except (OSError, calibre.ET.ParseError):
                info, bid, has = BookInfo(title=re.sub(r" \(\d+\)$", "", f), authors=[a]), None, False
            bid = bid if bid is not None else (int(m.group(1)) if m else 0)
            names = [x for x in os.listdir(p) if is_book(x)]
            size = sum(os.path.getsize(os.path.join(p, x)) for x in names)
            books.append(LibBook(bid, p, a, info, sorted(names), size, has))
    return books


def next_id(cfg: dict, db: DB, books: list[LibBook] | None = None) -> int:
    """Ids are never reused, even after a book is removed."""
    books = library_books(cfg) if books is None else books
    return max([b.id for b in books] + [db.high_water(), 0]) + 1


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

@dataclass
class Book:
    files: list[str]
    info: BookInfo
    sizes: int = 0
    cover: bytes | None = None
    cover_ext: str = ""
    source: str = ""
    skip: bool = False
    verified: str = ""
    cover_id: int | None = None
    duplicate_of: int | None = None
    sidecars: list[str] = field(default_factory=list)
    series_guess: tuple | None = None       # (series, number) filled in from the title, until someone changes it
    book_id: int | None = None
    dest: str = ""
    error: str = ""
    warning: str = ""


@dataclass
class Plan:
    name: str
    root: str
    books: list[Book]
    leftovers: list[tuple[str, int]]
    torrent: dict | None = None
    incomplete: list[str] = field(default_factory=list)
    global_errors: list[str] = field(default_factory=list)
    kind: str = "folder"
    series_partners: list[SeriesFix] = field(default_factory=list)   # library books that join a new book's series


def _key(info: BookInfo) -> tuple:
    return normalize(info.title), author_key(info.authors[0]) if info.authors else ""


def _stem(rel: str) -> str:
    name = os.path.basename(rel)
    return name[:-len(book_ext(rel))]


def make_plan(source: Source, cfg: dict, library: list[LibBook] | None = None) -> Plan:
    if not os.path.exists(source.path):
        raise PlanError(f"{source.path} not found")
    is_file = os.path.isfile(source.path)
    base = os.path.dirname(source.path) if is_file else source.path
    files = scan_files(source.path)
    incomplete = [rel for rel, _ in files if rel.endswith(".part")]
    book_files = [(rel, size) for rel, size in files if is_book(rel)]
    stems: dict[str, set[str]] = defaultdict(set)
    for rel, _ in book_files:
        stems[os.path.dirname(rel)].add(_stem(rel))
    used: set[str] = set()
    grouped: dict[tuple, Book] = {}
    order: list[Book] = []
    for rel, size in book_files:
        info, cover, cover_ext, origin = read_book(os.path.join(base, rel))
        folder = os.path.dirname(rel)
        sidecars = []
        # A Calibre export carries its own metadata and cover. Those only describe this book if the folder holds
        # one book (possibly in several formats); a cover.jpg beside fifty books belongs to none of them.
        if not is_file and len(stems[folder]) == 1:
            opf = os.path.join(base, folder, "metadata.opf")
            if os.path.isfile(opf):
                try:
                    side, _ = read_opf(opf)
                    if side.title:
                        side.authors = side.authors or info.authors
                        info, origin = side, "metadata.opf"
                        sidecars.append(os.path.join(folder, "metadata.opf"))
                except (OSError, calibre.ET.ParseError):
                    pass
            img = os.path.join(base, folder, "cover.jpg")
            if os.path.isfile(img):
                with open(img, "rb") as f:
                    cover, cover_ext = f.read(30 * 1024 * 1024), ".jpg"
                sidecars.append(os.path.join(folder, "cover.jpg"))
        info.authors = split_authors(info.authors)
        key = _key(info) if info.title else ("", "", rel)   # no title: nothing says two files are the same book
        existing = grouped.get(key)
        if existing and all(book_ext(x) != book_ext(rel) for x in existing.files):
            existing.files.append(rel)           # another format of the same book
            existing.sizes += size
            existing.sidecars += [s for s in sidecars if s not in existing.sidecars]
            existing.cover = existing.cover or cover
            existing.cover_ext = existing.cover_ext or cover_ext
        else:
            b = Book([rel], info, size, cover, cover_ext, origin, sidecars=sidecars)
            order.append(b)
            grouped.setdefault(key, b)
        used.update([rel, *sidecars])
    leftovers = [(rel, size) for rel, size in files if rel not in used and not rel.endswith(".part")]
    plan = Plan(source.name, source.path, order, leftovers, source.torrent, incomplete, kind=source.kind)
    library = library_books(cfg) if library is None else library
    known = {_key(b.info): b.id for b in library}
    for b in plan.books:
        dup = known.get(_key(b.info))
        if dup is not None:
            b.duplicate_of, b.skip = dup, True   # already in the library: off by default, Space includes it
    suggest_series(plan, library)
    return plan


def _series_item(key, info: BookInfo) -> series.Item:
    return series.Item(key, author_key(info.authors[0]) if info.authors else "", info.title, info.series, info.series_index, info.pubdate)


def suggest_series(plan: Plan, library: list[LibBook]) -> None:
    """Give a new book the series its title states, if that joins it to another book by the same author."""
    items = [_series_item(("lib", b.id), b.info) for b in library] + [_series_item(id(b), b.info) for b in plan.books]
    found = series.assign(items)
    for b in plan.books:
        a = found.get(id(b))
        if a:
            b.info.series, b.info.series_index = a.series, a.index
            b.series_guess = (a.series, a.index)


def apply_match(book: Book, match: BookMatch, overwrite: bool = False) -> None:
    """Automatic checks only fill gaps. A match the user chose may also replace the title and authors."""
    info = book.info
    if overwrite or not info.title:
        info.title = match.title
    if (overwrite or not info.authors) and match.authors:
        info.authors = split_authors(match.authors)
    if match.year and not info.pubdate:
        info.pubdate = f"{match.year}-01-01T00:00:00+00:00"
    # Open Library's results are per work, so its ISBN and publisher belong to an arbitrary edition: not worth copying.
    book.cover_id = match.cover_id
    book.verified = match.provider


def auto_match(plan: Plan, lookup: Lookup, progress=None) -> list[str]:
    todo = [b for b in plan.books if not b.verified and not b.skip]
    errors: set[str] = set()

    def work(b):
        try:
            return b, lookup.best(b.info.title, b.info.authors), list(lookup.errors)
        except Exception as e:  # a lookup problem must never stop an import
            return b, None, [str(e)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for n, (b, match, errs) in enumerate(pool.map(work, todo), start=1):
            errors.update(errs)
            if match:
                apply_match(b, match)
            if progress:
                progress(n, len(todo), b.info.title)
    return sorted(errors)


def check_plan(plan: Plan, cfg: dict, db: DB, library: list[LibBook] | None = None) -> None:
    """Assign ids and destinations, and record what's wrong on each book."""
    library = library_books(cfg) if library is None else library
    book_id = next_id(cfg, db, library)
    known = {_key(b.info): b.id for b in library}
    plan.series_partners = _series_partners(plan, library)
    plan.global_errors = []
    if plan.incomplete:
        plan.global_errors.append(f"{len(plan.incomplete)} file(s) are still incomplete (.part)")
    seen: set[str] = set()
    keys: set[tuple] = set()
    need = 0
    for b in plan.books:
        b.error = b.warning = ""
        b.duplicate_of = known.get(_key(b.info))     # an edit can make a book a duplicate, or stop it being one
        if b.skip:
            b.dest = ""
            if b.duplicate_of is not None:
                b.warning = f"already in the library (id {b.duplicate_of})"
            continue
        if not b.info.title.strip():
            b.error = "no title"
            continue
        if not b.info.authors:
            b.error = "no author"
            continue
        b.book_id = book_id
        book_id += 1
        b.dest = os.path.join(cfg["books_dir"], calibre.book_path(b.info.title, b.info.authors[0], b.book_id))
        stem = calibre.file_stem(b.info.title, b.info.authors[0])
        for rel in b.files:
            dst = os.path.join(b.dest, stem + book_ext(rel))
            if dst in seen:
                b.error = "two files would get the same name"
            seen.add(dst)
        if b.duplicate_of is not None:
            b.warning = f"importing a second copy of id {b.duplicate_of}"
        elif _key(b.info) in keys:
            b.warning = "same title and author as another book here"
        elif not b.verified:
            b.warning = "not found online; using the book's own metadata"
        if b.series_guess and b.series_guess == (b.info.series, b.info.series_index):
            b.warning = "; ".join(x for x in (b.warning, "series and number taken from the title") if x)
        keys.add(_key(b.info))
        need += b.sizes
    try:
        same_drive = os.stat(plan.root).st_dev == os.stat(_anchor(cfg["books_dir"])).st_dev
        free = shutil.disk_usage(_anchor(cfg["books_dir"])).free
        if not same_drive and need > free - int(cfg.get("min_free_gb", 2)) * GB:
            plan.global_errors.append(f"not enough room in {cfg['books_dir']}: needs {need // 2 ** 20} MiB")
    except OSError:
        pass


def _anchor(path: str) -> str:
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def plan_ok(plan: Plan) -> bool:
    return not plan.global_errors and not any(b.error for b in plan.books)


def other_media(plan: Plan) -> list[str]:
    """Videos or music in the source: never delete those or drop the torrent without being told to."""
    return [rel for rel, _ in plan.leftovers if os.path.splitext(rel)[1].lower() in OTHER_MEDIA]


def will_remove_torrent(plan: Plan, cfg: dict, remove_torrent: bool | None = None) -> bool:
    """A mixed torrent stays unless explicitly asked for: its videos or music still need sorting."""
    if not plan.torrent:
        return False
    if remove_torrent is not None:
        return remove_torrent
    return bool(cfg["remove_torrent"]) and not other_media(plan)


def files_to_delete(plan: Plan, cfg: dict, delete_leftovers: bool | None = None) -> list[str]:
    """Source files an import deletes, relative to the source. Nothing from a path named by hand or a mixed torrent;
    skipped books go with the rest, as skipped files do in plexsort."""
    delete_leftovers = cfg["delete_leftovers"] if delete_leftovers is None else delete_leftovers
    if not delete_leftovers or plan.kind == "path" or os.path.isfile(plan.root) or other_media(plan):
        return []
    junk = [rel for rel, _ in plan.leftovers]
    for b in plan.books:
        junk += b.sidecars + (b.files if b.skip else [])
    return list(dict.fromkeys(junk))


@dataclass
class Result:
    op_id: int
    imported: int = 0
    errors: list = field(default_factory=list)
    torrent_removed: bool = False
    leftovers_deleted: int = 0
    scanned: str = ""


def scan_kavita(cfg: dict, kavita) -> str:
    """Ask Kavita to pick up changes. Returns a short status; failure here never undoes an import."""
    if kavita is None or not cfg.get("scan_after_import", True):
        return ""
    try:
        lib_id = cfg.get("library_id") or 0
        if not lib_id:
            lib = kavita.library_for_folder(cfg["books_dir"])
            lib_id = lib["id"] if lib else 0
        if not lib_id:
            return f"no Kavita library contains {cfg['books_dir']}"
        # Always forced. A normal scan skips folders that look unchanged, and Kavita records a one-book series' home as
        # that book's own folder. So when a second book joins from another folder, a normal scan reads only the new one,
        # then removes the first book's volume as missing, though its file is still there.
        kavita.scan(lib_id, force=True)
        return "Kavita is scanning"
    except Exception as e:
        return f"Kavita scan failed: {e}"


def apply_plan(plan: Plan, cfg: dict, db: DB, transmission=None, kavita=None, lookup: Lookup | None = None,
               remove_torrent: bool | None = None, delete_leftovers: bool | None = None, log=None) -> Result:
    log = log or (lambda m: None)
    remove_torrent = will_remove_torrent(plan, cfg, remove_torrent)
    junk = files_to_delete(plan, cfg, delete_leftovers)
    if all(b.skip for b in plan.books):
        raise PlanError("nothing to import: every book is skipped")
    library = library_books(cfg)
    people = lru_cache(maxsize=1)(lambda: _read_people(library))
    check_plan(plan, cfg, db, library)
    if not plan_ok(plan):
        problems = plan.global_errors + [f"{b.info.title or b.files[0]}: {b.error}" for b in plan.books if b.error]
        raise PlanError("plan has problems: " + "; ".join(problems)[:400])
    single = os.path.isfile(plan.root)
    base = os.path.dirname(plan.root) if single else plan.root
    torrent = plan.torrent
    if torrent and transmission is not None:
        try:
            transmission.torrent_action("stop", [torrent["id"]])
            log("Stopped torrent")
        except Exception as e:
            raise PlanError(f"could not stop the torrent: {e}") from None
    op = db.begin("import", plan.name, plan.root, torrent)
    res = Result(op)
    try:
        for b in plan.books:
            if b.skip:
                continue
            stem = calibre.file_stem(b.info.title, b.info.authors[0])
            for rel in b.files:
                src = plan.root if single else os.path.join(base, rel)
                dst = os.path.join(b.dest, stem + book_ext(rel))
                move_file(src, dst)
                db.record(op, "moved", dst, src, book_id=b.book_id, title=b.info.title)
                problem = _write_epub(dst, op, db, b.book_id, b.info.title, authors=_import_authors(dst, b.info.authors, people),
                                      series_name=b.info.series or None, index=b.info.series_index)
                if problem:
                    res.errors.append(problem)
            if b.cover is None and b.cover_id and lookup is not None:
                b.cover = lookup.cover(b.cover_id)
                b.cover_ext = ".jpg" if b.cover else ""
            cover_name = None
            if b.cover and b.cover_ext:      # an image type we can't name correctly stays inside the book
                cover_name = "cover" + b.cover_ext
                cover_path = os.path.join(b.dest, cover_name)
                with open(cover_path, "wb") as f:
                    f.write(b.cover)
                db.record(op, "created", cover_path, book_id=b.book_id, title=b.info.title)
            if not b.info.timestamp:
                b.info.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            opf_path = os.path.join(b.dest, "metadata.opf")
            with open(opf_path, "w", encoding="utf-8") as f:
                f.write(write_opf(b.info, b.book_id, cover_name))
            db.record(op, "created", opf_path, book_id=b.book_id, title=b.info.title)
            db.set_high_water(max(db.high_water(), b.book_id))
            res.imported += 1
            log(f"→ {os.path.relpath(b.dest, cfg['books_dir'])}")
        for f in plan.series_partners:      # part of this operation, so undoing the import takes them back out
            res.errors += _apply_series_fix(f, op, db)
            log(f"Also in {f.series}, as #{f.index:g}: {f.book.info.title}")
    except (OSError, MoveRefused) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", f"stopped after {res.imported} book(s): {msg}")
        log(f"FAILED: {msg}")
        if torrent and transmission is not None and not db.changes(op):
            try:                             # nothing moved, so it can go on seeding
                transmission.torrent_action("start", [torrent["id"]])
            except Exception:
                pass
        return res
    if torrent and transmission is not None and remove_torrent:
        try:
            transmission.torrent_remove([torrent["id"]], delete_data=False)
            res.torrent_removed = True
            log("Removed torrent from Transmission (data kept)")
        except Exception as e:
            res.errors.append(f"could not remove torrent: {e}")
    elif torrent and transmission is not None and other_media(plan):
        log("Torrent kept, stopped: it still holds videos or music")
    if not single:
        for rel in junk:
            p = os.path.join(base, rel)
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    res.leftovers_deleted += 1
            except OSError as e:
                res.errors.append(f"could not delete {rel}: {e.strerror}")
        if res.leftovers_deleted:
            log(f"Deleted {res.leftovers_deleted} leftover file(s)")
        # Only folders this import emptied. A path named by hand keeps its top folder, and any other empty folder in it.
        for c in db.changes(op):
            if c["kind"] == "moved":
                remove_empty_dirs(os.path.dirname(c["src"]), base)
        if plan.kind != "path":
            for dp, _, _ in sorted(os.walk(base), key=lambda x: -x[0].count("/")):
                try:
                    if dp not in (cfg.get("inbox_dirs") or []):
                        os.rmdir(dp)
                except OSError:
                    pass
    db.finish(op, "applied", "; ".join(res.errors), torrent_removed=res.torrent_removed,
              leftovers_deleted=bool(res.leftovers_deleted))
    res.scanned = scan_kavita(cfg, kavita)
    if res.scanned:
        log(res.scanned)
    return res


# --------------------------------------------------------------------------
# Rewriting existing books: author merge and metadata edits
# --------------------------------------------------------------------------

@dataclass
class Rename:
    book: LibBook
    new_author: str              # new first author's name
    new_title: str
    new_path: str
    moves: list[tuple[str, str]]
    epub_authors: dict[str, list[str]] = field(default_factory=dict)   # book file name -> authors to write inside it
    epub_before: list[str] = field(default_factory=list)               # what the first of those files says now


def plan_rename(book: LibBook, cfg: dict, author: str, title: str | None = None) -> Rename:
    title = title if title is not None else book.info.title
    new_path = os.path.join(cfg["books_dir"], calibre.book_path(title, author, book.id))
    stem = calibre.file_stem(title, author)
    moves = []
    for name in os.listdir(book.path):
        src = os.path.join(book.path, name)
        new = stem + book_ext(name) if is_book(name) else name
        moves.append((src, os.path.join(new_path, new)))
    return Rename(book, author, title, new_path, moves)


def _write_epub(path: str, op: int, db: DB, book_id: int | None, title: str, authors: list[str] | None = None,
                series_name: str | None = None, index: float | None = None) -> str:
    """Make an EPUB say who wrote it and what series it's in, so Kavita agrees. A problem here is reported but never
    stops an import or a merge."""
    if book_ext(path) not in (".epub", ".kepub"):
        return ""
    try:
        original = epub.write_metadata(path, authors, series_name, index)
    except epub.EpubError as e:
        return f"metadata inside the book not changed: {e}"
    if original is not None:
        db.record(op, "epub", path, original=original, book_id=book_id, title=title)
    return ""


def _is_joint(name: str) -> bool:
    return len(split_authors([name])) > 1


@dataclass
class _People:
    """How the library's EPUBs name people, for keeping them consistent."""
    inside: list[tuple[LibBook, str, list[str]]]
    canonical: dict[str, str]
    spellings: dict[str, list[str]]
    keep_joint: set[str]             # joint fields the library uses more than it names their people alone

    def spell(self, person: str) -> str:
        key = author_key(person)
        if key in self.canonical:
            return self.canonical[key]
        names = self.spellings.get(key)
        return max(set(names), key=lambda n: (names.count(n), n)) if names else person

    def fixed(self, authors: list[str]) -> list[str]:
        """These authors the way Kavita should see them. A joint field the library uses, like 'Kugane Maruyama &
        so-bin', stays one entry, and the same people listed separately are joined up to match."""
        out: list[str] = []
        for a in authors:
            parts = [a] if a in self.keep_joint else [self.spell(first_last(p)) for p in split_authors([a])]
            out += [p for p in parts if p not in out]
        people = {author_key(p) for a in out for p in split_authors([a])}
        for joint in self.keep_joint:
            if people == {author_key(p) for p in split_authors([joint])}:
                return [joint]
        return out


def _read_people(library: list[LibBook], canonical: dict[str, str] | None = None) -> _People:
    inside: list[tuple[LibBook, str, list[str]]] = []
    for b in library:
        for name in b.files:
            if book_ext(name) in (".epub", ".kepub"):
                try:
                    inside.append((b, name, epub.read_authors(os.path.join(b.path, name))))
                except epub.EpubError:
                    pass
    spellings: dict[str, list[str]] = defaultdict(list)
    joint_books: dict[str, int] = defaultdict(int)
    alone_books: dict[str, int] = defaultdict(int)
    for _, _, authors in inside:
        for person in split_authors(authors):
            spellings[author_key(person)].append(person)
        for a in authors:
            if _is_joint(a):
                joint_books[a] += 1
            else:
                alone_books[author_key(a)] += 1
    keep = {j for j, n in joint_books.items()
            if "," not in j and n > max(alone_books[author_key(p)] for p in split_authors([j]))}
    return _People(inside, canonical or {}, spellings, keep)


def _import_authors(path: str, wanted: list[str], people) -> list[str] | None:
    """What an imported EPUB should say. If nobody changed the authors in the review, only tidy what the file says."""
    if book_ext(path) not in (".epub", ".kepub"):
        return None
    try:
        inner = epub.read_authors(path)
    except epub.EpubError:
        return wanted
    if inner and split_authors(inner) != wanted:
        return wanted
    return people().fixed(inner) if inner else wanted


def _epub_fixes(library: list[LibBook], canonical: dict[str, str]) -> dict[str, tuple[dict[str, list[str]], list[str]]]:
    """Which EPUBs make Kavita show one person as several: a reversed name (Kavita splits it at the comma), another
    spelling, or two people in one field when they're mostly named separately."""
    people = _read_people(library, canonical)
    fixes: dict[str, tuple[dict[str, list[str]], list[str]]] = {}
    for b, name, authors in people.inside:
        fixed = people.fixed(authors)
        if fixed and fixed != authors:
            fixes.setdefault(b.path, ({}, authors))[0][name] = fixed
    return fixes


def author_merge_plan(cfg: dict, library: list[LibBook] | None = None) -> list[Rename]:
    """Books whose author folder isn't 'First Last', that split one person across folders, or whose EPUB would make
    Kavita show one person as several."""
    library = library_books(cfg) if library is None else library
    by_person: dict[str, list[LibBook]] = defaultdict(list)
    for b in library:
        if b.info.authors:
            by_person[author_key(b.info.authors[0])].append(b)
    out = []
    planned: list[tuple[LibBook, str]] = []
    canonical: dict[str, str] = {}
    for key, books in by_person.items():
        names = [first_last(b.info.authors[0]) for b in books]
        # the most common 'First Last' spelling wins; a reversed name never does
        canonical[key] = max(set(names), key=lambda n: (names.count(n), n))
        planned += [(b, canonical[key]) for b in books]
    fixes = _epub_fixes(library, canonical)
    for b, best in planned:
        r = plan_rename(b, cfg, best)
        if b.path in fixes:
            r.epub_authors, r.epub_before = fixes[b.path]
        if b.info.authors[0] != best or r.new_path != b.path or r.epub_authors:
            out.append(r)
    return sorted(out, key=lambda r: r.new_path)


def _set_first_author(text: str, name: str) -> str:
    new = (f'<dc:creator opf:file-as={calibre.quoteattr(calibre.author_sort(name))} opf:role="aut">'
           f"{calibre.escape(name)}</dc:creator>")
    return re.sub(r"<dc:creator\b[^>]*>.*?</dc:creator>", lambda m: new, text, count=1, flags=re.S)


def _set_title(text: str, title: str) -> str:
    return re.sub(r"<dc:title>.*?</dc:title>", lambda m: f"<dc:title>{calibre.escape(title)}</dc:title>", text, count=1,
                  flags=re.S)


def apply_renames(renames: list[Rename], cfg: dict, db: DB, kavita=None, kind: str = "merge authors", log=None) -> Result:
    log = log or (lambda m: None)
    targets = [dst for r in renames for _, dst in r.moves]
    if len(set(targets)) != len(targets):
        raise PlanError("two books would end up at the same path")
    for r in renames:
        for src, dst in r.moves:
            if src != dst and os.path.lexists(dst):
                raise PlanError(f"{dst} already exists")
    op = db.begin(kind, kind)
    res = Result(op)
    try:
        for r in renames:
            old_dir = r.book.path
            opf = os.path.join(old_dir, "metadata.opf")
            if os.path.isfile(opf) and (r.new_author != r.book.info.authors[0] or r.new_title != r.book.info.title):
                with open(opf, encoding="utf-8") as f:
                    original = f.read()
                text = _set_first_author(original, r.new_author)
                if r.new_title != r.book.info.title:
                    text = _set_title(text, r.new_title)
                calibre.ET.fromstring(text.encode())   # never write a damaged file
                db.record(op, "edited", opf, original=original, book_id=r.book.id, title=r.new_title)
                with open(opf, "w", encoding="utf-8") as f:
                    f.write(text)
            for src, dst in r.moves:
                if src != dst:
                    move_file(src, dst)
                    db.record(op, "moved", dst, src, book_id=r.book.id, title=r.new_title)
            remove_empty_dirs(old_dir, cfg["books_dir"])
            stems = dict(r.moves)
            for name, authors in r.epub_authors.items():
                problem = _write_epub(stems[os.path.join(old_dir, name)], op, db, r.book.id, r.new_title, authors=authors)
                if problem:
                    res.errors.append(problem)
            res.imported += 1
            log(f"→ {os.path.relpath(r.new_path, cfg['books_dir'])}")
    except (OSError, MoveRefused, calibre.ET.ParseError) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", f"stopped after {res.imported} book(s): {msg}")
        return res
    db.finish(op, "applied", "; ".join(res.errors))
    res.scanned = scan_kavita(cfg, kavita)
    return res


def edit_book(book: LibBook, info: BookInfo, cfg: dict, db: DB, kavita=None) -> Result:
    """Rewrite one book's metadata; folder and file names follow the title and first author."""
    if not info.title.strip() or not info.authors:
        raise PlanError("a book needs a title and an author")
    if info.title != book.info.title:
        info.title_sort = ""
    r = plan_rename(book, cfg, info.authors[0], info.title)
    opf = os.path.join(book.path, "metadata.opf")
    cover = next((n for n in os.listdir(book.path) if n.startswith("cover.")), None)
    original = None
    if os.path.isfile(opf):
        with open(opf, encoding="utf-8") as f:
            original = f.read()
    op = db.begin("edit", info.title)
    res = Result(op)
    try:
        for src, dst in r.moves:
            if src != dst and os.path.lexists(dst):
                raise PlanError(f"{dst} already exists")
        text = write_opf(info, book.id, cover)
        if original is not None:
            # recorded before the rewrite: undo moves it back, then restores the text
            db.record(op, "edited", opf, original=original, book_id=book.id, title=info.title)
            with open(opf, "w", encoding="utf-8") as f:
                f.write(text)
        for src, dst in r.moves:
            if src != dst:
                move_file(src, dst)
                db.record(op, "moved", dst, src, book_id=book.id, title=info.title)
        if original is None:
            moved_opf = os.path.join(r.new_path, "metadata.opf")
            os.makedirs(r.new_path, exist_ok=True)
            with open(moved_opf, "w", encoding="utf-8") as f:
                f.write(text)
            db.record(op, "created", moved_opf, book_id=book.id, title=info.title)
        remove_empty_dirs(book.path, cfg["books_dir"])
        new_authors = info.authors if info.authors != book.info.authors else None
        new_series = info.series if (info.series, info.series_index) != (book.info.series, book.info.series_index) else None
        if new_authors is not None or new_series is not None:
            for _, dst in r.moves:
                problem = _write_epub(dst, op, db, book.id, info.title, new_authors, new_series, info.series_index)
                if problem:
                    res.errors.append(problem)
        res.imported = 1
    except (OSError, MoveRefused) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", msg)
        return res
    db.finish(op, "applied", "; ".join(res.errors))
    res.scanned = scan_kavita(cfg, kavita)
    return res


@dataclass
class SeriesFix:
    book: LibBook
    series: str
    index: float
    number_guessed: bool


def _has_epub(book: LibBook) -> bool:
    return any(book_ext(f) in (".epub", ".kepub") for f in book.files)


def series_fix_plan(cfg: dict, library: list[LibBook] | None = None) -> list[SeriesFix]:
    """EPUBs that Kavita would show as separate series, though they belong with another book by the same author."""
    library = library_books(cfg) if library is None else library
    found = series.assign([_series_item(b.id, b.info) for b in library])
    out = [SeriesFix(b, found[b.id].series, found[b.id].index, found[b.id].number_guessed) for b in library
           if b.id in found and _has_epub(b)]
    return sorted(out, key=lambda f: (normalize(f.series), f.index))


def _series_partners(plan: Plan, library: list[LibBook]) -> list[SeriesFix]:
    """Library books that belong in the series of a book being imported. Without them Kavita would show the new book's
    series and, beside it, each older book on its own. Follows the plan as it stands: a book that's skipped, or whose
    series was changed in the review, brings nobody along. Other ungrouped books in the library are left alone."""
    active = [b for b in plan.books if not b.skip and b.info.series and b.info.authors]
    if not active:
        return []
    found = series.assign([_series_item(b.id, b.info) for b in library] + [_series_item(id(b), b.info) for b in active])
    wanted = [(author_key(b.info.authors[0]), b.info.series) for b in active]
    out = []
    for b in library:
        a = found.get(b.id)
        if a and b.info.authors and _has_epub(b) and any(
                who == author_key(b.info.authors[0]) and series.same_series(name, a.series) for who, name in wanted):
            out.append(SeriesFix(b, a.series, a.index, a.number_guessed))
    return sorted(out, key=lambda f: (normalize(f.series), f.index))


def _apply_series_fix(f: SeriesFix, op: int, db: DB) -> list[str]:
    """Write one book's series beside it and inside it. Returns any problems; they never stop the rest."""
    problems = []
    opf = os.path.join(f.book.path, "metadata.opf")
    try:
        if os.path.isfile(opf):
            with open(opf, encoding="utf-8") as fh:
                original = fh.read()
            text = epub.set_series(original, f.series, f.index)
            calibre.ET.fromstring(text.encode())     # never write a damaged file
            db.record(op, "edited", opf, original=original, book_id=f.book.id, title=f.book.info.title)
            with open(opf, "w", encoding="utf-8") as fh:
                fh.write(text)
    except (OSError, epub.EpubError, calibre.ET.ParseError) as e:
        detail = permission_hint(e) if isinstance(e, OSError) else str(e)
        problems.append(f"{f.book.info.title}: metadata.opf not changed: {detail}")
    for name in f.book.files:
        problem = _write_epub(os.path.join(f.book.path, name), op, db, f.book.id, f.book.info.title, series_name=f.series, index=f.index)
        if problem:
            problems.append(f"{f.book.info.title}: {problem}")
    return problems


def apply_series(fixes: list[SeriesFix], cfg: dict, db: DB, kavita=None, log=None) -> Result:
    log = log or (lambda m: None)
    op = db.begin("group series", "group series")
    res = Result(op)
    for f in fixes:
        problems = _apply_series_fix(f, op, db)
        res.errors += problems
        res.imported += not problems
        log(f"{f.series} #{f.index:g}: {f.book.info.title}")
    db.finish(op, "applied", "; ".join(res.errors))
    res.scanned = scan_kavita(cfg, kavita)
    return res


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def undo_operation(op_id: int, cfg: dict, db: DB, transmission=None, kavita=None, readd_torrent: bool = False
                   ) -> tuple[int, list[str]]:
    op = db.operation(op_id)
    if not op:
        raise PlanError(f"no operation {op_id}")
    if op["status"] == "undone":
        raise PlanError(f"operation {op_id} was already undone")
    later = db.later_operations_on_same_books(op_id)
    if later:      # undoing this first would silently throw away what they did
        raise PlanError(f"operation {later[0]} changed the same books later: undo that one first")
    todo = [c for c in db.changes(op_id) if c["status"] == "done" and c["kind"] == "moved"]
    missing = [c["dst"] for c in todo if not os.path.exists(c["dst"])]
    if missing:
        raise PlanError(f"{len(missing)} file(s) are no longer where this operation put them, e.g. {missing[0]}. "
                        "If a later operation moved them, undo that one first.")
    taken = [c["src"] for c in todo if os.path.lexists(c["src"])]
    if taken:
        raise PlanError(f"{len(taken)} original location(s) are occupied, e.g. {taken[0]}")
    done, errors = 0, []
    for c in reversed(db.changes(op_id)):
        if c["status"] != "done":
            continue
        try:
            if c["kind"] == "moved":
                move_file(c["dst"], c["src"])
            elif c["kind"] == "created":
                if os.path.exists(c["dst"]):
                    os.remove(c["dst"])
            elif c["kind"] == "edited":
                os.makedirs(os.path.dirname(c["dst"]), exist_ok=True)
                with open(c["dst"], "w", encoding="utf-8") as f:
                    f.write(c["original"])
            elif c["kind"] == "epub":
                epub.restore_package(c["dst"], c["original"])
            db.set_change(c["id"], "undone")
            done += 1
            if c["kind"] in ("moved", "created"):
                remove_empty_dirs(os.path.dirname(c["dst"]), cfg["books_dir"])
        except (OSError, MoveRefused, epub.EpubError) as e:
            errors.append(permission_hint(e) if isinstance(e, OSError) else str(e))
    msg = f"undone: {done} change(s)"
    if readd_torrent and op["torrent_removed"] and op["magnet"] and transmission is not None:
        try:
            transmission.torrent_add(filename=op["magnet"], download_dir=op["download_dir"], paused=False)
            msg += "; torrent re-added"
        except Exception as e:
            errors.append(f"could not re-add torrent: {e}")
    db.finish(op_id, "undone" if not errors else "failed", msg + ("; " + "; ".join(errors) if errors else ""))
    scan_kavita(cfg, kavita)
    return done, errors
