import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ingest.chunk import chunk, chunk_all
from ingest.estimate import FREE_TIER_BYTES, estimate, scenarios, text_bytes
from ingest.html import html_to_text
from ingest.kali import load_convention
from ingest.legi import load_code_du_travail, normalise_path
from ingest.models import Article

FIXTURES = Path(__file__).parent / "fixtures"

L1234_1_SUBTITLES = (
    "Partie législative   - Première partie : Les relations individuelles de travail - "
    "Livre II : Le contrat de travail - Titre III : Rupture du contrat de travail à durée "
    "indéterminée - Chapitre IV : Conséquences du licenciement - Section 1 : Préavis et "
    "indemnité de licenciement - Sous-section 1 : Préavis et indemnité compensatrice de préavis."
)


def legi_row(**overrides):
    row = {
        "doc_id": "LEGIARTI000006901112",
        "chunk_index": 1,
        "number": "L1234-1",
        "status": "VIGUEUR",
        "title": "Code du travail",
        "subtitles": L1234_1_SUBTITLES,
        "start_date": "2008-05-01",
        "text": "Lorsque le licenciement n'est pas motivé par une faute grave...",
        "embeddings_bge-m3": "[0.1, 0.2]",
    }
    return row | overrides


@pytest.fixture
def legi_parquet(tmp_path):
    rows = [
        legi_row(),
        legi_row(doc_id="LEGIARTI000035644692", number="R1234-2", start_date="2017-09-27"),
        legi_row(doc_id="LEGIARTI000000000003", number="L1234-1", status="MODIFIE"),
        legi_row(doc_id="LEGIARTI000000000004", number="L9999-1", status="VIGUEUR_DIFF"),
        legi_row(doc_id="LEGIARTI000000000005", title="Code du travail applicable à Mayotte"),
        legi_row(chunk_index=2, text="second piece of the same article"),
    ]
    path = tmp_path / "legi.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_legi_keeps_only_in_force_code_du_travail(legi_parquet):
    articles = load_code_du_travail(legi_parquet)
    assert [a.num for a in articles] == ["L1234-1", "R1234-2"]
    first = articles[0]
    assert first.source == "code"
    assert first.date_debut_vigueur == "2008-05-01"
    assert first.legifrance_url == (
        "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006901112"
    )


def test_normalise_path_splits_levels():
    path = normalise_path(L1234_1_SUBTITLES)
    parts = path.split(" > ")
    assert parts[0] == "Partie législative"
    assert parts[1] == "Première partie : Les relations individuelles de travail"
    assert parts[-1] == "Sous-section 1 : Préavis et indemnité compensatrice de préavis"
    assert len(parts) == 7


def test_normalise_path_keeps_dash_inside_titles():
    path = normalise_path("Partie législative - Livre III : Durée du travail - repos - Titre I : X")
    assert path == "Partie législative > Livre III : Durée du travail - repos > Titre I : X"


def test_normalise_path_empty():
    assert normalise_path(None) == ""


def test_kali_filters_and_inherits_dates():
    articles = load_convention(FIXTURES / "kali_small.json", "1486")
    assert [a.id for a in articles] == ["KALIARTI000000000101", "KALIARTI000000000201"]

    preavis, salaires = articles
    assert preavis.num == "15"
    assert preavis.source == "IDCC 1486"
    assert preavis.date_debut_vigueur == "2023-07-01"
    assert preavis.path == (
        "Bureaux d'études techniques > Texte de base : Convention collective nationale du "
        "15 décembre 1987 > Titre II Préavis"
    )
    assert preavis.legifrance_url == (
        "https://www.legifrance.gouv.fr/conv_coll/article/KALIARTI000000000101"
    )

    assert salaires.num == "art. 30"  # no num in the source, falls back to intOrdre
    assert salaires.date_debut_vigueur == "2025-01-01"
    assert "230 | 1 900 €" in salaires.text


def test_html_to_text_paragraphs_and_tables():
    html = "<p>Premier&nbsp;alinéa.</p><p><br/>Second   alinéa.</p><table><tr><td>A</td><td> B </td></tr></table>"
    assert html_to_text(html) == "Premier alinéa.\nSecond alinéa.\nA | B"


def test_html_to_text_empty():
    assert html_to_text("") == ""


def make_article(text: str) -> Article:
    return Article(
        id="KALIARTI000000000999",
        num="7",
        source="IDCC 1979",
        path="HCR > Avenant n° 2",
        text=text,
        date_debut_vigueur="2024-01-01",
        legifrance_url="https://www.legifrance.gouv.fr/conv_coll/article/KALIARTI000000000999",
    )


def test_short_article_is_one_chunk():
    article = make_article("Un seul alinéa.")
    assert chunk(article) == [article]


def test_long_article_splits_on_paragraphs_with_path():
    paragraphs = [f"Alinéa {i} " + "x" * 900 for i in range(10)]
    pieces = chunk(make_article("\n".join(paragraphs)), max_chars=6000, target=4000)

    assert [p.id for p in pieces] == [
        f"KALIARTI000000000999#{i}" for i in range(1, len(pieces) + 1)
    ]
    assert len(pieces) == 3
    for piece in pieces:
        assert piece.text.startswith("HCR > Avenant n° 2\n")
        assert len(piece.text) <= 4000 + len(piece.path) + 1
        assert piece.num == "7" and piece.legifrance_url.endswith("KALIARTI000000000999")
    body = "\n".join(p.text.split("\n", 1)[1] for p in pieces)
    assert body == "\n".join(paragraphs)


def test_single_huge_paragraph_is_cut_on_spaces():
    pieces = chunk(make_article("mot " * 3000), max_chars=6000, target=4000)
    assert len(pieces) == 3
    assert all(p.text.split("\n", 1)[1].strip() for p in pieces)


def test_chunk_all_flattens():
    short, long = make_article("court"), make_article("y " * 4000)
    assert len(chunk_all([short, long])) == 1 + len(chunk(long))


def test_estimate_arithmetic():
    articles = [make_article("abc"), make_article("é")]
    e = estimate(articles, dims=512, vector_type="float32")
    assert e.vector_bytes == 2 * 512 * 4
    assert e.text_bytes == text_bytes(articles)
    assert e.total_bytes == e.vector_bytes + e.text_bytes
    assert e.fits
    assert estimate(articles, dims=512, vector_type="int8").vector_bytes == 2 * 512


def test_estimate_flags_overflow():
    many = [make_article("x")] * 10_000
    assert not estimate(many, dims=1536, vector_type="float32").fits  # 61 MB of vectors
    assert FREE_TIER_BYTES == 52_428_800


def test_scenarios_cover_scopes():
    code_l = Article("LEGIARTI1", "L1-1", "code", "p", "t", "2020-01-01", "u")
    code_r = Article("LEGIARTI2", "R1-1", "code", "p", "t", "2020-01-01", "u")
    rows = scenarios([code_l, code_r, make_article("k")])
    assert len(rows) == 8
    assert {r.scope: r.chunks for r in rows} == {"all": 3, "code L only": 1}


def test_jsonl_round_trip():
    article = make_article("Texte accentué : préavis")
    line = article.to_json()
    assert "accentué" in line  # not escaped
    assert json.loads(line)["num"] == "7"
    assert Article.from_json(line) == article
