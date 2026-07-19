"""Couche de stockage SQLite : historique des relevés et créneaux."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .models import Slot

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    center_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | success | error
    error       TEXT,
    slots_found INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
    center_id    TEXT NOT NULL,
    slot_date    TEXT NOT NULL,        -- YYYY-MM-DD
    slot_time    TEXT NOT NULL,        -- HH:MM
    price        REAL,
    base_price   REAL,
    control_type TEXT NOT NULL DEFAULT '',
    vehicle_type TEXT NOT NULL DEFAULT '',
    energy       TEXT NOT NULL DEFAULT '',
    agenda_id    TEXT NOT NULL DEFAULT '',
    extra        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_slots_center_date ON slots(center_id, slot_date, slot_time);
CREATE INDEX IF NOT EXISTS idx_slots_run ON slots(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_center ON scrape_runs(center_id, started_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ runs

    def start_run(self, center_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scrape_runs (center_id, started_at) VALUES (?, ?)",
            (center_id, utcnow()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, slots_found: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ?, slots_found = ?, error = ? WHERE id = ?",
            (utcnow(), status, slots_found, error, run_id),
        )
        self.conn.commit()

    def runs(self, center_id: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        sql = "SELECT * FROM scrape_runs"
        params: list = []
        if center_id:
            sql += " WHERE center_id = ?"
            params.append(center_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def latest_successful_run(self, center_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scrape_runs WHERE center_id = ? AND status = 'success' "
            "ORDER BY id DESC LIMIT 1",
            (center_id,),
        ).fetchone()

    # ----------------------------------------------------------------- slots

    def insert_slots(self, run_id: int, slots: list[Slot]) -> None:
        self.conn.executemany(
            "INSERT INTO slots (run_id, center_id, slot_date, slot_time, price, base_price,"
            " control_type, vehicle_type, energy, agenda_id, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, s.center_id, s.date, s.time, s.price, s.base_price,
                    s.control_type, s.vehicle_type, s.energy, s.agenda_id,
                    json.dumps(s.extra, ensure_ascii=False),
                )
                for s in slots
            ],
        )
        self.conn.commit()

    def latest_slots(self, center_id: str, date_from: str | None = None,
                     date_to: str | None = None) -> list[sqlite3.Row]:
        """Créneaux du dernier relevé réussi pour un centre."""
        run = self.latest_successful_run(center_id)
        if run is None:
            return []
        sql = "SELECT * FROM slots WHERE run_id = ?"
        params: list = [run["id"]]
        if date_from:
            sql += " AND slot_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND slot_date <= ?"
            params.append(date_to)
        sql += " ORDER BY slot_date, slot_time"
        return self.conn.execute(sql, params).fetchall()

    def price_history(self, center_id: str, limit: int = 50) -> list[sqlite3.Row]:
        """Tarif minimum observé à chaque relevé réussi (du plus ancien au plus récent)."""
        return self.conn.execute(
            "SELECT r.id AS run_id, r.started_at, MIN(s.price) AS min_price,"
            " MAX(s.price) AS max_price, COUNT(s.id) AS nb_slots"
            " FROM scrape_runs r JOIN slots s ON s.run_id = r.id"
            " WHERE r.center_id = ? AND r.status = 'success'"
            " GROUP BY r.id ORDER BY r.id DESC LIMIT ?",
            (center_id, limit),
        ).fetchall()[::-1]
