import asyncio
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from fastapi import HTTPException
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot import telemetry
from copilot.api import create_app, run_unless_disconnected
from copilot.limits import BlobCounter, Limiter, MemoryCounter, client_ip, limiter_from_env
from copilot.tools import ToolContext, make_tools
from tests.test_copilot import CORPUS, FakeCopilot

# Limits ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):  # 2027-01-15 08:00 UTC
        self.t = t

    def __call__(self) -> float:
        return self.t


def check(lim, ip):
    return asyncio.run(lim.check(ip))


def test_limiter_per_ip_window_slides():
    clock = Clock()
    lim = Limiter(per_ip=2, window_s=60, daily_cap=100, clock=clock)
    assert check(lim, "a") is None and check(lim, "a") is None
    assert check(lim, "a") == "ip"
    assert check(lim, "b") is None  # other callers unaffected
    clock.t += 61
    assert check(lim, "a") is None


def test_limiter_daily_cap_resets_next_utc_day():
    clock = Clock()
    lim = Limiter(per_ip=100, daily_cap=3, clock=clock)
    for ip in "abc":
        assert check(lim, ip) is None
    assert check(lim, "d") == "daily"
    clock.t += 24 * 3600
    assert check(lim, "d") is None


def test_refused_questions_do_not_count():
    clock = Clock()
    lim = Limiter(per_ip=1, daily_cap=2, clock=clock)
    assert check(lim, "a") is None
    assert check(lim, "a") == "ip"  # refused, so the daily count stays at 1
    assert check(lim, "b") is None


def test_client_ip_uses_the_address_the_ingress_appended():
    # The caller controls everything left of the last entry.
    assert client_ip("1.1.1.1, 203.0.113.7", "10.0.0.2") == "203.0.113.7"
    assert client_ip("203.0.113.7", "10.0.0.2") == "203.0.113.7"
    assert client_ip(" , ", "10.0.0.2") == "10.0.0.2"
    assert client_ip(None, "10.0.0.2") == "10.0.0.2"
    assert client_ip(None, None) == "unknown"


def test_forged_forwarded_for_does_not_dodge_the_per_ip_limit():
    app = create_app(FakeCopilot, limiter=Limiter(per_ip=1, daily_cap=10))
    with TestClient(app) as client:
        for i, expected in enumerate([200, 429]):
            headers = {"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.7"}
            resp = client.post("/ask", json={"question": "Préavis ?"}, headers=headers)
            assert resp.status_code == expected


# Durable daily counter ------------------------------------------------------------------------


class FakeBlob:
    """In-memory stand-in for an async BlobClient, with the ETag rules the counter relies on."""

    def __init__(self, store, name, race=0):
        self.store, self.name, self.race = store, name, race

    async def download_blob(self):
        if self.name not in self.store:
            raise ResourceNotFoundError("missing")
        data, etag = self.store[self.name]
        return SimpleNamespace(readall=_async(data), properties=SimpleNamespace(etag=etag))

    async def upload_blob(self, data, overwrite, etag=None, match_condition=None):
        if self.race:  # another writer slips in between our read and write
            self.race -= 1
            old = int(self.store.get(self.name, (b"0", ""))[0])
            self.store[self.name] = (str(old + 1).encode(), f"e{old + 1}")
        current = self.store.get(self.name)
        if match_condition == MatchConditions.IfMissing and current is not None:
            raise HttpResponseError(response=SimpleNamespace(status_code=409, reason="exists"))
        if match_condition == MatchConditions.IfNotModified and current[1] != etag:
            raise HttpResponseError(response=SimpleNamespace(status_code=412, reason="changed"))
        self.store[self.name] = (data, f"e{int(data)}")


def _async(value):
    async def f():
        return value

    return f


class FakeContainer:
    def __init__(self, race=0):
        self.store, self.race, self.closed = {}, race, False

    def get_blob_client(self, name):
        return FakeBlob(self.store, name, self.race)

    async def close(self):
        self.closed = True


def test_blob_counter_survives_a_restart():
    container = FakeContainer()

    def fresh():  # a new process: scale from zero or a new revision
        return Limiter(per_ip=100, daily_cap=2, clock=Clock(), counter=BlobCounter(container))

    for _ in range(2):
        assert check(fresh(), "a") is None
    assert check(fresh(), "a") == "daily"
    assert container.store == {"daily/2027-01-15.txt": (b"2", "e2")}


def test_blob_counter_retries_when_another_writer_wins():
    container = FakeContainer(race=1)
    counter = BlobCounter(container)
    assert asyncio.run(counter.take("2027-01-15", 10)) is True
    assert int(container.store["daily/2027-01-15.txt"][0]) == 2  # theirs and ours


def test_limiter_falls_back_to_memory_when_storage_fails():
    class Broken:
        async def take(self, day, cap):
            raise OSError("storage down")

    lim = Limiter(per_ip=100, daily_cap=1, clock=Clock(), counter=Broken())
    assert check(lim, "a") is None
    assert check(lim, "b") == "daily"


def test_limiter_from_env_uses_blob_only_when_configured(monkeypatch):
    monkeypatch.delenv("DAILY_COUNTER_BLOB_URL", raising=False)
    monkeypatch.setenv("DAILY_QUESTION_CAP", "7")
    lim = limiter_from_env()
    assert isinstance(lim.counter, MemoryCounter) and lim.daily_cap == 7
    monkeypatch.setenv("DAILY_COUNTER_BLOB_URL", "https://acct.blob.core.windows.net/counters")
    lim = limiter_from_env()
    assert isinstance(lim.counter, BlobCounter)
    asyncio.run(lim.aclose())


# API ------------------------------------------------------------------------------------------


def test_api_returns_429_with_french_message_over_the_limit():
    app = create_app(FakeCopilot, limiter=Limiter(per_ip=1, daily_cap=10))
    with TestClient(app) as client:
        assert client.post("/ask", json={"question": "Préavis ?"}).status_code == 200
        resp = client.post("/ask", json={"question": "Préavis ?"})
        assert resp.status_code == 429
        assert "réessayez" in resp.json()["detail"]


def test_api_serves_the_web_page_without_shadowing_the_api(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Copilot</h1>", encoding="utf-8")
    with TestClient(create_app(FakeCopilot, web_dist=tmp_path)) as client:
        assert "Copilot" in client.get("/").text
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/ask", json={"question": "Préavis ?"}).status_code == 200


def test_api_refuses_large_bodies():
    with TestClient(create_app(FakeCopilot)) as client:
        big = '{"question": "' + "a" * 20_000 + '"}'
        resp = client.post("/ask", content=big, headers={"Content-Type": "application/json"})
        assert resp.status_code == 413

        def chunks():  # no Content-Length: counted as it arrives
            yield b'{"question": "'
            for _ in range(20):
                yield b"a" * 1000
            yield b'"}'

        resp = client.post("/ask", content=chunks(), headers={"Content-Type": "application/json"})
        assert resp.status_code == 413


def test_api_sends_security_headers():
    with TestClient(create_app(FakeCopilot)) as client:
        headers = client.get("/health").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"


def test_api_docs_are_off_on_container_apps(monkeypatch):
    monkeypatch.setenv("CONTAINER_APP_NAME", "ddt-api")
    with TestClient(create_app(FakeCopilot)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
    monkeypatch.delenv("CONTAINER_APP_NAME")
    with TestClient(create_app(FakeCopilot)) as client:
        assert client.get("/openapi.json").status_code == 200


def test_agent_run_is_cancelled_when_the_caller_disconnects():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    class GoneRequest:
        async def is_disconnected(self):
            return started.is_set()

    async def scenario():
        with pytest.raises(HTTPException) as err:
            await run_unless_disconnected(GoneRequest(), slow(), poll_s=0.01)
        assert err.value.status_code == 499
        await asyncio.sleep(0)
        assert cancelled.is_set()

    asyncio.run(scenario())


# Telemetry ------------------------------------------------------------------------------------


def test_setup_without_connection_string_uses_a_local_provider(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert telemetry.setup() is False
    assert isinstance(trace.get_tracer_provider(), TracerProvider)


def test_tool_calls_tag_their_span_with_article_numbers():
    telemetry.setup()
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    calls = []
    tools = {t.name: t for t in make_tools(ToolContext(search=lambda q, s: [], corpus=CORPUS), calls)}
    with trace.get_tracer("test").start_as_current_span("execute_tool preavis"):
        tools["preavis"](36)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "execute_tool preavis"]
    assert span.attributes["copilot.tool.nums"] == ("L1234-1",)
