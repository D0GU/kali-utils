"""Careful file moves: never overwrite, and work across drives."""

from __future__ import annotations

import errno
import os
import shutil
import time

HOLD_DAYS = 30      # how long cleared files stay in a tool's holding folder


class MoveRefused(Exception):
    """A move would have overwritten something, or a plan isn't safe to apply."""


def permission_hint(e: OSError) -> str:
    if e.errno in (errno.EACCES, errno.EPERM):
        return (f"{e.strerror}: {e.filename} - your user needs write access to the torrent folders. "
                "If 'kali' was just added to the debian-transmission group, log out and back in.")
    return f"{e.strerror}: {e.filename}"


def move_file(src: str, dst: str) -> None:
    if os.path.lexists(dst):
        raise MoveRefused(f"refusing to overwrite {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(src, dst)  # different filesystem: copy + delete


def remove_empty_dirs(path: str, stop: str) -> None:
    """rmdir `path` and its parents while empty, never going above `stop`."""
    path, stop = os.path.abspath(path), os.path.abspath(stop)
    while path.startswith(stop) and path != stop:
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def purge_held(holding_dir: str) -> None:
    """Permanently remove operations' folders past their time. Only folders named as the tools name them: <operation id>."""
    try:
        names = os.listdir(holding_dir)
    except OSError:
        return
    cutoff = time.time() - HOLD_DAYS * 86400
    for name in names:
        path = os.path.join(holding_dir, name)
        try:
            if name.isdecimal() and not os.path.islink(path) and os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
