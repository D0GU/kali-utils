"""Shared test helpers: EPUBs built on the fly, a fake Kavita server and a throwaway workspace."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 4
API_KEY = "test-key-123"


def make_epub(path: str, title: str = "", authors=("Seth Ring",), series: str = "", index: float | None = None, cover: bytes | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = [f"<dc:title>{escape(title)}</dc:title>"] if title else []
    meta += [f"<dc:creator>{escape(a)}</dc:creator>" for a in authors]
    meta.append("<dc:language>en</dc:language>")
    if series:
        meta.append(f'<meta name="calibre:series" content="{escape(series)}"/>')
        if index is not None:
            meta.append(f'<meta name="calibre:series_index" content="{index}"/>')
    manifest = '<item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>'
    if cover:
        meta.append('<meta name="cover" content="cover-img"/>')
        manifest += '<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"/>'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">{"".join(meta)}</metadata>'
           f"<manifest>{manifest}</manifest></package>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/text.xhtml", "<html><body>text</body></html>")
        if cover:
            z.writestr("OEBPS/images/cover.jpg", cover)
    return path


def write_file(path: str, data: bytes | str = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    return path


class Workspace:
    """Temp folders and a config. Nothing here reaches the real library, torrents or settings."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="kvctl-test-")
        self.books = os.path.join(self.root, "Books")
        self.inbox = os.path.join(self.root, "inbox")
        self.downloads = os.path.join(self.root, "downloads")
        for d in (self.books, self.inbox, self.downloads):
            os.makedirs(d)
        self.cfg = {"url": "http://127.0.0.1:1", "api_key": "", "books_dir": self.books, "inbox_dirs": [self.inbox],
                    "database": os.path.join(self.root, "state", "library.db"), "library_id": 0, "remove_torrent": True,
                    "delete_leftovers": True, "online": False, "scan_after_import": True, "min_free_gb": 0}

    def add_library_book(self, title: str, author: str, book_id: int, folder_author: str | None = None) -> str:
        """A book as Calibre left it, with the author in `author` and the folder named `folder_author`."""
        from kvctl import calibre
        from kvctl.calibre import BookInfo
        folder = os.path.join(self.books, folder_author or calibre.author_folder(author), calibre.book_folder(title, book_id))
        os.makedirs(folder)
        make_epub(os.path.join(folder, calibre.file_stem(title, author) + ".epub"), title, [author])
        write_file(os.path.join(folder, "cover.jpg"), JPEG)
        with open(os.path.join(folder, "metadata.opf"), "w", encoding="utf-8") as f:
            f.write(calibre.write_opf(BookInfo(title=title, authors=[author]), book_id))
        return folder

    def tree(self, root: str | None = None) -> list[str]:
        root = root or self.root
        out = []
        for dp, _, fn in os.walk(root):
            out += [os.path.relpath(os.path.join(dp, f), root) for f in fn]
        return sorted(out)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class FakeTransmission:
    def __init__(self, torrents=None):
        self.torrents = torrents or []
        self.calls: list = []

    def torrent_get(self, fields, ids=None):
        return self.torrents

    def torrent_action(self, action, ids):
        self.calls.append((action, ids))

    def torrent_remove(self, ids, delete_data=False):
        self.calls.append(("remove", ids, delete_data))

    def torrent_add(self, **kw):
        self.calls.append(("add", kw))
        return "added", {}


def torrent_dict(tid: int, name: str, download_dir: str, left: int = 0) -> dict:
    return {"id": tid, "name": name, "hashString": f"{tid:02x}" * 20, "downloadDir": download_dir, "percentDone": 1.0,
            "leftUntilDone": left, "sizeWhenDone": 1, "doneDate": 1_700_000_000 + tid, "status": 6,
            "magnetLink": f"magnet:?xt=urn:btih:{tid:02x}", "metadataPercentComplete": 1.0}


class FakeKavita:
    """The parts of Kavita's REST API that kvctl uses."""

    def __init__(self, libraries=None, series=None, api_key: str = API_KEY):
        self.api_key = api_key
        self.libraries = libraries if libraries is not None else []
        self.series = series or []
        self.calls: list[tuple] = []     # (method, path, query, body)
        self.next_id = 100
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload=None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                url = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else None
                if url.path == "/api/Health":
                    return self._send(200, "Ok")
                if self.headers.get("x-api-key") != fake.api_key:
                    return self._send(401)
                fake.calls.append((self.command, url.path, query, body))
                return self._send(*fake.route(self.command, url.path, query, body))

            do_GET = do_POST = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method, path, query, body):
        if path == "/api/Library/libraries":
            return 200, self.libraries
        if path == "/api/Server/server-info-slim":
            return 200, {"kavitaVersion": "0.9.1.4"}
        if path == "/api/Library/create":
            self.next_id += 1
            self.libraries.append({**body, "id": self.next_id})
            return 200, None
        if path == "/api/Library/update":
            lib = next(l for l in self.libraries if l["id"] == body["id"])
            lib.update(body)
            return 200, None
        if path == "/api/Library/delete":
            self.libraries[:] = [l for l in self.libraries if str(l["id"]) != query["libraryId"]]
            return 200, True
        if path in ("/api/Library/scan", "/api/Library/scan-all"):
            return 200, None
        if path == "/api/Series/all-v2":
            page, size = int(query["PageNumber"]), int(query["PageSize"])
            ids = [int(s["value"]) for s in body["statements"] if s["field"] == 19]
            items = [s for s in self.series if not ids or s["libraryId"] in ids]
            return 200, items[(page - 1) * size:page * size]
        if path == "/api/Search/search":
            return 200, {"series": [s for s in self.series if query["queryString"].lower() in s["name"].lower()], "persons": []}
        return 404, None

    def scans(self) -> list[dict]:
        return [c[2] for c in self.calls if c[1] == "/api/Library/scan"]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
