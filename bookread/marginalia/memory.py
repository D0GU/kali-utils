"""The external memory: everything Marginalia knows about a book, by word position.

Every read takes `lock`, the last word the reader has reached, and selects only what ends at or before it. There is
no way to ask this class for anything later, which is what makes the spoiler lock a property of the data rather
than a request to the model. Each thread gets its own connection, since preparing and asking run in the background.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import NamedTuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book TEXT PRIMARY KEY, title TEXT NOT NULL, words INTEGER NOT NULL,
    lock INTEGER NOT NULL,              -- the last word the characters know about
    prepared INTEGER NOT NULL DEFAULT 0 -- passages before this index have been read for notes
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY, book TEXT NOT NULL, idx INTEGER NOT NULL,
    word_start INTEGER NOT NULL, word_end INTEGER NOT NULL, chapter TEXT NOT NULL, text TEXT NOT NULL,
    UNIQUE (book, idx));
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(text, content='passages', content_rowid='id');
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY, book TEXT NOT NULL, name TEXT NOT NULL, first_word INTEGER NOT NULL,
    UNIQUE (book, name));
CREATE TABLE IF NOT EXISTS aliases (
    book TEXT NOT NULL, alias TEXT NOT NULL, character INTEGER NOT NULL, first_word INTEGER NOT NULL,
    PRIMARY KEY (book, alias, character));
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, character INTEGER NOT NULL, word INTEGER NOT NULL, passage INTEGER NOT NULL,
    fact TEXT NOT NULL, quote TEXT NOT NULL, UNIQUE (character, fact));
CREATE TABLE IF NOT EXISTS talk (
    id INTEGER PRIMARY KEY, character INTEGER NOT NULL, word INTEGER NOT NULL,   -- where the reader was at the time
    question TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS facts_by_character ON facts (character, word);
CREATE INDEX IF NOT EXISTS talk_by_character ON talk (character, word);
"""

STOP_WORDS = frozenset("""a about after again all am an and any are as at be because been before but by can could did do
does for from had has have he her here him his how i if in into is it its just me more my no not of on or our she should
so some than that the their them then there they this to too up us was we were what when where which who whom why will
with would you your""".split())


class Passage(NamedTuple):
    idx: int
    word_start: int
    word_end: int
    chapter: str
    text: str


class Character(NamedTuple):
    id: int
    name: str
    first_word: int


def normal(text: str) -> str:
    """For comparing text: straight quotes, one space, no case."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).casefold()


def alias_form(name: str) -> str:
    """How a name is matched, so that "Captain" and "The Captain" are one person."""
    name = normal(name)
    return name[4:] if name.startswith("the ") else name


def search_terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+", text.casefold()) if w not in STOP_WORDS]


class Memory:
    def __init__(self, path: str):
        self.path = path if path == ":memory:" else os.path.abspath(path)
        if path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._local = threading.local()
        self._shared = sqlite3.connect(path, check_same_thread=False) if path == ":memory:" else None
        self._write_lock = threading.Lock()
        with self._writing() as db:
            db.executescript(SCHEMA)

    @property
    def db(self) -> sqlite3.Connection:
        if self._shared is not None:
            self._shared.row_factory = sqlite3.Row
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """This thread's connection. The others close with their threads."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _writing(self):
        memory = self

        class Writing:
            def __enter__(self):
                memory._write_lock.acquire()
                return memory.db

            def __exit__(self, kind, value, trace):
                try:
                    if kind:
                        memory.db.rollback()
                    else:
                        memory.db.commit()
                finally:
                    memory._write_lock.release()

        return Writing()

    # ---------------------------------------------------------------- the book and the lock

    def open_book(self, book: str, title: str, words: int, lock: int) -> None:
        """Remembers a book, keeping the lock it already has."""
        with self._writing() as db:
            db.execute("INSERT OR IGNORE INTO books(book, title, words, lock) VALUES (?,?,?,?)", (book, title, words, lock))

    def lock(self, book: str) -> int:
        row = self.db.execute("SELECT lock FROM books WHERE book = ?", (book,)).fetchone()
        return row["lock"] if row else 0

    def set_lock(self, book: str, word: int) -> None:
        with self._writing() as db:
            db.execute("UPDATE books SET lock = ? WHERE book = ?", (word, book))

    def prepared(self, book: str) -> int:
        row = self.db.execute("SELECT prepared FROM books WHERE book = ?", (book,)).fetchone()
        return row["prepared"] if row else 0

    def set_prepared(self, book: str, idx: int) -> None:
        with self._writing() as db:
            db.execute("UPDATE books SET prepared = ? WHERE book = ?", (idx, book))

    # ---------------------------------------------------------------- passages

    def has_passages(self, book: str) -> bool:
        return self.db.execute("SELECT 1 FROM passages WHERE book = ? LIMIT 1", (book,)).fetchone() is not None

    def add_passages(self, book: str, passages: list[Passage]) -> None:
        with self._writing() as db:
            for p in passages:
                cur = db.execute("INSERT INTO passages(book, idx, word_start, word_end, chapter, text) VALUES (?,?,?,?,?,?)",
                                 (book, p.idx, p.word_start, p.word_end, p.chapter, p.text))
                db.execute("INSERT INTO passages_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, p.text))

    def passage_count(self, book: str, lock: int) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM passages WHERE book = ? AND word_end <= ?", (book, lock)).fetchone()
        return row["n"]

    def passages_between(self, book: str, first: int, last: int, lock: int) -> list[Passage]:
        rows = self.db.execute(
            "SELECT idx, word_start, word_end, chapter, text FROM passages "
            "WHERE book = ? AND idx >= ? AND idx < ? AND word_end <= ? ORDER BY idx", (book, first, last, lock)).fetchall()
        return [Passage(*r) for r in rows]

    def search(self, book: str, query: str, lock: int, limit: int) -> list[Passage]:
        """The passages reached so far that best match, best first."""
        terms = search_terms(query)
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        rows = self.db.execute(
            "SELECT p.idx, p.word_start, p.word_end, p.chapter, p.text FROM passages_fts f "
            "JOIN passages p ON p.id = f.rowid "
            "WHERE passages_fts MATCH ? AND p.book = ? AND p.word_end <= ? ORDER BY bm25(passages_fts) LIMIT ?",
            (match, book, lock, limit)).fetchall()
        return [Passage(*r) for r in rows]

    # ---------------------------------------------------------------- characters

    def add_character(self, book: str, name: str, aliases: list[str], word: int) -> int:
        """Finds this character under any of these names, or starts a new one."""
        names = [name] + [a for a in aliases if a]
        with self._writing() as db:
            found = None
            for n in names:
                row = db.execute("SELECT character FROM aliases WHERE book = ? AND alias = ? ORDER BY first_word LIMIT 1",
                                 (book, alias_form(n))).fetchone()
                if row:
                    found = row["character"]
                    break
            if found is None:
                db.execute("INSERT OR IGNORE INTO characters(book, name, first_word) VALUES (?,?,?)", (book, name, word))
                found = db.execute("SELECT id FROM characters WHERE book = ? AND name = ?", (book, name)).fetchone()["id"]
            for n in names:
                db.execute("INSERT OR IGNORE INTO aliases(book, alias, character, first_word) VALUES (?,?,?,?)",
                           (book, alias_form(n), found, word))
        return found

    def characters(self, book: str, lock: int) -> list[Character]:
        """Everyone met so far with something known about them, the most talked about first."""
        rows = self.db.execute(
            "SELECT c.id, c.name, c.first_word FROM characters c "
            "JOIN facts f ON f.character = c.id AND f.word <= ? "
            "WHERE c.book = ? AND c.first_word <= ? GROUP BY c.id ORDER BY COUNT(f.id) DESC, c.first_word", (lock, book, lock)).fetchall()
        return [Character(*r) for r in rows]

    def known_names(self, character: int, lock: int) -> list[str]:
        rows = self.db.execute("SELECT alias FROM aliases WHERE character = ? AND first_word <= ?", (character, lock)).fetchall()
        return [r["alias"] for r in rows]

    # ---------------------------------------------------------------- facts

    def add_fact(self, character: int, word: int, passage: int, fact: str, quote: str) -> None:
        with self._writing() as db:
            db.execute("INSERT OR IGNORE INTO facts(character, word, passage, fact, quote) VALUES (?,?,?,?,?)",
                       (character, word, passage, fact, quote))

    def facts(self, character: int, lock: int, limit: int) -> list[str]:
        """The latest things known, oldest first so that they read in order."""
        rows = self.db.execute("SELECT fact FROM facts WHERE character = ? AND word <= ? ORDER BY word DESC, id DESC LIMIT ?",
                               (character, lock, limit)).fetchall()
        return [r["fact"] for r in reversed(rows)]

    # ---------------------------------------------------------------- conversations

    def add_talk(self, character: int, word: int, question: str, answer: str, sources: str) -> None:
        with self._writing() as db:
            db.execute("INSERT INTO talk(character, word, question, answer, sources, created) VALUES (?,?,?,?,?,?)",
                       (character, word, question, answer, sources, time.time()))

    def talk(self, character: int, lock: int, limit: int) -> list[sqlite3.Row]:
        """What was said at or before this place, oldest first. Going back to reread hides what was said later."""
        rows = self.db.execute("SELECT word, question, answer, sources FROM talk WHERE character = ? AND word <= ? "
                               "ORDER BY word DESC, id DESC LIMIT ?", (character, lock, limit)).fetchall()
        return list(reversed(rows))
