import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Article:
    id: str
    num: str
    source: str  # "code" or "IDCC 1486" / "IDCC 1979"
    path: str  # "Partie > Livre > Titre > Chapitre > ..."
    text: str
    date_debut_vigueur: str  # YYYY-MM-DD
    legifrance_url: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Article":
        return cls(**json.loads(line))
