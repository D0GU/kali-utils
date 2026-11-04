"""Lay a book out for a text width and page height. Runs again on every resize, so it works from numbers the parser
already measured and only touches the text of a word when that word has to be cut."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import NamedTuple

from .ebook import Book, cell_width


class Line(NamedTuple):
    block: int          # index into book.blocks; -1 for a blank line
    start: int          # slice of that block's text
    end: int
    word: int           # the first word on this line, or the next word if the line has none
    x: int              # cells in from the text's left edge
    page_break: bool = False


def _blank(word: int) -> Line:
    return Line(-1, 0, 0, word, 0)


def _wrap(book: Book, width: int) -> list[Line]:
    lines: list[Line] = []
    for index, block in enumerate(book.blocks):
        first = len(lines)
        if lines and not block.tight and not block.new_page:
            lines.append(_blank(block.word))
            first += 1
        if block.kind == "break":
            lines.append(Line(index, 0, len(block.text), block.word, max(0, (width - len(block.text)) // 2)))
        else:
            _wrap_block(lines, index, block, width)
        if block.new_page and first < len(lines):
            lines[first] = lines[first]._replace(page_break=True)
    return lines


def _wrap_block(lines: list[Line], index: int, block, width: int) -> None:
    indent = min(block.indent, width // 2)
    room = width - indent
    centred = block.kind == "h"
    text = block.text

    def emit(start: int, end: int, word: int, used: int) -> None:
        lines.append(Line(index, start, end, word, max(0, (width - used) // 2) if centred else indent))

    line_start = line_end = line_word = used = 0
    is_open = False
    word = block.word
    for start, end, cells in zip(block.starts, block.ends, block.widths):
        if is_open and used + (start - line_end) + cells <= room:
            used += start - line_end + cells
            line_end = end
        else:
            if is_open:
                emit(line_start, line_end, line_word, used)
            line_start, line_end, line_word, used = start, end, word, cells
            is_open = True
            if cells > room:     # too wide for any line: cut it, and the last part can be followed by the next word
                line_start, used = _cut_word(text, start, end, room, word, emit)
        word += 1
    if is_open:
        emit(line_start, line_end, line_word, used)


def _cut_word(text: str, start: int, end: int, room: int, word: int, emit) -> tuple[int, int]:
    used = 0
    for i in range(start, end):
        cells = cell_width(text[i])
        if used and used + cells > room:
            emit(start, i, word, used)
            start, used = i, 0
        used += cells
    return start, used


class Layout:
    def __init__(self, book: Book, width: int, rows: int):
        self.book, self.width, self.rows = book, width, rows
        self.lines = _wrap(book, width)
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._paginate()
        self._words = [self.lines[i].word for i in self._starts]

    def _is_heading(self, line: Line) -> bool:
        return line.block >= 0 and self.book.blocks[line.block].kind == "h"

    def _paginate(self) -> None:
        lines, rows = self.lines, self.rows
        n = len(lines)
        i = 0
        while i < n:
            if lines[i].block < 0:
                i += 1
                continue
            end = i + 1
            while end < n and end - i < rows and not lines[end].page_break:
                end += 1
            if end < n and not lines[end].page_break:
                end = self._keep_heading_with_text(i, end)
            self._starts.append(i)
            self._ends.append(end)
            i = end

    def _keep_heading_with_text(self, start: int, end: int) -> int:
        """Move a heading at the foot of a page over to the text it introduces, unless that would empty the page."""
        cut = end
        moved = False
        while cut > start:
            line = self.lines[cut - 1]
            if line.block >= 0 and not self._is_heading(line):
                break
            moved = moved or line.block >= 0
            cut -= 1
        return cut if moved and cut > start else end

    @property
    def pages(self) -> int:
        return len(self._starts)

    def page_lines(self, page: int) -> list[Line]:
        return self.lines[self._starts[page]:self._ends[page]]

    def start_word(self, page: int) -> int:
        return self._words[page]

    def end_word(self, page: int) -> int:
        return self._words[page + 1] if page + 1 < len(self._words) else self.book.words

    def page_of(self, word: int) -> int:
        """The page showing this word. A word cut across pages is found on its first part."""
        found = bisect_right(self._words, word) - 1
        return bisect_left(self._words, self._words[max(0, found)])

    def text(self, line: Line) -> str:
        return "" if line.block < 0 else self.book.blocks[line.block].text[line.start:line.end]

    def segments(self, line: Line) -> list[tuple[str, int]]:
        """The line's text split where its style changes, as [(text, style)]."""
        if line.block < 0:
            return []
        block = self.book.blocks[line.block]
        if not block.spans:
            return [(block.text[line.start:line.end], 0)]
        out, at = [], line.start
        for start, end, style in block.spans:
            start, end = max(start, line.start), min(end, line.end)
            if start >= end:
                continue
            if start > at:
                out.append((block.text[at:start], 0))
            out.append((block.text[start:end], style))
            at = end
        if at < line.end:
            out.append((block.text[at:line.end], 0))
        return out
