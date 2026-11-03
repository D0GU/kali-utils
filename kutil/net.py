"""JSON over HTTP for online lookups: optional cache, per-host pacing, retries, and skipping dead hosts."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DAY = 86400


class NetError(Exception):
    pass


class Http:
    def __init__(self, db=None, timeout: float = 12.0, user_agent: str = "kali-utils"):
        self.db = db
        self.timeout = timeout
        self.user_agent = user_agent
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._down: dict[str, float] = {}   # host -> time it failed (skip it for a while)

    def request(self, url: str, *, params: dict | None = None, data: dict | None = None,
                headers: dict | None = None, ttl: float = 7 * DAY, min_interval: float = 0.25,
                timeout: float | None = None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = json.dumps(data).encode() if data is not None else None
        key = url + (body.decode() if body else "")
        if self.db and ttl:
            with self._lock:
                cached = self.db.cache_get(key, ttl)
            if cached is not None:
                return cached
        host = urllib.parse.urlsplit(url).netloc
        if time.monotonic() - self._down.get(host, -1e9) < 600:
            raise NetError(f"{host}: unreachable (skipping for now)")
        with self._lock:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        with host_lock:
            return self._fetch(url, host, body, key, headers, ttl, min_interval, timeout)

    def _fetch(self, url, host, body, key, headers, ttl, min_interval, timeout):
        wait = self._last.get(host, 0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        for attempt in range(4):
            self._last[host] = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    result = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    result = None
                    break
                if e.code in (429, 503) and attempt < 3:   # rate limited or briefly overloaded
                    try:
                        delay = float(e.headers.get("Retry-After") or 0)
                    except ValueError:
                        delay = 0
                    time.sleep(min(30.0, delay or 2.0 ** (attempt + 1)) + 0.5)
                    continue
                if e.code == 401:
                    raise NetError(f"{host}: unauthorized - check the API key") from None
                raise NetError(f"{host}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                self._down[host] = time.monotonic()
                raise NetError(f"{host}: {reason}") from None
            except json.JSONDecodeError:
                raise NetError(f"{host}: invalid response") from None
        if self.db and ttl:
            with self._lock:
                self.db.cache_put(key, result)
        return result
