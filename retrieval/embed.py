import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import openai

from ingest.models import Article

BATCH_SIZE = 64
MAX_RETRIES = 8
RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)


def embedding_input(article: Article) -> str:
    # Split chunks already start with their path; whole articles get it here.
    if "#" in article.id:
        return f"{article.num} : {article.text}"
    return f"{article.num} : {article.path}\n{article.text}"


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _retry_after(err: Exception, attempt: int) -> float:
    response = getattr(err, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    try:
        return float(header) if header else min(2**attempt, 60)
    except ValueError:
        return min(2**attempt, 60)


def embed_batch(
    client,
    texts: list[str],
    deployment: str,
    dims: int,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[list[float]], int]:
    """Embed one batch, retrying on 429 / connection / 5xx. Returns vectors and tokens used."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.embeddings.create(model=deployment, input=texts, dimensions=dims)
            vectors = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            return vectors, resp.usage.total_tokens
        except RETRYABLE as err:
            if attempt == MAX_RETRIES - 1:
                raise
            sleep(_retry_after(err, attempt))
    raise RuntimeError("unreachable")


def load_cache(path: Path) -> dict[str, tuple[str, np.ndarray]]:
    """chunk id -> (input hash, vector)."""
    if not path.exists():
        return {}
    data = np.load(path)
    return {i: (h, v) for i, h, v in zip(data["ids"], data["hashes"], data["vectors"], strict=True)}


def save_cache(path: Path, cache: dict[str, tuple[str, np.ndarray]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = sorted(cache)
    tmp = path.with_suffix(".tmp.npz")
    np.savez(
        tmp,
        ids=np.array(ids),
        hashes=np.array([cache[i][0] for i in ids]),
        vectors=np.array([cache[i][1] for i in ids], dtype=np.float32),
    )
    tmp.replace(path)


def embed_corpus(
    articles: list[Article],
    cache_path: Path,
    client,
    deployment: str,
    dims: int,
    batch_size: int = BATCH_SIZE,
    save_every: int = 20,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, np.ndarray], int]:
    """Embed every chunk not already cached with the same input hash. Resumable."""
    cache = load_cache(cache_path)
    inputs = {a.id: embedding_input(a) for a in articles}
    todo = [i for i, text in inputs.items() if cache.get(i, ("",))[0] != input_hash(text)]
    log(f"{len(articles) - len(todo)} cached, {len(todo)} to embed")

    tokens = 0
    for n, start in enumerate(range(0, len(todo), batch_size), start=1):
        ids = todo[start : start + batch_size]
        vectors, used = embed_batch(client, [inputs[i] for i in ids], deployment, dims, sleep)
        tokens += used
        for i, v in zip(ids, vectors, strict=True):
            cache[i] = (input_hash(inputs[i]), np.asarray(v, dtype=np.float32))
        if n % save_every == 0:
            save_cache(cache_path, cache)
            log(f"{start + len(ids)}/{len(todo)} embedded, {tokens} tokens")
    if todo:
        save_cache(cache_path, cache)
    log(f"done: {len(todo)} embedded, {tokens} tokens")
    return {i: cache[i][1] for i in inputs}, tokens
