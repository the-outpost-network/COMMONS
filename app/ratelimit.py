"""Small in-memory rate limiter (the app runs as a single process)."""

from __future__ import annotations

import ipaddress
import time
from collections import deque

from fastapi import Request

# Only these direct peers may tell us the client address via X-Forwarded-For.
# The app is reachable only through Caddy on the internal Docker network.
_TRUSTED = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128")
]


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    try:
        trusted = any(ipaddress.ip_address(peer) in n for n in _TRUSTED)
    except ValueError:
        trusted = False
    forwarded = request.headers.get("x-forwarded-for")
    if trusted and forwarded:
        # Caddy ignores client-supplied X-Forwarded-For (no trusted_proxies set),
        # so the last entry is the address Caddy saw.
        candidate = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return peer or "unknown"


class RateLimiter:
    def __init__(self, per_minute: int, max_keys: int = 10_000) -> None:
        self.per_minute = per_minute
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._hits) >= self.max_keys:
            self._prune(now)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True

    def _prune(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= 60]:
            del self._hits[key]
        if len(self._hits) >= self.max_keys:  # still full: drop the oldest keys
            for key in list(self._hits)[: len(self._hits) // 2]:
                del self._hits[key]
