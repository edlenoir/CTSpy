import os
import tempfile

from ctspy.db import Database
from ctspy.models import Slot, normalize_time, parse_price


def test_parse_price():
    assert parse_price("75") == 75.0
    assert parse_price("75,50") == 75.5
    assert parse_price("75.50 €") == 75.5
    assert parse_price(" 69,90 € ") == 69.9
    assert parse_price("") is None
    assert parse_price(None) is None
    assert parse_price("N.C.") is None


def test_normalize_time():
    assert normalize_time("9h20") == "09:20"
    assert normalize_time("09:20") == "09:20"
    assert normalize_time("17h5") == "17:05"
    assert normalize_time("8h") == "08:00"


def make_slot(**kwargs) -> Slot:
    defaults = dict(center_id="c1", date="2026-07-27", time="09:20", price=75.0)
    defaults.update(kwargs)
    return Slot(**defaults)


def test_db_run_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        run_id = db.start_run("c1")
        db.insert_slots(run_id, [make_slot(), make_slot(time="10:00", price=69.5, base_price=75.0)])
        db.finish_run(run_id, "success", 2)

        rows = db.latest_slots("c1")
        assert len(rows) == 2
        assert rows[0]["slot_time"] == "09:20"
        assert rows[1]["price"] == 69.5
        assert rows[1]["base_price"] == 75.0

        # un nouveau relevé réussi remplace le précédent dans latest_slots
        run2 = db.start_run("c1")
        db.insert_slots(run2, [make_slot(time="14:00")])
        db.finish_run(run2, "success", 1)
        rows = db.latest_slots("c1")
        assert len(rows) == 1
        assert rows[0]["slot_time"] == "14:00"

        # un relevé en erreur n'écrase pas le dernier relevé réussi
        run3 = db.start_run("c1")
        db.finish_run(run3, "error", 0, error="boom")
        assert len(db.latest_slots("c1")) == 1

        history = db.price_history("c1")
        assert [h["min_price"] for h in history] == [69.5, 75.0]
        db.close()


def test_db_filters_by_date():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        run_id = db.start_run("c1")
        db.insert_slots(run_id, [
            make_slot(date="2026-07-27"),
            make_slot(date="2026-07-28"),
            make_slot(date="2026-08-03"),
        ])
        db.finish_run(run_id, "success", 3)
        rows = db.latest_slots("c1", date_from="2026-07-28", date_to="2026-07-31")
        assert [r["slot_date"] for r in rows] == ["2026-07-28"]
        db.close()
