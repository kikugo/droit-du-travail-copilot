import re
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

from ingest.models import Article

REPO_ID = "AgentPublic/legi"
REVISION = "59d6fd3570bc6f7060bc77c182897cc226ce2e9e"
SNAPSHOT = "legi-20260926"
CODE_TITLE = "Code du travail"
COLUMNS = ["doc_id", "chunk_index", "number", "status", "title", "subtitles", "start_date", "text"]

# subtitles look like "Partie législative - Première partie : ... - Livre II : ... - Section 1 : ...".
# Split only before a level keyword, so a " - " inside a title stays put.
_LEVEL = re.compile(
    r"\s+-\s+(?=(?:Partie|\w+ partie|Livre|Titre|Chapitre|Section|Sous-section|"
    r"Paragraphe|Sous-paragraphe)\b)"
)


def download_legi(raw_dir: Path, revision: str = REVISION, snapshot: str = SNAPSHOT) -> Path:
    filename = f"data/{snapshot}/legi_code_du_travail/legi_code_du_travail_part_0.parquet"
    local = hf_hub_download(
        REPO_ID, filename, repo_type="dataset", revision=revision, local_dir=raw_dir / "legi"
    )
    return Path(local)


def normalise_path(subtitles: str | None) -> str:
    if not subtitles:
        return ""
    parts = [" ".join(p.split()).rstrip(".").strip() for p in _LEVEL.split(subtitles)]
    return " > ".join(p for p in parts if p)


def legifrance_url(doc_id: str) -> str:
    return f"https://www.legifrance.gouv.fr/codes/article_lc/{doc_id}"


def load_code_du_travail(parquet_path: Path) -> list[Article]:
    """Articles of the Code du travail that are in force, one per article."""
    rows = pq.read_table(parquet_path, columns=COLUMNS).to_pylist()
    articles = []
    for row in rows:
        if row["title"] != CODE_TITLE or row["status"] != "VIGUEUR" or row["chunk_index"] != 1:
            continue
        articles.append(
            Article(
                id=row["doc_id"],
                num=(row["number"] or "").strip(),
                source="code",
                path=normalise_path(row["subtitles"]),
                text=(row["text"] or "").strip(),
                date_debut_vigueur=row["start_date"],
                legifrance_url=legifrance_url(row["doc_id"]),
            )
        )
    return articles
