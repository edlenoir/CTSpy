"""Interface commune des scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ScrapeResult


class BaseScraper(ABC):
    def __init__(self, center: dict, *, weeks_ahead: int, request_delay: float):
        self.center = center
        self.center_id: str = center["id"]
        self.weeks_ahead = weeks_ahead
        self.request_delay = request_delay
        self.labels: dict = center.get("labels", {})

    @abstractmethod
    def scrape(self) -> ScrapeResult:
        """Collecte les créneaux disponibles sur la fenêtre configurée."""
