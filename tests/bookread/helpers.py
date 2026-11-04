"""Books built on the fly and a throwaway workspace. Nothing here reaches a real library or real settings."""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.parse
import zipfile
from xml.sax.saxutils import escape


def make_book(path: str, chapters: list[tuple[str, str]], ncx: str = "", nav: str = "", css: str = "", opf_dir: str = "OEBPS",
              unlisted: tuple = (), encrypted: tuple = (), title: str = "Test", authors: tuple = (), extra_meta: str = "",
              extra_items: tuple | list = ()) -> str:
    """An EPUB to read: `chapters` is [(file name relative to the package, body or whole document)], in reading order.
    `unlisted` names files left out of the reading order, `encrypted` files marked as DRM-protected."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    join = (lambda name: f"{opf_dir}/{name}") if opf_dir else (lambda name: name)
    manifest, spine = "", ""
    # the manifest lists files in reverse, so anything following it instead of the spine gets the order wrong
    for i, (name, _) in reversed(list(enumerate(chapters))):
        manifest += f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>'
    for i, (name, _) in enumerate(chapters):
        linear = ' linear="no"' if name in unlisted else ""
        spine += f'<itemref idref="c{i}"{linear}/>'
    for i, (name, media, _) in enumerate(extra_items):
        manifest += f'<item id="x{i}" href="{name}" media-type="{media}"/>'
        spine += f'<itemref idref="x{i}"/>'
    if ncx:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    if nav:
        manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    if css:
        manifest += '<item id="css" href="style.css" media-type="text/css"/>'
    creators = "".join(f"<dc:creator>{escape(a)}</dc:creator>" for a in authors)
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(title)}</dc:title>{creators}{extra_meta}</metadata>'
           f'<manifest>{manifest}</manifest><spine{" toc=" + chr(34) + "ncx" + chr(34) if ncx else ""}>{spine}</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   f'<rootfiles><rootfile full-path="{join("content.opf")}" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr(join("content.opf"), opf)
        for name, body in chapters:
            doc = body if isinstance(body, bytes) or "<html" in body else f"<html><head><title>Not text</title></head><body>{body}</body></html>"
            z.writestr(join(urllib.parse.unquote(name)), doc)   # `name` is written as a link; the file has its real name
        for name, _, data in extra_items:
            z.writestr(join(name), data)
        if ncx:
            z.writestr(join("toc.ncx"), ncx)
        if nav:
            z.writestr(join("nav.xhtml"), nav)
        if css:
            z.writestr(join("style.css"), css)
        if encrypted:
            refs = "".join(f'<enc:EncryptedData xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptionMethod Algorithm="{alg}"/>'
                           f'<enc:CipherData><enc:CipherReference URI="{join(name)}"/></enc:CipherData></enc:EncryptedData>'
                           for name, alg in encrypted)
            z.writestr("META-INF/encryption.xml", f'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">{refs}</encryption>')
    return path


def write_file(path: str, data: bytes | str = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    return path


class Workspace:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="bookread-test-")
        self.books = os.path.join(self.root, "Books")
        self.database = os.path.join(self.root, "state", "reading.db")
        self.config = os.path.join(self.root, "config.toml")
        os.makedirs(self.books)
        with open(self.config, "w") as f:
            f.write(f'books_dir = "{self.books}"\ndatabase = "{self.database}"\n')

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
