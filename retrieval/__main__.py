import argparse
import json
from pathlib import Path

from azure.core.exceptions import HttpResponseError

from ingest.estimate import estimate
from ingest.models import Article
from retrieval.config import Clients, Settings
from retrieval.embed import embed_corpus
from retrieval.index import COMPRESSIONS, SCOPES, build, check_semantic, select_scope
from retrieval.search import Mode, build_query_args, needs_vector, search

ARTICLES = Path("data/articles.jsonl")
CORPUS_STATS = Path("results/corpus_stats.json")
RECORD_QUERIES = [
    "durée du préavis de licenciement",
    "L1237-19",
    "salaire minimum coefficient Syntec",
]


def load_articles(path: Path = ARTICLES) -> list[Article]:
    with path.open(encoding="utf-8") as f:
        return [Article.from_json(line) for line in f]


def cache_path(s: Settings) -> Path:
    return Path(f"data/embeddings/{s.embedding_deployment}-{s.embedding_dims}.npz")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def cmd_embed(clients: Clients, args) -> None:
    s = clients.settings
    _, tokens = embed_corpus(
        load_articles(), cache_path(s), clients.openai, s.embedding_deployment, s.embedding_dims
    )
    print(f"tokens this run: {tokens}")


def cmd_build(clients: Clients, args) -> None:
    s = clients.settings
    articles = select_scope(load_articles(), args.scope)
    vectors, tokens = embed_corpus(
        articles, cache_path(s), clients.openai, s.embedding_deployment, s.embedding_dims
    )
    if tokens:
        print(f"note: {tokens} tokens embedded for chunks missing from the cache")
    stats = build(
        clients.index_client,
        clients.search_client,
        articles,
        vectors,
        s.search_index,
        s.embedding_dims,
        args.compression,
        recreate=args.recreate,
    )
    corpus = json.loads(CORPUS_STATS.read_text(encoding="utf-8"))
    vector_type = "int8" if args.compression == "int8" else "float32"
    predicted = estimate(articles, s.embedding_dims, vector_type, args.scope)
    # One entry per scope and compression, so the runs can be compared side by side.
    path = Path("results/index_stats.json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    runs = {(r["scope"], r["compression"]): r for r in previous.get("runs", [])}
    runs[(args.scope, args.compression)] = {
        "scope": args.scope,
        "compression": args.compression,
        "chunks": len(articles),
        **stats,
        "fits": stats["storage_size"] <= corpus["free_tier_bytes"],
        "estimate_bytes": predicted.total_bytes,
    }
    write_json(
        path,
        {
            "index": s.search_index,
            "embedding": {"deployment": s.embedding_deployment, "dims": s.embedding_dims},
            "free_tier_bytes": corpus["free_tier_bytes"],
            "live": {"scope": args.scope, "compression": args.compression},
            "runs": [runs[k] for k in sorted(runs)],
        },
    )


def cmd_check_semantic(clients: Clients, args) -> None:
    result = check_semantic(clients.search_client)
    result["search_endpoint"] = clients.settings.search_endpoint
    write_json(Path("results/search_capabilities.json"), result)


def cmd_query(clients: Clients, args) -> None:
    for hit in search(clients, args.q, Mode(args.mode), args.top, args.source):
        rerank = f" rerank={hit.reranker_score:.2f}" if hit.reranker_score is not None else ""
        print(f"{hit.num:<14} {hit.source:<10} score={hit.score:.4f}{rerank}  {hit.url}")
        print(f"    {hit.path[-110:]}")


def cmd_record(clients: Clients, args) -> None:
    """Save raw search responses so tests can replay them without network."""
    s = clients.settings
    recorded = []
    for q in RECORD_QUERIES:
        vector = None
        for mode in Mode:
            if needs_vector(mode) and vector is None:
                vector = (
                    clients.openai.embeddings.create(
                        model=s.embedding_deployment, input=[q], dimensions=s.embedding_dims
                    )
                    .data[0]
                    .embedding
                )
            query_args = build_query_args(q, mode, vector, top=5)
            try:
                results = [dict(r) for r in clients.search_client.search(**query_args)]
            except HttpResponseError as err:  # e.g. semantic ranker off on the free tier
                recorded.append({"query": q, "mode": mode.value, "error": err.error.code})
                continue
            recorded.append({"query": q, "mode": mode.value, "results": results})
    write_json(Path("tests/fixtures/search_recorded.json"), {"recordings": recorded})


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m retrieval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("embed", help="embed data/articles.jsonl into the local cache (costs tokens)")
    p = sub.add_parser("build", help="create the index and upload every chunk")
    p.add_argument("--compression", choices=COMPRESSIONS, default="none")
    p.add_argument("--scope", choices=list(SCOPES), default="code-l+kali")
    p.add_argument("--recreate", action="store_true", help="delete the index first")
    sub.add_parser("check-semantic", help="find out whether the tier runs the semantic ranker")
    p = sub.add_parser("query", help="run one query")
    p.add_argument("q")
    p.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HYBRID.value)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--source")
    sub.add_parser("record", help="record responses for the test fixture")
    args = parser.parse_args()

    clients = Clients(Settings.from_env())
    commands = {
        "embed": cmd_embed,
        "build": cmd_build,
        "check-semantic": cmd_check_semantic,
        "query": cmd_query,
        "record": cmd_record,
    }
    commands[args.cmd](clients, args)


if __name__ == "__main__":
    main()
