"""plexsort settings (~/.config/plexsort/config.toml).

Library folders are lists: each library type can span several folders (e.g. on different drives).
The first folder is the preferred one; others are used when a title already lives there or when
the preferred drive doesn't have room.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "movies_dirs": ["/mnt/data/media/Movies"],
    "tv_dirs": ["/mnt/data/media/TV Shows"],
    "music_dirs": ["/mnt/data/media/Music"],
    "inbox_dirs": [],           # watched folders: their contents show up in the Inbox (Transmission's are automatic)
    "database": "~/.local/share/plexsort/library.db",
    "tmdb_api_key": "",
    "id_tags": True,            # add {tvdb-..}/{tmdb-..}/{imdb-..} to folder names so Plex matches exactly
    "remove_torrent": True,     # remove the torrent from Transmission after sorting (data is kept/moved)
    "delete_leftovers": True,   # delete junk (.txt, .nfo, samples) left behind in the torrent folder
    "online": True,             # look titles up online
    "download_cover_art": True, # fetch album covers from the Cover Art Archive when an album has none
    "min_free_gb": 2,           # always leave at least this much free space on a destination drive
}

BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
LIST_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, list)}
INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
LEGACY = {"movies_dir": "movies_dirs", "tv_dir": "tv_dirs", "music_dir": "music_dirs"}


def config_path() -> Path:
    if os.environ.get("PLEXSORT_CONFIG"):
        return Path(os.environ["PLEXSORT_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "plexsort" / "config.toml"


def exists() -> bool:
    return config_path().exists()


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    return [os.path.expanduser(str(v).strip()) for v in value or [] if str(v).strip()]


def load() -> dict:
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    path = config_path()
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
        for old, new in LEGACY.items():   # single-folder settings from earlier versions
            if old in data and new not in data:
                data[new] = [data.pop(old)]
            data.pop(old, None)
        cfg.update(data)
    for env in ("PLEXSORT_TMDB_KEY", "TMDB_API_KEY"):
        if os.environ.get(env):
            cfg["tmdb_api_key"] = os.environ[env]
    for key in LIST_KEYS:
        cfg[key] = _as_list(cfg.get(key))
    cfg["database"] = os.path.expanduser(str(cfg["database"]))
    return cfg


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)  # JSON string escaping is valid TOML


def save(cfg: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = ["# plexsort configuration - edit with `plexsort setup` or the Settings screen", ""]
    for key, default in DEFAULTS.items():
        lines.append(f"{key} = {_toml(cfg.get(key, default))}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # may hold an API key
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def parse_value(key: str, raw: str):
    if key in BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if key in LIST_KEYS:
        return _as_list(raw)
    if key in INT_KEYS:
        return int(raw)
    return raw
