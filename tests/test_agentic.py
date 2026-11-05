import json

import httpx

from eval.report import retrieval_table
from eval.run_retrieval import run_agentic_mode
from ingest.models import Article
from retrieval.agentic import (
    KNOWLEDGE_SOURCE,
    VECTORIZER,
    AgenticClient,
    keys_by_doc,
    knowledge_base_body,
    parse_retrieve,
    retrieve_body,
    vectorizer_patch,
)
from retrieval.index import doc_key

ENDPOINT = "https://svc.search.windows.net"
AOAI = "https://foundry.openai.azure.com/"


def article(aid, num, source="code"):
    return Article(aid, num, source, "Code du travail > X", "texte", "2008-05-01", "https://l")


ARTICLES = [article("LEGIARTI1", "L1234-1"), article("LEGIARTI2#2", "L1234-9"),
            article("KALIARTI3", "2", "IDCC 1486")]  # fmt: skip
BY_KEY = keys_by_doc(ARTICLES)


def test_doc_keys_match_the_uploaded_index_keys():
    assert BY_KEY[doc_key("LEGIARTI2#2")] == ("L1234-9", "code")


def test_vectorizer_goes_on_every_profile_without_a_key():
    index = {"vectorSearch": {"profiles": [{"name": "hnsw-profile", "algorithm": "hnsw"}]}}
    out = vectorizer_patch(index, AOAI, "text-embedding-3-small")
    assert out["vectorSearch"]["profiles"][0]["vectorizer"] == VECTORIZER
    params = out["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"]
    assert params["resourceUri"] == AOAI.rstrip("/") and "apiKey" not in params


def test_minimal_kb_has_no_model_and_low_kb_uses_the_planner():
    minimal = knowledge_base_body("minimal", AOAI, "gpt-5.4-mini")
    low = knowledge_base_body("low", AOAI, "gpt-5.4-mini")
    assert "models" not in minimal and minimal["retrievalReasoningEffort"] == {"kind": "minimal"}
    assert low["models"][0]["azureOpenAIParameters"]["deploymentId"] == "gpt-5.4-mini"
    assert low["outputMode"] == "extractiveData" == minimal["outputMode"]


def test_minimal_retrieves_by_intent_and_low_by_message():
    assert "intents" in retrieve_body("q", "minimal") and "messages" not in retrieve_body(
        "q", "minimal"
    )
    body = retrieve_body("préavis ?", "low")
    assert body["messages"][0]["content"][0]["text"] == "préavis ?"
    assert body["knowledgeSourceParams"][0]["knowledgeSourceName"] == KNOWLEDGE_SOURCE


def test_parse_orders_by_reranker_score_maps_keys_and_sums_tokens():
    payload = {
        "references": [
            {"docKey": doc_key("LEGIARTI1"), "rerankerScore": 1.2},
            {"docKey": doc_key("KALIARTI3"), "rerankerScore": 2.9},
            {"docKey": "not-in-corpus", "rerankerScore": 3.5},
            {"docKey": None},
        ],
        "activity": [
            {"type": "modelQueryPlanning", "inputTokens": 2000, "outputTokens": 100},
            {"type": "searchIndex", "count": 10},
        ],
    }
    got = parse_retrieve(payload, BY_KEY)
    assert got.ranked == [("2", "IDCC 1486"), ("L1234-1", "code")]
    assert got.unknown_keys == ["not-in-corpus"]
    assert (got.input_tokens, got.output_tokens) == (2000, 100)


def test_client_sends_bearer_token_and_preview_api_version():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        refs = [{"docKey": doc_key("LEGIARTI1")}]
        return httpx.Response(200, json={"references": refs, "activity": []})

    client = AgenticClient(
        ENDPOINT, lambda: "tok", httpx.Client(transport=httpx.MockTransport(handler))
    )
    got, _ = client.retrieve("minimal", "préavis", BY_KEY)
    assert got.ranked == [("L1234-1", "code")]
    req = seen[0]
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.url.params["api-version"] == "2026-08-01-preview"
    assert req.url.path == "/knowledgebases/ddt-kb-minimal/retrieve"
    assert json.loads(req.content)["intents"][0]["search"] == "préavis"


class FakeAgentic:
    def __init__(self, ranked, fail=False):
        self.ranked, self.fail = ranked, fail

    def retrieve(self, effort, question, by_key):
        if self.fail:
            raise RuntimeError("POST /knowledgebases/x/retrieve -> 403: forbidden")
        from retrieval.agentic import Retrieval

        return Retrieval(self.ranked, input_tokens=1000, output_tokens=200), 0.5


ROWS = [
    {"id": "g1", "type": "lookup", "question": "q", "gold": [{"num": "L1234-1", "source": "code"}]}
]
PRICE = {"input_per_1m": 0.75, "output_per_1m": 4.5}


def test_agentic_mode_scores_like_the_classic_modes_and_prices_planner_tokens():
    out = run_agentic_mode(FakeAgentic([("L1234-1", "code")]), ROWS, "low", BY_KEY, PRICE)
    t = out["totals"]
    assert out["mode"] == "agentic_low" and t["hit@1"] == 1.0
    assert t["planner_tokens_per_query"] == 1200
    assert round(t["usd_per_1000_queries"], 4) == round((1000 * 0.75 + 200 * 4.5) / 1e6 * 1000, 4)


def test_agentic_failure_is_unavailable_not_zero():
    out = run_agentic_mode(FakeAgentic([], fail=True), ROWS, "minimal", BY_KEY, PRICE)
    assert out["available"] is False and "403" in out["error"]


def test_report_adds_foundry_iq_rows_and_planner_cost_table():
    mode = run_agentic_mode(FakeAgentic([("L1234-1", "code")]), ROWS, "low", BY_KEY, PRICE)
    table = retrieval_table({"label": "basic", "n_questions": 1, "modes": [mode]})
    assert "Foundry IQ, low effort (gpt-5.4-mini planner)" in table
    assert "Planner tokens per query" in table and "| 1200 |" in table


def test_client_retries_timeouts_and_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("retrieval.agentic.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if calls["n"] == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"references": [], "activity": []})

    client = AgenticClient(
        ENDPOINT, lambda: "t", httpx.Client(transport=httpx.MockTransport(handler))
    )
    got, _ = client.retrieve("low", "q", BY_KEY)
    assert calls["n"] == 3 and got.ranked == []


def test_client_does_not_retry_bad_requests(monkeypatch):
    monkeypatch.setattr("retrieval.agentic.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    client = AgenticClient(
        ENDPOINT, lambda: "t", httpx.Client(transport=httpx.MockTransport(handler))
    )
    with __import__("pytest").raises(RuntimeError, match="400"):
        client.retrieve("low", "q", BY_KEY)
    assert calls["n"] == 1
