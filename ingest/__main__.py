import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from ingest import kali, legi
from ingest.chunk import chunk_all
from ingest.estimate import FREE_TIER_BYTES, scenarios


def mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build data/articles.jsonl")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data/articles.jsonl"))
    parser.add_argument("--stats", type=Path, default=Path("results/corpus_stats.json"))
    args = parser.parse_args()

    articles = legi.load_code_du_travail(legi.download_legi(args.raw_dir))
    for idcc, cont_id in kali.CONVENTIONS.items():
        articles += kali.load_convention(kali.download_kali(args.raw_dir, cont_id), idcc)
    chunks = chunk_all(articles)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(c.to_json() + "\n")

    article_counts = Counter(a.source for a in articles)
    chunk_counts = Counter(c.source for c in chunks)
    print(f"{'source':<12}{'articles':>10}{'chunks':>10}")
    for source in sorted(article_counts):
        print(f"{source:<12}{article_counts[source]:>10}{chunk_counts[source]:>10}")
    print(f"{'total':<12}{len(articles):>10}{len(chunks):>10}\n")

    estimates = scenarios(chunks)
    print(f"Index size estimate (lower bound) vs free tier {mb(FREE_TIER_BYTES)}")
    print(f"{'scope':<14}{'dims':>6}{'type':>9}{'vectors':>11}{'text':>10}{'total':>11}  fits")
    for e in estimates:
        print(
            f"{e.scope:<14}{e.dims:>6}{e.vector_type:>9}{mb(e.vector_bytes):>11}"
            f"{mb(e.text_bytes):>10}{mb(e.total_bytes):>11}  {'yes' if e.fits else 'no'}"
        )

    stats = {
        "sources": {
            "legi": {"repo": legi.REPO_ID, "revision": legi.REVISION, "snapshot": legi.SNAPSHOT},
            "kali": {"repo": "SocialGouv/kali-data", "revision": kali.KALI_DATA_REF},
        },
        "articles": dict(sorted(article_counts.items())),
        "chunks": dict(sorted(chunk_counts.items())),
        "code_by_kind": dict(
            sorted(Counter(a.num[:1] for a in articles if a.source == "code").items())
        ),
        # KALI base texts have no modifDate in kali-data; left empty rather than guessed.
        "articles_without_date": dict(
            sorted(Counter(a.source for a in articles if not a.date_debut_vigueur).items())
        ),
        "free_tier_bytes": FREE_TIER_BYTES,
        "estimates": [
            asdict(e) | {"total_bytes": e.total_bytes, "fits": e.fits} for e in estimates
        ],
        "articles_jsonl_sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
    }
    args.stats.parent.mkdir(parents=True, exist_ok=True)
    args.stats.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out} and {args.stats}")


if __name__ == "__main__":
    main()
