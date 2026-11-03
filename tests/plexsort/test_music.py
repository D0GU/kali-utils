"""Music support and keyless providers: tag reading, album detection, naming, sorting, scoring. Offline."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # kali-utils root
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "trctl")]  # trctl's fake daemon

from plexsort import engine, naming  # noqa: E402
from plexsort.db import DB  # noqa: E402
from plexsort.music import parse_album_dir  # noqa: E402
from plexsort.parse import Plan, build_items  # noqa: E402
from plexsort.providers import (Match, Matcher, Wikidata, apply_match, query_variants, score_match,  # noqa: E402
                                score_music)
from plexsort.tags import read_tags  # noqa: E402


# --------------------------------------------------------------------------
# Hand-built tagged files
# --------------------------------------------------------------------------

def vorbis_block(tags: dict) -> bytes:
    out = struct.pack("<I", 1) + b"x" + struct.pack("<I", len(tags))
    for k, v in tags.items():
        e = f"{k}={v}".encode()
        out += struct.pack("<I", len(e)) + e
    return out


def flac(tags: dict) -> bytes:
    streaminfo = b"\x00" + (34).to_bytes(3, "big") + b"\x00" * 34
    vc = vorbis_block(tags)
    return b"fLaC" + streaminfo + bytes([0x84]) + len(vc).to_bytes(3, "big") + vc + b"\xff\xf8audio"


def syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3(tags: dict, version: int = 3) -> bytes:
    frames = b""
    for fid, text in tags.items():
        if version == 4:
            data = b"\x03" + text.encode("utf-8")
            frames += fid.encode() + syncsafe(len(data)) + b"\x00\x00" + data
        else:
            data = b"\x01" + text.encode("utf-16")  # with BOM
            frames += fid.encode() + struct.pack(">I", len(data)) + b"\x00\x00" + data
    return b"ID3" + bytes([version, 0, 0]) + syncsafe(len(frames) + 10) + frames + b"\x00" * 10 + b"\xff\xfbmp3"


def atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def m4a(title: str, album: str, track: int, total: int) -> bytes:
    def text_item(kind, value):
        return atom(kind, atom(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + value.encode()))
    trkn = atom(b"trkn", atom(b"data", b"\x00" * 8 + struct.pack(">HHHH", 0, track, total, 0)))
    ilst = atom(b"ilst", text_item(b"\xa9nam", title) + text_item(b"\xa9alb", album) + trkn)
    meta = atom(b"meta", b"\x00\x00\x00\x00" + ilst)
    return atom(b"ftyp", b"M4A \x00\x00\x00\x00") + atom(b"mdat", b"\x00" * 64) + atom(b"moov", atom(b"udta", meta))


def ogg_page(packet: bytes, seq: int) -> bytes:
    segs = [255] * (len(packet) // 255) + [len(packet) % 255]
    return (b"OggS\x00\x00" + b"\x00" * 8 + b"\x01\x00\x00\x00" + struct.pack("<I", seq) + b"\x00" * 4
            + bytes([len(segs)]) + bytes(segs) + packet)


def opus(tags: dict) -> bytes:
    return ogg_page(b"OpusHead" + b"\x01" * 11, 0) + ogg_page(b"OpusTags" + vorbis_block(tags), 1)


class TestTags(unittest.TestCase):
    def read(self, name, data):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, name)
            Path(p).write_bytes(data)
            return read_tags(p)

    def test_flac(self):
        t = self.read("a.flac", flac({"TITLE": "Airbag", "ARTIST": "Radiohead", "ALBUM": "OK Computer",
                                      "TRACKNUMBER": "1/12", "DATE": "1997-05-21", "DISCNUMBER": "1"}))
        self.assertEqual((t["title"], t["artist"], t["album"], t["tracknumber"], t["tracktotal"], t["date"]),
                         ("Airbag", "Radiohead", "OK Computer", "1", "12", "1997-05-21"))

    def test_id3v23_utf16_and_v24_utf8(self):
        t = self.read("a.mp3", id3({"TIT2": "Ünïcode Song", "TPE1": "Björk", "TALB": "Homogenic", "TRCK": "3/10",
                                    "TYER": "1997"}, 3))
        self.assertEqual((t["title"], t["artist"], t["tracknumber"], t["date"]), ("Ünïcode Song", "Björk", "3", "1997"))
        t = self.read("b.mp3", id3({"TIT2": "Song", "TPE2": "Various Artists", "TDRC": "2001", "TPOS": "2/2"}, 4))
        self.assertEqual((t["albumartist"], t["date"], t["discnumber"], t["disctotal"]), ("Various Artists", "2001", "2", "2"))

    def test_id3v1_fallback(self):
        v1 = b"TAG" + b"Old Song".ljust(30, b"\x00") + b"Old Artist".ljust(30, b"\x00") + b"Old Album".ljust(30, b"\x00") \
            + b"1985" + b"\x00" * 28 + b"\x00\x07" + b"\x00"
        t = self.read("c.mp3", b"\xff\xfb" + b"\x00" * 200 + v1)
        self.assertEqual((t["title"], t["artist"], t["tracknumber"], t["date"]), ("Old Song", "Old Artist", "7", "1985"))

    def test_m4a_and_opus(self):
        t = self.read("d.m4a", m4a("Digital Love", "Discovery", 3, 14))
        self.assertEqual((t["title"], t["album"], t["tracknumber"], t["tracktotal"]), ("Digital Love", "Discovery", "3", "14"))
        t = self.read("e.opus", opus({"title": "Opus Song", "artist": "Someone", "tracknumber": "5"}))
        self.assertEqual((t["title"], t["artist"], t["tracknumber"]), ("Opus Song", "Someone", "5"))

    def test_garbage_is_harmless(self):
        self.assertEqual(self.read("x.flac", b"fLaC\x01\x02"), {})
        self.assertEqual(self.read("x.mp3", b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f"), {})


# --------------------------------------------------------------------------
# Album detection and naming
# --------------------------------------------------------------------------

CFG = {"movies_dir": "/lib/Movies", "tv_dir": "/lib/TV Shows", "music_dir": "/lib/Music", "id_tags": True}


class TestAlbums(unittest.TestCase):
    def groups(self, root, files, tags=None):
        items, leftovers, _ = build_items(root, [(f, 30_000_000) for f in files], tags)
        return Plan("x", "/x", items, leftovers).groups(), leftovers

    def test_album_dir_names(self):
        self.assertEqual(parse_album_dir("Radiohead - OK Computer (1997) [FLAC 24-96]"), ("Radiohead", "OK Computer", 1997))
        self.assertEqual(parse_album_dir("Pink Floyd - The Wall [2CD] (1979)"), ("Pink Floyd", "The Wall", 1979))
        self.assertEqual(parse_album_dir("VA - Now 50 (2001) MP3 320"), ("Various Artists", "Now 50", 2001))
        self.assertEqual(parse_album_dir("1997 - OK Computer"), (None, "OK Computer", 1997))

    def test_multi_disc_from_folders_with_art(self):
        groups, leftovers = self.groups("Pink Floyd - The Wall (1979) [FLAC]", [
            "CD1/01 - In the Flesh.flac", "CD1/02 - The Thin Ice.flac", "CD2/01 - Hey You.flac",
            "folder.jpg", "Scans/back.jpg", "CD1/rip.log", "info.nfo"])
        (g,) = groups
        self.assertEqual((g.category, g.artist, g.title, g.year), ("music", "Pink Floyd", "The Wall", 1979))
        dests = [naming.destination(i, CFG) for i in g.items]
        self.assertEqual(dests, ["/lib/Music/Pink Floyd/The Wall (1979)/1-01 - In the Flesh.flac",
                                 "/lib/Music/Pink Floyd/The Wall (1979)/1-02 - The Thin Ice.flac",
                                 "/lib/Music/Pink Floyd/The Wall (1979)/2-01 - Hey You.flac"])
        art = sorted(inside for i in g.items for _, inside in i.art)
        self.assertEqual(art, ["Scans/back.jpg", "folder.jpg", "rip.log"])
        self.assertEqual(leftovers, [("info.nfo", 30_000_000)])

    def test_artist_album_folders_and_loose_cover(self):
        groups, _ = self.groups("Daft Punk", ["Discovery (2001)/01. One More Time.mp3",
                                              "Discovery (2001)/02. Aerodynamic.mp3", "Discovery (2001)/scan001.png"])
        (g,) = groups
        self.assertEqual((g.artist, g.title, g.year), ("Daft Punk", "Discovery", 2001))
        self.assertEqual(g.items[0].art, [("Discovery (2001)/scan001.png", "cover.png")])

    def test_compilation_from_tags(self):
        tags = {"01 - a.mp3": {"artist": "Artist A", "title": "Song A", "album": "Hits", "tracknumber": "1", "date": "2001"},
                "02 - b.mp3": {"artist": "Artist B", "title": "Song B", "album": "Hits", "tracknumber": "2", "date": "2001"}}
        groups, _ = self.groups("Hits", list(tags), lambda rel: tags[rel])
        (g,) = groups
        self.assertEqual(g.artist, "Various Artists")
        self.assertEqual(naming.destination(g.items[1], CFG), "/lib/Music/Various Artists/Hits (2001)/02 - Artist B - Song B.mp3")

    def test_untagged_without_numbers_numbered_by_order(self):
        groups, _ = self.groups("Some Artist - Demo Tape", ["b side.mp3", "a side.mp3"])
        self.assertEqual([(i.track, i.track_title) for i in groups[0].items], [(1, "a side"), (2, "b side")])

    def test_video_pack_with_soundtrack(self):
        groups, _ = self.groups("Show.S01.1080p", ["Show.S01E01.mkv", "Show.S01E02.mkv",
                                                   "OST/01 - Opening Theme.flac", "OST/cover.jpg"])
        self.assertEqual(sorted(g.category for g in groups), ["music", "show"])


class TestMusicMatching(unittest.TestCase):
    def test_score_prefers_track_count_and_artist(self):
        q = query_variants("OK Computer")
        right = Match("MusicBrainz", "1", "music", "OK Computer", 1997, artist="Radiohead", tracks=12)
        deluxe = Match("MusicBrainz", "2", "music", "OK Computer OKNOTOK 1997 2017", 2017, artist="Radiohead", tracks=23)
        other = Match("MusicBrainz", "3", "music", "OK Computer", 2007, artist="Tribute Band", tracks=12)
        scores = [score_music(m, q, "Radiohead", 1997, 12) for m in (right, deluxe, other)]
        self.assertEqual(scores.index(max(scores)), 0)

    def test_apply_music_match_offline(self):
        items, _, _ = build_items("Radiohead - OK Computer", [("01 - airbag.flac", 1), ("02 - paranoid.flac", 1)])
        apply_match(items, Match("MusicBrainz", "mbid-1", "music", "OK Computer", 1997, {"mbid": "mbid-1"},
                                 artist="Radiohead"))
        self.assertTrue(all(i.verified == "MusicBrainz" and i.ids["mbid"] == "mbid-1" for i in items))
        self.assertEqual(naming.destination(items[0], CFG), "/lib/Music/Radiohead/OK Computer (1997)/01 - airbag.flac")


class FakeHttp:
    """Serves canned Wikidata API answers."""

    def __init__(self):
        self.calls = []

    def request(self, url, params=None, **kw):
        self.calls.append(params)
        if params.get("list") == "search":
            return {"query": {"search": [{"title": "Q1"}, {"title": "Q2"}, {"title": "Q3"}]}}
        claim = lambda v: [{"mainsnak": {"datavalue": {"value": v}}}]
        return {"entities": {
            "Q1": {"labels": {"en": {"value": "Breaking Bad"}}, "descriptions": {"en": {"value": "American crime drama"}},
                   "claims": {"P4835": claim("81189"), "P4983": claim("1396"), "P345": claim("tt0903747"),
                              "P580": claim({"time": "+2008-01-20T00:00:00Z"})}},
            "Q2": {"labels": {"en": {"value": "Breaking Bad"}}, "descriptions": {"en": {"value": "franchise"}},
                   "claims": {"P4835": claim("lists/breaking-bad")}},
            "Q3": {"labels": {}, "claims": {}}}}


class TestWikidata(unittest.TestCase):
    def test_parses_ids_and_skips_franchises(self):
        wd = Wikidata(FakeHttp())
        results = wd.search_tv("Breaking Bad")
        self.assertEqual(len(results), 1)
        m = results[0]
        self.assertEqual((m.title, m.year, m.ids["tvdb"], m.ids["tmdb"], m.ids["imdb"]),
                         ("Breaking Bad", 2008, 81189, 1396, "tt0903747"))
        self.assertEqual(naming.id_tag("show", m.ids), " {tvdb-81189}")

    def test_country_and_popularity_tiebreak(self):
        cfg = {"database": os.path.join(tempfile.gettempdir(), "unused.db"), "tmdb_api_key": ""}
        matcher = Matcher(cfg, http=FakeHttp())
        us = Match("TVmaze", "1", "show", "The Office", 2005, {"tvdb": 1}, "NBC · US · Comedy", popularity=0.99)
        old = Match("TVmaze", "2", "show", "The Office", 1995, {"tvdb": 2}, "CBS · US · Comedy", popularity=0.1)
        uk = Match("TVmaze", "3", "show", "The Office", 2001, {"tvdb": 3}, "BBC Two · GB · Comedy", popularity=0.9)
        matcher.sources = lambda category, anime: [("TVmaze", lambda q: [us, old, uk])]
        results = matcher.search("The Office US", "show")
        self.assertEqual(results[0].title, "The Office")
        self.assertEqual(results[0].year, 2005)
        self.assertGreater(results[0].score - results[1].score, 0.04)


# --------------------------------------------------------------------------
# Sorting music end to end
# --------------------------------------------------------------------------

class TestMusicSort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.cfg = {"movies_dir": f"{t}/media/Movies", "tv_dir": f"{t}/media/TV Shows", "music_dir": f"{t}/media/Music",
                    "database": f"{t}/db/library.db", "tmdb_api_key": "", "id_tags": True, "remove_torrent": True,
                    "delete_leftovers": True, "online": False, "download_cover_art": False, "inbox_dirs": ""}
        self.db = DB(self.cfg["database"])
        self.root = os.path.join(t, "incoming", "Radiohead - OK Computer (1997) [FLAC]")
        os.makedirs(self.root)
        for n, title in enumerate(["Airbag", "Paranoid Android"], start=1):
            Path(self.root, f"{n:02d} - {title}.flac").write_bytes(flac({
                "TITLE": title, "ARTIST": "Radiohead", "ALBUM": "OK Computer", "TRACKNUMBER": str(n), "DATE": "1997"}))
        Path(self.root, "folder.jpg").write_bytes(b"\xff\xd8\xffjpeg")
        Path(self.root, "rip.log").write_text("EAC log")
        Path(self.root, "info.nfo").write_text("junk")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_sort_retitle_scan_undo(self):
        plan = engine.make_plan(engine.source_for_path(self.root))
        (g,) = plan.groups()
        self.assertEqual((g.artist, g.title, g.year, g.items[0].note), ("Radiohead", "OK Computer", 1997, "audio tags"))
        result = engine.apply_plan(plan, self.cfg, self.db)
        self.assertEqual((result.errors, result.moved), ([], 4))  # 2 tracks + cover + log
        album = os.path.join(self.cfg["music_dir"], "Radiohead", "OK Computer (1997)")
        self.assertEqual(sorted(os.listdir(album)), ["01 - Airbag.flac", "02 - Paranoid Android.flac",
                                                     "folder.jpg", "rip.log"])
        self.assertFalse(os.path.exists(self.root))
        (row,) = self.db.titles("music")
        self.assertEqual((row["artist"], row["tracks"]), ("Radiohead", 2))

        moved, errors = engine.retitle(row["id"], "OK Computer OKNOTOK", 2017, {}, "manual", self.cfg, self.db,
                                       artist="Radiohead")
        self.assertEqual(errors, [])
        new = os.path.join(self.cfg["music_dir"], "Radiohead", "OK Computer OKNOTOK (2017)")
        self.assertEqual(sorted(os.listdir(new)), ["01 - Airbag.flac", "02 - Paranoid Android.flac",
                                                   "folder.jpg", "rip.log"])
        self.assertFalse(os.path.exists(album))

        Path(new, "03 - Bonus.flac").write_bytes(b"x")
        self.assertEqual(engine.scan_library(self.cfg, self.db), (1, 0))

        restored, errors = engine.undo_operation(result.op_id, self.cfg, self.db)
        self.assertEqual((restored, errors), (4, []))
        self.assertTrue(os.path.exists(os.path.join(self.root, "02 - Paranoid Android.flac")))


if __name__ == "__main__":
    unittest.main()
