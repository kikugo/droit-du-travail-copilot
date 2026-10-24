from dataclasses import replace

from ingest.models import Article

MAX_CHARS = 6000
TARGET_CHARS = 4000


def _pack(paragraphs: list[str], target: int) -> list[str]:
    pieces, current = [], ""
    for para in paragraphs:
        # A single paragraph longer than the target is cut on whitespace.
        while len(para) > target:
            cut = para.rfind(" ", 0, target)
            cut = cut if cut > 0 else target
            if current:
                pieces.append(current)
                current = ""
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + 1 + len(para) > target:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def chunk(
    article: Article, max_chars: int = MAX_CHARS, target: int = TARGET_CHARS
) -> list[Article]:
    """One chunk per article; only articles over max_chars are split, on paragraph boundaries.

    Each piece gets the article path prepended so it still makes sense on its own.
    """
    if len(article.text) <= max_chars:
        return [article]
    paragraphs = [p.strip() for p in article.text.split("\n") if p.strip()]
    pieces = _pack(paragraphs, target)
    return [
        replace(article, id=f"{article.id}#{i}", text=f"{article.path}\n{piece}")
        for i, piece in enumerate(pieces, start=1)
    ]


def chunk_all(articles: list[Article], **kwargs) -> list[Article]:
    return [piece for article in articles for piece in chunk(article, **kwargs)]
