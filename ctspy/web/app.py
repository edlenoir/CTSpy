"""Dashboard web Flask : vue des créneaux collectés, centre par centre."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date

from flask import Flask, abort, jsonify, render_template, request

from ..config import Config
from ..db import Database


def _center_view(db: Database, center: dict) -> dict:
    """Prépare les données d'un centre pour le template."""
    run = db.latest_successful_run(center["id"])
    rows = db.latest_slots(center["id"])

    days: "OrderedDict[str, list[dict]]" = OrderedDict()
    prices = []
    for row in rows:
        slot = {
            "time": row["slot_time"],
            "price": row["price"],
            "base_price": row["base_price"],
            "is_promo": row["base_price"] is not None and row["price"] is not None
                        and row["price"] < row["base_price"],
        }
        days.setdefault(row["slot_date"], []).append(slot)
        if row["price"] is not None:
            prices.append(row["price"])

    history = [dict(h) for h in db.price_history(center["id"], limit=30)]
    return {
        "center": center,
        "run": dict(run) if run else None,
        "days": days,
        "nb_slots": len(rows),
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "history": history,
    }


FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
             "août", "sept.", "oct.", "nov.", "déc."]


def create_app(config: Config) -> Flask:
    app = Flask(__name__)

    @app.template_filter("fr_date")
    def fr_date(value: str) -> str:
        """'2026-07-27' -> 'lundi 27 juil.'"""
        try:
            d = date.fromisoformat(value)
        except (TypeError, ValueError):
            return value
        return f"{FR_DAYS[d.weekday()]} {d.day:02d} {FR_MONTHS[d.month - 1]}"

    def open_db() -> Database:
        return Database(config.database)

    @app.route("/")
    def index():
        db = open_db()
        try:
            views = [_center_view(db, center) for center in config.centers]
        finally:
            db.close()
        return render_template("index.html", views=views)

    @app.route("/api/centers")
    def api_centers():
        return jsonify([
            {"id": c["id"], "name": c.get("name"), "city": c.get("city"),
             "scraper": c.get("scraper")}
            for c in config.centers
        ])

    @app.route("/api/slots")
    def api_slots():
        center_id = request.args.get("center")
        if not center_id:
            abort(400, "paramètre 'center' requis")
        try:
            config.center(center_id)
        except KeyError:
            abort(404, f"centre inconnu : {center_id}")
        db = open_db()
        try:
            rows = db.latest_slots(center_id,
                                   request.args.get("date_from"),
                                   request.args.get("date_to"))
            run = db.latest_successful_run(center_id)
        finally:
            db.close()
        return jsonify({
            "center": center_id,
            "scraped_at": run["started_at"] if run else None,
            "slots": [
                {
                    "date": r["slot_date"], "time": r["slot_time"],
                    "price": r["price"], "base_price": r["base_price"],
                    "control_type": r["control_type"],
                    "vehicle_type": r["vehicle_type"], "energy": r["energy"],
                }
                for r in rows
            ],
        })

    return app
