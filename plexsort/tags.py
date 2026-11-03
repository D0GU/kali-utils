"""Minimal, dependency-free readers for audio tags: FLAC, Ogg Vorbis/Opus, MP3 (ID3v2.2-2.4 + v1), MP4/M4A.

Returns a dict with lower-case keys: title, artist, albumartist, album, date, tracknumber, tracktotal,
discnumber, disctotal, compilation. Missing tags are simply absent; unreadable files give {}.
"""

from __future__ import annotations

import os
import struct

MAX_TAG_BYTES = 16 * 1024 * 1024

VORBIS_KEYS = {"title": "title", "artist": "artist", "albumartist": "albumartist", "album artist": "albumartist",
               "album_artist": "albumartist", "album": "album", "date": "date", "year": "date",
               "originaldate": "originaldate", "tracknumber": "tracknumber", "tracktotal": "tracktotal",
               "totaltracks": "tracktotal", "discnumber": "discnumber", "disctotal": "disctotal",
               "totaldiscs": "disctotal", "compilation": "compilation"}


def read_tags(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(0)
            if head == b"fLaC":
                tags = _flac(f)
            elif head == b"OggS":
                tags = _ogg(f)
            elif head[:3] == b"ID3" or ext == ".mp3":
                tags = _id3(f)
            elif ext in (".m4a", ".mp4", ".m4b", ".aac", ".alac"):
                tags = _mp4(f)
            else:
                tags = {}
    except (OSError, struct.error, ValueError, IndexError):
        return {}
    return _normalize(tags)


def _normalize(tags: dict) -> dict:
    out = {k: v.strip() for k, v in tags.items() if isinstance(v, str) and v.strip()}
    for key, total in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
        if "/" in out.get(key, ""):
            num, _, tot = out[key].partition("/")
            out[key] = num.strip()
            if tot.strip() and total not in out:
                out[total] = tot.strip()
    if "date" not in out and "originaldate" in out:
        out["date"] = out["originaldate"]
    return out


def _vorbis_comments(data: bytes) -> dict:
    pos = 0
    vendor_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + vendor_len
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    tags: dict = {}
    for _ in range(min(count, 1000)):
        length = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        entry = data[pos:pos + length].decode("utf-8", "replace")
        pos += length
        key, sep, value = entry.partition("=")
        key = VORBIS_KEYS.get(key.lower())
        if sep and key and key not in tags:
            tags[key] = value
    return tags


def _flac(f) -> dict:
    f.read(4)
    while True:
        header = f.read(4)
        if len(header) < 4:
            return {}
        last, btype = header[0] & 0x80, header[0] & 0x7F
        length = int.from_bytes(header[1:4], "big")
        if btype == 4:
            return _vorbis_comments(f.read(min(length, MAX_TAG_BYTES)))
        f.seek(length, os.SEEK_CUR)
        if last:
            return {}


def _ogg(f) -> dict:
    packets: list[bytes] = []
    current = b""
    read = 0
    while len(packets) < 2 and read < MAX_TAG_BYTES:
        header = f.read(27)
        if len(header) < 27 or header[:4] != b"OggS":
            break
        nseg = header[26]
        segs = f.read(nseg)
        for size in segs:
            current += f.read(size)
            read += size
            if size < 255:
                packets.append(current)
                current = b""
    if len(packets) < 2:
        return {}
    p = packets[1]
    if p.startswith(b"\x03vorbis"):
        return _vorbis_comments(p[7:])
    if p.startswith(b"OpusTags"):
        return _vorbis_comments(p[8:])
    return {}


ID3_FRAMES = {"TIT2": "title", "TPE1": "artist", "TPE2": "albumartist", "TALB": "album", "TRCK": "tracknumber",
              "TPOS": "discnumber", "TDRC": "date", "TYER": "date", "TDOR": "originaldate", "TORY": "originaldate",
              "TCMP": "compilation",
              "TT2": "title", "TP1": "artist", "TP2": "albumartist", "TAL": "album", "TRK": "tracknumber",
              "TPA": "discnumber", "TYE": "date", "TCP": "compilation"}


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_text(data: bytes) -> str:
    if not data:
        return ""
    enc, body = data[0], data[1:]
    if enc == 0:
        text = body.decode("latin-1", "replace")
    elif enc == 1:
        text = body.decode("utf-16", "replace")
    elif enc == 2:
        text = body.decode("utf-16-be", "replace")
    else:
        text = body.decode("utf-8", "replace")
    return text.split("\x00")[0].strip()  # first value of multi-value frames


def _id3(f) -> dict:
    header = f.read(10)
    tags: dict = {}
    if header[:3] == b"ID3":
        major, flags = header[3], header[5]
        size = _syncsafe(header[6:10])
        data = f.read(min(size, MAX_TAG_BYTES))
        if flags & 0x80 and major < 4:  # unsynchronisation
            data = data.replace(b"\xff\x00", b"\xff")
        pos = 0
        if flags & 0x40 and major >= 3:  # extended header
            ext = _syncsafe(data[:4]) if major == 4 else struct.unpack(">I", data[:4])[0] + 4
            pos = ext
        id_len, hdr_len = (3, 6) if major == 2 else (4, 10)
        while pos + hdr_len <= len(data):
            fid = data[pos:pos + id_len]
            if not fid.strip(b"\x00") or not fid.isalnum():
                break
            if major == 2:
                fsize = int.from_bytes(data[pos + 3:pos + 6], "big")
            elif major == 4:
                fsize = _syncsafe(data[pos + 4:pos + 8])
            else:
                fsize = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            body = data[pos + hdr_len:pos + hdr_len + fsize]
            pos += hdr_len + fsize
            key = ID3_FRAMES.get(fid.decode("latin-1"))
            if key and key not in tags:
                tags[key] = _id3_text(body)
    if not tags:  # ID3v1 at the end of the file
        f.seek(0, os.SEEK_END)
        if f.tell() >= 128:
            f.seek(-128, os.SEEK_END)
            v1 = f.read(128)
            if v1[:3] == b"TAG":
                def field(a, b):
                    return v1[a:b].split(b"\x00")[0].decode("latin-1").strip()
                tags = {"title": field(3, 33), "artist": field(33, 63), "album": field(63, 93), "date": field(93, 97)}
                if v1[125] == 0 and v1[126]:
                    tags["tracknumber"] = str(v1[126])
    return tags


MP4_KEYS = {b"\xa9nam": "title", b"\xa9ART": "artist", b"aART": "albumartist", b"\xa9alb": "album",
            b"\xa9day": "date", b"trkn": "tracknumber", b"disk": "discnumber", b"cpil": "compilation"}


def _atoms(f, end: int):
    while f.tell() + 8 <= end:
        start = f.tell()
        size, kind = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header:
            return
        yield kind, start + header, start + size
        f.seek(start + size)


def _mp4(f) -> dict:
    f.seek(0, os.SEEK_END)
    end = f.tell()
    f.seek(0)
    for kind, body, stop in _atoms(f, end):
        if kind == b"moov":
            return _mp4_moov(f, body, stop)
    return {}


def _mp4_moov(f, start: int, stop: int) -> dict:
    def find(path: list[bytes], s: int, e: int):
        f.seek(s)
        for kind, body, end in list(_atoms(f, e)):
            if kind == path[0]:
                if len(path) == 1:
                    return body, end
                inner = body + 4 if kind == b"meta" else body  # meta is a full box
                return find(path[1:], inner, end)
        return None
    found = find([b"udta", b"meta", b"ilst"], start, stop)
    if not found:
        return {}
    s, e = found
    tags: dict = {}
    f.seek(s)
    for kind, body, end in list(_atoms(f, e)):
        key = MP4_KEYS.get(kind)
        if not key:
            continue
        f.seek(body)
        for dkind, dbody, dend in list(_atoms(f, end)):
            if dkind != b"data":
                continue
            f.seek(dbody + 8)
            value = f.read(dend - dbody - 8)
            if key in ("tracknumber", "discnumber"):
                if len(value) >= 6:
                    num, total = struct.unpack(">HH", value[2:6])
                    tags[key] = f"{num}/{total}" if total else str(num)
            elif key == "compilation":
                tags[key] = "1" if value[:1] == b"\x01" else "0"
            else:
                tags[key] = value.decode("utf-8", "replace")
            break
    return tags
