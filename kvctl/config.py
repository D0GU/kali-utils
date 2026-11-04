"""kvctl settings (~/.config/kvctl/config.toml). The API key is only ever written with mode 600."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "url": "http://127.0.0.1:5000",
    "api_key": "",
    "books_dir": "/mnt/data/media/Books",
    "inbox_dirs": ["/mnt/data/kvctl"],   # watched folders; finished torrents with books are always offered too
    "database": "~/.local/share/kvctl/library.db",
    "library_id": 0,                     # Kavita library to scan after importing; 0 = the one holding books_dir
    "remove_torrent": True,
    "delete_leftovers": True,            # moved to holding_dir and kept 30 days, not deleted at once
    "holding_dir": "/mnt/data/kvctl-holding",
    "online": True,
    "scan_after_import": True,
    "min_free_gb": 2,
}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
LIST_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, list)}
INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}


def config_path() -> Path:
    if os.environ.get("KVCTL_CONFIG"):
        return Path(os.environ["KVCTL_CONFIG"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kvctl" / "config.toml"


def load() -> dict:
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    path = config_path()
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    if os.environ.get("KVCTL_API_KEY"):
        cfg["api_key"] = os.environ["KVCTL_API_KEY"]
    if os.environ.get("KVCTL_URL"):
        cfg["url"] = os.environ["KVCTL_URL"]
    cfg["inbox_dirs"] = [os.path.abspath(os.path.expanduser(d)) for d in cfg["inbox_dirs"]]
    cfg["books_dir"] = os.path.abspath(os.path.expanduser(cfg["books_dir"]))
    cfg["holding_dir"] = os.path.abspath(os.path.expanduser(cfg["holding_dir"]))
    cfg["database"] = os.path.expanduser(cfg["database"])
    return cfg


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def save(cfg: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stored: dict = {}
    if path.exists():
        with path.open("rb") as f:
            stored = tomllib.load(f)
    cfg = dict(cfg)
    for key, env in (("api_key", "KVCTL_API_KEY"), ("url", "KVCTL_URL")):   # environment overrides stay out of the file
        if os.environ.get(env) and cfg.get(key) == os.environ[env]:
            cfg[key] = stored.get(key, DEFAULTS[key])
    lines = ["# kvctl configuration - edit with `kvctl setup`, `kvctl config` or Settings in the UI", ""]
    lines += [f"{k} = {_toml(cfg.get(k, d))}" for k, d in DEFAULTS.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def parse_value(key: str, raw: str):
    if key in BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if key in LIST_KEYS:
        return [d.strip() for d in raw.split(",") if d.strip()]
    if key in INT_KEYS:
        return int(raw)
    return raw
