"""Spend guard for the public /ask: a per-IP sliding window and a global daily cap.

Counters live in memory. That is enough because the container app runs one replica at most
(max_replicas = 1); a restart resets them, which only ever lets a few more questions through.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime


class Limiter:
    def __init__(
        self,
        per_ip: int = 5,
        window_s: float = 60.0,
        daily_cap: int = 300,
        clock: Callable[[], float] = time.time,
    ):
        self.per_ip, self.window_s, self.daily_cap, self.clock = per_ip, window_s, daily_cap, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day, self._count = "", 0

    def check(self, ip: str) -> str | None:
        """Record one question. Returns None when allowed, else the reason it was refused."""
        now = self.clock()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if day != self._day:
            self._day, self._count = day, 0
        if self._count >= self.daily_cap:
            return "daily"
        hits = self._hits[ip]
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.per_ip:
            return "ip"
        hits.append(now)
        self._count += 1
        return None


def client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """Container Apps ingress puts the caller first in X-Forwarded-For."""
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return peer or "unknown"
