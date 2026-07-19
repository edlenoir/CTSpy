"""Chargement de la configuration YAML (centres, options globales)."""

from __future__ import annotations

import os

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")


class Config:
    def __init__(self, data: dict, path: str):
        self.path = path
        self.data = data
        self.weeks_ahead: int = int(data.get("weeks_ahead", 4))
        self.request_delay: float = float(data.get("request_delay", 1.5))
        db = data.get("database", "ctspy.db")
        if not os.path.isabs(db):
            db = os.path.join(os.path.dirname(os.path.abspath(path)), db)
        self.database: str = db
        self.centers: list[dict] = data.get("centers", [])

    def center(self, center_id: str) -> dict:
        for c in self.centers:
            if c.get("id") == center_id:
                return c
        raise KeyError(f"Centre inconnu dans config.yaml : {center_id}")


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("CTSPY_CONFIG", DEFAULT_CONFIG_PATH)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config(data, path)
