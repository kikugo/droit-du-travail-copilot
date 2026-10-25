import re
import time
from collections.abc import Callable

from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes.models import (
    CustomAnalyzer,
    HnswAlgorithmConfiguration,
    HnswParameters,
    LexicalAnalyzerName,
    PatternTokenizer,
    RescoringOptions,
    ScalarQuantizationCompression,
    ScoringProfile,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    TextWeights,
    TokenFilterName,
    VectorSearch,
    VectorSearchCompressionRescoreStorageMethod,
    VectorSearchProfile,
)

from ingest.models import Article

VECTOR_FIELD = "content_vector"
NUM_ANALYZER = "num_analyzer"
NUM_BOOST = 5.0  # num_search vs content: articles citing L1237-19 often out-score L1237-19 itself
SEMANTIC_CONFIG = "default"
UPLOAD_BATCH = 500
COMPRESSIONS = ("none", "int8")

# Which chunks go into the index. The free tier (50 MB) can't hold the full corpus,
# so it gets a trimmed scope; "full" is for a Basic service.
SCOPES = {
    "full": lambda a: True,
    "code-l+kali": lambda a: a.source != "code" or a.num.startswith("L"),
    "code-l+kali-base": lambda a: (
        a.num.startswith("L") if a.source == "code" else " > Textes Attachés" not in a.path
    ),
    "code-l": lambda a: a.source == "code" and a.num.startswith("L"),
}


def select_scope(articles: list[Article], scope: str) -> list[Article]:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {list(SCOPES)}")
    return [a for a in articles if SCOPES[scope](a)]


def doc_key(chunk_id: str) -> str:
    """Search keys allow letters, digits, '_', '-' and '='; chunk ids use '#' for pieces."""
    return re.sub(r"[^A-Za-z0-9_\-=]", "_", chunk_id)


def index_definition(name: str, dims: int, compression: str = "none") -> SearchIndex:
    if compression not in COMPRESSIONS:
        raise ValueError(f"compression must be one of {COMPRESSIONS}")
    fr = LexicalAnalyzerName.FR_MICROSOFT
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="num", type=SearchFieldDataType.String, filterable=True),
        # Code article numbers as single tokens ("L1237-19"), so a question naming an article
        # matches it exactly and not L1237-19-3. Empty for KALI, whose "1", "2" would match digits.
        SearchableField(name="num_search", analyzer_name=NUM_ANALYZER),
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
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic,
        scoring_profiles=[
            ScoringProfile(
                name="num-boost", text_weights=TextWeights(weights={"num_search": NUM_BOOST})
            )
        ],
        default_scoring_profile="num-boost",
        tokenizers=[PatternTokenizer(name="num_tokenizer", pattern=r"[^\w\-]+")],
        analyzers=[
            CustomAnalyzer(
                name=NUM_ANALYZER,
                tokenizer_name="num_tokenizer",
                token_filters=[TokenFilterName.LOWERCASE],
            )
        ],
    )


def to_document(article: Article, vector) -> dict:
    return {
        "id": doc_key(article.id),
        "num": article.num,
        "num_search": article.num if article.source == "code" else "",
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
    return wait_for_stats(index_client, name, len(docs), log=log)


def wait_for_stats(
    index_client,
    name: str,
    expected_docs: int,
    tries: int = 30,
    steady: int = 3,
    sleep=time.sleep,
    log=print,
) -> dict:
    """Free-tier statistics lag: the vector index is counted first, the text a few minutes later.

    Poll until every doc is counted, text storage shows up, and `steady` readings agree.
    """
    readings: list[dict] = []
    for _ in range(tries):
        stats = index_stats(index_client, name)
        text_counted = stats["storage_size"] - stats["vector_index_size"] > 1_048_576
        if stats["document_count"] == expected_docs and text_counted:
            readings.append(stats)
            if len(readings) >= steady and all(r == stats for r in readings[-steady:]):
                return stats
        else:
            readings.clear()
        sleep(60)
    log("statistics did not settle; returning the last reading")
    return stats


def index_stats(index_client, name: str) -> dict:
    # SDK 12 returns a model object; dict() on it gives the wire (camelCase) keys.
    s = index_client.get_index_statistics(name)
    return {
        "document_count": s.document_count,
        "storage_size": s.storage_size,
        "vector_index_size": s.vector_index_size,
    }


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
