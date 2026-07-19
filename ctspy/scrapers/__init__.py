"""Registre des scrapers disponibles, indexés par la clé `scraper` de config.yaml."""

from __future__ import annotations

from .base import BaseScraper
from .rdvonline import RdvOnlineScraper


def get_scraper(center: dict, *, weeks_ahead: int, request_delay: float) -> BaseScraper:
    kind = center.get("scraper")
    if kind == "rdvonline":
        return RdvOnlineScraper(center, weeks_ahead=weeks_ahead, request_delay=request_delay)
    if kind == "securitest":
        # Import différé : Playwright n'est nécessaire que pour ce scraper.
        from .securitest import SecuritestScraper
        return SecuritestScraper(center, weeks_ahead=weeks_ahead, request_delay=request_delay)
    raise ValueError(f"Scraper inconnu pour le centre {center.get('id')} : {kind!r}")
