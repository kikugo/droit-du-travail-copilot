"""Legal minimums from the Code du travail. Collective agreements may be more favourable.

Not handled: agreement-specific rules (Syntec, HCR...), inaptitude d'origine professionnelle
(doubled indemnity, L1226-14), the reference salary itself (R1234-4: the caller passes it),
CDD, rupture conventionnelle.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

STATUTS = ("normal", "faute_grave", "faute_lourde")


@dataclass(frozen=True)
class Resultat:
    valeur: float | None  # months for preavis, euros for indemnite; None = not set by the Code
    unite: str
    articles: list[str] = field(default_factory=list)
    explication: str = ""


def _check(anciennete_mois, statut: str) -> int:
    if statut not in STATUTS:
        raise ValueError(f"statut doit être l'un de {STATUTS}")
    if isinstance(anciennete_mois, bool) or not isinstance(anciennete_mois, int | float):
        raise TypeError("anciennete_mois doit être un nombre de mois")
    if anciennete_mois < 0:
        raise ValueError("anciennete_mois ne peut pas être négative")
    return int(anciennete_mois)  # only complete months count


def preavis(anciennete_mois: int, statut: str = "normal") -> Resultat:
    """Préavis de licenciement minimum (article L1234-1)."""
    mois = _check(anciennete_mois, statut)
    if statut != "normal":
        return Resultat(
            0, "mois", ["L1234-1"], "Pas de préavis en cas de faute grave ou lourde (L1234-1)."
        )
    if mois < 6:
        return Resultat(
            None,
            "mois",
            ["L1234-1"],
            "Moins de 6 mois d'ancienneté : la durée du préavis est fixée par la loi, la "
            "convention ou l'accord collectif, ou à défaut par les usages (L1234-1, 1°).",
        )
    if mois < 24:
        return Resultat(
            1,
            "mois",
            ["L1234-1"],
            "Entre 6 mois et moins de 2 ans d'ancienneté : 1 mois (L1234-1, 2°).",
        )
    return Resultat(2, "mois", ["L1234-1"], "Au moins 2 ans d'ancienneté : 2 mois (L1234-1, 3°).")


def indemnite_licenciement(
    anciennete_mois: int, salaire_ref: float, statut: str = "normal"
) -> Resultat:
    """Indemnité légale de licenciement (L1234-9, R1234-1, R1234-2)."""
    mois = _check(anciennete_mois, statut)
    if isinstance(salaire_ref, bool) or not isinstance(salaire_ref, int | float):
        raise TypeError("salaire_ref doit être un montant en euros")
    if salaire_ref < 0:
        raise ValueError("salaire_ref ne peut pas être négatif")
    articles = ["L1234-9", "R1234-1", "R1234-2"]
    if statut != "normal":
        return Resultat(
            0, "euros", ["L1234-9"], "Pas d'indemnité en cas de faute grave ou lourde (L1234-9)."
        )
    if mois < 8:
        return Resultat(
            0, "euros", ["L1234-9"], "Il faut 8 mois d'ancienneté ininterrompue (L1234-9)."
        )
    annees = Fraction(mois, 12)  # incomplete years count by complete months (R1234-1)
    jusqua_10 = min(annees, 10)
    au_dela = max(annees - 10, 0)
    mois_de_salaire = Fraction(1, 4) * jusqua_10 + Fraction(1, 3) * au_dela  # R1234-2
    montant = (
        Decimal(str(salaire_ref))
        * Decimal(mois_de_salaire.numerator)
        / Decimal(mois_de_salaire.denominator)
    )
    montant = montant.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Resultat(
        float(montant),
        "euros",
        articles,
        f"1/4 de mois de salaire par année jusqu'à 10 ans, 1/3 au-delà (R1234-2), "
        f"années incomplètes au prorata des mois complets (R1234-1) : "
        f"{float(mois_de_salaire):.4f} mois × {salaire_ref} €.",
    )
