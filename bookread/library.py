"""Find books in a folder and say what they are. Only zip directories and metadata are read, so scanning is quick."""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from dataclasses import dataclass, field

from .ebook import MAX_PACKAGE, clean_text, package_path, parse_xml

READABLE = (".epub", ".kepub")
OTHER_FORMATS = (".mobi", ".azw", ".azw3", ".fb2", ".fb2.zip", ".pdf", ".lit", ".djvu")


@dataclass
class Entry:
    path: str
    title: str
    authors: list[str] = field(default_factory=list)
    series: str = ""
    series_index: float | None = None
    key: str = ""

    @property
    def readable(self) -> bool:
        return is_readable(self.path)

    @property
    def format(self) -> str:
        return os.path.splitext(self.path)[1].lstrip(".").lower()


def is_readable(path: str) -> bool:
    return path.lower().endswith(READABLE)


def is_book(name: str) -> bool:
    return name.lower().endswith(READABLE + OTHER_FORMATS)


def book_key(path: str) -> str:
    """Identifies a book by what's in it, from the checksums in its zip directory. The package document is left out,
    so moving, renaming or editing the metadata doesn't lose your place. Something that isn't a zip goes by its path."""
    try:
        with zipfile.ZipFile(path) as z:
            parts = sorted((i.filename, i.CRC, i.file_size) for i in z.infolist() if not i.filename.lower().endswith(".opf"))
    except Exception:           # a damaged file fails in many ways inside zipfile; it mustn't stop the rest of the list
        return path
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _from_package(data: bytes) -> dict:
    """Title, authors and series from a package document."""
    title, authors, series, index = "", [], "", None
    for e in parse_xml(data).iter():
        name, text = _local(e.tag), " ".join((e.text or "").split())
        if name == "title" and text and not title:
            title = text
        elif name == "creator" and text:
            authors.append(text)
        elif name == "meta":
            if e.get("name") == "calibre:series":
                series = e.get("content") or series
            elif e.get("name") == "calibre:series_index":
                index = _number(e.get("content"))
            elif e.get("property") == "belongs-to-collection":
                series = text or series
            elif e.get("property") == "group-position":
                index = _number(text)
    return {"title": title, "authors": authors, "series": series, "series_index": index}


def _from_sidecar(path: str) -> dict | None:
    """A metadata.opf beside the book, as Calibre leaves it, unless the folder holds several books it might describe."""
    folder = os.path.dirname(path)
    sidecar = os.path.join(folder, "metadata.opf")
    try:
        if not os.path.isfile(sidecar) or os.path.getsize(sidecar) > MAX_PACKAGE:
            return None
        if sum(1 for n in os.listdir(folder) if is_book(n)) != 1:
            return None
        with open(sidecar, "rb") as f:
            return _from_package(f.read())
    except Exception:
        return None


def _from_epub(path: str) -> dict | None:
    if not is_readable(path):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            name = package_path(z)
            if z.getinfo(name).file_size > MAX_PACKAGE:
                return None
            return _from_package(z.read(name))
    except Exception:
        return None


def _from_name(path: str) -> dict:
    """`Title - Author.ext`, or the whole name as the title."""
    stem = os.path.basename(path)
    for ext in sorted(READABLE + OTHER_FORMATS, key=len, reverse=True):
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    title, sep, author = stem.rpartition(" - ")
    return {"title": title.strip(), "authors": [author.strip()], "series": "", "series_index": None} if sep and title.strip() and author.strip() \
        else {"title": stem, "authors": [], "series": "", "series_index": None}


def _label(text: str) -> str:
    """Safe to print: names come from file names and book metadata, and may hold escapes or undecodable bytes."""
    return " ".join(clean_text(text.encode("utf-8", "replace").decode("utf-8")).split())


def read_entry(path: str) -> Entry:
    found = _from_name(path)
    for source in (_from_epub(path), _from_sidecar(path)):      # the later source wins, field by field
        for key, value in (source or {}).items():
            if value:
                found[key] = value
    return Entry(path, _label(found["title"]), [_label(a) for a in found["authors"]], _label(found["series"]), found["series_index"], book_key(path))


def _natural(text: str) -> list:
    """Sorts `Vol 2` before `Vol 10`."""
    return [(0, int(part), "") if part.isdecimal() else (1, 0, part) for part in re.split(r"(\d+)", text.casefold()) if part]


def _shelf_position(entry: Entry) -> tuple:
    """Alphabetical, with a series filed under its name and in reading order."""
    return _natural(entry.series or entry.title), entry.series_index or 0, _natural(entry.title)


def scan(folder: str) -> list[Entry]:
    entries = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        entries += [read_entry(os.path.join(root, f)) for f in files if is_book(f) and not f.startswith(".")]
    return sorted(entries, key=_shelf_position)


def matches(entry: Entry, query: str) -> bool:
    """Every word of the query is in the title, an author or the series."""
    text = " ".join([entry.title, *entry.authors, entry.series]).lower()
    return all(word in text for word in query.lower().split())
