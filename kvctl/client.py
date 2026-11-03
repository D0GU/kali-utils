"""Kavita REST client (API checked against the v0.9.1.4 openapi.json). Auth is the `x-api-key` header."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

BOOK_LIBRARY = 2
LIBRARY_TYPES = {0: "Manga", 1: "Comic", 2: "Book", 3: "Image", 4: "Light Novel", 5: "Comic (ComicVine)"}
# File type groups Kavita scans for: 1 archive, 2 EPUB, 3 PDF, 4 images
BOOK_FILE_TYPES = [2, 3]
FILTER_LIBRARIES = 19     # SeriesFilterField.Libraries
FILTER_CONTAINS = 5       # FilterComparison.Contains
FILTER_AND = 1            # FilterCombination.And
SORT_NAME = 1             # SeriesSortField.SortName


class KavitaError(Exception):
    pass


class AuthError(KavitaError):
    pass


class Kavita:
    def __init__(self, url: str, api_key: str = "", timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, params: dict | None = None, body=None, auth: bool = True):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: ("true" if v is True else "false" if v is False else v)
                                                 for k, v in params.items()})
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        if auth:
            if not self.api_key:
                raise AuthError("no API key configured - run `kvctl setup`")
            req.add_header("x-api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", "replace").strip()
            if e.code == 401:
                raise AuthError("Kavita rejected the API key (HTTP 401) - run `kvctl setup`") from None
            if e.code == 403:
                raise KavitaError("not allowed (HTTP 403) - this needs an admin account's API key") from None
            raise KavitaError(f"Kavita: HTTP {e.code} on {path}" + (f": {detail}" if detail else "")) from None
        except urllib.error.URLError as e:
            raise KavitaError(f"cannot reach Kavita at {self.url}: {e.reason}") from None
        except (socket.timeout, TimeoutError):
            raise KavitaError(f"timed out talking to Kavita at {self.url}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # -- server ---------------------------------------------------------------

    def healthy(self) -> bool:
        return self._call("GET", "/api/Health", auth=False) is not None

    def info(self) -> dict:
        return self._call("GET", "/api/Server/server-info-slim") or {}

    def stats(self) -> dict:
        return self._call("GET", "/api/Stats/server/stats") or {}

    # -- libraries ------------------------------------------------------------

    def libraries(self) -> list[dict]:
        return self._call("GET", "/api/Library/libraries") or []

    def library_for_folder(self, folder: str) -> dict | None:
        """The library that covers `folder`, preferring the closest root."""
        folder = folder.rstrip("/")
        best, best_len = None, -1
        for lib in self.libraries():
            for root in lib.get("folders") or []:
                root = root.rstrip("/")
                if (folder == root or folder.startswith(root + "/") or root.startswith(folder + "/")) and len(root) > best_len:
                    best, best_len = lib, len(root)
        return best

    @staticmethod
    def _library_dto(name: str, folders: list[str], kind: int, **extra) -> dict:
        dto = {"id": 0, "name": name, "type": kind, "folders": folders, "folderWatching": True, "includeInDashboard": True,
               "includeInSearch": True, "manageCollections": True, "manageReadingLists": True, "allowScrobbling": True,
               "allowMetadataMatching": True, "enableMetadata": True, "removePrefixForSortName": False,
               "inheritWebLinksFromFirstChapter": False, "defaultLanguage": "", "fileGroupTypes": BOOK_FILE_TYPES,
               "excludePatterns": []}
        dto.update(extra)
        return dto

    def create_library(self, name: str, folders: list[str], kind: int = BOOK_LIBRARY) -> None:
        self._call("POST", "/api/Library/create", body=self._library_dto(name, folders, kind))

    def update_library(self, lib: dict, name: str | None = None, folders: list[str] | None = None) -> None:
        """Change a library, keeping every other setting it already has."""
        groups = lib.get("libraryFileTypes") or []
        types = [g.get("fileTypeGroup", g) if isinstance(g, dict) else g for g in groups] or BOOK_FILE_TYPES
        keep = {k: lib[k] for k in ("folderWatching", "includeInDashboard", "includeInSearch", "manageCollections",
                                    "manageReadingLists", "allowScrobbling", "allowMetadataMatching", "enableMetadata",
                                    "removePrefixForSortName", "inheritWebLinksFromFirstChapter", "defaultLanguage",
                                    "metadataProvider") if k in lib}
        patterns = [p.get("pattern", p) if isinstance(p, dict) else p for p in lib.get("excludePatterns") or []]
        dto = self._library_dto(name or lib["name"], folders if folders is not None else lib.get("folders") or [],
                                lib.get("type", BOOK_LIBRARY), id=lib["id"], fileGroupTypes=types,
                                excludePatterns=patterns, **keep)
        self._call("POST", "/api/Library/update", body=dto)

    def delete_library(self, library_id: int) -> None:
        self._call("DELETE", "/api/Library/delete", {"libraryId": library_id})

    def scan(self, library_id: int, force: bool = False) -> None:
        self._call("POST", "/api/Library/scan", {"libraryId": library_id, "force": force})

    def scan_all(self, force: bool = False) -> None:
        self._call("POST", "/api/Library/scan-all", {"force": force})

    def refresh_metadata(self, library_id: int, force: bool = False) -> None:
        self._call("POST", "/api/Library/refresh-metadata", {"libraryId": library_id, "force": force,
                                                             "forceColorscape": False})

    # -- content --------------------------------------------------------------

    def series(self, library_id: int | None = None, page: int = 1, size: int = 200) -> list[dict]:
        statements = []
        if library_id:
            statements.append({"comparison": FILTER_CONTAINS, "field": FILTER_LIBRARIES, "value": str(library_id)})
        body = {"statements": statements, "combination": FILTER_AND,
                "sortOptions": {"sortField": SORT_NAME, "isAscending": True}, "limitTo": 0}
        return self._call("POST", "/api/Series/all-v2", {"PageNumber": page, "PageSize": size}, body) or []

    def search(self, query: str) -> dict:
        return self._call("GET", "/api/Search/search", {"queryString": query, "includeChapterAndFiles": False}) or {}

    def delete_series(self, series_id: int) -> None:
        self._call("DELETE", f"/api/Series/{series_id}")
