"""SQLite journal: every import, merge and edit, so it can be listed and undone."""

from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL, source_name TEXT, source_root TEXT,
    torrent_hash TEXT, magnet TEXT, download_dir TEXT, status TEXT NOT NULL,
    torrent_removed INTEGER DEFAULT 0, leftovers_deleted INTEGER DEFAULT 0, message TEXT);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, operation_id INTEGER REFERENCES operations(id),
    kind TEXT NOT NULL,      -- moved | created | edited | held (to the holding folder) | epub (the package document inside a book, in `original`)
    src TEXT, dst TEXT NOT NULL, original TEXT, book_id INTEGER, title TEXT, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS changes_op ON changes(operation_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, fetched REAL);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def begin(self, kind: str, source_name: str = "", source_root: str = "", torrent: dict | None = None) -> int:
        t = torrent or {}
        cur = self.conn.execute(
            "INSERT INTO operations(created, kind, source_name, source_root, torrent_hash, magnet, download_dir, status)"
            " VALUES (?,?,?,?,?,?,?, 'running')",
            (now(), kind, source_name, source_root, t.get("hashString"), t.get("magnetLink"), t.get("downloadDir")))
        self.conn.commit()
        return cur.lastrowid

    def finish(self, op_id: int, status: str, message: str = "", **flags) -> None:
        sets = ["status = ?", "message = ?"] + [f"{k} = ?" for k in flags]
        self.conn.execute(f"UPDATE operations SET {', '.join(sets)} WHERE id = ?",
                          [status, message, *[int(v) for v in flags.values()], op_id])
        self.conn.commit()

    def record(self, op_id: int, kind: str, dst: str, src: str = "", original: str | None = None,
               book_id: int | None = None, title: str = "") -> None:
        self.conn.execute("INSERT INTO changes(operation_id, kind, src, dst, original, book_id, title, status)"
                          " VALUES (?,?,?,?,?,?,?, 'done')", (op_id, kind, src, dst, original, book_id, title))
        self.conn.commit()

    def operation(self, op_id: int):
        return self.conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()

    def operations(self) -> list:
        return self.conn.execute("""
            SELECT o.*, COUNT(c.id) AS changes, COUNT(DISTINCT c.book_id) AS books
            FROM operations o LEFT JOIN changes c ON c.operation_id = o.id GROUP BY o.id ORDER BY o.id DESC""").fetchall()

    def changes(self, op_id: int) -> list:
        return self.conn.execute("SELECT * FROM changes WHERE operation_id = ? ORDER BY id", (op_id,)).fetchall()

    def later_operations_on_same_books(self, op_id: int) -> list[int]:
        """Later operations, not undone, that changed a book this one changed."""
        return [r["id"] for r in self.conn.execute("""
            SELECT DISTINCT o.id FROM changes c JOIN operations o ON o.id = c.operation_id
            WHERE o.id > ? AND o.status != 'undone' AND c.status = 'done' AND c.book_id IN (
                SELECT book_id FROM changes WHERE operation_id = ? AND status = 'done' AND book_id IS NOT NULL)
            ORDER BY o.id DESC""", (op_id, op_id)).fetchall()]

    def set_change(self, change_id: int, status: str) -> None:
        self.conn.execute("UPDATE changes SET status = ? WHERE id = ?", (status, change_id))
        self.conn.commit()

    def high_water(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'max_book_id'").fetchone()
        return int(row["value"]) if row else 0

    def set_high_water(self, value: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('max_book_id', ?)", (str(value),))
        self.conn.commit()

    def cache_get(self, key: str, max_age: float):
        row = self.conn.execute("SELECT value, fetched FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row and time.time() - row["fetched"] < max_age else None

    def cache_put(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache(key, value, fetched) VALUES (?,?,?)",
                          (key, json.dumps(value), time.time()))
        self.conn.commit()
