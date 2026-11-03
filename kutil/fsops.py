"""Careful file moves: never overwrite, and work across drives."""

from __future__ import annotations

import errno
import os
import shutil


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
