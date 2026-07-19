"""Modèles de données partagés entre les scrapers, la base et le dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Slot:
    """Un créneau de rendez-vous disponible dans un centre."""

    center_id: str
    date: str            # YYYY-MM-DD
    time: str            # HH:MM
    price: float | None  # en euros, None si non communiqué
    control_type: str = ""
    vehicle_type: str = ""
    energy: str = ""
    agenda_id: str = ""      # identifiant de ligne d'agenda côté site (si exposé)
    base_price: float | None = None  # tarif avant promotion, si différent
    extra: dict = field(default_factory=dict)

    @property
    def is_promo(self) -> bool:
        return self.base_price is not None and self.price is not None \
            and self.price < self.base_price


@dataclass
class ScrapeResult:
    """Résultat d'un passage de scraping sur un centre."""

    center_id: str
    slots: list[Slot]
    min_price: float | None = None
    max_price: float | None = None
    errors: list[str] = field(default_factory=list)


def parse_price(raw: str | None) -> float | None:
    """Convertit '75', '75,50', '75.50 €' ... en float. None si vide/illisible."""
    if not raw:
        return None
    cleaned = raw.replace("€", "").replace("\xa0", " ").strip().replace(",", ".")
    # garde uniquement la première valeur numérique
    number = ""
    for ch in cleaned:
        if ch.isdigit() or (ch == "." and "." not in number):
            number += ch
        elif number:
            break
    try:
        return float(number)
    except ValueError:
        return None


def normalize_time(raw: str) -> str:
    """Normalise '9h20', '09:20', '9:20' en '09:20'."""
    cleaned = raw.strip().lower().replace("h", ":")
    if ":" not in cleaned:
        return cleaned
    hours, _, minutes = cleaned.partition(":")
    if not minutes:
        minutes = "00"
    return f"{int(hours):02d}:{int(minutes):02d}"
