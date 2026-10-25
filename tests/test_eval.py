import asyncio
import json
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError
from pydantic import BaseModel

from copilot.prompts import NOTICE, REFUSAL
from eval import metrics, report
from eval.cache import DiskCache, key
from eval.draft_questions import (
    CALC_SPECS,
    build_prompt,
    expected_calc,
    finalize,
    sample_specs,
)
from eval.golden import TARGET_MIX, GoldenSetError, load_golden, smoke_subset, validate_row
from eval.judge import FoundryJudge
from eval.review import parse_gold, review, to_golden
from eval.run_agent import answer_body, run, score_row, totals
from eval.run_retrieval import run_mode
from ingest.models import Article
from retrieval.search import Mode

# golden schema --------------------------------------------------------------------------------


def golden_row(**over):
    row = {
        "id": "g001",
        "question": "Quel préavis après 3 ans ?",
        "gold": [{"num": "L1234-1", "source": "code"}],
        "gold_answer": "2 mois",
        "type": "lookup",
        "answerable": True,
    }
    return row | over


def test_validate_row_accepts_each_type():
    validate_row(golden_row())
    validate_row(golden_row(type="num_in_question", question="Que dit l'article L1234-1 ?"))
    validate_row(golden_row(type="convention", gold=[{"num": "4.2", "source": "IDCC 1486"}]))
    validate_row(golden_row(type="calculator", calc={"tool": "preavis", "args": {}, "expected": 2}))
    validate_row(golden_row(type="refusal", gold=[], gold_answer="", answerable=False))


@pytest.mark.parametrize(
    "bad",
    [
        {"gold": []},  # answerable without gold
        {"type": "refusal"},  # refusal with gold
        {"gold": [{"num": "L1", "source": "IDCC 9999"}]},
        {"gold": [{"num": "L1", "source": "code"}, {"num": "L1", "source": "code"}]},
        {"type": "convention"},  # code-only gold
        {"type": "num_in_question"},  # number not in the question
        {"type": "calculator"},  # no calc
        {"gold_answer": " "},
        {"type": "nope"},
    ],
)
def test_validate_row_rejects(bad):
    with pytest.raises(GoldenSetError):
        validate_row(golden_row(**bad))


def test_load_golden_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps(golden_row()) + "\n" + json.dumps(golden_row()) + "\n")
    with pytest.raises(GoldenSetError):
        load_golden(p)


def test_smoke_subset_is_stratified_and_fixed():
    rows = []
    for t, n in TARGET_MIX.items():
        for i in range(n):
            rows.append({"id": f"{t}{i}", "type": t})
    sub = smoke_subset(rows)
    assert len(sub) == 20
    assert {r["type"] for r in sub} == set(TARGET_MIX)
    assert smoke_subset(rows) == sub


# metrics: hand-computed ---------------------------------------------------------------------

A, B, C, D = ("L1", "code"), ("L2", "code"), ("4.2", "IDCC 1486"), ("L3", "code")


def test_retrieval_metrics_hand_computed():
    ranked = [B, B, A, C, D]  # B twice: a split article counts once
    s = metrics.retrieval_scores(ranked, [A, C])
    assert s["hit@1"] == 0 and s["hit@3"] == 1 and s["hit@5"] == 1
    assert s["mrr@10"] == pytest.approx(1 / 2)  # after dedupe, A is at rank 2
    assert s["recall@5"] == 1.0
    assert metrics.recall_at_k([A, B], [A, C], 5) == 0.5
    assert metrics.mrr_at_k([B, D], [A]) == 0.0


def test_citation_accuracy():
    assert metrics.citation_accuracy([A], [A, B], True) == 1
    assert metrics.citation_accuracy([A, D], [A, B], True) == 0  # an extra article fails
    assert metrics.citation_accuracy([], [A], True) == 0
    assert metrics.citation_accuracy([], [], False) == 1
    assert metrics.citation_accuracy([A], [], False) == 0
    assert metrics.citation_hit([A, D], [A, B]) == 1


@pytest.mark.parametrize(
    "text, n",
    [
        ("Indemnité : 3 125 € brut", 3125.0),
        ("soit 333,33 €", 333.33),
        ("1 850,50 €", 1850.5),
        ("9500.00 euros", 9500.0),
        ("2 mois", 2.0),
    ],
)
def test_numbers_in_french_notation(text, n):
    assert n in metrics._numbers(text)


def test_calculator_correct():
    calc = {"tool": "indemnite_licenciement",
            "args": {"anciennete_mois": 60, "salaire_ref": 2500, "statut": "normal"},
            "expected": 3125.0}  # fmt: skip
    good = [{"name": "indemnite_licenciement",
             "args": {"anciennete_mois": 60, "salaire_ref": 2500.0, "statut": "normal"}}]  # fmt: skip
    assert metrics.calculator_correct(calc, good, "L'indemnité est de 3 125 €.") == 1
    assert metrics.calculator_correct(calc, good, "L'indemnité est de 3 000 €.") == 0
    wrong_args = [
        {"name": "indemnite_licenciement", "args": {"anciennete_mois": 48, "salaire_ref": 2500}}
    ]
    assert metrics.calculator_correct(calc, wrong_args, "3 125 €") == 0
    assert metrics.calculator_correct(calc, [], "3 125 €") == 0
    none = {"tool": "preavis", "args": {"anciennete_mois": 4, "statut": "normal"}, "expected": None}
    call = [{"name": "preavis", "args": {"anciennete_mois": 4, "statut": "normal"}}]
    assert metrics.calculator_correct(none, call, "Il est fixé par la convention collective.") == 1


def test_percentile_and_bootstrap():
    assert metrics.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert metrics.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    lo, hi = metrics.bootstrap_ci([1.0] * 8 + [0.0] * 2)
    assert 0.4 <= lo <= 0.8 <= hi <= 1.0
    assert metrics.bootstrap_ci([1.0, 0.0, 1.0]) == metrics.bootstrap_ci([1.0, 0.0, 1.0])


def test_cost():
    price = {"input_per_1m": 0.75, "output_per_1m": 4.5}
    assert metrics.cost_usd(1_000_000, 100_000, price) == pytest.approx(1.2)


# cache and judge ------------------------------------------------------------------------------


def test_cache_round_trip(tmp_path):
    c = DiskCache(tmp_path / "c.sqlite")
    k = key("agent", "q", {"chat": "m"})
    assert c.get("agent", k) is None
    c.put("agent", k, {"answer": "é"})
    assert c.get("agent", k) == {"answer": "é"}
    assert (c.hits, c.misses) == (1, 1)
    assert key("agent", "q", {"chat": "m"}) != key("agent", "q", {"chat": "other"})


class Verdict(BaseModel):
    verdict: str


class FakeChat:
    def __init__(self, content):
        self.content, self.calls = content, []
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
        )


def test_judge_parses_schema_and_caches(tmp_path):
    chat = FakeChat('{"verdict": "yes"}')
    judge = FoundryJudge(
        SimpleNamespace(chat=chat), "gpt-4.1-mini", DiskCache(tmp_path / "c.sqlite")
    )
    out = judge.generate("prompt", schema=Verdict)
    assert out == Verdict(verdict="yes")
    assert judge.generate("prompt", schema=Verdict) == out
    assert len(chat.calls) == 1  # second call came from the cache
    assert chat.calls[0]["temperature"] == 0
    assert chat.calls[0]["response_format"] == {"type": "json_object"}
    assert asyncio.run(judge.a_generate("prompt", schema=Verdict)) == out
    assert judge.usage["calls"] == 1


# drafting -------------------------------------------------------------------------------------


def article(num, source="code", path="Partie législative > Livre II > Titre III", n=400):
    return Article(f"ID{num}{source}", num, source, path, f"Texte de l'article {num}. " * (n // 25),
                   "2008-05-01", f"https://www.legifrance.gouv.fr/x/{num}")  # fmt: skip


def corpus_articles():
    arts = [
        article(f"L{i}", path=f"Partie législative > Livre {i % 7} > Titre")
        for i in range(1000, 1200)
    ]
    arts += [article(f"{i}.1", "IDCC 1486", "Syntec > Texte de base") for i in range(20)]
    arts += [article(f"{i}", "IDCC 1979", "HCR > Texte de base") for i in range(20)]
    return arts


def test_sample_specs_meets_oversampled_targets_deterministically():
    specs = sample_specs(corpus_articles())
    counts = {t: sum(1 for s in specs if s["type"] == t) for t in TARGET_MIX}
    assert counts == {t: round(n * 1.3) for t, n in TARGET_MIX.items()}
    assert [s["cid"] for s in specs] == [f"c{i:03d}" for i in range(1, len(specs) + 1)]
    assert sample_specs(corpus_articles()) == specs


def test_calc_specs_cover_edge_cases():
    months = {a["anciennete_mois"] for t, a in CALC_SPECS}
    assert {6, 8, 24, 126}.issubset(months)
    assert expected_calc("indemnite_licenciement",
                         {"anciennete_mois": 60, "salaire_ref": 2500, "statut": "normal"})["expected"] == 3125.0  # fmt: skip


def test_prompts_mention_the_constraints():
    a = article("L1234-1").__dict__
    assert "« article L1234-1 »" in build_prompt({"type": "num_in_question", "article": a})
    assert "ne relève PAS" in build_prompt({"type": "refusal", "topic": "la TVA"})
    calc = expected_calc("preavis", {"anciennete_mois": 30, "statut": "normal"})
    assert "30 mois" in build_prompt({"type": "calculator", "calc": calc})


def test_finalize_flags_missing_quotes_and_sets_gold():
    a = article("L1234-1").__dict__
    spec = {"cid": "c001", "type": "lookup", "article": a}
    ok = finalize(spec, {"question": "Q ?", "evidence_quote": "Texte de l’article L1234-1.",
                         "gold_answer": "R"}, None)  # fmt: skip
    bad = finalize(spec, {"question": "Q ?", "evidence_quote": "inventé", "gold_answer": "R"}, None)
    assert ok["quote_found"] is True and bad["quote_found"] is False
    assert ok["gold"] == [{"num": "L1234-1", "source": "code"}]
    calc = finalize({"cid": "c2", "type": "calculator",
                     "calc": expected_calc("indemnite_licenciement",
                                           {"anciennete_mois": 60, "salaire_ref": 2500, "statut": "normal"})},
                    {"question": "Q ?", "evidence_quote": "", "gold_answer": ""}, None)  # fmt: skip
    assert [g["num"] for g in calc["gold"]] == ["L1234-9", "R1234-1", "R1234-2"]
    assert calc["gold_answer"] == "3125.00 €"


# review ---------------------------------------------------------------------------------------


def candidates():
    return [
        {"cid": "c001", "type": "lookup", "question": "Q1 ?", "gold": [{"num": "L1", "source": "code"}],
         "gold_answer": "A1", "source_text": "t", "evidence_quote": "t", "quote_found": True},
        {"cid": "c002", "type": "refusal", "question": "Q2 ?", "gold": [], "gold_answer": ""},
        {"cid": "c003", "type": "lookup", "question": "Q3 ?", "gold": [{"num": "L3", "source": "code"}],
         "gold_answer": "A3"},
    ]  # fmt: skip


def test_review_keeps_edits_drops_and_resumes(tmp_path, capsys):
    log, golden = tmp_path / "log.jsonl", tmp_path / "golden.jsonl"
    answers = iter(["k", "d", "", "e", "Q3 reformulée ?", "", "L3, L4"])
    seen_retrieval = []
    review(candidates(), ask=lambda _: next(answers),
           retrieve=lambda q: seen_retrieval.append(q) or [], log_path=log, golden_path=golden)  # fmt: skip
    rows = load_golden(golden)
    assert [r["id"] for r in rows] == ["g001", "g002"]
    assert rows[1]["question"] == "Q3 reformulée ?"
    assert rows[1]["gold"] == [{"num": "L3", "source": "code"}, {"num": "L4", "source": "code"}]
    assert seen_retrieval == ["Q2 ?"]  # only the refusal row shows system output
    # nothing left to review on the next run
    assert review(candidates(), ask=lambda _: "q", log_path=log, golden_path=golden)["lookup"] == 2


def test_to_golden_and_parse_gold():
    row = to_golden(candidates()[1], 5)
    assert row["id"] == "g005" and row["answerable"] is False
    assert parse_gold("4.2@IDCC 1486", []) == [{"num": "4.2", "source": "IDCC 1486"}]
    assert parse_gold("  ", [{"num": "L1", "source": "code"}]) == [{"num": "L1", "source": "code"}]


# runners ------------------------------------------------------------------------------------


class FakeSearch:
    def __init__(self, fail_semantic=False):
        self.fail_semantic = fail_semantic

    def search(self, **args):
        if self.fail_semantic and args.get("query_type") == "semantic":
            raise HttpResponseError(message="Semantic search is not enabled")
        doc = {"id": "x", "num": "L1", "source": "code", "path": "p", "content": "c",
               "legifrance_url": "u", "date_debut_vigueur": "", "@search.score": 1.0}  # fmt: skip
        return [doc, doc | {"num": "L2"}]


def test_run_mode_scores_and_marks_unavailable_semantic():
    rows = [golden_row(id="g1", gold=[{"num": "L2", "source": "code"}])]
    out = run_mode(FakeSearch(), rows, Mode.KEYWORD, {})
    assert out["available"] and out["totals"]["hit@1"] == 0 and out["totals"]["hit@3"] == 1
    out = run_mode(FakeSearch(fail_semantic=True), rows, Mode.HYBRID_SEMANTIC, {"g1": [0.1]})
    assert out == {"mode": "hybrid_semantic", "available": False, "error": "HttpResponseError"}


class StubCopilot:
    def __init__(self):
        self.calls = 0

    async def ask(self, question):
        self.calls += 1
        refusal = "plus-value" in question
        answer = (
            f"{REFUSAL}\n\n{NOTICE}"
            if refusal
            else f"2 mois (article L1234-1).\n\nSources :\n- x\n\n{NOTICE}"
        )
        cites = (
            []
            if refusal
            else [{"num": "L1234-1", "source": "code", "url": "u", "date_debut_vigueur": ""}]
        )
        return SimpleNamespace(to_dict=lambda: {
            "question": question, "answer": answer, "citations": cites, "contexts": ["ctx"],
            "tool_calls": [{"name": "preavis", "args": {"anciennete_mois": 36, "statut": "normal"}, "nums": ["L1234-1"]}],
            "trace_id": "0" * 32, "unsupported_nums": [], "latency_s": 1.5,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        })  # fmt: skip


def test_run_agent_scores_caches_and_totals(tmp_path):
    rows = [
        golden_row(id="g1", type="calculator", calc={"tool": "preavis", "expected": 2,
                   "args": {"anciennete_mois": 36, "statut": "normal"}}),
        golden_row(id="g2", type="refusal", question="La plus-value ?", gold=[], gold_answer="", answerable=False),
    ]  # fmt: skip
    cache, stub = DiskCache(tmp_path / "c.sqlite"), StubCopilot()
    out = asyncio.run(run(stub, rows, cache, {"chat": "m"}, judge=None, log=lambda m: None))
    again = asyncio.run(run(stub, rows, cache, {"chat": "m"}, judge=None, log=lambda m: None))
    assert stub.calls == 2 and all(r["cached"] for r in again)
    assert (
        out[0]["scores"]["calculator_correct"] == 1 and out[0]["scores"]["citation_accuracy"] == 1
    )
    assert out[1]["scores"]["refused"] and out[1]["scores"]["citation_accuracy"] == 1
    t = totals(out, {"input_per_1m": 0.75, "output_per_1m": 4.5})
    assert t["refusal_accuracy"] == 1 and t["false_refusal_rate"] == 0
    assert t["usd_per_1000_queries"] == pytest.approx(1.2)


def test_answer_body_strips_sources_and_notice():
    assert answer_body(f"Réponse.\n\nSources :\n- a\n\n{NOTICE}") == "Réponse."


def test_score_row_skips_judge_on_refusals():
    class Boom:
        pass

    row = golden_row(type="refusal", gold=[], gold_answer="", answerable=False)
    res = {"answer": REFUSAL, "citations": [], "tool_calls": [], "contexts": []}
    assert "faithfulness" not in score_row(row, res, judge=Boom())


# report ---------------------------------------------------------------------------------------


def test_report_writes_between_markers_idempotently(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    mode = {"mode": "keyword", "available": True, "totals": {"hit@1": 0.5, "hit@3": 0.75, "hit@5": 0.8,
            "hit@5_ci": [0.7, 0.9], "mrr@10": 0.61, "recall@5": 0.8, "p50_ms": 40, "p95_ms": 90}}  # fmt: skip
    (results / "retrieval_free.json").write_text(json.dumps({
        "label": "free", "n_questions": 85,
        "modes": [mode, {"mode": "hybrid_semantic", "available": False, "error": "FeatureNotSupportedInService"}],
    }))  # fmt: skip
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n\nIntro.\n")
    report.write(readme, results)
    first = readme.read_text()
    report.write(readme, results)
    assert readme.read_text() == first
    assert "| BM25 (keyword) | 50% | 75% | 80% (70–90) | 0.61 | 80% | 90 ms |" in first
    assert "not available on this tier" in first
    assert first.count(report.START) == 1
