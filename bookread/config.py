"""bookread settings: ~/.config/bookread/config.toml, edited by hand. It's optional; these are the defaults."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULTS = {"books_dir": "/mnt/data/media/Books", "database": "~/.local/share/bookread/reading.db"}


def config_path() -> Path:
    if os.environ.get("BOOKREAD_CONFIG"):
        return Path(os.environ["BOOKREAD_CONFIG"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bookread" / "config.toml"


def load() -> dict:
    cfg = dict(DEFAULTS)
    path = config_path()
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    cfg["books_dir"] = os.path.abspath(os.path.expanduser(cfg["books_dir"]))
    cfg["database"] = os.path.expanduser(cfg["database"])
    return cfg
