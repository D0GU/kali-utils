"""Human-friendly formatting helpers and a small table renderer."""

from __future__ import annotations

import shutil
import time
import unicodedata

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
          "blue": "34", "magenta": "35", "cyan": "36"}


class Painter:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles if s)
        return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


paint = Painter()


def human_size(n: float | None) -> str:
    if n is None or n < 0:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"  # pragma: no cover


def human_rate(n: float | None) -> str:
    return "-" if not n else human_size(n) + "/s"


def human_eta(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def human_time(epoch: int | None) -> str:
    if not epoch or epoch <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def human_ratio(r: float | None) -> str:
    if r is None or r < 0:
        return "-" if r != -2 else "inf"
    return f"{r:.2f}"


def percent(frac: float | None) -> str:
    if frac is None:
        return "-"
    return "100%" if frac >= 1 else f"{frac * 100:.1f}%"


def bar(frac: float, width: int = 20) -> str:
    frac = max(0.0, min(1.0, frac or 0.0))
    eighths = int(round(frac * width * 8))
    full, part = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[part] if part and full < width else ""
    return ("█" * full + partial).ljust(width, "·")


def text_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def truncate(s: str, width: int) -> str:
    if text_width(s) <= width:
        return s
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > width - 1:
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


def terminal_width() -> int:
    return shutil.get_terminal_size((120, 24)).columns


def table(headers: list[str], rows: list[list], aligns: str, flex: int | None = None,
          width: int | None = None) -> str:
    """Render rows as an aligned table.

    Each cell is either a string or a (text, style...) tuple. `aligns` has one
    char per column ('<' or '>'). The `flex` column absorbs leftover width and
    is truncated to fit the terminal.
    """
    def split(cell):
        if isinstance(cell, tuple):
            return str(cell[0]), cell[1:]
        return str(cell), ()

    cells = [[split(c) for c in row] for row in rows]
    widths = [text_width(h) for h in headers]
    for row in cells:
        for i, (text, _) in enumerate(row):
            widths[i] = max(widths[i], text_width(text))

    if flex is not None:
        total = width or terminal_width()
        fixed = sum(w for i, w in enumerate(widths) if i != flex) + 2 * (len(widths) - 1)
        widths[flex] = max(12, min(widths[flex], total - fixed))

    def fmt_cell(text, styles, i):
        if i == flex:
            text = truncate(text, widths[i])
        pad = widths[i] - text_width(text)
        if i == len(widths) - 1 and aligns[i] == "<":
            pad = 0  # no trailing whitespace on the last column
        styled = paint(text, *styles)
        return (" " * pad + styled) if aligns[i] == ">" else (styled + " " * pad)

    out = ["  ".join(fmt_cell(h, ("bold",), i) for i, h in enumerate(headers)).rstrip()]
    for row in cells:
        out.append("  ".join(fmt_cell(t, s, i) for i, (t, s) in enumerate(row)).rstrip())
    return "\n".join(out)
