"""Minimal client for the Transmission RPC protocol (stdlib only)."""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.request

SESSION_HEADER = "X-Transmission-Session-Id"


class RPCError(Exception):
    """The daemon could not be reached or rejected a request."""


class AuthError(RPCError):
    """The daemon rejected our credentials (HTTP 401)."""


class Client:
    def __init__(self, url: str, username: str | None = None,
                 password: str | None = None, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self._session_id: str | None = None
        self._auth: str | None = None
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._auth = "Basic " + token

    # -- transport -----------------------------------------------------------

    def call(self, method: str, arguments: dict | None = None) -> dict:
        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        for _attempt in range(2):
            req = urllib.request.Request(self.url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            if self._auth:
                req.add_header("Authorization", self._auth)
            if self._session_id:
                req.add_header(SESSION_HEADER, self._session_id)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                # Transmission's CSRF protection: first request returns 409 with
                # the session id we must echo back.
                if e.code == 409 and e.headers.get(SESSION_HEADER):
                    self._session_id = e.headers[SESSION_HEADER]
                    e.close()
                    continue
                if e.code == 401:
                    raise AuthError("authentication failed (HTTP 401) - check username/password "
                                    "or run `trctl setup`") from None
                if e.code == 403:
                    raise RPCError("forbidden (HTTP 403) - this host is probably not in the "
                                   "daemon's rpc-whitelist") from None
                raise RPCError(f"HTTP {e.code} {e.reason} from {self.url}") from None
            except urllib.error.URLError as e:
                raise RPCError(f"cannot reach {self.url}: {e.reason}") from None
            except (socket.timeout, TimeoutError):
                raise RPCError(f"timed out talking to {self.url}") from None
            except json.JSONDecodeError:
                raise RPCError(f"{self.url} did not return JSON - is this a Transmission RPC URL?") from None
        else:
            raise RPCError("daemon kept rejecting the session id (HTTP 409)")

        result = body.get("result")
        if result != "success":
            raise RPCError(f"{method}: {result}")
        return body.get("arguments", {})

    # -- torrents ------------------------------------------------------------

    def torrent_get(self, fields: list[str], ids: list | None = None) -> list[dict]:
        args: dict = {"fields": fields}
        if ids is not None:
            args["ids"] = ids
        return self.call("torrent-get", args).get("torrents", [])

    def torrent_add(self, *, filename: str | None = None, metainfo: bytes | None = None,
                    download_dir: str | None = None, paused: bool = False,
                    labels: list[str] | None = None) -> tuple[str, dict]:
        args: dict = {"paused": paused}
        if filename:
            args["filename"] = filename
        if metainfo is not None:
            args["metainfo"] = base64.b64encode(metainfo).decode()
        if download_dir:
            args["download-dir"] = download_dir
        if labels:
            args["labels"] = labels
        res = self.call("torrent-add", args)
        if "torrent-duplicate" in res:
            return "duplicate", res["torrent-duplicate"]
        return "added", res.get("torrent-added", {})

    def torrent_action(self, action: str, ids: list) -> None:
        # action: start, start-now, stop, verify, reannounce
        self.call(f"torrent-{action}", {"ids": ids})

    def torrent_remove(self, ids: list, delete_data: bool = False) -> None:
        self.call("torrent-remove", {"ids": ids, "delete-local-data": delete_data})

    def torrent_set(self, ids: list, **fields) -> None:
        args = {k.replace("_", "-"): v for k, v in fields.items()}
        args["ids"] = ids
        self.call("torrent-set", args)

    def queue_move(self, where: str, ids: list) -> None:
        # where: top, up, down, bottom
        self.call(f"queue-move-{where}", {"ids": ids})

    def set_location(self, ids: list, location: str, move: bool = True) -> None:
        self.call("torrent-set-location", {"ids": ids, "location": location, "move": move})

    def rename_path(self, torrent_id: int, path: str, name: str) -> dict:
        return self.call("torrent-rename-path", {"ids": [torrent_id], "path": path, "name": name})

    # -- session -------------------------------------------------------------

    def session_get(self, fields: list[str] | None = None) -> dict:
        return self.call("session-get", {"fields": fields} if fields else {})

    def session_set(self, args: dict) -> None:
        self.call("session-set", args)

    def session_stats(self) -> dict:
        return self.call("session-stats")

    def free_space(self, path: str) -> dict:
        return self.call("free-space", {"path": path})
