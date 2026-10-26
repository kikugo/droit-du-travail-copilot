"""Retrieval metrics for the four modes over the answerable golden rows.

    uv run python -m eval.run_retrieval --label free
    uv run python -m eval.run_retrieval --label basic --search-endpoint https://... --index articles-full

Query embeddings are cached on disk (eval/cache.py), so a rerun only pays for search calls,
which are free on both tiers. A mode the service refuses (semantic on the free tier) is
recorded as unavailable, never as a score of zero.
"""

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from azure.core.exceptions import HttpResponseError

from eval.cache import DiskCache, key
from eval.golden import load_golden
from eval.metrics import bootstrap_ci, keys, mean, percentile, retrieval_scores
from retrieval.search import Hit, Mode, build_query_args

TOP_K = 10  # MRR@10 needs ten; hit@k and recall@5 use the top of the same list


def query_vector(clients, cache: DiskCache, text: str) -> list[float]:
    s = clients.settings
    k = key("qemb", s.embedding_deployment, s.embedding_dims, text)
    cached = cache.get("qemb", k)
    if cached is not None:
        return cached
    resp = clients.openai.embeddings.create(
        model=s.embedding_deployment, input=[text], dimensions=s.embedding_dims
    )
    vector = resp.data[0].embedding
    cache.put("qemb", k, vector)
    return vector


def run_mode(search_client, rows, mode: Mode, vectors: dict[str, list[float]]) -> dict[str, Any]:
    per_question, latencies = [], []
    for row in rows:
        args = build_query_args(row["question"], mode, vectors.get(row["id"]), top=TOP_K)
        start = time.perf_counter()
        try:
            hits = [Hit.from_result(r) for r in search_client.search(**args)]
        except HttpResponseError as err:
            code = err.error.code if err.error else type(err).__name__
            return {"mode": mode.value, "available": False, "error": code}
        latencies.append(time.perf_counter() - start)
        ranked = [(h.num, h.source) for h in hits]
        scores = retrieval_scores(ranked, keys(row["gold"]))
        per_question.append(
            {"id": row["id"], "type": row["type"], "ranked": ranked[:TOP_K], **scores}
        )
    return {
        "mode": mode.value,
        "available": True,
        **summarise(per_question, latencies),
        "questions": per_question,
    }


def summarise(per_question: list[dict], latencies: list[float]) -> dict[str, Any]:
    metrics = ["hit@1", "hit@3", "hit@5", "mrr@10", "recall@5"]
    totals = {m: mean([q[m] for q in per_question]) for m in metrics}
    totals["hit@5_ci"] = bootstrap_ci([q["hit@5"] for q in per_question])
    totals["p50_ms"] = percentile(latencies, 50) * 1000
    totals["p95_ms"] = percentile(latencies, 95) * 1000
    by_type: dict[str, dict] = {}
    for t in sorted({q["type"] for q in per_question}):
        sub = [q for q in per_question if q["type"] == t]
        by_type[t] = {
            "n": len(sub),
            "hit@5": mean([q["hit@5"] for q in sub]),
            "mrr@10": mean([q["mrr@10"] for q in sub]),
        }
    return {"n": len(per_question), "totals": totals, "by_type": by_type}


def run(clients, rows, label: str, cache: DiskCache) -> dict[str, Any]:
    answerable = [r for r in rows if r["answerable"]]
    vectors = {r["id"]: query_vector(clients, cache, r["question"]) for r in answerable}
    modes = [run_mode(clients.search_client, answerable, m, vectors) for m in Mode]
    return {
        "label": label,
        "search_endpoint": clients.settings.search_endpoint,
        "index": clients.settings.search_index,
        "embedding": {
            "deployment": clients.settings.embedding_deployment,
            "dims": clients.settings.embedding_dims,
        },
        "n_questions": len(answerable),
        "modes": modes,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.run_retrieval")
    parser.add_argument("--label", required=True, help="free or basic: names the results file")
    parser.add_argument("--search-endpoint", help="override AZURE_SEARCH_ENDPOINT (Basic service)")
    parser.add_argument("--index", help="override SEARCH_INDEX")
    args = parser.parse_args(argv)

    from retrieval.config import Clients, Settings

    settings = Settings.from_env()
    if args.search_endpoint:
        settings = replace(settings, search_endpoint=args.search_endpoint)
    if args.index:
        settings = replace(settings, search_index=args.index)
    cache = DiskCache()
    result = run(Clients(settings), load_golden(), args.label, cache)
    out = Path(f"results/retrieval_{args.label}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for m in result["modes"]:
        if m["available"]:
            t = m["totals"]
            print(
                f"{m['mode']:<16} hit@1 {t['hit@1']:.2f}  hit@5 {t['hit@5']:.2f}  "
                f"mrr@10 {t['mrr@10']:.2f}  p95 {t['p95_ms']:.0f} ms"
            )
        else:
            print(f"{m['mode']:<16} unavailable ({m['error']})")
    print(f"wrote {out}; embedding cache hits {cache.hits}, misses {cache.misses}")


if __name__ == "__main__":
    main()
