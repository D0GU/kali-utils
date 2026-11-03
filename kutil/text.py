"""Comparing titles and names."""

from __future__ import annotations

import difflib
import re


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
