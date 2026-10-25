"""Retrieval and answer metrics, to the unified-rag (B1) Phase 2 definitions.

Retrieval units are articles, keyed (num, source): an article split into pieces counts once,
at the rank of its best piece. Answer metrics follow the guide: citation accuracy means every
cited article is in gold. Everything here is pure, so it's unit-tested on hand-computed cases.
"""

import math
import random
import re
from collections.abc import Iterable, Sequence

from copilot.prompts import REFUSAL

Key = tuple[str, str]


def keys(items: Iterable[dict]) -> list[Key]:
    return [(str(i["num"]), i["source"]) for i in items]


def dedupe(ranked: Sequence[Key]) -> list[Key]:
    seen, out = set(), []
    for k in ranked:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def hit_at_k(ranked: Sequence[Key], gold: Sequence[Key], k: int) -> float:
    return float(any(r in set(gold) for r in dedupe(ranked)[:k]))


def mrr_at_k(ranked: Sequence[Key], gold: Sequence[Key], k: int = 10) -> float:
    for rank, r in enumerate(dedupe(ranked)[:k], start=1):
        if r in set(gold):
            return 1 / rank
    return 0.0


def recall_at_k(ranked: Sequence[Key], gold: Sequence[Key], k: int = 5) -> float:
    if not gold:
        return 0.0
    return len(set(dedupe(ranked)[:k]) & set(gold)) / len(set(gold))


def retrieval_scores(ranked: Sequence[Key], gold: Sequence[Key]) -> dict[str, float]:
    return {
        "hit@1": hit_at_k(ranked, gold, 1),
        "hit@3": hit_at_k(ranked, gold, 3),
        "hit@5": hit_at_k(ranked, gold, 5),
        "mrr@10": mrr_at_k(ranked, gold, 10),
        "recall@5": recall_at_k(ranked, gold, 5),
    }


def refused(answer: str) -> bool:
    return REFUSAL.rstrip(".") in answer


def citation_accuracy(cited: Sequence[Key], gold: Sequence[Key], answerable: bool) -> float:
    """1 when every cited article is in gold (and something is cited, for answerable rows).
    For a refusal row, 1 when nothing is cited."""
    if not answerable:
        return float(not cited)
    return float(bool(cited) and set(cited) <= set(gold))


def citation_hit(cited: Sequence[Key], gold: Sequence[Key]) -> float:
    """Lenient companion: at least one gold article is cited."""
    return float(bool(set(cited) & set(gold)))


def _numbers(text: str) -> set[float]:
    """Numbers in French or English notation: '3 125,50 €', '3125.5', '2 mois'."""
    text = text.replace(" ", " ").replace("\xa0", " ")
    found = set()
    for m in re.finditer(r"\d{1,3}(?:[ .]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?", text):
        raw = m.group()
        if "," in raw:
            raw = raw.replace(" ", "").replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+", raw):
            raw = raw.replace(" ", "").replace(".", "")
        try:
            found.add(float(raw))
        except ValueError:
            continue
    return found


def calculator_correct(calc: dict, tool_calls: Sequence[dict], answer: str) -> float:
    """The expected tool ran with the expected arguments and the answer states its value."""
    call = next((c for c in tool_calls if c["name"] == calc["tool"]), None)
    if call is None:
        return 0.0
    args = {k: call["args"].get(k) for k in calc["args"]}
    same_args = all(
        (abs(float(args[k]) - float(v)) < 0.01) if isinstance(v, int | float) else args[k] == v
        for k, v in calc["args"].items()
        if args[k] is not None
    ) and all(args[k] is not None for k in calc["args"] if k != "statut")
    if not same_args:
        return 0.0
    expected = calc["expected"]
    if expected is None:  # under 6 months: set by agreement or usage
        return float(bool(re.search(r"convention|usage|accord", answer, re.IGNORECASE)))
    if expected == 0:
        return float(bool(re.search(r"\b(aucun|pas d[e'’]|0)\b", answer, re.IGNORECASE)))
    return float(any(abs(n - float(expected)) < 0.01 for n in _numbers(answer)))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, q in [0, 100]."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def bootstrap_ci(
    values: Sequence[float], n: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float]:
    """95% percentile bootstrap CI of the mean, seeded so reports are reproducible."""
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(mean([rng.choice(values) for _ in values]) for _ in range(n))
    lo = means[int(alpha / 2 * n)]
    hi = means[min(n - 1, int((1 - alpha / 2) * n))]
    return (lo, hi)


def cost_usd(input_tokens: int, output_tokens: int, price: dict) -> float:
    return (input_tokens * price["input_per_1m"] + output_tokens * price["output_per_1m"]) / 1e6
