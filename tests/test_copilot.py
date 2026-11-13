import asyncio
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot import calculators
from copilot.api import create_app
from copilot.core import (
    MAX_OUTPUT_TOKENS,
    AskResult,
    Copilot,
    collapse_repeats,
    ensure_notice,
    extract_citations,
    finalize,
    strip_links,
)
from copilot.piste import PisteClient, PisteError
from copilot.prompts import NOTICE
from copilot.tools import MAX_ARTICLE, Corpus, ToolCall, ToolContext, _cite, make_tools
from ingest.models import Article
from retrieval.search import Hit

# calculators: worked examples to check against service-public.fr --------------------------


@pytest.mark.parametrize(
    "mois, attendu",
    [(4, None), (6, 1), (23, 1), (24, 2), (120, 2)],
)
def test_preavis_legal_minimum(mois, attendu):
    res = calculators.preavis(mois)
    assert res.valeur == attendu
    assert res.articles == ["L1234-1"]


@pytest.mark.parametrize("statut", ["faute_grave", "faute_lourde"])
def test_preavis_none_for_serious_misconduct(statut):
    assert calculators.preavis(60, statut).valeur == 0


@pytest.mark.parametrize(
    "mois, salaire, attendu",
    [
        (7, 2000, 0),  # under 8 months: not eligible (L1234-9)
        (8, 2000, 333.33),  # 2000 x 1/4 x 8/12
        (60, 2500, 3125.00),  # 2500 x 1/4 x 5
        (126, 2400, 6400.00),  # 2400 x (1/4 x 10 + 1/3 x 0.5)
        (144, 3000, 9500.00),  # 3000 x (1/4 x 10 + 1/3 x 2)
    ],
)
def test_indemnite_licenciement(mois, salaire, attendu):
    assert calculators.indemnite_licenciement(mois, salaire).valeur == attendu


def test_indemnite_none_for_faute_grave():
    assert calculators.indemnite_licenciement(144, 3000, "faute_grave").valeur == 0


def test_indemnite_cites_its_articles():
    assert calculators.indemnite_licenciement(60, 2500).articles == [
        "L1234-9",
        "R1234-1",
        "R1234-2",
    ]


@pytest.mark.parametrize(
    "call, error",
    [
        (lambda: calculators.preavis(-1), ValueError),
        (lambda: calculators.preavis("3 ans"), TypeError),
        (lambda: calculators.preavis(12, "licenciement"), ValueError),
        (lambda: calculators.indemnite_licenciement(12, -5), ValueError),
        (lambda: calculators.indemnite_licenciement(12, True), TypeError),
    ],
)
def test_calculators_validate_inputs(call, error):
    with pytest.raises(error):
        call()


# PISTE client -------------------------------------------------------------------------------


class FakePiste:
    """httpx.MockTransport handler: a token endpoint and a scripted article endpoint."""

    def __init__(self, article_responses):
        self.article_responses = list(article_responses)
        self.token_calls = 0
        self.article_calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            self.token_calls += 1
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(
                200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600}
            )
        self.article_calls.append(
            (request.url.path, json.loads(request.content), request.headers["authorization"])
        )
        return self.article_responses.pop(0)


ARTICLE_BODY = {
    "article": {
        "id": "LEGIARTI000006901112",
        "num": "L1234-1",
        "texte": "Lorsque le licenciement n'est pas motivé par une faute grave...",
        "dateDebut": 1209600000000,  # 2008-05-01
        "etat": "VIGUEUR",
    }
}


def piste_client(handler, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PisteClient("id", "secret", http=http, sleep=lambda s: None, **kwargs)


def test_piste_get_article_parses_and_caches_token():
    fake = FakePiste([httpx.Response(200, json=ARTICLE_BODY)] * 2)
    client = piste_client(fake)
    a = client.get_article("LEGIARTI000006901112")
    client.get_article("LEGIARTI000006901112")
    assert (a.num, a.date_debut_vigueur, a.etat) == ("L1234-1", "2008-05-01", "VIGUEUR")
    assert fake.token_calls == 1
    path, body, auth = fake.article_calls[0]
    assert path.endswith("/consult/getArticle") and body == {"id": "LEGIARTI000006901112"}
    assert auth == "Bearer tok1"


def test_piste_routes_kali_ids():
    fake = FakePiste([httpx.Response(200, json=ARTICLE_BODY)])
    piste_client(fake).get_article("KALIARTI000005826251")
    assert fake.article_calls[0][0].endswith("/consult/kaliArticle")


def test_piste_refreshes_expired_token():
    now = [0.0]
    fake = FakePiste([httpx.Response(200, json=ARTICLE_BODY)] * 2)
    client = piste_client(fake, clock=lambda: now[0])
    client.get_article("LEGIARTI1")
    now[0] = 3600.0
    client.get_article("LEGIARTI1")
    assert fake.token_calls == 2


def test_piste_retries_429_then_succeeds():
    fake = FakePiste(
        [httpx.Response(429, headers={"retry-after": "0"}), httpx.Response(200, json=ARTICLE_BODY)]
    )
    assert piste_client(fake).get_article("LEGIARTI1").num == "L1234-1"
    assert len(fake.article_calls) == 2


def test_piste_raises_on_empty_article():
    fake = FakePiste([httpx.Response(200, json={"article": {}})])
    with pytest.raises(PisteError):
        piste_client(fake).get_article("LEGIARTI1")


def test_piste_from_env_needs_both_keys(monkeypatch):
    monkeypatch.delenv("PISTE_CLIENT_ID", raising=False)
    monkeypatch.delenv("PISTE_CLIENT_SECRET", raising=False)
    assert PisteClient.from_env() is None


# tools --------------------------------------------------------------------------------------


def art(id, num, source="code", text="texte", path="Partie législative > Livre II"):
    url = (
        f"https://www.legifrance.gouv.fr/codes/article_lc/{id.split('#')[0]}"
        if source == "code"
        else f"https://www.legifrance.gouv.fr/conv_coll/article/{id.split('#')[0]}"
    )
    return Article(id, num, source, path, text, "2008-05-01", url)


CORPUS = Corpus(
    [
        art("LEGIARTI000006901112", "L1234-1", text="Préavis..."),
        art("LEGIARTI000006901125", "L1234-9", text="Indemnité..."),
        art("LEGIARTI000035644692", "R1234-2", text="Un quart de mois..."),
        art(
            "KALIARTI1#1",
            "4.2",
            "IDCC 1486",
            "Syntec > Titre 4\nPremière partie",
            "Syntec > Titre 4",
        ),
        art(
            "KALIARTI1#2",
            "4.2",
            "IDCC 1486",
            "Syntec > Titre 4\nSeconde partie",
            "Syntec > Titre 4",
        ),
        art("KALIARTI2", "4.2", "IDCC 1979", "HCR..."),
    ]
)


def hit(num, source="code"):
    a = CORPUS.lookup(num, source)[0]
    return Hit(a.id, a.num, a.source, a.path, a.text, a.legifrance_url, a.date_debut_vigueur, 0.03)


def tools(piste=None, search=None):
    calls: list[ToolCall] = []
    ctx = ToolContext(search=search or (lambda q, s: [hit("L1234-1")]), corpus=CORPUS, piste=piste)
    return {t.name: t for t in make_tools(ctx, calls)}, calls


def test_corpus_merges_split_pieces():
    (syntec,) = CORPUS.lookup("4.2", "IDCC 1486")
    assert syntec.id == "KALIARTI1"
    assert syntec.text == "Première partie\nSeconde partie"


def test_get_article_falls_back_to_local_copy_for_r_articles():
    t, calls = tools()
    out = json.loads(t["get_article"]("R1234-2"))
    (a,) = out["articles"]
    assert a["num"] == "R1234-2" and a["origine"] == "copie locale"
    assert calls[0].nums == ["R1234-2"]


def test_get_article_uses_piste_when_available():
    live = SimpleNamespace(text="Texte vivant", date_debut_vigueur="2019-01-01")
    t, _ = tools(piste=SimpleNamespace(get_article=lambda id: live))
    (a,) = json.loads(t["get_article"]("L1234-1"))["articles"]
    assert a["texte"] == "Texte vivant" and a["origine"].startswith("Légifrance")
    assert a["date_debut_vigueur"] == "2019-01-01"


def test_get_article_survives_piste_failure():
    def boom(id):
        raise PisteError("HTTP 503")

    t, _ = tools(piste=SimpleNamespace(get_article=boom))
    (a,) = json.loads(t["get_article"]("L1234-1"))["articles"]
    assert a["texte"] == "Préavis..." and "indisponible" in a["origine"]


def test_get_article_truncates_long_texts():
    long = SimpleNamespace(text="x" * 50_000, date_debut_vigueur="")
    t, calls = tools(piste=SimpleNamespace(get_article=lambda id: long))
    (a,) = json.loads(t["get_article"]("L1234-1"))["articles"]
    assert len(a["texte"]) == MAX_ARTICLE
    assert len(calls[0].texts[0]) < MAX_ARTICLE + 50


def test_default_agent_caps_output_tokens():
    copilot = Copilot(chat_client=object(), ctx=ToolContext(search=None, corpus=CORPUS))
    assert copilot._default_agent([]).default_options["max_tokens"] == MAX_OUTPUT_TOKENS


def test_get_article_kali_needs_source_and_unknown_num_errors():
    t, _ = tools()
    both = json.loads(t["get_article"]("4.2"))["articles"]
    assert {a["source"] for a in both} == {"IDCC 1486", "IDCC 1979"}
    assert json.loads(t["get_article"]("4.2", "IDCC 1979"))["articles"][0]["source"] == "IDCC 1979"
    assert "erreur" in json.loads(t["get_article"]("L9999-99"))


def test_search_code_shapes_results_and_ignores_bad_source():
    seen = []
    t, calls = tools(search=lambda q, s: seen.append((q, s)) or [hit("L1234-1")])
    out = json.loads(t["search_code"]("préavis", "IDCC 9999"))
    assert seen == [("préavis", None)]
    assert out["resultats"][0]["url"].endswith("LEGIARTI000006901112")
    assert calls[0].name == "search_code" and calls[0].nums == ["L1234-1"]


def test_calculator_tools_record_citations_with_urls():
    t, calls = tools()
    res = json.loads(t["indemnite_licenciement"](60, 2500.0))
    assert res["valeur"] == 3125.0
    urls = {a["num"]: a["url"] for a in calls[0].articles}
    assert urls["L1234-9"].endswith("LEGIARTI000006901125")


# ask() post-processing --------------------------------------------------------------------------


def test_ensure_notice_appends_once():
    assert ensure_notice("Réponse.").endswith(NOTICE)
    assert ensure_notice(f"Réponse.\n{NOTICE}").count(NOTICE) == 1


def test_extract_citations_by_number_or_url():
    calls = [
        ToolCall("search_code", {}, ["L1234-1", "4.2"], [
            {"num": "L1234-1", "url": "https://x/L1", "source": "code"},
            {"num": "4.2", "url": "https://x/K", "source": "IDCC 1486"},
            {"num": "L1234-5", "url": "https://x/L5", "source": "code"},
        ]),
        ToolCall("get_article", {}, ["L1234-1"], [{"num": "L1234-1", "url": "https://x/L1", "source": "code"}]),
    ]  # fmt: skip
    answer = (
        "Selon l'article L. 1234-1, deux mois. Syntec : voir https://x/K. Le 4.2 % n'est pas cité."
    )
    cited = extract_citations(answer, calls)
    assert [(c["num"], c["source"]) for c in cited] == [("L1234-1", "code"), ("4.2", "IDCC 1486")]


class StubAgent:
    def __init__(self, tools, answer):
        self.tools, self.answer = {t.name: t for t in tools}, answer

    async def run(self, question):
        self.tools["preavis"](36)  # what the model would do
        return SimpleNamespace(
            text=self.answer,
            usage_details={
                "input_token_count": 10,
                "output_token_count": 5,
                "total_token_count": 15,
            },
        )


def test_ask_returns_answer_citations_tools_and_trace_id():
    ctx = ToolContext(search=lambda q, s: [], corpus=CORPUS)
    answer = "Le préavis est de 2 mois (article L1234-1)."
    copilot = Copilot(
        chat_client=None, ctx=ctx, agent_factory=lambda tools: StubAgent(tools, answer)
    )
    result = asyncio.run(copilot.ask("Préavis après 3 ans ?"))
    assert result.answer.endswith(NOTICE)
    assert [c["num"] for c in result.citations] == ["L1234-1"]
    assert result.tool_calls == [
        {
            "name": "preavis",
            "args": {"anciennete_mois": 36, "statut": "normal"},
            "nums": ["L1234-1"],
        }
    ]
    assert re.fullmatch(r"[0-9a-f]{32}", result.trace_id) and result.trace_id != "0" * 32
    assert result.usage["total_tokens"] == 15


# API ------------------------------------------------------------------------------------------


class FakeCopilot:
    async def ask(self, question):
        return AskResult(question, f"Réponse. {NOTICE}", [{"num": "L1234-1", "url": "u", "source": "code"}],
                         [{"name": "preavis", "args": {"anciennete_mois": 36}, "nums": ["L1234-1"]}], "ab" * 16)  # fmt: skip


def test_api_ask_schema():
    with TestClient(create_app(FakeCopilot)) as client:
        resp = client.post("/ask", json={"question": "Préavis après 3 ans ?"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"answer", "citations", "tool_calls", "trace_id"}
        assert body["citations"][0]["num"] == "L1234-1"
        assert client.get("/health").json() == {"status": "ok"}


def test_api_rejects_empty_question():
    with TestClient(create_app(FakeCopilot)) as client:
        assert client.post("/ask", json={"question": ""}).status_code == 422


def test_collapse_repeats_keeps_one_copy():
    once = "Les sources dont je dispose ne couvrent pas cette question.\n\nCeci n'est pas un avis juridique."
    assert collapse_repeats(f"{once} {once}") == once
    assert collapse_repeats("Une réponse normale.") == "Une réponse normale."


def test_strip_links_removes_mangled_legifrance_urls():
    text = "Voir l'article L1237-19 — https://www.legifrance.gouv.fr/codes/article_lc/LEGIART000035623969 — 2018."
    assert "legifrance" not in strip_links(text)
    assert "article L1237-19" in strip_links(text)


def test_finalize_appends_sources_with_tool_links_and_notice_last():
    calls = [
        ToolCall("get_article", {}, ["L1237-19"], [
            {"num": "L1237-19", "url": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000035623969",
             "source": "code", "date_debut_vigueur": "2018-01-01"},
        ]),
        ToolCall("search_code", {}, ["4.2", "4.2"], [
            {"num": "4.2", "url": "https://x/syntec", "source": "IDCC 1486", "date_debut_vigueur": ""},
            {"num": "4.2", "url": "https://x/hcr", "source": "IDCC 1979", "date_debut_vigueur": ""},
        ]),
    ]  # fmt: skip
    text = (
        "L'article L1237-19 prévoit... (https://www.legifrance.gouv.fr/codes/article_lc/LEGIART0000356)\n"
        f"Voir aussi l'article 4.2 de la convention Syntec.\n{NOTICE}"
    )
    answer, citations, unsupported = finalize(text, calls)
    assert unsupported == []
    assert [(c["num"], c["source"]) for c in citations] == [
        ("L1237-19", "code"),
        ("4.2", "IDCC 1486"),
    ]
    assert "LEGIART0000356" not in answer
    assert "LEGIARTI000035623969" in answer and "en vigueur depuis le 2018-01-01" in answer
    assert answer.endswith(NOTICE) and answer.count(NOTICE) == 1
    assert answer.index("Sources :") < answer.index(NOTICE)


def test_finalize_drops_model_sources_list_and_flags_unretrieved_nums():
    calls = [ToolCall("search_code", {}, ["L3141-4"], [
        {"num": "L3141-4", "url": "https://x/4", "source": "code", "date_debut_vigueur": "2016-08-10"},
    ])]  # fmt: skip
    text = (
        "2,5 jours par mois (article L3141-3), et l'article L3141-4 assimile 4 semaines à un mois.\n\n"
        "Sources :\n- article L3141-4 du Code du travail\n- article L3141-3"
    )
    answer, citations, unsupported = finalize(text, calls)
    assert answer.count("Sources :") == 1
    assert [c["num"] for c in citations] == ["L3141-4"]
    assert unsupported == ["L3141-3"]


def test_convention_article_cited_by_its_avenant():
    art = _cite("2", "https://www.legifrance.gouv.fr/conv_coll/id/KALIARTI1", "IDCC 1486", "2023-01-01",
                "Syntec > Textes Salaires > Avenant n° 47 du 31 mars 2022 relatif aux salaires minimaux")  # fmt: skip
    assert art["avenant"] == "47"
    calls = [ToolCall("search_code", {}, ["2"], [art])]
    answer = "Le minimum Syntec est de 2 241 € selon l'avenant n° 47 du 31 mars 2022."
    assert extract_citations(answer, calls) == [{k: v for k, v in art.items() if k != "avenant"}]
    assert extract_citations("Selon l'avenant n° 47, le minimum est de 2 241 €.", calls) == []
    assert extract_citations("Le minimum Syntec suit l'avenant n° 43.", calls) == []


def _grid_call():
    old = _cite("1er", "u-old", "IDCC 1486", "2013-07-01", "Syntec > Avenant n° 43 du 21 mai 2013")
    new = _cite("2", "u-new", "IDCC 1486", "2023-01-01", "Syntec > Avenant n° 47 du 31 mars 2022")
    texts = ["1er (IDCC 1486) : la valeur du point est portée à 2,46 €",
             "2 (IDCC 1486) : position 2.1, coefficient 105 : 2 241 € ; coefficient 115 : 2 454 €"]  # fmt: skip
    return [ToolCall("search_code", {"source": "IDCC 1486"}, ["1er", "2"], [old, new], texts)]


def test_uncited_convention_answer_falls_back_to_the_article_holding_the_figure():
    answer = f"Le minimum Syntec (IDCC 1486) en position 2.1 est de 2 241 € brut par mois.\n\n{NOTICE}"
    _, citations, _ = finalize(answer, _grid_call())
    assert [c["url"] for c in citations] == ["u-new"]
    assert "avenant" not in citations[0]


def test_figure_fallback_skips_refusals_small_numbers_and_cited_answers():
    calls = _grid_call()
    assert finalize("Les sources dont je dispose ne couvrent pas cette question. 2 241", calls)[1] == []
    assert finalize("La position 2.1 a le coefficient 105.", calls)[1] == []
    assert finalize("Voir la convention IDCC 1486.", calls)[1] == []  # the label isn't a figure
    cited = finalize("Selon l'article 1er de la convention Syntec, 2 241 €.", calls)[1]
    assert [c["url"] for c in cited] == ["u-old"]  # an explicit citation wins over the fallback
