"""Scraper des agendas Sécuritest / plateforme Genilink (agenda2.securitest.org).

Contrairement à RDV-Online, ce site est une application AngularJS : le planning
n'apparaît qu'après avoir renseigné plusieurs étapes (type de véhicule,
énergie, type de contrôle). On pilote donc un vrai navigateur (Playwright /
Chromium) qui :

  1. ouvre la page et ferme la bannière de cookies ;
  2. franchit les étapes en cliquant les libellés configurés dans
     config.yaml (clé `steps`) puis les boutons « Suivant / Continuer » ;
  3. une fois le planning affiché, récupère les créneaux de deux façons
     complémentaires :
       a. interception des réponses JSON de l'API Genilink pendant la
          navigation (le plus fiable : on y cherche récursivement des objets
          date + heure + tarif) ;
       b. à défaut, lecture heuristique du DOM (éléments dont le texte est un
          horaire, rattachés à la colonne/l'en-tête de jour la plus proche).

En cas d'échec, des artefacts de debug (capture d'écran, HTML, JSON
interceptés) sont écrits dans `debug/securitest/` pour ajuster facilement les
sélecteurs — la structure exacte des écrans peut varier selon le centre.
"""

from __future__ import annotations

import json
import os
import re
import time as time_mod
from datetime import date

from ..models import ScrapeResult, Slot, normalize_time, parse_price
from .base import BaseScraper

TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)\s*$")
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
FR_NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
FR_MONTHS = {
    "janvier": 1, "janv": 1, "février": 2, "fevrier": 2, "févr": 2, "fevr": 2,
    "mars": 3, "avril": 4, "avr": 4, "mai": 5, "juin": 6,
    "juillet": 7, "juil": 7, "août": 8, "aout": 8, "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10, "novembre": 11, "nov": 11, "décembre": 12,
    "decembre": 12, "déc": 12, "dec": 12,
}
FR_TEXT_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(" + "|".join(FR_MONTHS) + r")\.?\s*(\d{4})?", re.IGNORECASE
)

# Textes usuels de la bannière cookies / boutons d'acceptation
COOKIE_TEXTS = ["OK", "Accepter", "J'accepte", "Tout accepter", "Continuer sans accepter"]


def parse_french_date(text: str, reference: date | None = None) -> str | None:
    """Extrait une date d'un texte français et la renvoie en YYYY-MM-DD.

    Gère '2026-07-27', '27/07/2026', '27/07', 'lundi 27 juillet', 'Mar 28 juil.'.
    Sans année explicite, choisit l'année qui donne la date la plus proche dans
    le futur par rapport à `reference` (aujourd'hui par défaut).
    """
    reference = reference or date.today()

    m = ISO_DATE_RE.search(text)
    if m:
        return m.group(1)

    def resolve_year(day: int, month: int, year: int | None) -> str | None:
        try:
            if year:
                if year < 100:
                    year += 2000
                return date(year, month, day).isoformat()
            candidate = date(reference.year, month, day)
            if candidate < reference:  # un planning n'affiche que le futur
                candidate = date(reference.year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            return None

    m = FR_TEXT_DATE_RE.search(text)
    if m:
        month = FR_MONTHS[m.group(2).lower().rstrip(".")]
        parsed = resolve_year(int(m.group(1)), month, int(m.group(3)) if m.group(3) else None)
        if parsed:
            return parsed

    m = FR_NUM_DATE_RE.search(text)
    if m:
        parsed = resolve_year(int(m.group(1)), int(m.group(2)),
                              int(m.group(3)) if m.group(3) else None)
        if parsed:
            return parsed
    return None


def find_slots_in_json(payload, found: list[dict] | None = None) -> list[dict]:
    """Cherche récursivement des objets ressemblant à des créneaux dans du JSON.

    Un créneau = un dict contenant une valeur date (YYYY-MM-DD) et une valeur
    horaire (HH:MM), le tarif étant lu dans les clés usuelles s'il est présent.
    """
    if found is None:
        found = []
    if isinstance(payload, dict):
        found_date = found_time = None
        price = None
        for key, value in payload.items():
            if isinstance(value, str):
                if found_date is None:
                    m = ISO_DATE_RE.search(value)
                    if m:
                        found_date = m.group(1)
                        # la même valeur peut contenir date ET heure ('2026-07-27 09:20')
                        tm = re.search(r"\b([01]?\d|2[0-3])[h:]([0-5]\d)\b", value)
                        if tm:
                            found_time = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                        continue
                if found_time is None and TIME_RE.match(value):
                    found_time = normalize_time(value)
            if price is None and isinstance(value, (int, float, str)) \
                    and re.search(r"tarif|prix|price|montant|amount", str(key), re.I):
                price = parse_price(str(value))
        if found_date and found_time:
            found.append({"date": found_date, "time": found_time, "price": price,
                          "raw": payload})
        else:
            for value in payload.values():
                find_slots_in_json(value, found)
    elif isinstance(payload, list):
        for value in payload:
            find_slots_in_json(value, found)
    return found


# Script exécuté dans la page pour l'extraction DOM heuristique : renvoie les
# éléments-feuilles visibles dont le texte est un horaire, avec le texte de
# l'en-tête de colonne/jour le plus proche et un éventuel prix ambiant.
DOM_EXTRACT_JS = """
() => {
    const timeRe = /^\\s*([01]?\\d|2[0-3])\\s*[h:]\\s*[0-5]\\d\\s*$/;
    const results = [];
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    for (const el of document.querySelectorAll('body *')) {
        if (el.children.length > 0) continue;
        const text = (el.textContent || '').trim();
        if (!timeRe.test(text) || !isVisible(el)) continue;

        let dayText = '', priceText = '', node = el;
        for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
            if (!priceText) {
                const m = (node.textContent || '').match(/\\d+[.,]?\\d*\\s*€/);
                if (m) priceText = m[0];
            }
            for (const attr of ['data-date', 'data-jour', 'data-day', 'date']) {
                const v = node.getAttribute && node.getAttribute(attr);
                if (v) { dayText = v; break; }
            }
            if (dayText) break;
            const header = node.querySelector &&
                node.querySelector('.day-header, .jour, .date, th, header, h1, h2, h3, h4');
            if (header && header.textContent.trim() && !timeRe.test(header.textContent.trim())) {
                dayText = header.textContent.trim();
                break;
            }
        }
        results.push({ time: text, dayText, priceText });
    }
    return results;
}
"""


class SecuritestScraper(BaseScraper):
    def __init__(self, center: dict, *, headless: bool = True,
                 debug_dir: str | None = None, **kwargs):
        super().__init__(center, **kwargs)
        self.url: str = center["url"]
        self.steps: list[list[str]] = [list(s) for s in center.get("steps", [])]
        self.next_texts: list[str] = list(center.get("next_button_texts",
                                                     ["Suivant", "Continuer", "Valider"]))
        self.headless = headless
        self.debug_dir = debug_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "debug", "securitest",
        )

    # ------------------------------------------------------------ navigation

    def _click_first_matching(self, page, texts: list[str], timeout_ms: int = 4000) -> bool:
        for text in texts:
            try:
                locator = page.get_by_text(text, exact=False).first
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.click(timeout=timeout_ms)
                return True
            except Exception:
                continue
        return False

    def _dismiss_cookie_banner(self, page) -> None:
        try:
            banner = page.locator("#cookie-banner")
            if banner.count() > 0:
                close = banner.locator(".close")
                if close.count() > 0:
                    close.first.click(timeout=2000)
                    return
        except Exception:
            pass
        self._click_first_matching(page, COOKIE_TEXTS, timeout_ms=1500)

    def _save_debug(self, page, captured: list[dict], reason: str) -> str:
        os.makedirs(self.debug_dir, exist_ok=True)
        stamp = time_mod.strftime("%Y%m%d-%H%M%S")
        prefix = os.path.join(self.debug_dir, f"{self.center_id}-{stamp}")
        try:
            page.screenshot(path=f"{prefix}.png", full_page=True)
            with open(f"{prefix}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass
        with open(f"{prefix}-api.json", "w", encoding="utf-8") as f:
            json.dump({"reason": reason, "responses": captured}, f,
                      ensure_ascii=False, indent=2, default=str)
        return prefix

    # ---------------------------------------------------------------- scrape

    def scrape(self) -> ScrapeResult:
        from playwright.sync_api import sync_playwright

        result = ScrapeResult(center_id=self.center_id, slots=[])
        captured: list[dict] = []

        launch_kwargs: dict = {"headless": self.headless}
        # Permet d'utiliser un Chromium déjà installé (ex. CTSPY_CHROMIUM=/usr/bin/chromium)
        # au lieu de celui téléchargé par `playwright install chromium`.
        if os.environ.get("CTSPY_CHROMIUM"):
            launch_kwargs["executable_path"] = os.environ["CTSPY_CHROMIUM"]

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(locale="fr-FR")
            page = context.new_page()

            def on_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if "json" not in ctype:
                        return
                    body = response.json()
                    captured.append({"url": response.url, "body": body})
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                self._dismiss_cookie_banner(page)

                for step_texts in self.steps:
                    clicked = self._click_first_matching(page, step_texts)
                    if not clicked:
                        result.errors.append(
                            f"Étape introuvable (aucun libellé cliquable parmi {step_texts})"
                        )
                    page.wait_for_timeout(1500)
                    # certains écrans demandent une confirmation explicite
                    self._click_first_matching(page, self.next_texts, timeout_ms=1500)
                    page.wait_for_timeout(1500)

                # laisse le temps au planning et à ses appels API de se charger
                page.wait_for_timeout(4000)

                slots = self._extract_from_captured_json(captured)
                source = "api"
                if not slots:
                    slots = self._extract_from_dom(page)
                    source = "dom"

                if slots:
                    for slot in slots:
                        slot.extra["source"] = source
                    result.slots = sorted(slots, key=lambda s: (s.date, s.time))
                else:
                    prefix = self._save_debug(page, captured, "aucun créneau détecté")
                    result.errors.append(
                        "Aucun créneau détecté (parcours incomplet ou sélecteurs à "
                        f"ajuster). Artefacts de debug : {prefix}*"
                    )
            except Exception as exc:
                prefix = self._save_debug(page, captured, str(exc))
                result.errors.append(f"Échec navigation Playwright : {exc}. "
                                     f"Artefacts de debug : {prefix}*")
            finally:
                browser.close()

        prices = [s.price for s in result.slots if s.price is not None]
        result.min_price = min(prices) if prices else None
        result.max_price = max(prices) if prices else None
        return result

    # ----------------------------------------------------------- extraction

    def _make_slot(self, day: str, hour: str, price: float | None,
                   extra: dict | None = None) -> Slot:
        return Slot(
            center_id=self.center_id,
            date=day,
            time=hour,
            price=price,
            control_type=self.labels.get("control", ""),
            vehicle_type=self.labels.get("vehicle", ""),
            energy=self.labels.get("energy", ""),
            extra=extra or {},
        )

    def _extract_from_captured_json(self, captured: list[dict]) -> list[Slot]:
        slots: dict[tuple[str, str], Slot] = {}
        today = date.today().isoformat()
        horizon = date.today().toordinal() + self.weeks_ahead * 7
        for response in captured:
            for item in find_slots_in_json(response.get("body")):
                if item["date"] < today:
                    continue
                if date.fromisoformat(item["date"]).toordinal() > horizon:
                    continue
                key = (item["date"], item["time"])
                if key not in slots or (slots[key].price is None and item["price"] is not None):
                    slots[key] = self._make_slot(
                        item["date"], item["time"], item["price"],
                        extra={"api_url": response.get("url", "")},
                    )
        return list(slots.values())

    def _extract_from_dom(self, page) -> list[Slot]:
        try:
            raw_items = page.evaluate(DOM_EXTRACT_JS)
        except Exception:
            return []
        slots: dict[tuple[str, str], Slot] = {}
        for item in raw_items:
            day = parse_french_date(item.get("dayText", "") or "")
            if not day:
                continue
            hour = normalize_time(item["time"])
            price = parse_price(item.get("priceText"))
            slots.setdefault((day, hour), self._make_slot(day, hour, price))
        return list(slots.values())
