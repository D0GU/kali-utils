"""SQLite library database: what was sorted, where it went, and how to undo it."""

from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    created TEXT NOT NULL,
    source_name TEXT,
    source_root TEXT,
    torrent_hash TEXT,
    magnet TEXT,
    download_dir TEXT,
    status TEXT NOT NULL,              -- running | applied | failed | undone
    torrent_removed INTEGER DEFAULT 0,
    leftovers_deleted INTEGER DEFAULT 0,
    message TEXT
);
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,            -- movie | show | music (music: title = album, artist = album artist)
    title TEXT NOT NULL,
    year INTEGER,
    path TEXT NOT NULL UNIQUE,
    ids TEXT DEFAULT '{}',
    provider TEXT DEFAULT '',
    anime INTEGER DEFAULT 0,
    added TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    title_id INTEGER REFERENCES titles(id) ON DELETE CASCADE,
    operation_id INTEGER REFERENCES operations(id),
    kind TEXT,                         -- movie | episode | extra | subtitle | track | artwork
    season INTEGER, episode INTEGER, episode_end INTEGER, ep_title TEXT,   -- tracks: disc, track, -, title
    part INTEGER, extra_type TEXT, extra_name TEXT, lang TEXT,
    src TEXT,                          -- original absolute path (for undo)
    dst TEXT NOT NULL,                 -- current absolute path in the library
    size INTEGER,
    status TEXT NOT NULL,              -- present | undone | missing
    added TEXT
);
CREATE INDEX IF NOT EXISTS files_title ON files(title_id);
CREATE INDEX IF NOT EXISTS files_dst ON files(dst);
CREATE INDEX IF NOT EXISTS files_op ON files(operation_id);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, fetched REAL);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release."""
        for table, column, decl in (("titles", "artist", "TEXT DEFAULT ''"), ("files", "artist", "TEXT")):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- operations -----------------------------------------------------------

    def begin_operation(self, source_name, source_root, torrent=None) -> int:
        t = torrent or {}
        cur = self.conn.execute(
            "INSERT INTO operations(created, source_name, source_root, torrent_hash, magnet, download_dir, status)"
            " VALUES (?,?,?,?,?,?, 'running')",
            (now(), source_name, source_root, t.get("hashString"), t.get("magnetLink"), t.get("downloadDir")))
        self.conn.commit()
        return cur.lastrowid

    def finish_operation(self, op_id: int, status: str, message: str = "", **flags) -> None:
        sets = ["status = ?", "message = ?"] + [f"{k} = ?" for k in flags]
        self.conn.execute(f"UPDATE operations SET {', '.join(sets)} WHERE id = ?",
                          [status, message, *[int(v) for v in flags.values()], op_id])
        self.conn.commit()

    def operation(self, op_id: int):
        return self.conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()

    def operations(self) -> list:
        return self.conn.execute("""
            SELECT o.*, COUNT(f.id) AS files, COALESCE(SUM(f.size), 0) AS size,
                   SUM(f.status = 'present') AS present
            FROM operations o LEFT JOIN files f ON f.operation_id = o.id
            GROUP BY o.id ORDER BY o.id DESC""").fetchall()

    def operation_files(self, op_id: int) -> list:
        return self.conn.execute("SELECT * FROM files WHERE operation_id = ? ORDER BY id", (op_id,)).fetchall()

    # -- titles & files -------------------------------------------------------

    def upsert_title(self, category, title, year, path, ids=None, provider="", anime=False, artist="") -> int:
        row = self.conn.execute("SELECT id FROM titles WHERE path = ?", (path,)).fetchone()
        ids_json = json.dumps(ids or {}, sort_keys=True)
        if row:
            self.conn.execute("UPDATE titles SET category=?, title=?, year=?, ids=?, provider=?, anime=?, artist=?,"
                              " updated=? WHERE id=?",
                              (category, title, year, ids_json, provider, int(anime), artist or "", now(), row["id"]))
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO titles(category, title, year, path, ids, provider, anime, artist, added, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (category, title, year, path, ids_json, provider, int(anime), artist or "", now(), now()))
        self.conn.commit()
        return cur.lastrowid

    def update_title(self, title_id: int, **fields) -> None:
        if "ids" in fields:
            fields["ids"] = json.dumps(fields["ids"] or {}, sort_keys=True)
        fields["updated"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE titles SET {sets} WHERE id = ?", [*fields.values(), title_id])
        self.conn.commit()

    def title(self, title_id: int):
        return self.conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone()

    def title_by_path(self, path: str):
        return self.conn.execute("SELECT * FROM titles WHERE path = ?", (path,)).fetchone()

    def add_file(self, title_id: int, op_id: int | None, kind: str, src: str, dst: str, size: int,
                 **meta) -> int:
        cols = ["title_id", "operation_id", "kind", "src", "dst", "size", "status", "added", *meta]
        vals = [title_id, op_id, kind, src, dst, size, "present", now(), *meta.values()]
        cur = self.conn.execute(f"INSERT INTO files({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        self.conn.commit()
        return cur.lastrowid

    def update_file(self, file_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE files SET {sets} WHERE id = ?", [*fields.values(), file_id])
        self.conn.commit()

    def file_by_dst(self, dst: str):
        return self.conn.execute("SELECT * FROM files WHERE dst = ? AND status != 'undone'", (dst,)).fetchone()

    def titles(self, category: str | None = None, search: str | None = None) -> list:
        sql = """
            SELECT t.*,
                   SUM(f.kind = 'episode' AND f.status = 'present') AS episodes,
                   COUNT(DISTINCT CASE WHEN f.kind = 'episode' AND f.status = 'present' THEN f.season END) AS seasons,
                   SUM(f.kind IN ('movie', 'episode', 'extra') AND f.status = 'present') AS videos,
                   SUM(f.kind = 'track' AND f.status = 'present') AS tracks,
                   COUNT(DISTINCT CASE WHEN f.kind = 'track' AND f.status = 'present' THEN f.season END) AS discs,
                   SUM(f.status = 'missing') AS missing,
                   COALESCE(SUM(CASE WHEN f.status = 'present' THEN f.size END), 0) AS size,
                   MAX(f.added) AS last_added
            FROM titles t LEFT JOIN files f ON f.title_id = t.id
            WHERE 1 = 1"""
        args: list = []
        if category:
            sql += " AND t.category = ?"
            args.append(category)
        if search:
            sql += " AND (t.title LIKE ? OR t.artist LIKE ?)"
            args += [f"%{search}%", f"%{search}%"]
        sql += (" GROUP BY t.id HAVING SUM(f.status IN ('present', 'missing')) > 0"
                " ORDER BY t.category = 'music', t.artist COLLATE NOCASE, t.title COLLATE NOCASE")
        return self.conn.execute(sql, args).fetchall()

    def title_files(self, title_id: int, include_undone: bool = False) -> list:
        sql = "SELECT * FROM files WHERE title_id = ?" + ("" if include_undone else " AND status != 'undone'")
        return self.conn.execute(sql + " ORDER BY kind IN ('subtitle', 'artwork'), season, episode, part, dst",
                                 (title_id,)).fetchall()

    def counts(self) -> dict:
        rows = self.titles()
        return {"movie": sum(1 for r in rows if r["category"] == "movie"),
                "show": sum(1 for r in rows if r["category"] == "show"),
                "music": sum(1 for r in rows if r["category"] == "music"),
                "episodes": sum(r["episodes"] or 0 for r in rows),
                "tracks": sum(r["tracks"] or 0 for r in rows),
                "size": sum(r["size"] or 0 for r in rows)}

    def prune_titles(self) -> None:
        self.conn.execute("DELETE FROM titles WHERE id NOT IN "
                          "(SELECT title_id FROM files WHERE status IN ('present', 'missing'))")
        self.conn.commit()

    def forget_title(self, title_id: int) -> None:
        self.conn.execute("DELETE FROM files WHERE title_id = ?", (title_id,))
        self.conn.execute("DELETE FROM titles WHERE id = ?", (title_id,))
        self.conn.commit()

    # -- HTTP cache -----------------------------------------------------------

    def cache_get(self, key: str, max_age: float):
        row = self.conn.execute("SELECT value, fetched FROM cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["fetched"] < max_age:
            return json.loads(row["value"])
        return None

    def cache_put(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache(key, value, fetched) VALUES (?,?,?)",
                          (key, json.dumps(value), time.time()))
        self.conn.commit()
