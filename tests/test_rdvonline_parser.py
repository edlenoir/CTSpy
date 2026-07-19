import os

from ctspy.scrapers.rdvonline import RdvOnlineScraper

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rdvonline_resultslots.html")

CENTER = {
    "id": "test-center",
    "base_url": "https://example.invalid",
    "booking_path": "/1393-Test/rendez-vous",
    "id_etab": 1393,
    "vehicle": {"id_tvehi": 11, "id_energie": 5, "id_tctrl": 99},
    "labels": {"vehicle": "Véhicule particulier", "energy": "Essence", "control": "CTP"},
}


def make_scraper() -> RdvOnlineScraper:
    return RdvOnlineScraper(CENTER, weeks_ahead=1, request_delay=0)


def test_parse_all_slots():
    with open(FIXTURE, encoding="utf-8") as f:
        slots = make_scraper().parse_slots_html(f.read())
    assert len(slots) == 5
    assert [(s.date, s.time) for s in slots] == [
        ("2026-07-27", "09:20"),
        ("2026-07-27", "10:00"),
        ("2026-07-27", "10:40"),
        ("2026-07-28", "08:40"),
        ("2026-07-28", "11:20"),
    ]


def test_prices_and_promo():
    with open(FIXTURE, encoding="utf-8") as f:
        slots = make_scraper().parse_slots_html(f.read())
    by_key = {(s.date, s.time): s for s in slots}

    standard = by_key[("2026-07-27", "09:20")]
    assert standard.price == 75.0
    assert standard.base_price is None
    assert not standard.is_promo

    promo = by_key[("2026-07-28", "08:40")]
    assert promo.price == 69.5
    assert promo.base_price == 75.0
    assert promo.is_promo
    assert promo.extra.get("data-id-promo") == "12"

    # prix absent de data-tarif -> repli sur le texte .tarifInSlot
    fallback = by_key[("2026-07-28", "11:20")]
    assert fallback.price == 75.0


def test_labels_and_agenda():
    with open(FIXTURE, encoding="utf-8") as f:
        slots = make_scraper().parse_slots_html(f.read())
    slot = slots[0]
    assert slot.center_id == "test-center"
    assert slot.vehicle_type == "Véhicule particulier"
    assert slot.energy == "Essence"
    assert slot.control_type == "CTP"
    assert slot.agenda_id == "2"


def test_empty_html():
    assert make_scraper().parse_slots_html("") == []
    assert make_scraper().parse_slots_html("<div>Aucun créneau</div>") == []
