import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Annotated

from agent_framework import tool
from opentelemetry import trace
from pydantic import Field

from copilot import calculators
from copilot.piste import PisteClient, PisteError
from ingest.models import Article

MAX_TEXT = 1500  # characters of article text per search hit sent to the model
MAX_ARTICLE = 4000  # characters per get_article result; 99% of articles are shorter
SOURCES = ("code", "IDCC 1486", "IDCC 1979")


@dataclass
class ToolCall:
    name: str
    args: dict
    nums: list[str] = field(default_factory=list)
    articles: list[dict] = field(default_factory=list)  # {num, url, source} for citations
    texts: list[str] = field(default_factory=list)  # what the tool showed the model (eval context)


AVENANT = re.compile(r"\bavenant\s*(?:n\s*[°o]\.?\s*)?(\d+)\b", re.IGNORECASE)


def _cite(num: str, url: str, source: str, date: str = "", path: str = "") -> dict:
    cite = {"num": num, "url": url, "source": source, "date_debut_vigueur": date}
    # Convention salary texts number their articles 1er, 2, ...; answers name the avenant instead.
    m = AVENANT.search(path.split(" > ")[-1]) if source != "code" else None
    if m:
        cite["avenant"] = m.group(1)
    return cite


class Corpus:
    """data/articles.jsonl indexed by article number, with split pieces merged back."""

    def __init__(self, articles: list[Article]):
        pieces: dict[str, list[Article]] = defaultdict(list)
        for a in articles:
            pieces[a.id.split("#")[0]].append(a)
        self.by_num: dict[str, list[Article]] = defaultdict(list)
        for base_id, parts in pieces.items():
            first = parts[0]
            text = "\n".join(p.text.split("\n", 1)[1] if "#" in p.id else p.text for p in parts)
            self.by_num[first.num.upper()].append(
                Article(base_id, first.num, first.source, first.path, text,
                        first.date_debut_vigueur, first.legifrance_url)
            )  # fmt: skip

    def lookup(self, num: str, source: str | None = None) -> list[Article]:
        found = self.by_num.get(num.strip().upper(), [])
        return [a for a in found if source is None or a.source == source]


@dataclass
class ToolContext:
    search: Callable  # (query, source) -> list[retrieval.search.Hit]
    corpus: Corpus
    piste: PisteClient | None = None


def make_tools(ctx: ToolContext, calls: list[ToolCall]) -> list:
    """Tools bound to one request; every call is appended to `calls`."""

    def _record(name: str, args: dict, articles: list[dict], texts: list[str] = ()) -> None:
        nums = [a["num"] for a in articles]
        calls.append(ToolCall(name, args, nums, articles, list(texts)))
        # Runs inside the framework's execute_tool span when instrumentation is on.
        trace.get_current_span().set_attribute("copilot.tool.nums", nums)

    def _cite_code(num: str) -> dict:
        found = ctx.corpus.lookup(num, "code")
        if not found:
            return _cite(num, "", "code")
        return _cite(num, found[0].legifrance_url, "code", found[0].date_debut_vigueur)

    def _calc_text(res) -> str:
        return f"Calcul : {res.valeur} {res.unite}. {res.explication}"

    def _calc_output(res) -> str:
        # Give the model the link and date of every article the calculator applied.
        out = asdict(res)
        out["articles"] = []
        for n in res.articles:
            found = ctx.corpus.lookup(n, "code")
            out["articles"].append({
                "num": n,
                "url": found[0].legifrance_url if found else "",
                "date_debut_vigueur": found[0].date_debut_vigueur if found else "",
            })  # fmt: skip
        return json.dumps(out, ensure_ascii=False)

    @tool(
        name="search_code",
        description=(
            "Recherche hybride (mots-clés + sémantique) dans le Code du travail et les "
            "conventions collectives Syntec (IDCC 1486) et HCR (IDCC 1979). Renvoie les 5 "
            "articles les plus pertinents avec numéro, chemin, texte et lien Légifrance."
        ),
    )
    def search_code(
        query: Annotated[str, Field(description="La question ou les mots-clés, en français.")],
        source: Annotated[
            str | None,
            Field(
                description="Filtre optionnel : 'code', 'IDCC 1486' (Syntec) ou 'IDCC 1979' (HCR)."
            ),
        ] = None,
    ) -> str:
        if source is not None and source not in SOURCES:
            source = None
        hits = ctx.search(query, source)
        results = [
            {
                "num": h.num,
                "source": h.source,
                "path": h.path,
                "texte": h.text[:MAX_TEXT],
                "date_debut_vigueur": h.date_debut_vigueur,
                "url": h.url,
            }
            for h in hits
        ]
        _record("search_code", {"query": query, "source": source},
                [_cite(h.num, h.url, h.source, h.date_debut_vigueur, h.path) for h in hits],
                [f"{r['num']} ({r['source']}) : {r['texte']}" for r in results])  # fmt: skip
        return json.dumps({"resultats": results}, ensure_ascii=False)

    @tool(
        name="get_article",
        description=(
            "Récupère le texte en vigueur d'un article par son numéro (ex. L1234-1, R1234-2), "
            "depuis Légifrance si disponible, sinon depuis la copie locale."
        ),
    )
    def get_article(
        num: Annotated[str, Field(description="Numéro de l'article, par ex. 'L1234-9'.")],
        source: Annotated[
            str | None,
            Field(
                description="'code' par défaut ; 'IDCC 1486' ou 'IDCC 1979' pour une convention."
            ),
        ] = None,
    ) -> str:
        matches = ctx.corpus.lookup(num, source or ("code" if num[:1].isalpha() else None))
        if not matches:
            _record("get_article", {"num": num, "source": source}, [])
            return json.dumps({"erreur": f"Article {num} introuvable dans les sources."},
                              ensure_ascii=False)  # fmt: skip
        out = []
        for a in matches[:3]:
            entry = {"num": a.num, "source": a.source, "path": a.path, "url": a.legifrance_url,
                     "texte": a.text[:MAX_ARTICLE], "date_debut_vigueur": a.date_debut_vigueur,
                     "origine": "copie locale"}  # fmt: skip
            if ctx.piste is not None:
                try:
                    live = ctx.piste.get_article(a.id)
                    entry.update(texte=live.text[:MAX_ARTICLE], origine="Légifrance (API PISTE)")
                    if live.date_debut_vigueur:
                        entry["date_debut_vigueur"] = live.date_debut_vigueur
                except (PisteError, OSError) as err:
                    entry["origine"] = f"copie locale (Légifrance indisponible : {err})"
            out.append(entry)
        _record("get_article", {"num": num, "source": source},
                [_cite(e["num"], e["url"], e["source"], e["date_debut_vigueur"]) for e in out],
                [f"{e['num']} ({e['source']}) : {e['texte']}" for e in out])  # fmt: skip
        return json.dumps({"articles": out}, ensure_ascii=False)

    @tool(
        name="preavis",
        description=(
            "Calcule la durée minimale légale du préavis de licenciement (article L1234-1) "
            "selon l'ancienneté en mois et le motif."
        ),
    )
    def preavis(
        anciennete_mois: Annotated[int, Field(description="Ancienneté en mois complets.", ge=0)],
        statut: Annotated[
            str, Field(description="'normal', 'faute_grave' ou 'faute_lourde'.")
        ] = "normal",
    ) -> str:
        res = calculators.preavis(anciennete_mois, statut)
        _record("preavis", {"anciennete_mois": anciennete_mois, "statut": statut},
                [_cite_code(n) for n in res.articles], [_calc_text(res)])  # fmt: skip
        return _calc_output(res)

    @tool(
        name="indemnite_licenciement",
        description=(
            "Calcule l'indemnité légale de licenciement (L1234-9, R1234-1, R1234-2) à partir de "
            "l'ancienneté en mois et du salaire de référence mensuel brut en euros."
        ),
    )
    def indemnite_licenciement(
        anciennete_mois: Annotated[int, Field(description="Ancienneté en mois complets.", ge=0)],
        salaire_ref: Annotated[
            float, Field(description="Salaire de référence mensuel brut (€).", ge=0)
        ],
        statut: Annotated[
            str, Field(description="'normal', 'faute_grave' ou 'faute_lourde'.")
        ] = "normal",
    ) -> str:
        res = calculators.indemnite_licenciement(anciennete_mois, salaire_ref, statut)
        _record("indemnite_licenciement",
                {"anciennete_mois": anciennete_mois, "salaire_ref": salaire_ref, "statut": statut},
                [_cite_code(n) for n in res.articles], [_calc_text(res)])  # fmt: skip
        return _calc_output(res)

    return [search_code, get_article, preavis, indemnite_licenciement]
