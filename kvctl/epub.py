"""Change the authors and series stored inside an EPUB. Kavita reads them from there, not from metadata.opf beside the
book. It splits authors on commas, so 'Ring, Seth' shows up as two people, and it groups books by the series name and
number held there, ignoring a series that has no number.

Only the package document (the EPUB's internal .opf) changes, and only those entries. Every other entry is copied
as it is and checked before the new file replaces the old one. The file's date is left to change on purpose: that's
how Kavita notices a file needs rescanning.
"""

from __future__ import annotations

import copy
import html
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

from .metadata import MAX_XML, ZIP_ERRORS

MAX_TOTAL = 4 * 1024 ** 3   # uncompressed; anything bigger isn't a book we should unpack on a guess
# Kavita's list of roles that mean "wrote it"
AUTHOR_ROLES = {"", "aut", "author", "creator", "cre"}
_CREATOR = re.compile(r"<((?:[\w.-]+:)?creator)\b([^>]*?)(/?)>(?:(.*?)</\1\s*>)?", re.S)
# Self-closed and paired. Group 2 is the attributes, group 3 the text (None if self-closed). A self-closed tag must not
# run on to the next closing tag, which would hide the element after it.
_META = re.compile(r"<((?:[\w.-]+:)?meta)\b((?:[^>/]|/(?!>))*)(?:/>|>(.*?)</\1\s*>)", re.S)
_METADATA_END = re.compile(r"[ \t]*</(?:[\w.-]+:)?metadata\s*>")


class EpubError(Exception):
    pass


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf"""(?:^|\s)(?:[\w.-]+:)?{name}\s*=\s*(?:"([^"]*)"|'([^']*)')""", attrs)
    return None if not m else (m.group(1) if m.group(1) is not None else m.group(2))


def _xml_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(text: str) -> str:
    return _xml_text(text).replace('"', "&quot;")


def _plain(xml: str) -> str:
    try:
        return re.sub(r"\s+", " ", ET.fromstring(f"<x>{xml}</x>").text or "").strip()
    except ET.ParseError:
        return xml.strip()


def _refinements(opf: str) -> dict[str, dict[str, re.Match]]:
    """creator id -> {property: its <meta> element}."""
    out: dict[str, dict[str, re.Match]] = {}
    for m in _META.finditer(opf):
        refines = _attr(m.group(2), "refines")
        prop = _attr(m.group(2), "property")
        if refines and prop:
            out.setdefault(refines.lstrip("#"), {})[prop] = m
    return out


def _author_elements(opf: str) -> list[re.Match]:
    """Creators who wrote the book; an illustrator or translator keeps their own entry."""
    refined = _refinements(opf)
    out = []
    for m in _CREATOR.finditer(opf):
        if m.group(3) or not (m.group(4) or "").strip():
            continue
        role_meta = refined.get(_attr(m.group(2), "id") or "", {}).get("role")
        role = (role_meta.group(3) or "").strip() if role_meta else (_attr(m.group(2), "role") or "")
        if role.lower() in AUTHOR_ROLES:
            out.append(m)
    return out


def authors_in(opf: str) -> list[str]:
    return [_plain(m.group(4)) for m in _author_elements(opf)]


def _sort_name(name: str) -> str:
    parts = name.split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name


def _with_file_as(attrs: str, name: str) -> str:
    return re.sub(r"""((?:[\w.-]+:)?file-as\s*=\s*)(?:"[^"]*"|'[^']*')""", lambda m: f'{m.group(1)}"{_xml_attr(_sort_name(name))}"', attrs)


def set_authors(opf: str, authors: list[str]) -> str:
    """The same document with these authors. Everything else in it stays exactly as written."""
    if not authors:
        raise EpubError("a book needs an author")
    elements = _author_elements(opf)
    if not elements:
        raise EpubError("no author entry to change")
    refined = _refinements(opf)
    edits: list[tuple[int, int, str]] = []
    first = elements[0]
    for i, m in enumerate(elements):
        cid = _attr(m.group(2), "id") or ""
        if i >= len(authors):        # fewer authors than before: the entry and what describes it both go
            edits.append((m.start(), m.end(), ""))
            edits += [(r.start(), r.end(), "") for r in refined.get(cid, {}).values()]
            continue
        name = authors[i]
        edits.append((m.start(), m.end(), f"<{m.group(1)}{_with_file_as(m.group(2), name)}>{_xml_text(name)}</{m.group(1)}>"))
        file_as = refined.get(cid, {}).get("file-as")
        if file_as:
            edits.append((file_as.start(3), file_as.end(3), _xml_text(_sort_name(name))))
    if len(authors) > len(elements):
        indent = re.search(r"[ \t]*$", opf[:first.start()]).group(0)
        attrs = re.sub(r"""\s(?:[\w.-]+:)?id\s*=\s*(?:"[^"]*"|'[^']*')""", "", first.group(2))
        # A role given by a <meta> doesn't carry over, and a creator with no role counts as an author.
        extra = "".join(f"\n{indent}<{first.group(1)}{_with_file_as(attrs, n)}>{_xml_text(n)}</{first.group(1)}>" for n in authors[len(elements):])
        last = elements[-1]
        edits.append((last.end(), last.end(), extra))
    for start, end, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        opf = opf[:start] + text + opf[end:]
    return opf


def series_in(opf: str) -> tuple[str, float | None]:
    """The series and number as Kavita reads them: in document order, with the last one found winning."""
    series, number = "", ""
    for m in _META.finditer(opf):
        attrs, text = m.group(2), _plain(m.group(3) or "")
        name, prop = _attr(attrs, "name"), _attr(attrs, "property")
        if name == "calibre:series":
            series = html.unescape(_attr(attrs, "content") or "")
        elif name == "calibre:series_index":
            number = _attr(attrs, "content") or ""
        if prop == "belongs-to-collection":
            series = text
        elif prop == "group-position":
            number = text
    try:
        return series.strip(), float(number) if number else None
    except ValueError:
        return series.strip(), None


def _whole_line(opf: str, m: re.Match) -> tuple[int, int, str]:
    """An edit removing an element along with the indentation before it."""
    start = m.start()
    while start and opf[start - 1] in " \t":
        start -= 1
    if start and opf[start - 1] == "\n":
        start -= 1
    return start, m.end(), ""


def set_series(opf: str, series: str, index: float | None) -> str:
    """The same document in this series; "" takes it out of any series. Kavita needs a number, so no number means 1."""
    number = f"{index if index is not None else 1:g}"
    old_series, _ = series_in(opf)
    refined = _refinements(opf)
    edits: list[tuple[int, int, str]] = []
    ours: set[str] = set()     # collections holding the current series; any other (a box set, say) stays as it is
    for m in _META.finditer(opf):
        attrs = m.group(2)
        if _attr(attrs, "property") == "belongs-to-collection" and _plain(m.group(3) or "") == old_series:
            ours.add(_attr(attrs, "id") or "")
    have_name = have_number = False
    for m in _META.finditer(opf):
        attrs = m.group(2)
        name, prop = _attr(attrs, "name"), _attr(attrs, "property")
        if name in ("calibre:series", "calibre:series_index"):
            value = series if name == "calibre:series" else number
            have_name, have_number = have_name or name == "calibre:series", have_number or name != "calibre:series"
            if not series:
                edits.append(_whole_line(opf, m))
            else:
                edits.append((m.start(2), m.end(2), re.sub(r"""(content\s*=\s*)(?:"[^"]*"|'[^']*')""",
                                                           lambda c: f'{c.group(1)}"{_xml_attr(value)}"', attrs)))
        elif prop == "belongs-to-collection" and (_attr(attrs, "id") or "") in ours:
            if not series:
                edits.append(_whole_line(opf, m))
                edits += [_whole_line(opf, r) for r in refined.get(_attr(attrs, "id") or "", {}).values()]
            else:
                edits.append((m.start(3), m.end(3), _xml_text(series)))
                position = refined.get(_attr(attrs, "id") or "", {}).get("group-position")
                if position:
                    edits.append((position.start(3), position.end(3), number))
    if series and not (have_name and have_number):
        end = _METADATA_END.search(opf)
        if not end:
            raise EpubError("no metadata section")
        items = _META.findall(opf) or [""]
        indent = re.search(r"[ \t]*$", opf[:opf.rfind("<", 0, end.start())]).group(0) if items else ""
        added = ""
        if not have_name:
            added += f'{indent}<meta name="calibre:series" content="{_xml_attr(series)}"/>\n'
        if not have_number:
            added += f'{indent}<meta name="calibre:series_index" content="{number}"/>\n'
        edits.append((end.start(), end.start(), added))
    seen: set[tuple[int, int]] = set()
    for start, end, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if (start, end) not in seen:
            seen.add((start, end))
            opf = opf[:start] + text + opf[end:]
    return opf


def _rootfile(z: zipfile.ZipFile) -> str:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    for e in container.iter():
        if e.tag.rsplit("}", 1)[-1].lower() == "rootfile" and e.get("full-path"):
            return e.get("full-path")
    raise EpubError("no package document")


def read_package(path: str) -> str:
    """The EPUB's internal .opf as text. Raises EpubError if it can't be safely changed later."""
    try:
        with zipfile.ZipFile(path) as z:
            name = _rootfile(z)
            info = z.getinfo(name)
            if info.file_size > MAX_XML:
                raise EpubError("package document is too large")
            return z.read(name).decode("utf-8")
    except (OSError, *ZIP_ERRORS, ET.ParseError, KeyError) as e:
        raise EpubError(f"can't read {os.path.basename(path)}: {e}") from None


def read_authors(path: str) -> list[str]:
    return authors_in(read_package(path))


def read_series(path: str) -> tuple[str, float | None]:
    return series_in(read_package(path))


def _rewrite(path: str, text: str) -> None:
    """Replace the package document, verify the result, and only then swap the new file in."""
    tmp = f"{path}.kvctl-tmp"
    try:
        with zipfile.ZipFile(path) as old:
            name = _rootfile(old)
            infos = old.infolist()
            if any(i.flag_bits & 1 for i in infos):
                raise EpubError("the zip is encrypted")
            if sum(i.file_size for i in infos) > MAX_TOTAL:
                raise EpubError("too large to rewrite")
            with zipfile.ZipFile(tmp, "w", allowZip64=True) as new:
                new.comment = old.comment
                for info in infos:
                    item = copy.copy(info)
                    if info.filename == name:
                        new.writestr(item, text.encode("utf-8"), compress_type=info.compress_type)
                    else:
                        with old.open(info) as src, new.open(item, "w", force_zip64=info.file_size >= 2 ** 31) as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
            with zipfile.ZipFile(tmp) as new:
                if new.testzip() is not None:
                    raise EpubError("the rewritten file failed its check")
                before = [(i.filename, i.CRC, i.compress_type) for i in infos if i.filename != name]
                after = [(i.filename, i.CRC, i.compress_type) for i in new.infolist() if i.filename != name]
                if before != after or [i.filename for i in infos] != [i.filename for i in new.infolist()]:
                    raise EpubError("the rewritten file differs from the original")
                if new.read(name).decode("utf-8") != text:
                    raise EpubError("the rewritten file lost its metadata")
        shutil.copymode(path, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except (OSError, *ZIP_ERRORS, ET.ParseError, KeyError) as e:
        raise EpubError(f"can't rewrite {os.path.basename(path)}: {e}") from None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_metadata(path: str, authors: list[str] | None = None, series: str | None = None, index: float | None = None) -> str | None:
    """Put these authors and this series inside the EPUB; None leaves that part alone, and series "" removes it.
    Returns the old package document for undo, or None if nothing changed."""
    original = read_package(path)
    text = original
    wanted_series = None
    if authors is not None and authors_in(text) != authors:
        text = set_authors(text, authors)
    if series is not None:
        wanted_series = (series, (index if index is not None else 1.0) if series else None)
        if series_in(text) != wanted_series:
            text = set_series(text, series, index)
    if text == original:
        return None
    try:
        ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        raise EpubError(f"{os.path.basename(path)}: the result wouldn't be valid XML: {e}") from None
    if (authors is not None and authors_in(text) != authors) or (wanted_series is not None and series_in(text) != wanted_series):
        raise EpubError(f"{os.path.basename(path)}: the change didn't read back correctly")
    _rewrite(path, text)
    return original


def write_authors(path: str, authors: list[str]) -> str | None:
    return write_metadata(path, authors=authors)


def restore_package(path: str, original: str) -> None:
    _rewrite(path, original)
