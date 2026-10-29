from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot import telemetry
from copilot.api import create_app
from copilot.limits import Limiter, client_ip
from copilot.tools import ToolContext, make_tools
from tests.test_copilot import CORPUS, FakeCopilot

# Limits ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):  # 2027-01-15 08:00 UTC
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_limiter_per_ip_window_slides():
    clock = Clock()
    lim = Limiter(per_ip=2, window_s=60, daily_cap=100, clock=clock)
    assert lim.check("a") is None and lim.check("a") is None
    assert lim.check("a") == "ip"
    assert lim.check("b") is None  # other callers unaffected
    clock.t += 61
    assert lim.check("a") is None


def test_limiter_daily_cap_resets_next_utc_day():
    clock = Clock()
    lim = Limiter(per_ip=100, daily_cap=3, clock=clock)
    for ip in "abc":
        assert lim.check(ip) is None
    assert lim.check("d") == "daily"
    clock.t += 24 * 3600
    assert lim.check("d") is None


def test_refused_questions_do_not_count():
    clock = Clock()
    lim = Limiter(per_ip=1, daily_cap=2, clock=clock)
    assert lim.check("a") is None
    assert lim.check("a") == "ip"  # refused, so the daily count stays at 1
    assert lim.check("b") is None


def test_client_ip_prefers_first_forwarded_address():
    assert client_ip("203.0.113.7, 10.0.0.1", "10.0.0.2") == "203.0.113.7"
    assert client_ip(None, "10.0.0.2") == "10.0.0.2"
    assert client_ip(None, None) == "unknown"


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
