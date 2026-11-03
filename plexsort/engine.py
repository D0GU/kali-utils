"""Inbox discovery, planning, applying (moving files), undo, re-titling and library scans."""

from __future__ import annotations

import errno
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import naming, space
from .db import DB
from .parse import Item, Plan, build_items
from .providers import Matcher, ProviderError, apply_match, download_cover
from .tags import read_tags

TORRENT_FIELDS = ["id", "name", "hashString", "downloadDir", "percentDone", "leftUntilDone", "sizeWhenDone",
                  "doneDate", "status", "magnetLink", "metadataPercentComplete", "error", "errorString", "labels"]


class SortError(Exception):
    pass


def permission_hint(e: OSError) -> str:
    if e.errno in (errno.EACCES, errno.EPERM):
        return (f"{e.strerror}: {e.filename} - your user needs write access to the torrent folders. "
                "If 'kali' was just added to the debian-transmission group, log out and back in.")
    return f"{e.strerror}: {e.filename}"


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------

@dataclass
class Source:
    kind: str            # torrent | folder
    name: str
    path: str            # absolute path to the torrent folder or single file
    size: int = 0
    done_date: int = 0
    torrent: dict | None = None
    exists: bool = True
    note: str = ""


def dir_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dp, _, fn in os.walk(path):
        for f in fn:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def list_sources(cfg: dict, client=None) -> tuple[list[Source], int, str]:
    """Completed torrents (and loose folders in inbox_dirs). Returns (sources, n_downloading, error)."""
    sources: list[Source] = []
    downloading = 0
    error = ""
    torrent_paths = set()
    dirs = list(cfg.get("inbox_dirs") or [])
    if isinstance(dirs, str):
        dirs = [d.strip() for d in dirs.split(",") if d.strip()]
    if client is not None:
        try:
            torrents = client.torrent_get(TORRENT_FIELDS)
            try:
                dirs.append(client.session_get(["download-dir"])["download-dir"])
            except Exception:
                pass
        except Exception as e:  # RPCError, AuthError
            torrents, error = [], f"Transmission: {e}"
        for t in torrents:
            path = os.path.join(t["downloadDir"], t["name"])
            torrent_paths.add(path)
            complete = t.get("metadataPercentComplete", 1) >= 1 and t["leftUntilDone"] == 0
            if not complete:
                downloading += 1
                continue
            sources.append(Source("torrent", t["name"], path, t["sizeWhenDone"], t.get("doneDate") or 0, t,
                                  os.path.exists(path), "" if os.path.exists(path) else "data not found on disk"))
    for d in dict.fromkeys(os.path.expanduser(d) for d in dirs):
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            path = os.path.join(d, name)
            if path in torrent_paths or name.startswith("."):
                continue
            if os.path.isfile(path):
                from .music import AUDIO_EXT
                from .parse import VIDEO_EXT
                if os.path.splitext(name)[1].lower() not in VIDEO_EXT | AUDIO_EXT:
                    continue
            st = os.stat(path)
            sources.append(Source("folder", name, path, dir_size(path), int(st.st_mtime)))
    sources.sort(key=lambda s: -(s.done_date or 0))
    return sources, downloading, error


def source_for_path(path: str) -> Source:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise SortError(f"{path} does not exist")
    return Source("folder", os.path.basename(path.rstrip("/")), path, dir_size(path))


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def scan_files(root: str) -> list[tuple[str, int]]:
    if os.path.isfile(root):
        return [(os.path.basename(root), os.path.getsize(root))]
    files = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            try:
                files.append((os.path.relpath(p, root), os.lstat(p).st_size))
            except OSError:
                pass
    return files


def make_plan(source: Source) -> Plan:
    if not os.path.exists(source.path):
        raise SortError(f"{source.path} not found")
    base = os.path.dirname(source.path) if os.path.isfile(source.path) else source.path

    def tags_for(rel: str) -> dict:
        return read_tags(source.path if os.path.isfile(source.path) else os.path.join(base, rel))
    items, leftovers, incomplete = build_items(source.name, scan_files(source.path), tags_for)
    return Plan(source.name, source.path, items, leftovers, source.torrent, incomplete)


def auto_match(plan: Plan, matcher: Matcher, progress=None) -> list[str]:
    """Verify every unverified group online, in parallel. Returns error messages."""
    groups = [g for g in plan.groups() if not g.verified]
    errors: list[str] = []

    def work(g):
        try:
            return g, matcher.best(g), list(matcher.errors)
        except ProviderError as e:
            return g, None, [str(e)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for n, (g, match, errs) in enumerate(pool.map(work, groups), start=1):
            errors.extend(errs)
            if match:
                apply_match(g.items, match, matcher)
            if progress:
                progress(n, len(groups), g.title)
    return sorted(set(errors))


@dataclass
class Check:
    dests: dict = field(default_factory=dict)      # id(item) -> dst
    sub_dests: dict = field(default_factory=dict)  # (id(item), sub.src) -> dst
    errors: dict = field(default_factory=dict)     # id(item) -> message (blocks apply)
    warnings: dict = field(default_factory=dict)   # id(item) -> message
    global_errors: list = field(default_factory=list)
    placements: list = field(default_factory=list)  # space.Placement per title

    @property
    def ok(self) -> bool:
        return not self.errors and not self.global_errors


def check_plan(plan: Plan, cfg: dict, db: DB | None = None) -> Check:
    c = Check()
    seen: dict[str, Item] = {}
    if plan.incomplete:
        c.global_errors.append(f"{len(plan.incomplete)} file(s) are still incomplete (.part)")
    try:
        c.placements = space.place(plan, cfg)
    except ValueError as e:  # no library folder configured
        c.global_errors.append(str(e))
    for pl in c.placements:
        if not pl.fits:
            c.global_errors.append(f"“{pl.label}”: {pl.reason}")
    for it in plan.items:
        if it.kind == "skip":
            continue
        problems = []
        if not it.title.strip():
            problems.append("no title")
        if it.kind == "episode" and it.episode is None:
            problems.append("no episode number")
        dst = naming.destination(it, cfg)
        c.dests[id(it)] = dst
        if dst in seen:
            problems.append(f"same destination as {os.path.basename(seen[dst].src)}")
        elif os.path.exists(dst):
            problems.append("destination already exists in the library")
        seen[dst] = it
        for sub in it.subs:
            sdst = naming.sub_destination(dst, sub)
            if sdst in seen or os.path.exists(sdst):
                n = 2
                base, ext = os.path.splitext(sdst)
                while f"{base}.{n}{ext}" in seen or os.path.exists(f"{base}.{n}{ext}"):
                    n += 1
                sdst = f"{base}.{n}{ext}"
            seen[sdst] = it
            c.sub_dests[(id(it), sub.src)] = sdst
        for src_rel, inside in it.art:
            adst = naming.art_destination(dst, inside)
            if adst in seen or os.path.exists(adst):
                base, ext = os.path.splitext(adst)
                n = 2
                while f"{base} ({n}){ext}" in seen or os.path.exists(f"{base} ({n}){ext}"):
                    n += 1
                adst = f"{base} ({n}){ext}"
            seen[adst] = it
            c.sub_dests[(id(it), src_rel)] = adst
        if it.kind == "track" and not it.title.strip():
            problems.append("no album")
        if problems:
            c.errors[id(it)] = "; ".join(problems)
        warn = []
        if not it.verified:
            warn.append("not verified online")
        if it.ep_found is False:
            warn.append("track not on the release" if it.kind == "track" else "episode not in provider's list")
        if warn:
            c.warnings[id(it)] = "; ".join(warn)
    return c


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def move_file(src: str, dst: str) -> None:
    if os.path.lexists(dst):
        raise SortError(f"refusing to overwrite {dst}")
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


@dataclass
class Result:
    op_id: int
    moved: int = 0
    errors: list = field(default_factory=list)
    torrent_removed: bool = False
    leftovers_deleted: int = 0


def apply_plan(plan: Plan, cfg: dict, db: DB, client=None, remove_torrent: bool | None = None,
               delete_leftovers: bool | None = None, log=None) -> Result:
    remove_torrent = cfg["remove_torrent"] if remove_torrent is None else remove_torrent
    delete_leftovers = cfg["delete_leftovers"] if delete_leftovers is None else delete_leftovers
    log = log or (lambda msg: None)
    check = check_plan(plan, cfg)
    if not check.ok:
        raise SortError("plan has problems: " + "; ".join(check.global_errors + list(check.errors.values()))[:400])
    single_file = os.path.isfile(plan.root)
    base = os.path.dirname(plan.root) if single_file else plan.root

    def src_abs(rel: str) -> str:
        return plan.root if single_file else os.path.join(base, rel)

    torrent = plan.torrent
    if torrent and client is not None:
        try:
            client.torrent_action("stop", [torrent["id"]])
            log("Stopped torrent")
        except Exception as e:
            raise SortError(f"could not stop the torrent: {e}") from None

    op_id = db.begin_operation(plan.name, plan.root, torrent)
    result = Result(op_id)
    cover_art = cfg.get("download_cover_art", True) and cfg.get("online", True)
    albums_needing_cover: dict[str, tuple] = {}
    try:
        for it in plan.items:
            if it.kind == "skip":
                continue
            dst = check.dests[id(it)]
            title_path = naming.title_root(dst, it.category, cfg)
            title_id = db.upsert_title(it.category, it.title, it.year, title_path, it.ids, it.verified, it.anime,
                                       artist=it.artist)
            if it.kind == "track":
                meta = dict(season=it.disc, episode=it.track, ep_title=it.track_title, artist=it.track_artist or None)
            else:
                meta = dict(season=it.season, episode=it.episode, episode_end=it.episode_end, ep_title=it.ep_title,
                            part=it.part, extra_type=it.extra_type if it.kind == "extra" else None,
                            extra_name=it.extra_name or None)
            src = src_abs(it.src)
            move_file(src, dst)
            db.add_file(title_id, op_id, it.kind, src, dst, it.size, **meta)
            result.moved += 1
            log(f"→ {os.path.relpath(dst, naming.root_of(dst, it.category, cfg))}")
            for sub in it.subs:
                sdst = check.sub_dests[(id(it), sub.src)]
                ssrc = os.path.join(base, sub.src)
                move_file(ssrc, sdst)
                db.add_file(title_id, op_id, "subtitle", ssrc, sdst, os.path.getsize(sdst),
                            season=it.season, episode=it.episode, lang=sub.lang or None)
                result.moved += 1
            for src_rel, _inside in it.art:
                adst = check.sub_dests[(id(it), src_rel)]
                asrc = os.path.join(base, src_rel)
                move_file(asrc, adst)
                db.add_file(title_id, op_id, "artwork", asrc, adst, os.path.getsize(adst))
                result.moved += 1
            if it.kind == "track" and it.verified == "MusicBrainz" and cover_art:
                albums_needing_cover.setdefault(title_path, (it.ids.get("mbid"), title_id))
    except (OSError, SortError) as e:
        msg = permission_hint(e) if isinstance(e, OSError) else str(e)
        result.errors.append(msg)
        db.finish_operation(op_id, "failed", f"stopped after {result.moved} files: {msg}")
        log(f"FAILED: {msg}")
        return result

    for folder, (mbid, title_id) in albums_needing_cover.items():
        has_art = any(os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".webp")
                      for f in os.listdir(folder)) if os.path.isdir(folder) else True
        if mbid and not has_art:
            dest = os.path.join(folder, "cover.jpg")
            if download_cover(mbid, dest):
                saved = dest if os.path.exists(dest) else os.path.splitext(dest)[0] + ".png"
                db.add_file(title_id, op_id, "artwork", "", saved, os.path.getsize(saved))
                log(f"Downloaded cover art for {os.path.basename(folder)}")

    if torrent and client is not None and remove_torrent:
        try:
            client.torrent_remove([torrent["id"]], delete_data=False)
            result.torrent_removed = True
            log("Removed torrent from Transmission (data kept)")
        except Exception as e:
            result.errors.append(f"could not remove torrent: {e}")

    if delete_leftovers and not single_file:
        junk = [rel for rel, _ in plan.leftovers] + [i.src for i in plan.skipped()]
        for rel in junk:
            p = os.path.join(base, rel)
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    result.leftovers_deleted += 1
            except OSError as e:
                result.errors.append(f"could not delete {rel}: {e.strerror}")
        # remove now-empty folders bottom-up, then the torrent folder itself
        for dp, dn, fn in sorted(os.walk(base), key=lambda x: -x[0].count("/")):
            try:
                os.rmdir(dp)
            except OSError:
                pass
        if result.leftovers_deleted:
            log(f"Deleted {result.leftovers_deleted} leftover file(s)")
    elif not single_file:
        for dp, dn, fn in sorted(os.walk(base), key=lambda x: -x[0].count("/")):
            try:
                os.rmdir(dp)
            except OSError:
                pass

    db.finish_operation(op_id, "applied", "; ".join(result.errors), torrent_removed=result.torrent_removed,
                        leftovers_deleted=bool(result.leftovers_deleted))
    return result


# --------------------------------------------------------------------------
# Undo / re-title / scan
# --------------------------------------------------------------------------

def undo_operation(op_id: int, cfg: dict, db: DB, client=None, readd_torrent: bool = False, log=None) -> tuple[int, list]:
    log = log or (lambda msg: None)
    op = db.operation(op_id)
    if not op:
        raise SortError(f"no operation {op_id}")
    restored, errors = 0, []
    for f in reversed(db.operation_files(op_id)):
        if f["status"] == "missing":
            errors.append(f"missing: {f['dst']}")
            continue
        if f["status"] != "present":
            continue
        if not os.path.exists(f["dst"]):
            db.update_file(f["id"], status="missing")
            errors.append(f"missing: {f['dst']}")
            continue
        try:
            if not f["src"]:   # created by plexsort (downloaded cover art): just remove it
                os.remove(f["dst"])
            else:
                move_file(f["dst"], f["src"])
        except (OSError, SortError) as e:
            errors.append(permission_hint(e) if isinstance(e, OSError) else str(e))
            continue
        db.update_file(f["id"], status="undone")
        restored += 1
        title = db.title(f["title_id"])
        if title:
            remove_empty_dirs(os.path.dirname(f["dst"]), naming.root_of(f["dst"], title["category"], cfg))
    db.prune_titles()
    msg = f"undone: {restored} file(s) restored"
    if readd_torrent and op["magnet"] and client is not None:
        try:
            client.torrent_add(filename=op["magnet"], download_dir=op["download_dir"], paused=False)
            msg += "; torrent re-added"
            log("Re-added torrent to Transmission")
        except Exception as e:
            errors.append(f"could not re-add torrent: {e}")
    db.finish_operation(op_id, "undone" if not errors else "failed", msg + ("; " + "; ".join(errors) if errors else ""),
                        torrent_removed=op["torrent_removed"], leftovers_deleted=op["leftovers_deleted"])
    return restored, errors


def item_from_row(row, title) -> Item:
    if row["kind"] == "track":
        return Item(src=row["dst"], size=row["size"] or 0, kind="track", category="music", title=title["title"],
                    year=title["year"], artist=title.get("artist") or "", disc=row["season"], track=row["episode"],
                    track_title=row["ep_title"] or "", track_artist=row["artist"] or "")
    kind = row["kind"] if row["kind"] != "subtitle" else "episode"
    return Item(src=row["dst"], size=row["size"] or 0, kind=kind, category=title["category"], title=title["title"],
                year=title["year"], season=row["season"], episode=row["episode"], episode_end=row["episode_end"],
                ep_title=row["ep_title"] or "", part=row["part"], extra_type=row["extra_type"] or "other",
                extra_name=row["extra_name"] or "")


def retitle(title_id: int, new_title: str, new_year: int | None, new_ids: dict, provider: str,
            cfg: dict, db: DB, category: str | None = None, artist: str | None = None) -> tuple[int, list]:
    """Rename a library title (folder + files) after fixing its match."""
    title = db.title(title_id)
    if not title:
        raise SortError(f"no title {title_id}")
    category = category or title["category"]
    artist = artist if artist is not None else (title["artist"] or "")
    files = [f for f in db.title_files(title_id) if f["status"] == "present"]
    plans = []
    new_title_row = {"category": category, "title": new_title, "year": new_year, "artist": artist}
    videos = {}
    for f in files:
        if f["kind"] in ("subtitle", "artwork"):
            continue
        it = item_from_row(f, new_title_row)
        it.ids = new_ids
        it.category = category
        # stay on the same drive/library folder unless the title changes library type
        it.root = (naming.root_of(title["path"], category, cfg) if category == title["category"]
                   else naming.library_dir(category, cfg))
        dst = naming.destination(it, cfg)
        videos[(f["season"], f["episode"], f["part"])] = dst
        plans.append((f, dst))
    for f in files:
        if f["kind"] != "subtitle":
            continue
        vdst = videos.get((f["season"], f["episode"], None)) or next(iter(videos.values()), None)
        if not vdst:
            continue
        old_ext_parts = os.path.basename(f["dst"]).split(".")
        suffix = "." + ".".join(old_ext_parts[-2:]) if len(old_ext_parts) > 2 and len(old_ext_parts[-2]) <= 3 \
            else os.path.splitext(f["dst"])[1]
        plans.append((f, os.path.splitext(vdst)[0] + suffix))
    if plans:
        new_root_dir = naming.title_root(plans[0][1], category, cfg)
        for f in files:
            if f["kind"] == "artwork":
                plans.append((f, os.path.join(new_root_dir, os.path.relpath(f["dst"], title["path"]))))
    targets = [d for _, d in plans]
    if len(set(targets)) != len(targets):
        raise SortError("the new names would collide; edit episode numbers first")
    for f, dst in plans:
        if dst != f["dst"] and os.path.lexists(dst):
            raise SortError(f"{dst} already exists")
    new_root = naming.title_root(plans[0][1], category, cfg) if plans else title["path"]
    existing = db.title_by_path(new_root)
    if existing and existing["id"] != title_id:
        target_id = existing["id"]
        db.update_title(target_id, title=new_title, year=new_year, ids=new_ids, provider=provider, artist=artist)
    else:
        target_id = title_id
        db.update_title(title_id, category=category, title=new_title, year=new_year, path=new_root,
                        ids=new_ids, provider=provider, artist=artist)
    moved, errors = 0, []
    for f, dst in plans:
        if dst == f["dst"]:
            db.update_file(f["id"], title_id=target_id)
            continue
        try:
            move_file(f["dst"], dst)
        except (OSError, SortError) as e:
            errors.append(permission_hint(e) if isinstance(e, OSError) else str(e))
            continue
        db.update_file(f["id"], dst=dst, title_id=target_id)
        remove_empty_dirs(os.path.dirname(f["dst"]), naming.root_of(f["dst"], title["category"], cfg))
        moved += 1
    db.prune_titles()
    return moved, errors


FOLDER_RE = re.compile(r"^(?P<title>.+?)(?: \((?P<year>\d{4})\))?(?: \{(?P<src>tvdb|tmdb|imdb)-(?P<id>[^}]+)\})?$")
EPISODE_FILE_RE = re.compile(r"(?i)\bs(\d{1,2})e(\d{1,4})(?:-e(\d{1,4}))?\b(?: - (.+))?$")
SUB_EXTS = (".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup")
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg")


MUSIC_FILE_RE = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3}) - (.+)$")
ALBUM_DIR_RE = re.compile(r"^(?P<title>.+?)(?: \((?P<year>\d{4})\))?$")


def scan_music(cfg: dict, db: DB) -> int:
    added = 0
    for base in naming.library_roots("music", cfg):
        if os.path.isdir(base):
            added += _scan_music_root(base, db)
    return added


def _scan_music_root(base: str, db: DB) -> int:
    from .music import ART_EXT, AUDIO_EXT
    added = 0
    for artist in sorted(os.listdir(base)):
        artist_dir = os.path.join(base, artist)
        if not os.path.isdir(artist_dir) or artist.startswith("."):
            continue
        for album in sorted(os.listdir(artist_dir)):
            folder = os.path.join(artist_dir, album)
            if not os.path.isdir(folder):
                continue
            m = ALBUM_DIR_RE.match(album)
            title_id = None
            for dp, _, fn in os.walk(folder):
                for f in sorted(fn):
                    path = os.path.join(dp, f)
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in AUDIO_EXT | ART_EXT | {".cue", ".log"} or db.file_by_dst(path):
                        continue
                    if title_id is None:
                        row = db.title_by_path(folder)
                        title_id = row["id"] if row else db.upsert_title(
                            "music", m["title"], int(m["year"]) if m["year"] else None, folder, {}, "scan",
                            artist=artist)
                    if ext in AUDIO_EXT:
                        fm = MUSIC_FILE_RE.match(os.path.splitext(f)[0])
                        meta = dict(season=int(fm[1]) if fm and fm[1] else None, episode=int(fm[2]) if fm else None,
                                    ep_title=fm[3] if fm else os.path.splitext(f)[0])
                        db.add_file(title_id, None, "track", "", path, os.path.getsize(path), **meta)
                    else:
                        db.add_file(title_id, None, "artwork", "", path, os.path.getsize(path))
                    added += 1
    return added


def scan_library(cfg: dict, db: DB) -> tuple[int, int]:
    """Index files already in the library folders; mark vanished files missing. Returns (added, missing)."""
    added = scan_music(cfg, db)
    missing = 0
    for category, base in [(c, r) for c in ("movie", "show") for r in naming.library_roots(c, cfg)]:
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            folder = os.path.join(base, entry)
            if not os.path.isdir(folder) or entry.startswith("."):
                continue
            m = FOLDER_RE.match(entry)
            ids = {m["src"]: (m["id"] if m["src"] == "imdb" else int(m["id"]) if m["id"].isdigit() else m["id"])} \
                if m and m["src"] else {}
            title_id = None
            for dp, _, fn in os.walk(folder):
                for f in sorted(fn):
                    path = os.path.join(dp, f)
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXTS + SUB_EXTS or db.file_by_dst(path):
                        continue
                    if title_id is None:
                        row = db.title_by_path(folder)
                        title_id = row["id"] if row else db.upsert_title(
                            category, m["title"] if m else entry, int(m["year"]) if m and m["year"] else None,
                            folder, ids, "scan")
                    rel_dir = os.path.relpath(dp, folder)
                    stem = os.path.splitext(f)[0]
                    meta: dict = {}
                    if ext in SUB_EXTS:
                        kind = "subtitle"
                    elif category == "show" and (em := EPISODE_FILE_RE.search(stem)):
                        kind = "episode"
                        meta = dict(season=int(em[1]), episode=int(em[2]),
                                    episode_end=int(em[3]) if em[3] else None, ep_title=em[4] or "")
                    elif rel_dir != "." and rel_dir.split("/")[0] in naming.EXTRA_DIRS.values():
                        kind = "extra"
                        rev = {v: k for k, v in naming.EXTRA_DIRS.items()}
                        meta = dict(extra_type=rev[rel_dir.split("/")[0]], extra_name=stem)
                    else:
                        kind = "movie" if category == "movie" else "extra"
                    db.add_file(title_id, None, kind, "", path, os.path.getsize(path), **meta)
                    added += 1
    for row in db.conn.execute("SELECT id, dst FROM files WHERE status = 'present'").fetchall():
        if not os.path.exists(row["dst"]):
            db.update_file(row["id"], status="missing")
            missing += 1
    for row in db.conn.execute("SELECT id, dst FROM files WHERE status = 'missing'").fetchall():
        if os.path.exists(row["dst"]):
            db.update_file(row["id"], status="present")
    return added, missing
