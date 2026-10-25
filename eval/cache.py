"""Disk cache for paid calls (drafts, agent answers, query embeddings, judge verdicts).

Keys are a sha256 of the inputs that change the output, so a rerun of the same eval costs
nothing and any change to a prompt, model or question is a cache miss, never a stale hit.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

CACHE_PATH = Path("data/eval_cache.sqlite")


def key(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, path: Path = CACHE_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (ns TEXT, k TEXT, v TEXT, PRIMARY KEY (ns, k))"
        )
        self.hits = self.misses = 0

    def get(self, ns: str, k: str) -> Any | None:
        row = self.db.execute("SELECT v FROM cache WHERE ns = ? AND k = ?", (ns, k)).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(row[0])

    def put(self, ns: str, k: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (ns, k, v) VALUES (?, ?, ?)",
            (ns, k, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
