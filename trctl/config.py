"""Connection settings: defaults < config file < environment < command-line flags."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_URL = "http://127.0.0.1:9091/transmission/rpc"


def config_path() -> Path:
    if os.environ.get("TRCTL_CONFIG"):
        return Path(os.environ["TRCTL_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "trctl" / "config.toml"


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    url: str = DEFAULT_URL
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0


def normalize_url(url: str) -> str:
    """Accept `host`, `host:port`, or a full URL; return a full RPC URL."""
    url = url.strip()
    bare = "://" not in url
    if bare:
        url = "http://" + url
    parts = urlsplit(url)
    netloc = parts.netloc
    # Only assume Transmission's default port for bare `host` input; a full URL
    # without a port (e.g. behind a reverse proxy) is taken as-is.
    if bare and parts.port is None:
        netloc += ":9091"
    path = parts.path if parts.path not in ("", "/") else "/transmission/rpc"
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def read_config_file(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if "password" in data and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"trctl: warning: {path} contains a password but is readable by other users "
              f"(fix: chmod 600 '{path}')", file=sys.stderr)
    return data


def run_password_command(cmd: str) -> str:
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise ConfigError(f"password_command failed: {e}") from None
    return out.stdout.rstrip("\n")


def load_settings(cli_url: str | None = None, cli_user: str | None = None) -> Settings:
    file = read_config_file()
    env = os.environ
    s = Settings()

    s.url = normalize_url(cli_url or env.get("TRCTL_URL") or file.get("url") or DEFAULT_URL)
    s.username = cli_user or env.get("TRCTL_USERNAME") or file.get("username") or None
    s.timeout = float(file.get("timeout", s.timeout))

    if "TRCTL_PASSWORD" in env:
        s.password = env["TRCTL_PASSWORD"]
    elif file.get("password") is not None and (not cli_user or cli_user == file.get("username")):
        s.password = str(file["password"])
    elif file.get("password_command") and (not cli_user or cli_user == file.get("username")):
        s.password = run_password_command(file["password_command"])

    if s.username and s.password is None and sys.stdin.isatty():
        s.password = getpass.getpass(f"Password for {s.username}: ")
    return s


def write_config(values: dict, path: Path | None = None) -> Path:
    """Write a flat key/value TOML file readable only by the current user."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = ["# trctl configuration - see README.md", ""]
    for key, value in values.items():
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key} = {value}")
        else:
            # JSON string escaping is valid for TOML basic strings.
            lines.append(f"{key} = {json.dumps(str(value), ensure_ascii=False)}")
    data = "\n".join(lines) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path
