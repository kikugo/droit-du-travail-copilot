import json
import urllib.request
from pathlib import Path

from ingest.html import html_to_text
from ingest.models import Article

KALI_DATA_REF = "84b1d6713c20184f2fec889c69a056864f8c5246"  # SocialGouv/kali-data
CONVENTIONS = {
    "1486": "KALICONT000005635173",  # Syntec (bureaux d'études techniques)
    "1979": "KALICONT000005635534",  # HCR (hôtels, cafés, restaurants)
}


def download_kali(raw_dir: Path, cont_id: str, ref: str = KALI_DATA_REF) -> Path:
    target = raw_dir / "kali" / ref / f"{cont_id}.json"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/SocialGouv/kali-data/{ref}/data/{cont_id}.json"
        with urllib.request.urlopen(url, timeout=120) as resp:
            target.write_bytes(resp.read())
    return target


def legifrance_url(article_id: str) -> str:
    return f"https://www.legifrance.gouv.fr/conv_coll/article/{article_id}"


def _in_force(etat: str | None) -> bool:
    # VIGUEUR, VIGUEUR_ETEN (extended) and VIGUEUR_NON_ETEN are all in force.
    return bool(etat) and etat.startswith("VIGUEUR")


def load_convention(json_path: Path, idcc: str) -> list[Article]:
    tree = json.loads(json_path.read_text(encoding="utf-8"))
    title = tree["data"].get("shortTitle") or tree["data"].get("title", "")
    articles: list[Article] = []

    def walk(node: dict, path: list[str], date: str) -> None:
        data = node.get("data", {})
        if node.get("type") == "article":
            if not _in_force(data.get("etat")):
                return
            text = html_to_text(data.get("content", ""))
            if not text:
                return
            num = (data.get("num") or "").strip() or f"art. {data.get('intOrdre', '')}"
            articles.append(
                Article(
                    id=data["id"],
                    num=num,
                    source=f"IDCC {idcc}",
                    path=" > ".join(path),
                    text=text,
                    date_debut_vigueur=date,
                    legifrance_url=legifrance_url(data["id"]),
                )
            )
            return
        # Sections are kept only while in force. Texts (KALITEXT) carry modifDate, used as the
        # article date; base texts have none, so their articles keep an empty date.
        if node.get("type") == "section" and data.get("etat") and not _in_force(data["etat"]):
            return
        child_path = path + [" ".join(data["title"].split())] if data.get("title") else path
        child_date = data.get("modifDate") or date
        for child in node.get("children", []):
            walk(child, child_path, child_date)

    for child in tree.get("children", []):
        walk(child, [title], "")
    return articles
