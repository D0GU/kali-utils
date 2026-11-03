"""An in-process fake transmission-daemon RPC server for tests.

Implements the 409 session-id handshake, optional basic auth, and the RPC
methods trctl uses, backed by an in-memory list of torrents.
"""

from __future__ import annotations

import base64
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_ID = "fake-session-123"


def make_torrent(tid, name, files, *, status=4, done=None, rate_down=0, rate_up=0,
                 download_dir="/data/complete", error=0, error_string="", labels=None):
    """files: list of (relative_path, size). done: fraction completed of each file."""
    multi = len(files) > 1 or "/" in files[0][0]
    flist, fstats = [], []
    for path, size in files:
        completed = int(size * (1.0 if done is None else done))
        flist.append({"name": f"{name}/{path}" if multi else name, "length": size,
                      "bytesCompleted": completed})
        fstats.append({"wanted": True, "priority": 0, "bytesCompleted": completed})
    t = {
        "id": tid, "name": name, "status": status, "error": error, "errorString": error_string,
        "metadataPercentComplete": 1.0, "hashString": f"{tid:02x}" * 20, "downloadDir": download_dir,
        "rateDownload": rate_down, "rateUpload": rate_up, "uploadRatio": 0.5, "eta": 120,
        "addedDate": 1_700_000_000 + tid, "doneDate": 0, "activityDate": 1_700_000_100,
        "peersConnected": 3, "peersSendingToUs": 2, "peersGettingFromUs": 1,
        "labels": labels or [], "queuePosition": tid, "recheckProgress": 0.0,
        "downloadedEver": 0, "uploadedEver": 0, "corruptEver": 0, "comment": "", "creator": "",
        "dateCreated": 0, "isPrivate": False, "pieceCount": 10, "pieceSize": 262144,
        "magnetLink": f"magnet:?xt=urn:btih:{tid:02x}", "trackerStats": [],
        "secondsDownloading": 0, "secondsSeeding": 0, "webseedsSendingToUs": 0,
        "files": flist, "fileStats": fstats,
        "downloadLimit": 100, "downloadLimited": False, "uploadLimit": 100, "uploadLimited": False,
        "honorsSessionLimits": True, "bandwidthPriority": 0, "seedRatioMode": 0, "seedRatioLimit": 2.0,
        "seedIdleMode": 0, "seedIdleLimit": 30, "peer-limit": 50,
        "trackerList": "https://tracker.example.org/announce\n\nudp://backup.example.net:1337/announce",
        "peers": [{"address": "203.0.113.7", "clientName": "qBittorrent 5.0.1", "progress": 1.0,
                   "rateToClient": rate_down, "rateToPeer": 0, "flagStr": "DEI"},
                  {"address": "198.51.100.23", "clientName": "Transmission 4.0.6", "progress": 0.31,
                   "rateToClient": 0, "rateToPeer": rate_up, "flagStr": "UXI"}] if rate_down or rate_up else [],
    }
    sync_trackers(t)
    recompute(t)
    return t


def sync_trackers(t):
    stats = []
    for tier, block in enumerate(b for b in t["trackerList"].split("\n\n") if b.strip()):
        for url in block.split("\n"):
            if url.strip():
                host = url.split("/")[2] if "//" in url else url
                stats.append({"id": len(stats), "tier": tier, "announce": url, "host": host,
                              "lastAnnounceResult": "Success", "lastAnnounceSucceeded": True,
                              "seederCount": 12, "leecherCount": 3})
    t["trackerStats"] = stats


def recompute(t):
    total = sum(f["length"] for f in t["files"])
    wanted = [(f, s) for f, s in zip(t["files"], t["fileStats"]) if s["wanted"]]
    size = sum(f["length"] for f, _ in wanted)
    have = sum(s["bytesCompleted"] for _, s in wanted)
    t["totalSize"] = total
    t["sizeWhenDone"] = size
    t["leftUntilDone"] = size - have
    t["percentDone"] = have / size if size else 1.0
    if t["leftUntilDone"] == 0:
        t["eta"] = -1
        if t["status"] == 4:
            t["status"] = 6


class FakeDaemon:
    def __init__(self, torrents=None, username=None, password=None):
        self.torrents = torrents or []
        self.username, self.password = username, password
        self.calls: list[tuple[str, dict]] = []
        self.session = {"version": "4.1.1 (fake)", "rpc-version": 18, "download-dir": "/data/complete",
                        "incomplete-dir-enabled": False, "speed-limit-down-enabled": True,
                        "speed-limit-down": 5000, "speed-limit-up-enabled": False, "speed-limit-up": 100,
                        "alt-speed-enabled": False, "alt-speed-down": 50, "alt-speed-up": 50,
                        "start-added-torrents": True, "download-queue-enabled": True,
                        "download-queue-size": 5, "seedRatioLimited": False, "seedRatioLimit": 2.0,
                        "peer-limit-global": 200}
        self.next_id = max((t["id"] for t in self.torrents), default=0) + 1
        self.lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if daemon.username:
                    expected = "Basic " + base64.b64encode(
                        f"{daemon.username}:{daemon.password}".encode()).decode()
                    if self.headers.get("Authorization") != expected:
                        self.send_response(401)
                        self.end_headers()
                        return
                if self.headers.get("X-Transmission-Session-Id") != SESSION_ID:
                    self.send_response(409)
                    self.send_header("X-Transmission-Session-Id", SESSION_ID)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with daemon.lock:
                    daemon.calls.append((body["method"], body.get("arguments", {})))
                    try:
                        result, args = "success", daemon.dispatch(body["method"], body.get("arguments", {}))
                    except Exception as e:  # report like the daemon does
                        result, args = str(e), {}
                data = json.dumps({"result": result, "arguments": args}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/transmission/rpc"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def tick(self, seconds: float = 1.0) -> None:
        """Advance downloads as if `seconds` passed (used by serve_fake.py)."""
        with self.lock:
            for t in self.torrents:
                if t["status"] != 4 or not t["rateDownload"]:
                    continue
                budget = t["rateDownload"] * seconds
                for f, st in zip(t["files"], t["fileStats"]):
                    if st["wanted"] and st["bytesCompleted"] < f["length"] and budget > 0:
                        step = min(budget, f["length"] - st["bytesCompleted"])
                        st["bytesCompleted"] += int(step)
                        f["bytesCompleted"] = st["bytesCompleted"]
                        budget -= step
                recompute(t)
                left = t["leftUntilDone"]
                t["eta"] = int(left / t["rateDownload"]) if left else -1
                if not left:
                    t["rateDownload"] = 0
                    t["doneDate"] = int(__import__("time").time())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # -- RPC methods ----------------------------------------------------------

    def _select(self, ids):
        if ids is None:
            return list(self.torrents)
        if isinstance(ids, (int, str)):
            ids = [ids]
        return [t for t in self.torrents if t["id"] in ids or t["hashString"] in ids]

    def dispatch(self, method, a):
        if method == "torrent-get":
            out = []
            for t in self._select(a.get("ids")):
                out.append({f: copy.deepcopy(t[f]) for f in a["fields"] if f in t})
            return {"torrents": out}
        if method == "torrent-add":
            name = a.get("filename", "uploaded.torrent").rsplit("/", 1)[-1][:40]
            for t in self.torrents:
                if t["name"] == name:
                    return {"torrent-duplicate": {"id": t["id"], "name": name, "hashString": t["hashString"]}}
            t = make_torrent(self.next_id, name, [("file.bin", 1000)], done=0.0,
                             status=0 if a.get("paused") else 4,
                             download_dir=a.get("download-dir", "/data/complete"), labels=a.get("labels"))
            self.next_id += 1
            self.torrents.append(t)
            return {"torrent-added": {"id": t["id"], "name": t["name"], "hashString": t["hashString"]}}
        if method in ("torrent-start", "torrent-start-now"):
            for t in self._select(a.get("ids")):
                t["status"] = 6 if t["leftUntilDone"] == 0 else 4
            return {}
        if method == "torrent-stop":
            for t in self._select(a.get("ids")):
                t["status"] = 0
            return {}
        if method in ("torrent-verify", "torrent-reannounce"):
            return {}
        if method == "torrent-remove":
            ids = {t["id"] for t in self._select(a.get("ids"))}
            self.torrents = [t for t in self.torrents if t["id"] not in ids]
            return {}
        if method == "torrent-set":
            for t in self._select(a.get("ids")):
                for key, wanted in (("files-wanted", True), ("files-unwanted", False)):
                    for i in a.get(key, []):
                        t["fileStats"][i]["wanted"] = wanted
                for key, prio in (("priority-high", 1), ("priority-normal", 0), ("priority-low", -1)):
                    for i in a.get(key, []):
                        t["fileStats"][i]["priority"] = prio
                for key in ("downloadLimit", "downloadLimited", "uploadLimit", "uploadLimited",
                            "honorsSessionLimits", "bandwidthPriority", "seedRatioMode", "seedRatioLimit",
                            "seedIdleMode", "seedIdleLimit", "peer-limit", "labels", "trackerList"):
                    if key in a:
                        t[key] = a[key]
                if "trackerList" in a:
                    sync_trackers(t)
                recompute(t)
            return {}
        if method.startswith("queue-move-"):
            where = method.rsplit("-", 1)[1]
            order = sorted(self.torrents, key=lambda t: t["queuePosition"])
            moving = [t for t in order if t["id"] in a["ids"]]
            rest = [t for t in order if t["id"] not in a["ids"]]
            if where == "top":
                order = moving + rest
            elif where == "bottom":
                order = rest + moving
            else:
                for t in (moving if where == "up" else reversed(moving)):
                    i = order.index(t)
                    j = i - 1 if where == "up" else i + 1
                    if 0 <= j < len(order):
                        order[i], order[j] = order[j], order[i]
            for pos, t in enumerate(order):
                t["queuePosition"] = pos
            return {}
        if method == "session-set":
            self.session.update(a)
            return {}
        if method == "torrent-set-location":
            for t in self._select(a.get("ids")):
                t["downloadDir"] = a["location"]
            return {}
        if method == "torrent-rename-path":
            (t,) = self._select(a["ids"])
            path, new = a["path"], a["name"]
            parent = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
            new_path = parent + new
            hit = False
            for f in t["files"]:
                if f["name"] == path or f["name"].startswith(path + "/"):
                    f["name"] = new_path + f["name"][len(path):]
                    hit = True
            if not hit:
                raise ValueError("Invalid argument")
            if path == t["name"]:
                t["name"] = new
            return {"id": t["id"], "path": path, "name": new}
        if method == "session-get":
            return dict(self.session)
        if method == "session-stats":
            return {"torrentCount": len(self.torrents), "activeTorrentCount": 1, "pausedTorrentCount": 0,
                    "downloadSpeed": sum(t["rateDownload"] for t in self.torrents),
                    "uploadSpeed": sum(t["rateUpload"] for t in self.torrents),
                    "current-stats": {"downloadedBytes": 1 << 30, "uploadedBytes": 1 << 20, "secondsActive": 3600},
                    "cumulative-stats": {"downloadedBytes": 5 << 30, "uploadedBytes": 2 << 30,
                                         "secondsActive": 86400 * 3, "sessionCount": 4}}
        if method == "free-space":
            return {"path": a["path"], "size-bytes": 1_500_000_000_000, "total_size": 2_000_000_000_000}
        raise ValueError(f"method name not recognized: {method}")
