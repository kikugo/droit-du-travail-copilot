"""Draft golden-set candidates with gpt-5.4-mini; an LLM reviewer then keeps, edits or drops each one.

    uv run python -m eval.draft_questions --dry-run   # counts and token estimate, no calls
    uv run python -m eval.draft_questions             # costs tokens; cached, so reruns are free

Sampling is seeded and stratified. Articles come only from the free index's scope (Code L
articles, Syntec/HCR base and salary texts) so the same set scores both indexes. Calculator
rows get their numbers from code and their expected value from copilot.calculators; the model
only words the question. Each answerable draft must quote its evidence verbatim; a quote that
isn't in the article is flagged for the reviewer.
"""

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from copilot import calculators
from copilot.tools import Corpus
from eval.cache import DiskCache, key
from eval.golden import EVAL_DIR, TARGET_MIX
from ingest.models import Article
from retrieval.index import SCOPES

CANDIDATES_PATH = EVAL_DIR / "candidates.jsonl"
ARTICLES = Path("data/articles.jsonl")
OVERSAMPLE = 1.3
SEED = 7
DRAFTER_MODEL = "gpt-5.4-mini"
EST_TOKENS_PER_DRAFT = 1100

REFUSAL_TOPICS = [
    "l'imposition de la plus-value lors de la vente d'une résidence secondaire",
    "la peine encourue pour un vol simple",
    "le statut des fonctionnaires de l'État et leur avancement d'échelon",
    "le salaire minimum légal en Allemagne",
    "le montant de l'allocation chômage versée par France Travail",
    "le calcul de la pension de retraite du régime général",
    "le divorce par consentement mutuel et la garde des enfants",
    "le bail d'habitation et le préavis du locataire",
    "la TVA applicable aux prestations de conseil",
    "le congé de maternité indemnisé par la Sécurité sociale (montant des indemnités journalières)",
    "le droit du travail au Québec",
    "la création d'une SAS et le capital social minimum",
    "les règles de la garantie légale de conformité pour un achat en ligne",
    "le permis de conduire à points",
    "le régime fiscal des micro-entrepreneurs",
    "la succession et les droits de mutation entre frères et sœurs",
    "le droit d'asile et le titre de séjour",
    "le statut des militaires et leur temps de service",
    "les règles d'urbanisme pour construire une véranda",
    "le droit de la consommation sur le démarchage téléphonique",
]

# (tool, args) specs. Edge cases on purpose: thresholds at 6, 8 and 24 months, and the 10-year
# change of rate for the indemnity.
CALC_SPECS = [
    ("preavis", {"anciennete_mois": m, "statut": s})
    for m, s in [
        (4, "normal"),
        (6, "normal"),
        (11, "normal"),
        (18, "normal"),
        (23, "normal"),
        (24, "normal"),
        (40, "normal"),
        (96, "normal"),
        (30, "faute_grave"),
        (60, "faute_lourde"),
        (14, "normal"),
    ]
] + [
    ("indemnite_licenciement", {"anciennete_mois": m, "salaire_ref": sal, "statut": s})
    for m, sal, s in [
        (7, 2100, "normal"),
        (8, 2000, "normal"),
        (15, 1900, "normal"),
        (36, 2400, "normal"),
        (60, 2500, "normal"),
        (100, 3200, "normal"),
        (120, 2800, "normal"),
        (126, 2400, "normal"),
        (144, 3000, "normal"),
        (186, 3500, "normal"),
        (240, 4100, "normal"),
        (300, 2600, "normal"),
        (90, 2700, "faute_grave"),
        (52, 1850.5, "normal"),
        (200, 5000, "normal"),
    ]
]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question", "evidence_quote", "gold_answer"],
    "properties": {
        "question": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "gold_answer": {"type": "string"},
    },
}

COMMON = """Tu rédiges des questions pour évaluer un assistant en droit du travail français.
Écris une question naturelle, comme la poserait un salarié ou un service RH, en français.
Réponds en JSON : {"question": ..., "evidence_quote": ..., "gold_answer": ...}."""


def build_prompt(spec: dict[str, Any]) -> str:
    kind = spec["type"]
    if kind == "refusal":
        return (
            f"{COMMON}\nSujet : {spec['topic']}.\nCe sujet ne relève PAS du Code du travail ni "
            "des conventions Syntec et HCR. Écris une question précise sur ce sujet. "
            'Mets "" dans evidence_quote et gold_answer.'
        )
    if kind == "calculator":
        tool, args = spec["calc"]["tool"], spec["calc"]["args"]
        motif = {
            "normal": "un licenciement sans faute grave",
            "faute_grave": "un licenciement pour faute grave",
            "faute_lourde": "un licenciement pour faute lourde",
        }[args["statut"]]
        what = (
            "la durée du préavis"
            if tool == "preavis"
            else "le montant de l'indemnité légale de licenciement"
        )
        extra = (
            f" et un salaire de référence de {args['salaire_ref']} € brut par mois"
            if "salaire_ref" in args
            else ""
        )
        return (
            f"{COMMON}\nLa question porte sur {what} pour {motif}, avec une ancienneté de "
            f"{args['anciennete_mois']} mois{extra}. Exprime l'ancienneté naturellement (en "
            "années et mois si besoin) sans changer sa valeur. Ne donne pas la réponse. "
            'Mets "" dans evidence_quote et gold_answer.'
        )
    article = spec["article"]
    head = (
        f"{COMMON}\nArticle {article['num']} ({article['source']}), chemin : {article['path']}\n"
        f"Texte :\n{article['text'][:3000]}\n"
    )
    rules = {
        "lookup": "La question doit avoir sa réponse dans cet article, sans citer son numéro et "
        "sans recopier ses formulations (reformule).",
        "num_in_question": f"La question doit contenir exactement « article {article['num']} » "
        "et porter sur ce que dit cet article.",
        "convention": "La question doit porter sur cette convention collective (nomme-la : "
        f"{'Syntec' if article['source'] == 'IDCC 1486' else 'HCR'}) et avoir sa "
        "réponse dans cet article, sans citer son numéro.",
    }[kind]
    return (
        f"{head}{rules} evidence_quote : une phrase recopiée mot pour mot de l'article qui "
        "justifie la réponse. gold_answer : la réponse courte attendue."
    )


def load_scope_articles(path: Path = ARTICLES) -> list[Article]:
    with path.open(encoding="utf-8") as f:
        chunks = [Article.from_json(line) for line in f]
    in_scope = [c for c in chunks if SCOPES["code-l+kali-base"](c)]
    corpus = Corpus(in_scope)  # merges split pieces back into whole articles
    return [a for items in corpus.by_num.values() for a in items]


def _usable(a: Article) -> bool:
    # "Partie législative ancienne" holds pre-2008 numbering no user asks about
    return (
        200 <= len(a.text) <= 4000
        and not a.num.startswith("art.")
        and "ancienne" not in a.path.split(" > ")[0]
    )


def _stratified(rng: random.Random, articles: list[Article], n: int) -> list[Article]:
    """Spread picks across the top two levels of the path (Partie > Livre, or text)."""
    groups: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        groups[" > ".join(a.path.split(" > ")[:2])].append(a)
    for g in groups.values():
        rng.shuffle(g)
    order = sorted(groups)
    rng.shuffle(order)
    picked: list[Article] = []
    while len(picked) < n and any(groups.values()):
        for g in order:
            if groups[g] and len(picked) < n:
                picked.append(groups[g].pop())
    return picked


def sample_specs(articles: list[Article], seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    want = {t: round(n * OVERSAMPLE) for t, n in TARGET_MIX.items()}
    code_l = sorted((a for a in articles if a.source == "code" and _usable(a)), key=lambda a: a.id)
    kali = sorted((a for a in articles if a.source != "code" and _usable(a)), key=lambda a: a.id)
    picked = _stratified(rng, code_l, want["lookup"] + want["num_in_question"])
    specs: list[dict[str, Any]] = []
    for i, a in enumerate(picked):
        kind = "lookup" if i < want["lookup"] else "num_in_question"
        specs.append({"type": kind, "article": a.__dict__})
    syntec = [a for a in kali if a.source == "IDCC 1486"]
    hcr = [a for a in kali if a.source == "IDCC 1979"]
    half = want["convention"] // 2
    for a in _stratified(rng, syntec, want["convention"] - half) + _stratified(rng, hcr, half):
        specs.append({"type": "convention", "article": a.__dict__})
    for tool, args in CALC_SPECS[: want["calculator"]]:
        specs.append({"type": "calculator", "calc": expected_calc(tool, args)})
    for topic in REFUSAL_TOPICS[: want["refusal"]]:
        specs.append({"type": "refusal", "topic": topic})
    for i, s in enumerate(specs, start=1):
        s["cid"] = f"c{i:03d}"
    return specs


def extra_lookup_specs(
    articles: list[Article], existing: list[dict], n: int, seed: int = SEED + 1
) -> list[dict[str, Any]]:
    """More lookup candidates on articles not drafted yet, numbered after the existing ones."""
    used = {(g["num"], g["source"]) for c in existing for g in c.get("gold", [])}
    pool = sorted(
        (
            a
            for a in articles
            if a.source == "code" and _usable(a) and (a.num, a.source) not in used
        ),
        key=lambda a: a.id,
    )
    start = max(int(c["cid"][1:]) for c in existing) + 1
    picks = _stratified(random.Random(seed), pool, n)
    return [
        {"type": "lookup", "article": a.__dict__, "cid": f"c{start + i:03d}"}
        for i, a in enumerate(picks)
    ]


def expected_calc(tool: str, args: dict) -> dict:
    fn = calculators.preavis if tool == "preavis" else calculators.indemnite_licenciement
    res = fn(**args)
    return {"tool": tool, "args": args, "expected": res.valeur, "articles": res.articles}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip().lower()


def finalize(spec: dict[str, Any], draft: dict[str, Any], corpus: Corpus) -> dict[str, Any]:
    row = {"cid": spec["cid"], "type": spec["type"], "question": draft["question"].strip()}
    if spec["type"] == "refusal":
        return row | {"gold": [], "gold_answer": "", "topic": spec["topic"]}
    if spec["type"] == "calculator":
        calc = spec["calc"]
        gold = []
        for num in calc["articles"]:
            if num not in [g["num"] for g in gold]:
                gold.append({"num": num, "source": "code"})
        return row | {"gold": gold, "gold_answer": _calc_answer(calc), "calc": calc}
    a = spec["article"]
    quote = draft["evidence_quote"].strip()
    return row | {
        "gold": [{"num": a["num"], "source": a["source"]}],
        "gold_answer": draft["gold_answer"].strip(),
        "evidence_quote": quote,
        "quote_found": bool(quote) and _norm(quote) in _norm(a["text"]),
        "source_text": a["text"],
        "path": a["path"],
        "url": a["legifrance_url"],
    }


def _calc_answer(calc: dict) -> str:
    v = calc["expected"]
    if calc["tool"] == "preavis":
        return "fixé par la convention ou l'usage" if v is None else f"{v} mois"
    return f"{v:.2f} €"


def draft_one(client, cache: DiskCache, prompt: str, model: str = DRAFTER_MODEL) -> dict:
    k = key("draft", model, prompt)
    cached = cache.get("draft", k)
    if cached is not None:
        return cached
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "draft", "schema": SCHEMA, "strict": True},
        },
    )
    out = json.loads(resp.choices[0].message.content)
    out["_tokens"] = resp.usage.total_tokens
    cache.put("draft", k, out)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.draft_questions")
    parser.add_argument("--dry-run", action="store_true", help="show counts and cost estimate")
    parser.add_argument(
        "--extra-lookup",
        type=int,
        default=0,
        help="append N new lookup candidates to candidates.jsonl",
    )
    args = parser.parse_args(argv)

    articles = load_scope_articles()
    existing = []
    if args.extra_lookup:
        existing = [
            json.loads(line) for line in CANDIDATES_PATH.read_text(encoding="utf-8").splitlines()
        ]
        specs = extra_lookup_specs(articles, existing, args.extra_lookup)
    else:
        specs = sample_specs(articles)
    counts = defaultdict(int)
    for s in specs:
        counts[s["type"]] += 1
    est = len(specs) * EST_TOKENS_PER_DRAFT
    print(f"{len(specs)} candidates {dict(counts)}; about {est} tokens before cache")
    if args.dry_run:
        return

    from retrieval.config import Clients, Settings

    client = Clients(Settings.from_env()).openai
    cache, corpus, rows, tokens = DiskCache(), Corpus(articles), [], 0
    for i, spec in enumerate(specs, start=1):
        misses = cache.misses
        draft = draft_one(client, cache, build_prompt(spec))
        if cache.misses > misses:  # only count tokens actually spent in this run
            tokens += draft.get("_tokens", 0)
        rows.append(finalize(spec, draft, corpus))
        if i % 20 == 0:
            print(f"{i}/{len(specs)} drafted")
    CANDIDATES_PATH.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in existing + rows),
        encoding="utf-8",
    )
    flagged = sum(1 for r in rows if r.get("quote_found") is False)
    print(
        f"wrote {CANDIDATES_PATH} ({len(rows)} rows, {flagged} quotes not found; "
        f"{tokens} tokens this run, cache hits {cache.hits}, misses {cache.misses})"
    )


if __name__ == "__main__":
    main()
