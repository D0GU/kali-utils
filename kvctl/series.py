"""Work out which books belong to one series. Kavita groups books by the series name and number inside the EPUB. Without
both it falls back to the title, which usually makes each book its own series.

A title alone is weak evidence ('Catch 22: A Novel' isn't book 22 of 'Catch'), so a guess only counts when it puts two
or more books by the same author together.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from kutil.text import normalize

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d{1,3}(?:\.\d+)?|" + "|".join(_WORDS) + r")"
_MARK = r"(?:book|vol(?:ume)?\.?|#)"
_PATTERNS = [
    re.compile(rf"\(\s*(?P<name>[^()]+?)[,:]?\s+{_MARK}\s*{_NUM}\s*\)", re.I),         # Savage Webs (Apocalypse Cultivation Book 2)
    re.compile(rf"^(?P<name>.+?)[,:]?\s+{_MARK}\s*{_NUM}\b", re.I),                    # The Saga of Tanya the Evil, Vol. 1: Deus lo Vult
    re.compile(r"^(?P<name>.+?)\s+(\d{1,3})(?=\s*:|\s*$|\s+-\s)", re.I),               # Combat Healer 2: Holstaur Essence Archer
]


def guess(title: str) -> tuple[str, float] | None:
    """(series name, number) if the title states them."""
    clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", title).strip()
    for pattern in _PATTERNS:
        m = pattern.search(clean)
        if m:
            number = m.group(2).lower()
            return m.group("name").strip(" ,:-"), float(_WORDS.get(number, number))
    return None


@dataclass
class Item:
    key: object
    author: str         # author_key of the first author
    title: str
    series: str = ""
    index: float | None = None
    date: str = ""


@dataclass
class Assignment:
    series: str
    index: float
    number_guessed: bool = False    # no number in the title: it took the free place in the series


def same_series(a: str, b: str) -> bool:
    """Equal, or one name inside the other as whole words ('Reincarnated as a Slime' in 'That Time I Got ...')."""
    a, b = normalize(a), normalize(b)
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short.split()) >= 2 and f" {short} " in f" {long} "


def _names_series(title: str, name: str) -> bool:
    """The title is the series name, or the series name is its main title or its subtitle."""
    parts = [normalize(p) for p in title.split(":", 1)]
    return normalize(name) in parts


def assign(items: list[Item]) -> dict[object, Assignment]:
    """What to give the books with no series or no number. Books that already have both are never changed."""
    by_author: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_author[item.author].append(item)
    out: dict[object, Assignment] = {}
    for books in by_author.values():
        groups: list[dict] = []      # {name, members: [(item, number or None)]}

        def group_for(name: str, add: bool = True):
            for g in groups:
                if same_series(g["name"], name):
                    return g
            if not add:
                return None
            groups.append({"name": name, "members": []})
            return groups[-1]

        for item in books:
            if item.series:
                group_for(item.series)["members"].append((item, item.index))
        loose = [i for i in books if not i.series]
        unplaced = []
        for item in loose:
            found = guess(item.title)
            if found:
                group_for(found[0])["members"].append((item, found[1]))
            else:
                unplaced.append(item)
        for item in unplaced:
            for g in groups:
                if _names_series(item.title, g["name"]):
                    g["members"].append((item, None))
                    break
        for g in groups:
            if len(g["members"]) < 2:
                continue
            taken = {n for _, n in g["members"] if n is not None}
            free = (n for n in range(1, 10_000) if n not in taken)
            # A book titled just the series name came first; the rest go by publication date.
            waiting = sorted((i for i, n in g["members"] if n is None),
                             key=lambda i: (normalize(i.title) != normalize(g["name"]), i.date or "9", i.title))
            numbers = {id(i): float(next(free)) for i in waiting}
            for item, number in g["members"]:
                if item.series and item.index is not None:
                    continue
                out[item.key] = Assignment(item.series or g["name"], number if number is not None else numbers[id(item)], number is None)
    return out
