from dataclasses import dataclass

from ingest.models import Article

FREE_TIER_BYTES = 52_428_800  # storageSize quota reported by the free search service
DIMS = (1536, 512)
BYTES_PER_DIM = {"float32": 4, "int8": 1}


@dataclass(frozen=True)
class Estimate:
    scope: str
    dims: int
    vector_type: str
    chunks: int
    vector_bytes: int
    text_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.vector_bytes + self.text_bytes

    @property
    def fits(self) -> bool:
        return self.total_bytes <= FREE_TIER_BYTES


def text_bytes(chunks: list[Article]) -> int:
    fields = ("id", "num", "source", "path", "text", "date_debut_vigueur", "legifrance_url")
    return sum(len(getattr(c, f).encode("utf-8")) for c in chunks for f in fields)


def estimate(chunks: list[Article], dims: int, vector_type: str, scope: str = "all") -> Estimate:
    """Lower bound: vectors x dims x bytes + stored text. Inverted index overhead is not included."""
    return Estimate(
        scope=scope,
        dims=dims,
        vector_type=vector_type,
        chunks=len(chunks),
        vector_bytes=len(chunks) * dims * BYTES_PER_DIM[vector_type],
        text_bytes=text_bytes(chunks),
    )


def scenarios(chunks: list[Article]) -> list[Estimate]:
    scopes = {
        "all": chunks,
        "code L only": [c for c in chunks if c.source == "code" and c.num.startswith("L")],
    }
    return [
        estimate(subset, dims, vtype, scope)
        for scope, subset in scopes.items()
        for dims in DIMS
        for vtype in BYTES_PER_DIM
    ]
