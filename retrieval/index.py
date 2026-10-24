import re
from collections.abc import Callable

from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    LexicalAnalyzerName,
    RescoringOptions,
    ScalarQuantizationCompression,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchCompressionRescoreStorageMethod,
    VectorSearchProfile,
)

from ingest.models import Article

VECTOR_FIELD = "content_vector"
SEMANTIC_CONFIG = "default"
UPLOAD_BATCH = 500
COMPRESSIONS = ("none", "int8")


def doc_key(chunk_id: str) -> str:
    """Search keys allow letters, digits, '_', '-' and '='; chunk ids use '#' for pieces."""
    return re.sub(r"[^A-Za-z0-9_\-=]", "_", chunk_id)


def index_definition(name: str, dims: int, compression: str = "none") -> SearchIndex:
    if compression not in COMPRESSIONS:
        raise ValueError(f"compression must be one of {COMPRESSIONS}")
    fr = LexicalAnalyzerName.FR_MICROSOFT
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="num", filterable=True),
        SimpleField(
            name="source", type=SearchFieldDataType.String, filterable=True, facetable=True
        ),
        SearchableField(name="path", analyzer_name=fr),
        SearchableField(name="content", analyzer_name=fr),
        SearchField(
            name=VECTOR_FIELD,
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            stored=False,  # vectors are only searched, never returned
            vector_search_dimensions=dims,
            vector_search_profile_name="hnsw-profile",
        ),
        SimpleField(name="date_debut_vigueur", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="legifrance_url", type=SearchFieldDataType.String),
    ]
    compressions = []
    if compression == "int8":
        compressions.append(
            ScalarQuantizationCompression(
                compression_name="int8",
                rescoring_options=RescoringOptions(
                    enable_rescoring=False,
                    rescore_storage_method=VectorSearchCompressionRescoreStorageMethod.DISCARD_ORIGINALS,
                ),
            )
        )
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="hnsw", parameters=HnswParameters(metric="cosine"))
        ],
        compressions=compressions or None,
        profiles=[
            VectorSearchProfile(
                name="hnsw-profile",
                algorithm_configuration_name="hnsw",
                compression_name="int8" if compressions else None,
            )
        ],
    )
    semantic = SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIG,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="path"),
                    content_fields=[SemanticField(field_name="content")],
                ),
            )
        ],
    )
    return SearchIndex(
        name=name, fields=fields, vector_search=vector_search, semantic_search=semantic
    )


def to_document(article: Article, vector) -> dict:
    return {
        "id": doc_key(article.id),
        "num": article.num,
        "source": article.source,
        "path": article.path,
        "content": article.text,
        VECTOR_FIELD: [float(x) for x in vector],
        "date_debut_vigueur": article.date_debut_vigueur,
        "legifrance_url": article.legifrance_url,
    }


def build(
    index_client,
    search_client,
    articles: list[Article],
    vectors: dict,
    name: str,
    dims: int,
    compression: str = "none",
    recreate: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Create (or recreate) the index and upload every chunk. Returns index statistics."""
    if recreate and name in set(index_client.list_index_names()):
        index_client.delete_index(name)
        log(f"deleted index {name}")
    index_client.create_or_update_index(index_definition(name, dims, compression))
    docs = [to_document(a, vectors[a.id]) for a in articles]
    failed = 0
    for start in range(0, len(docs), UPLOAD_BATCH):
        results = search_client.upload_documents(docs[start : start + UPLOAD_BATCH])
        failed += sum(1 for r in results if not r.succeeded)
        log(f"uploaded {min(start + UPLOAD_BATCH, len(docs))}/{len(docs)}")
    if failed:
        raise RuntimeError(f"{failed} documents failed to upload")
    return dict(index_client.get_index_statistics(name))


def check_semantic(search_client, query: str = "durée du préavis de licenciement") -> dict:
    """Run one semantic query to find out whether this tier runs the semantic ranker."""
    try:
        results = search_client.search(
            search_text=query,
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIG,
            semantic_error_mode="fail",  # default "partial" would silently skip reranking
            top=3,
            select=["num"],
        )
        hits = [(r["num"], r.get("@search.reranker_score")) for r in results]
    except HttpResponseError as err:  # the service answers with an error when it's unavailable
        return {"semantic_ranker": False, "query": query, "error": f"{type(err).__name__}: {err}"}
    reranked = any(score is not None for _, score in hits)
    return {"semantic_ranker": reranked, "query": query, "top_nums": [n for n, _ in hits]}
