from datetime import date

from ctspy.scrapers.securitest import find_slots_in_json, parse_french_date

REF = date(2026, 7, 19)


def test_parse_french_date_formats():
    assert parse_french_date("2026-07-27", REF) == "2026-07-27"
    assert parse_french_date("le 27/07/2026 c'est bien", REF) == "2026-07-27"
    assert parse_french_date("lundi 27 juillet", REF) == "2026-07-27"
    assert parse_french_date("Mar 28 juil.", REF) == "2026-07-28"
    assert parse_french_date("Lundi 03 août", REF) == "2026-08-03"
    assert parse_french_date("1er février", REF) == "2027-02-01"  # passé -> année suivante
    assert parse_french_date("aucune date ici", REF) is None


def test_find_slots_in_json_flat():
    payload = {
        "creneaux": [
            {"date": "2026-07-27", "heure": "09:20", "tarif": "75.00"},
            {"date": "2026-07-27", "heure": "10h40", "tarif": 69.5},
        ]
    }
    slots = find_slots_in_json(payload)
    assert [(s["date"], s["time"], s["price"]) for s in slots] == [
        ("2026-07-27", "09:20", 75.0),
        ("2026-07-27", "10:40", 69.5),
    ]


def test_find_slots_in_json_nested_and_combined_datetime():
    payload = {
        "data": {
            "jours": [
                {
                    "libelle": "lundi",
                    "disponibilites": [
                        {"dateHeure": "2026-07-28 08:40", "prixTTC": "69,50"},
                    ],
                }
            ]
        }
    }
    slots = find_slots_in_json(payload)
    assert len(slots) == 1
    assert slots[0]["date"] == "2026-07-28"
    assert slots[0]["time"] == "08:40"
    assert slots[0]["price"] == 69.5


def test_find_slots_in_json_ignores_non_slots():
    payload = {"config": {"version": "3.9"}, "centre": {"nom": "AAB", "cp": "11000"}}
    assert find_slots_in_json(payload) == []


def make_api_scraper():
    from ctspy.scrapers.securitest import SecuritestScraper
    center = {
        "id": "test-sec", "url": "https://agenda2.example.invalid/rdv/?c=X",
        "code_centre": "S000", "api": {"type_rdv": 1, "type_veh": 1, "carb_veh": 1},
        "labels": {"vehicle": "Véhicule particulier", "energy": "Essence", "control": "CTP"},
    }
    return SecuritestScraper(center, weeks_ahead=1, request_delay=0)


def test_slot_from_api_with_promo():
    scraper = make_api_scraper()
    item = {"controleur_id": "7465", "heure_from": "08:00:00", "heure_to": "08:30:00",
            "price": 85, "final_price": 80, "hour": "08:00:00",
            "promo": {"value": 5, "type": "€", "promo_type": "pel", "start_price": 85}}
    slot = scraper._slot_from_api("2026-07-21", item)
    assert slot.date == "2026-07-21"
    assert slot.time == "08:00"
    assert slot.price == 80.0
    assert slot.base_price == 85.0
    assert slot.is_promo
    assert slot.agenda_id == "7465"
    assert slot.extra["promo_type"] == "pel"
    assert slot.extra["heure_to"] == "08:30"


def test_slot_from_api_without_promo():
    scraper = make_api_scraper()
    slot = scraper._slot_from_api("2026-07-21", {"hour": "14:45:00", "price": 85})
    assert slot.time == "14:45"
    assert slot.price == 85.0
    assert slot.base_price is None
    assert not slot.is_promo


def test_slot_from_api_invalid():
    scraper = make_api_scraper()
    assert scraper._slot_from_api("2026-07-21", {"price": 85}) is None
