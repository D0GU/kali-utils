"""Inbox, plans, importing, library listing, author merging, editing and undo."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from kutil.fsops import MoveRefused, move_file, permission_hint, remove_empty_dirs
from kutil.text import normalize

from . import calibre
from .calibre import BookInfo, author_key, first_last, read_opf, write_opf
from .db import DB
from .metadata import book_ext, is_book, read_book
from .providers import BookMatch, Lookup

TORRENT_FIELDS = ["id", "name", "hashString", "downloadDir", "percentDone", "leftUntilDone", "sizeWhenDone",
                  "doneDate", "status", "magnetLink", "metadataPercentComplete"]
OTHER_MEDIA = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
GB = 1024 ** 3


class ImportError_(MoveRefused):
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
    kind: str   # torrent | folder
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
        raise ImportError_(f"{path} does not exist")
    n, size = _count_books(path)
    return Source("folder", os.path.basename(path.rstrip("/")), path, n, size)


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


def library_books(cfg: dict) -> list[LibBook]:
    root = cfg["books_dir"]
    books = []
    if not os.path.isdir(root):
        return books
    for a in sorted(os.listdir(root)):
        ap = os.path.join(root, a)
        if not os.path.isdir(ap):
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


def _key(info: BookInfo) -> tuple:
    return normalize(info.title), author_key(info.authors[0]) if info.authors else ""


def make_plan(source: Source, cfg: dict, library: list[LibBook] | None = None) -> Plan:
    if not os.path.exists(source.path):
        raise ImportError_(f"{source.path} not found")
    is_file = os.path.isfile(source.path)
    base = os.path.dirname(source.path) if is_file else source.path
    files = scan_files(source.path)
    incomplete = [rel for rel, _ in files if rel.endswith(".part")]
    book_files = [(rel, size) for rel, size in files if is_book(rel)]
    used: set[str] = set()
    grouped: dict[tuple, Book] = {}
    order: list[Book] = []
    for rel, size in book_files:
        info, cover, cover_ext, origin = read_book(os.path.join(base, rel))
        folder = os.path.dirname(rel)
        sidecars = []
        if folder or not is_file:   # a Calibre export carries its own metadata and cover
            opf = os.path.join(base, folder, "metadata.opf")
            if os.path.isfile(opf) and sum(1 for r, _ in book_files if os.path.dirname(r) == folder) <= 3:
                try:
                    side, _ = read_opf(opf)
                    if side.title:
                        info, origin = side, "metadata.opf"
                        info.uuid = side.uuid
                        sidecars.append(os.path.join(folder, "metadata.opf"))
                except (OSError, calibre.ET.ParseError):
                    pass
            img = os.path.join(base, folder, "cover.jpg")
            if os.path.isfile(img):
                with open(img, "rb") as f:
                    cover, cover_ext = f.read(30 * 1024 * 1024), ".jpg"
                sidecars.append(os.path.join(folder, "cover.jpg"))
        info.authors = split_authors(info.authors)
        key = _key(info) + (folder if not info.title else "",)
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
    plan = Plan(source.name, source.path, order, leftovers, source.torrent, incomplete)
    library = library_books(cfg) if library is None else library
    known = {_key(b.info): b.id for b in library}
    for b in plan.books:
        dup = known.get(_key(b.info))
        if dup is not None:
            b.duplicate_of, b.skip = dup, True   # already in the library: off by default, Space includes it
    return plan


def apply_match(book: Book, match: BookMatch, overwrite: bool = False) -> None:
    """Automatic checks only fill gaps. A match the user chose may also replace the title and authors."""
    info = book.info
    if overwrite or not info.title:
        info.title = match.title
    if (overwrite or not info.authors) and match.authors:
        info.authors = split_authors(match.authors)
    if match.year and not info.pubdate:
        info.pubdate = f"{match.year}-01-01T00:00:00+00:00"
    info.isbn = info.isbn or match.isbn
    info.publisher = info.publisher or match.publisher
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
    plan.global_errors = []
    if plan.incomplete:
        plan.global_errors.append(f"{len(plan.incomplete)} file(s) are still incomplete (.part)")
    seen: set[str] = set()
    need = 0
    for b in plan.books:
        b.error = b.warning = ""
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
        elif not b.verified:
            b.warning = "not found online; using the book's own metadata"
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
        kavita.scan(lib_id)
        return "Kavita is scanning"
    except Exception as e:
        return f"Kavita scan failed: {e}"


def apply_plan(plan: Plan, cfg: dict, db: DB, transmission=None, kavita=None, lookup: Lookup | None = None,
               remove_torrent: bool | None = None, delete_leftovers: bool | None = None, log=None) -> Result:
    log = log or (lambda m: None)
    remove_torrent = cfg["remove_torrent"] if remove_torrent is None else remove_torrent
    delete_leftovers = cfg["delete_leftovers"] if delete_leftovers is None else delete_leftovers
    check_plan(plan, cfg, db)
    if not plan_ok(plan):
        problems = plan.global_errors + [f"{b.info.title or b.files[0]}: {b.error}" for b in plan.books if b.error]
        raise ImportError_("plan has problems: " + "; ".join(problems)[:400])
    single = os.path.isfile(plan.root)
    base = os.path.dirname(plan.root) if single else plan.root
    torrent = plan.torrent
    if torrent and transmission is not None:
        try:
            transmission.torrent_action("stop", [torrent["id"]])
            log("Stopped torrent")
        except Exception as e:
            raise ImportError_(f"could not stop the torrent: {e}") from None
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
            if b.cover is None and b.cover_id and lookup is not None:
                b.cover = lookup.cover(b.cover_id)
                b.cover_ext = ".jpg" if b.cover else ""
            cover_name = None
            if b.cover:
                cover_name = "cover" + (b.cover_ext or ".jpg")
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
    except (OSError, MoveRefused) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", f"stopped after {res.imported} book(s): {msg}")
        log(f"FAILED: {msg}")
        return res
    if torrent and transmission is not None and remove_torrent:
        try:
            transmission.torrent_remove([torrent["id"]], delete_data=False)
            res.torrent_removed = True
            log("Removed torrent from Transmission (data kept)")
        except Exception as e:
            res.errors.append(f"could not remove torrent: {e}")
    if not single:
        if delete_leftovers:
            junk = [rel for rel, _ in plan.leftovers] + [r for b in plan.books for r in b.sidecars if not b.skip]
            if not other_media(plan):
                junk += [r for b in plan.books if b.skip for r in b.files + b.sidecars]
            for rel in junk:
                p = os.path.join(base, rel)
                try:
                    if os.path.isfile(p) or os.path.islink(p):
                        os.remove(p)
                        res.leftovers_deleted += 1
                except OSError as e:
                    res.errors.append(f"could not delete {rel}: {e.strerror}")
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


def author_merge_plan(cfg: dict, library: list[LibBook] | None = None) -> list[Rename]:
    """Books whose author folder isn't 'First Last', or that split one person across folders."""
    library = library_books(cfg) if library is None else library
    by_person: dict[str, list[LibBook]] = defaultdict(list)
    for b in library:
        if b.info.authors:
            by_person[author_key(b.info.authors[0])].append(b)
    out = []
    for books in by_person.values():
        names = [first_last(b.info.authors[0]) for b in books]
        # the most common 'First Last' spelling wins; a reversed name never does
        best = max(set(names), key=lambda n: (names.count(n), n))
        for b in books:
            r = plan_rename(b, cfg, best)
            if b.info.authors[0] != best or r.new_path != b.path:
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
        raise ImportError_("two books would end up at the same path")
    for r in renames:
        for src, dst in r.moves:
            if src != dst and os.path.lexists(dst):
                raise ImportError_(f"{dst} already exists")
    op = db.begin(kind, kind)
    res = Result(op)
    try:
        for r in renames:
            old_dir = r.book.path
            opf = os.path.join(old_dir, "metadata.opf")
            if os.path.isfile(opf):
                with open(opf, encoding="utf-8") as f:
                    original = f.read()
                text = _set_first_author(original, r.new_author)
                if r.new_title != r.book.info.title:
                    text = _set_title(text, r.new_title)
                calibre.ET.fromstring(text.encode())   # never write a damaged file
                with open(opf, "w", encoding="utf-8") as f:
                    f.write(text)
                db.record(op, "edited", opf, original=original, book_id=r.book.id, title=r.new_title)
            for src, dst in r.moves:
                if src != dst:
                    move_file(src, dst)
                    db.record(op, "moved", dst, src, book_id=r.book.id, title=r.new_title)
            remove_empty_dirs(old_dir, cfg["books_dir"])
            res.imported += 1
            log(f"→ {os.path.relpath(r.new_path, cfg['books_dir'])}")
    except (OSError, MoveRefused, calibre.ET.ParseError) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", f"stopped after {res.imported} book(s): {msg}")
        return res
    db.finish(op, "applied")
    res.scanned = scan_kavita(cfg, kavita)
    return res


def edit_book(book: LibBook, info: BookInfo, cfg: dict, db: DB, kavita=None) -> Result:
    """Rewrite one book's metadata; folder and file names follow the title and first author."""
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
                raise ImportError_(f"{dst} already exists")
        moved_opf = os.path.join(r.new_path, "metadata.opf")
        if original is not None:
            # recorded before the rewrite: undo restores the text, then moves it back
            db.record(op, "edited", opf, original=original, book_id=book.id, title=info.title)
        with open(opf, "w", encoding="utf-8") as f:
            f.write(write_opf(info, book.id, cover))
        for src, dst in r.moves:
            if src != dst:
                move_file(src, dst)
                db.record(op, "moved", dst, src, book_id=book.id, title=info.title)
        if original is None and os.path.isfile(opf) and opf != moved_opf:
            move_file(opf, moved_opf)
        remove_empty_dirs(book.path, cfg["books_dir"])
        res.imported = 1
    except (OSError, MoveRefused) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        res.errors.append(msg)
        db.finish(op, "failed", msg)
        return res
    db.finish(op, "applied")
    res.scanned = scan_kavita(cfg, kavita)
    return res


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def undo_operation(op_id: int, cfg: dict, db: DB, transmission=None, kavita=None, readd_torrent: bool = False
                   ) -> tuple[int, list[str]]:
    op = db.operation(op_id)
    if not op:
        raise ImportError_(f"no operation {op_id}")
    if op["status"] == "undone":
        raise ImportError_(f"operation {op_id} was already undone")
    done, errors = 0, []
    for c in reversed(db.changes(op_id)):
        if c["status"] != "done":
            continue
        try:
            if c["kind"] == "moved":
                if not os.path.exists(c["dst"]):
                    errors.append(f"missing: {c['dst']}")
                    continue
                move_file(c["dst"], c["src"])
            elif c["kind"] == "created":
                if os.path.exists(c["dst"]):
                    os.remove(c["dst"])
            elif c["kind"] == "edited":
                os.makedirs(os.path.dirname(c["dst"]), exist_ok=True)
                with open(c["dst"], "w", encoding="utf-8") as f:
                    f.write(c["original"])
            db.set_change(c["id"], "undone")
            done += 1
            if c["kind"] != "edited":
                remove_empty_dirs(os.path.dirname(c["dst"]), cfg["books_dir"])
        except (OSError, MoveRefused) as e:
            errors.append(permission_hint(e) if isinstance(e, OSError) else str(e))
    msg = f"undone: {done} change(s)"
    if readd_torrent and op["magnet"] and transmission is not None:
        try:
            transmission.torrent_add(filename=op["magnet"], download_dir=op["download_dir"], paused=False)
            msg += "; torrent re-added"
        except Exception as e:
            errors.append(f"could not re-add torrent: {e}")
    db.finish(op_id, "undone" if not errors else "failed", msg + ("; " + "; ".join(errors) if errors else ""))
    scan_kavita(cfg, kavita)
    return done, errors
