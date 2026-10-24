import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx2
import numpy as np
import openai
import pytest

from ingest.models import Article
from retrieval.embed import embed_batch, embed_corpus, embedding_input, load_cache
from retrieval.index import VECTOR_FIELD, doc_key, index_definition, to_document
from retrieval.search import Hit, Mode, build_query_args, needs_vector, source_filter

RECORDED = Path(__file__).parent / "fixtures" / "search_recorded.json"


def article(id="LEGIARTI000006901112", num="L1234-1", text="Le préavis est d'un mois."):
    return Article(
        id=id,
        num=num,
        source="code",
        path="Partie législative > Livre II : Le contrat de travail",
        text=text,
        date_debut_vigueur="2008-05-01",
        legifrance_url=f"https://www.legifrance.gouv.fr/codes/article_lc/{id.split('#')[0]}",
    )


# index definition ---------------------------------------------------------------------------


def fields_by_name(index):
    return {f.name: f for f in index.fields}


def test_index_fields_and_semantic_config():
    index = index_definition("articles-v1", dims=512)
    fields = fields_by_name(index)
    assert list(fields) == [
        "id", "num", "num_search", "source", "path", "content", VECTOR_FIELD,
        "date_debut_vigueur", "legifrance_url",
    ]  # fmt: skip
    assert fields["id"].key
    assert fields["source"].filterable and fields["num"].filterable
    assert fields["content"].analyzer_name == "fr.microsoft"
    assert fields["path"].analyzer_name == "fr.microsoft"
    assert fields["num_search"].analyzer_name == "num_analyzer"
    (analyzer,) = index.analyzers
    (tokenizer,) = index.tokenizers
    assert analyzer.tokenizer_name == tokenizer.name
    assert re.split(tokenizer.pattern, "l'article L1237-19 ?") == ["l", "article", "L1237-19", ""]
    vector = fields[VECTOR_FIELD]
    assert vector.vector_search_dimensions == 512
    assert vector.stored is False
    config = index.semantic_search.configurations[0]
    assert config.prioritized_fields.title_field.field_name == "path"
    assert [f.field_name for f in config.prioritized_fields.content_fields] == ["content"]
    assert not index.vector_search.compressions
    assert index.default_scoring_profile == "num-boost"
    assert index.scoring_profiles[0].text_weights.weights == {"num_search": 5.0}


def test_index_int8_discards_originals():
    index = index_definition("articles-v1", dims=512, compression="int8")
    (compression,) = index.vector_search.compressions
    assert compression.rescoring_options.rescore_storage_method == "discardOriginals"
    assert index.vector_search.profiles[0].compression_name == compression.compression_name


def test_index_rejects_unknown_compression():
    with pytest.raises(ValueError):
        index_definition("x", dims=512, compression="binary")


def test_doc_key_sanitises_piece_ids():
    assert doc_key("KALIARTI000005826370#2") == "KALIARTI000005826370_2"
    assert doc_key("LEGIARTI000006901112") == "LEGIARTI000006901112"


def test_to_document():
    doc = to_document(article(id="KALIARTI1#3"), np.array([0.5, -0.25], dtype=np.float32))
    assert doc["id"] == "KALIARTI1_3"
    assert doc["num_search"] == "L1234-1"
    kali = to_document(replace(article(), source="IDCC 1486", num="2"), [0.0])
    assert kali["num_search"] == ""
    assert doc["content"] == "Le préavis est d'un mois."
    assert doc[VECTOR_FIELD] == [0.5, -0.25]
    assert all(type(x) is float for x in doc[VECTOR_FIELD])


# query construction ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, has_text, has_vector, semantic",
    [
        (Mode.KEYWORD, True, False, False),
        (Mode.VECTOR, False, True, False),
        (Mode.HYBRID, True, True, False),
        (Mode.HYBRID_SEMANTIC, True, True, True),
    ],
)
def test_build_query_args(mode, has_text, has_vector, semantic):
    vector = [0.1] * 4 if needs_vector(mode) else None
    args = build_query_args("préavis", mode, vector, top=5)
    assert ("search_text" in args) == has_text
    assert ("vector_queries" in args) == has_vector
    assert (args.get("query_type") == "semantic") == semantic
    assert args["top"] == 5 and args["filter"] is None
    if has_vector:
        (vq,) = args["vector_queries"]
        assert vq.fields == VECTOR_FIELD and vq.k_nearest_neighbors == 5
    if semantic:
        assert args["semantic_configuration_name"] == "default"


def test_build_query_args_source_filter():
    args = build_query_args("préavis", Mode.KEYWORD, source="IDCC 1486")
    assert args["filter"] == "source eq 'IDCC 1486'"


def test_source_filter_escapes_quotes():
    assert source_filter("l'IDCC") == "source eq 'l''IDCC'"


def test_vector_modes_need_a_vector():
    with pytest.raises(ValueError):
        build_query_args("préavis", Mode.HYBRID)


# embedding ---------------------------------------------------------------------------


def test_embedding_input_adds_path_for_whole_articles_only():
    assert embedding_input(article()).startswith("L1234-1 : Partie législative > Livre II")
    piece = article(id="KALIARTI1#2", num="15", text="HCR > Avenant\nTexte")
    assert embedding_input(piece) == "15 : HCR > Avenant\nTexte"


class FakeEmbeddings:
    """Mimics client.embeddings.create; raises the queued errors first."""

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    def create(self, model, input, dimensions):
        self.calls.append(list(input))
        if self.errors:
            raise self.errors.pop(0)
        data = [
            SimpleNamespace(index=i, embedding=[float(len(t)), 1.0]) for i, t in enumerate(input)
        ]
        return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=10 * len(input)))


def rate_limit_error(retry_after="1.5"):
    request = httpx2.Request("POST", "https://example.openai.azure.com/openai/v1/embeddings")
    response = httpx2.Response(429, headers={"retry-after": retry_after}, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def test_embed_batch_retries_on_429():
    fake = FakeEmbeddings(errors=[rate_limit_error(), rate_limit_error("3")])
    sleeps = []
    vectors, tokens = embed_batch(
        SimpleNamespace(embeddings=fake), ["ab", "c"], "dep", 2, sleep=sleeps.append
    )
    assert sleeps == [1.5, 3.0]
    assert vectors == [[2.0, 1.0], [1.0, 1.0]]
    assert tokens == 20
    assert len(fake.calls) == 3


def test_embed_corpus_resumes_from_cache(tmp_path):
    cache = tmp_path / "cache.npz"
    fake = FakeEmbeddings()
    client = SimpleNamespace(embeddings=fake)
    first = [article(id=f"A{i}", text=f"texte {i}") for i in range(5)]

    vectors, tokens = embed_corpus(first, cache, client, "dep", 2, batch_size=2, log=lambda m: m)
    assert len(vectors) == 5 and tokens == 50
    assert set(load_cache(cache)) == {f"A{i}" for i in range(5)}

    fake.calls.clear()
    changed = first[:4] + [article(id="A4", text="texte modifié"), article(id="A5", text="neuf")]
    vectors, tokens = embed_corpus(changed, cache, client, "dep", 2, batch_size=2, log=lambda m: m)
    assert tokens == 20  # only A4 (changed) and A5 (new)
    assert [len(c) for c in fake.calls] == [2]
    assert set(vectors) == {f"A{i}" for i in range(6)}


# recorded responses ---------------------------------------------------------------------------


@pytest.mark.skipif(not RECORDED.exists(), reason="run `python -m retrieval record` first")
def test_recorded_responses_parse_into_hits():
    recordings = json.loads(RECORDED.read_text(encoding="utf-8"))["recordings"]
    assert {r["mode"] for r in recordings} == {m.value for m in Mode}
    for rec in recordings:
        if "error" in rec:  # only the semantic mode may be refused (free tier)
            assert rec["mode"] == Mode.HYBRID_SEMANTIC
            assert rec["error"] in {"FeatureNotSupportedInService", "SemanticQueriesNotAvailable"}
            continue
        hits = [Hit.from_result(r) for r in rec["results"]]
        assert hits, f"no results for {rec['mode']} / {rec['query']}"
        for hit in hits:
            assert hit.num and hit.text and hit.url.startswith("https://www.legifrance.gouv.fr/")
        if rec["mode"] == Mode.HYBRID_SEMANTIC and any(
            r.get("@search.reranker_score") is not None for r in rec["results"]
        ):
            assert all(h.reranker_score is not None for h in hits)


@pytest.mark.skipif(not RECORDED.exists(), reason="run `python -m retrieval record` first")
def test_recorded_keyword_finds_article_by_number():
    recordings = json.loads(RECORDED.read_text(encoding="utf-8"))["recordings"]
    rec = next(r for r in recordings if r["query"] == "L1237-19" and r["mode"] == "keyword")
    assert rec["results"][0]["num"] == "L1237-19"


def test_wait_for_stats_waits_for_text_and_steady_readings():
    from retrieval.index import wait_for_stats

    mb = 1_048_576
    readings = iter(
        [
            {"document_count": 90, "storage_size": 50 * mb, "vector_index_size": 40 * mb},
            {"document_count": 100, "storage_size": 40 * mb, "vector_index_size": 40 * mb},
            {"document_count": 100, "storage_size": 55 * mb, "vector_index_size": 40 * mb},
            {"document_count": 100, "storage_size": 58 * mb, "vector_index_size": 40 * mb},
            {"document_count": 100, "storage_size": 58 * mb, "vector_index_size": 40 * mb},
            {"document_count": 100, "storage_size": 58 * mb, "vector_index_size": 40 * mb},
        ]
    )

    class FakeIndexClient:
        def get_index_statistics(self, name):
            return SimpleNamespace(**next(readings))

    sleeps = []
    stats = wait_for_stats(FakeIndexClient(), "x", 100, sleep=sleeps.append)
    assert stats["storage_size"] == 58 * mb
    assert len(sleeps) == 5
