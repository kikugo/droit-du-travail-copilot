from dataclasses import dataclass
from enum import StrEnum

from azure.search.documents.models import VectorizedQuery

from retrieval.index import SEMANTIC_CONFIG, VECTOR_FIELD

SELECT = ["id", "num", "source", "path", "content", "date_debut_vigueur", "legifrance_url"]


class Mode(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"
    HYBRID_SEMANTIC = "hybrid_semantic"


@dataclass(frozen=True)
class Hit:
    id: str
    num: str
    source: str
    path: str
    text: str
    url: str
    date_debut_vigueur: str
    score: float
    reranker_score: float | None = None

    @classmethod
    def from_result(cls, r: dict) -> "Hit":
        return cls(
            id=r["id"],
            num=r["num"],
            source=r["source"],
            path=r["path"],
            text=r["content"],
            url=r["legifrance_url"],
            date_debut_vigueur=r.get("date_debut_vigueur") or "",
            score=r["@search.score"],
            reranker_score=r.get("@search.reranker_score"),
        )


def source_filter(source: str | None) -> str | None:
    if source is None:
        return None
    return "source eq '{}'".format(source.replace("'", "''"))


def build_query_args(
    query: str,
    mode: Mode,
    query_vector: list[float] | None = None,
    top: int = 5,
    source: str | None = None,
) -> dict:
    """Keyword arguments for SearchClient.search for one retrieval mode."""
    mode = Mode(mode)
    args: dict = {"top": top, "select": SELECT, "filter": source_filter(source)}
    if mode in (Mode.KEYWORD, Mode.HYBRID, Mode.HYBRID_SEMANTIC):
        args["search_text"] = query
    if mode in (Mode.VECTOR, Mode.HYBRID, Mode.HYBRID_SEMANTIC):
        if query_vector is None:
            raise ValueError(f"mode {mode} needs a query vector")
        args["vector_queries"] = [
            VectorizedQuery(vector=query_vector, k_nearest_neighbors=top, fields=VECTOR_FIELD)
        ]
    if mode is Mode.HYBRID_SEMANTIC:
        args["query_type"] = "semantic"
        args["semantic_configuration_name"] = SEMANTIC_CONFIG
    return args


def needs_vector(mode: Mode) -> bool:
    return Mode(mode) is not Mode.KEYWORD


def search(
    clients,
    query: str,
    mode: Mode = Mode.HYBRID_SEMANTIC,
    top: int = 5,
    source: str | None = None,
) -> list[Hit]:
    """Run one query in the given mode. `clients` is a retrieval.config.Clients."""
    vector = None
    if needs_vector(mode):
        s = clients.settings
        resp = clients.openai.embeddings.create(
            model=s.embedding_deployment, input=[query], dimensions=s.embedding_dims
        )
        vector = resp.data[0].embedding
    args = build_query_args(query, mode, vector, top, source)
    return [Hit.from_result(r) for r in clients.search_client.search(**args)]
