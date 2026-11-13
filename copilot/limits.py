"""Spend guard for the public /ask: a per-IP sliding window and a global daily cap.

The per-IP window lives in memory: the app runs one replica at most (max_replicas = 1) and a
restart only forgets the last minute. The daily cap must survive scale-to-zero and deploys, so
when DAILY_COUNTER_BLOB_URL is set it is kept in one small blob per UTC day, updated with an
ETag compare-and-swap. Without it (local runs, tests) the count stays in memory.
"""

import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

log = logging.getLogger(__name__)


class DailyCounter(Protocol):
    async def take(self, day: str, cap: int) -> bool:
        """Count one question for `day` unless `cap` is reached. True when counted."""


class MemoryCounter:
    def __init__(self):
        self._day, self._count = "", 0

    async def take(self, day: str, cap: int) -> bool:
        if day != self._day:
            self._day, self._count = day, 0
        if self._count >= cap:
            return False
        self._count += 1
        return True


class BlobCounter:
    """One blob per day (daily/2026-11-13.txt) holding the count as text."""

    def __init__(self, container, attempts: int = 5, closers=()):
        self.container = container  # azure.storage.blob.aio.ContainerClient
        self.attempts = attempts
        self._closers = [container.close, *closers]

    @classmethod
    def from_url(cls, container_url: str, credential) -> "BlobCounter":
        from azure.storage.blob.aio import ContainerClient

        container = ContainerClient.from_container_url(container_url, credential=credential)
        return cls(container, closers=[credential.close])

    async def take(self, day: str, cap: int) -> bool:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        blob = self.container.get_blob_client(f"daily/{day}.txt")
        for _ in range(self.attempts):
            try:
                stream = await blob.download_blob()
                count = int((await stream.readall()) or 0)
                etag = stream.properties.etag
                cond = {"etag": etag, "match_condition": MatchConditions.IfNotModified}
            except ResourceNotFoundError:
                count, cond = 0, {"match_condition": MatchConditions.IfMissing}
            if count >= cap:
                return False
            try:
                await blob.upload_blob(str(count + 1).encode(), overwrite=True, **cond)
                return True
            except HttpResponseError as err:
                if err.status_code not in (409, 412):  # 409 created meanwhile, 412 changed
                    raise
        raise RuntimeError(f"daily counter still contended after {self.attempts} attempts")

    async def aclose(self) -> None:
        for close in self._closers:
            await close()


class Limiter:
    def __init__(
        self,
        per_ip: int = 5,
        window_s: float = 60.0,
        daily_cap: int = 300,
        clock: Callable[[], float] = time.time,
        counter: DailyCounter | None = None,
    ):
        self.per_ip, self.window_s, self.daily_cap, self.clock = per_ip, window_s, daily_cap, clock
        self.counter = counter or MemoryCounter()
        # Fallback when the durable counter fails: never worse than the old in-memory cap.
        self._fallback = MemoryCounter() if counter is not None else None
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def check(self, ip: str) -> str | None:
        """Record one question. Returns None when allowed, else the reason it was refused."""
        now = self.clock()
        hits = self._hits[ip]
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.per_ip:
            return "ip"
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if not await self._take(day):
            return "daily"
        hits.append(now)
        return None

    async def _take(self, day: str) -> bool:
        if self._fallback is None:
            return await self.counter.take(day, self.daily_cap)
        local = await self._fallback.take(day, self.daily_cap)
        try:
            return await self.counter.take(day, self.daily_cap)
        except Exception:
            log.exception("durable daily counter failed; using the in-memory count")
            return local

    async def aclose(self) -> None:
        if hasattr(self.counter, "aclose"):
            await self.counter.aclose()


def limiter_from_env() -> Limiter:
    cap = int(os.environ.get("DAILY_QUESTION_CAP", "300"))
    url = os.environ.get("DAILY_COUNTER_BLOB_URL")
    if not url:
        return Limiter(daily_cap=cap)
    from azure.identity.aio import DefaultAzureCredential

    return Limiter(daily_cap=cap, counter=BlobCounter.from_url(url, DefaultAzureCredential()))


def client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """The rightmost X-Forwarded-For entry: Container Apps ingress appends the address it saw
    and passes on whatever the caller sent before it, so the leftmost entries can be forged."""
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer or "unknown"
