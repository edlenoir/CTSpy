"""Scraper des sites de la plateforme RDV-Online (Karoil) — ex. control-v11.fr.

Le planning n'est pas dans le HTML de la page : il est chargé en AJAX par un
POST vers `scripts/genereDisplay/Horaires.php`, qui renvoie du JSON contenant
notamment :
  - resultSlots      : fragment HTML de la grille de la semaine demandée
  - tarifAPartirDe   : tarif minimum de la semaine
  - isEmpty          : true si aucun créneau
  - prochaineDispo   : date du prochain créneau disponible quand la semaine est vide

Chaque créneau réservable du fragment HTML est un élément `.hourSlotsItem`
portant des attributs data-* auto-suffisants :
  data-jour="YYYY-MM-DD", data-horaire="HH:MM", data-tarif="75",
  data-agenda, data-tarif-base / data-id-promo(-pel) en cas de promotion.

On appelle donc l'endpoint directement, semaine par semaine, sans navigateur.
"""

from __future__ import annotations

import time as time_mod
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ..models import ScrapeResult, Slot, normalize_time, parse_price
from .base import BaseScraper

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class RdvOnlineScraper(BaseScraper):
    def __init__(self, center: dict, **kwargs):
        super().__init__(center, **kwargs)
        self.base_url: str = center["base_url"].rstrip("/")
        self.booking_path: str = center.get("booking_path", "/")
        self.id_etab = str(center["id_etab"])
        vehicle = center.get("vehicle", {})
        self.id_tvehi = str(vehicle.get("id_tvehi", 11))
        self.id_energie = str(vehicle.get("id_energie", 5))
        self.id_tctrl = str(vehicle.get("id_tctrl", 99))
        self.id_pref_tcli = str(vehicle.get("id_pref_tcli", 5))

    # ------------------------------------------------------------------ http

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url + self.booking_path,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        return session

    def _fetch_week(self, session: requests.Session, selected_date: date) -> dict:
        data = {
            "selectedDate": selected_date.strftime("%Y-%m-%d"),
            "idEtab": self.id_etab,
            "idClient": "",
            "idPrefTCli": self.id_pref_tcli,
            "idTVTE[0][idTVehi]": self.id_tvehi,
            "idTVTE[0][idTEnergie]": self.id_energie,
            "idTCtrl": self.id_tctrl,
            "promoUrl": "",
        }
        resp = session.post(
            f"{self.base_url}/scripts/genereDisplay/Horaires.php",
            data=data,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # --------------------------------------------------------------- parsing

    def parse_slots_html(self, html: str) -> list[Slot]:
        """Extrait les créneaux d'un fragment `resultSlots`."""
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(".hourSlotsItem[data-jour]")
        if not items:
            # filet de sécurité si les classes évoluent : tout élément portant
            # à la fois un jour et un horaire est un créneau
            items = soup.select("[data-jour][data-horaire]")

        slots = []
        for item in items:
            day = (item.get("data-jour") or "").strip()
            hour = (item.get("data-horaire") or "").strip()
            if not day or not hour:
                continue
            price = parse_price(item.get("data-tarif"))
            if price is None:
                tarif_el = item.select_one(".tarifInSlot")
                if tarif_el:
                    price = parse_price(tarif_el.get_text())
            base_price = parse_price(item.get("data-tarif-base"))
            extra = {}
            for attr in ("data-id-promo", "data-id-promo-pel", "data-pel-valeur",
                         "data-force-pel-valeur", "data-idconf"):
                if item.get(attr):
                    extra[attr] = item.get(attr)
            slots.append(Slot(
                center_id=self.center_id,
                date=day,
                time=normalize_time(hour),
                price=price,
                base_price=base_price,
                agenda_id=(item.get("data-agenda") or "").strip(),
                control_type=self.labels.get("control", ""),
                vehicle_type=self.labels.get("vehicle", ""),
                energy=self.labels.get("energy", ""),
                extra=extra,
            ))
        return slots

    # ---------------------------------------------------------------- scrape

    def scrape(self) -> ScrapeResult:
        result = ScrapeResult(center_id=self.center_id, slots=[])
        session = self._make_session()

        # Visite de la page publique : récupère les cookies de session PHP
        try:
            session.get(self.base_url + self.booking_path, timeout=30)
        except requests.RequestException as exc:
            result.errors.append(f"Page de RDV inaccessible : {exc}")

        seen: set[tuple[str, str, str]] = set()
        current = date.today()
        end_limit = date.today() + timedelta(weeks=self.weeks_ahead)
        min_prices: list[float] = []

        for _ in range(self.weeks_ahead * 2):  # borne dure contre les boucles
            if current >= end_limit:
                break
            try:
                payload = self._fetch_week(session, current)
            except (requests.RequestException, ValueError) as exc:
                result.errors.append(f"Semaine du {current}: {exc}")
                current += timedelta(weeks=1)
                time_mod.sleep(self.request_delay)
                continue

            if payload.get("reponse") != "Succes":
                result.errors.append(
                    f"Semaine du {current}: réponse inattendue "
                    f"({payload.get('reponse')!r}: {payload.get('message')!r})"
                )
                current += timedelta(weeks=1)
                time_mod.sleep(self.request_delay)
                continue

            if payload.get("isEmpty"):
                # Semaine vide : le site indique parfois la prochaine dispo,
                # on saute directement à cette date si elle est dans la fenêtre.
                prochaine = payload.get("prochaineDispo") or {}
                next_date = prochaine.get("date") if isinstance(prochaine, dict) else None
                if next_date:
                    try:
                        jump = date.fromisoformat(str(next_date)[:10])
                        if current < jump < end_limit:
                            current = jump
                            time_mod.sleep(self.request_delay)
                            continue
                    except ValueError:
                        pass
                current += timedelta(weeks=1)
                time_mod.sleep(self.request_delay)
                continue

            tarif_min = parse_price(str(payload.get("tarifAPartirDe", "")))
            if tarif_min is not None:
                min_prices.append(tarif_min)

            for slot in self.parse_slots_html(payload.get("resultSlots", "")):
                key = (slot.date, slot.time, slot.agenda_id)
                if key in seen:
                    continue
                seen.add(key)
                result.slots.append(slot)

            current += timedelta(weeks=1)
            time_mod.sleep(self.request_delay)

        prices = [s.price for s in result.slots if s.price is not None]
        result.min_price = min(prices) if prices else (min(min_prices) if min_prices else None)
        result.max_price = max(prices) if prices else None
        result.slots.sort(key=lambda s: (s.date, s.time))
        return result
