"""What bookread remembers: your place in each book, your reading speed, and how you like the page to look."""

from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS reading (
    book TEXT PRIMARY KEY,   -- library.book_key: the same when a book is moved, renamed or has its metadata edited
    position INTEGER NOT NULL, words INTEGER NOT NULL, percent REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lengths (book TEXT PRIMARY KEY, words INTEGER NOT NULL);   -- books not opened yet
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            path = os.path.abspath(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=3)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def meta_get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def lengths(self) -> dict[str, int]:
        return {r["book"]: r["words"] for r in self.conn.execute("SELECT * FROM lengths")}

    def length_save(self, book: str, words: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO lengths(book, words) VALUES (?, ?)", (book, words))
        self.conn.commit()

    def reading_get(self, book: str):
        return self.conn.execute("SELECT * FROM reading WHERE book = ?", (book,)).fetchone()

    def reading_all(self) -> dict:
        return {r["book"]: r for r in self.conn.execute("SELECT * FROM reading")}

    def reading_save(self, book: str, position: int, words: int, percent: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO reading(book, position, words, percent, updated) VALUES (?,?,?,?,?)",
                          (book, position, words, percent, time.time()))
        self.conn.commit()
