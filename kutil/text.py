"""Comparing titles and names, and cleaning text that came from files or the internet."""

from __future__ import annotations

import difflib
import re
import unicodedata

# Control and invisible characters. Escapes must never reach a terminal, and these have no width to lay out.
_INVISIBLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def clean_text(text: str) -> str:
    return unicodedata.normalize("NFC", _INVISIBLE.sub("", text))


def normalize(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[\'’`]", "", s)
    s = re.sub(r"[^\w]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta):
        ratio = max(ratio, 0.75 + 0.25 * min(len(ta), len(tb)) / max(len(ta), len(tb)))
    return ratio
