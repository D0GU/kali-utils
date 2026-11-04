"""Reading speed, learned from how long each page stays on screen.

It starts at the global average and is a running average of one sample per page turn. The starting value counts as
PRIOR_PAGES pages, so a few odd turns don't swing it, and the average stops growing at MAX_PAGES, so a change of
speed still shows up after a few hundred pages.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WPM = 238.0      # Brysbaert 2019, meta-analysis of silent reading (English non-fiction; fiction was 260)
PRIOR_PAGES = 20
MAX_PAGES = 200
MIN_WORDS = 30           # a chapter's last few lines say little about speed
MIN_WPM, MAX_WPM = 60, 1000   # outside this nobody was reading: flipping through, or away from the screen


@dataclass
class Pace:
    wpm: float = DEFAULT_WPM
    samples: int = 0

    @classmethod
    def load(cls, db) -> "Pace":
        try:
            return cls(float(db.meta_get("pace_wpm", DEFAULT_WPM)), int(db.meta_get("pace_samples", 0)))
        except ValueError:
            return cls()

    def save(self, db) -> None:
        db.meta_set("pace_wpm", self.wpm)
        db.meta_set("pace_samples", self.samples)

    def record(self, words: int, seconds: float) -> bool:
        """One page read. False if the turn doesn't measure reading; the estimate is then unchanged."""
        if words < MIN_WORDS or seconds <= 0:
            return False
        sample = words / seconds * 60
        if not MIN_WPM <= sample <= MAX_WPM:
            return False
        self.wpm += (sample - self.wpm) / (min(self.samples + PRIOR_PAGES, MAX_PAGES) + 1)
        self.samples += 1
        return True

    def seconds_for(self, words: int) -> float:
        return words / self.wpm * 60


def human_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"
