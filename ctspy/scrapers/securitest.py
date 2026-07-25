"""Scraper des agendas Sécuritest / plateforme Genilink (agenda2.securitest.org).

Ce site est une application AngularJS, mais son API JSON est appelable
directement (vérifié en conditions réelles) — c'est le **mode API**, utilisé
en premier :

  1. GET de la page d'accueil (`?origine=affilie&c=...`) : le serveur associe
     le centre à la session PHP (cookie PHPSESSID) ;
  2. GET `/rdv/api/calendar?dateFrom=..&dateTo=..&typeRdv=..&typeVeh=..
     &carbVeh=..&codeCentre=..` : liste des jours ouverts avec tarif du jour ;
  3. GET `/rdv/api/creneau?date=..&...` pour chaque jour ouvert : les horaires
     avec `price` / `final_price` / `promo` (ex. remise paiement en ligne).

Identifiants utiles (endpoint `/rdv/api/types`) : typeVeh 1=VP, 2=VU... ;
carbVeh 1=Essence, 2=Diesel, 3=Gaz, 4=Hybride, 5=Electrique ;
typeRdv 1=CTP (contrôle périodique), cf. `rdv_types` de `/rdv/api/config`.

En cas d'échec de l'API (évolution du site), un **mode navigateur** de repli
pilote Chromium via Playwright : il franchit les étapes en cliquant les
libellés configurés (`steps` dans config.yaml) puis récupère les créneaux en
interceptant les réponses JSON, ou à défaut par lecture heuristique du DOM.
Les échecs du mode navigateur produisent des artefacts de debug (capture
d'écran, HTML, JSON interceptés) dans `debug/securitest/`.
"""

from __future__ import annotations

import json
import os
import re
import time as time_mod
from datetime import date, timedelta
from urllib.parse import urlsplit

import requests

from ..models import ScrapeResult, Slot, normalize_time, parse_price
from .base import BaseScraper

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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
        self.code_centre: str = str(center.get("code_centre", ""))
        api = center.get("api", {})
        self.type_rdv = str(api.get("type_rdv", 1))    # 1 = CTP
        self.type_veh = str(api.get("type_veh", 1))    # 1 = Véhicule particulier
        self.carb_veh = str(api.get("carb_veh", 1))    # 1 = Essence
        self.browser_fallback: bool = bool(center.get("browser_fallback", True))
        self.headless = headless
        self.debug_dir = debug_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "debug", "securitest",
        )

    # ------------------------------------------------------------- mode API

    @property
    def _api_base(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    def _make_api_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        # Le GET de la page associe le code centre (?c=...) à la session PHP :
        # sans ce passage, l'API répond « Aucun centre ne correspond ».
        session.get(self.url, timeout=30)
        session.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.url,
            "Accept": "application/json, text/plain, */*",
        })
        return session

    def _api_params(self, **extra) -> dict:
        params = {
            "typeRdv": self.type_rdv,
            "typeVeh": self.type_veh,
            "carbVeh": self.carb_veh,
            "codePromo": "",
            "codeCentre": self.code_centre,
        }
        params.update(extra)
        return params

    def _scrape_api(self, result: ScrapeResult) -> None:
        session = self._make_api_session()
        today = date.today()
        horizon = today + timedelta(weeks=self.weeks_ahead)

        # Jours ouverts : le calendrier se demande par fenêtres de 27 jours
        # (même découpage que l'application officielle).
        open_days: list[dict] = []
        window_start = today - timedelta(days=today.weekday())  # lundi courant
        while window_start <= horizon:
            resp = session.get(
                f"{self._api_base}/rdv/api/calendar",
                params=self._api_params(
                    dateFrom=window_start.isoformat(),
                    dateTo=(window_start + timedelta(days=26)).isoformat(),
                ),
                timeout=30,
            )
            resp.raise_for_status()
            weeks = resp.json()
            if not isinstance(weeks, list):
                raise ValueError(f"réponse calendar inattendue : {str(weeks)[:200]}")
            for week in weeks:
                for day in week.get("days", []):
                    if day.get("disabled") or not day.get("display_web", True):
                        continue
                    day_date = day.get("date", "")
                    if not day_date or day_date < today.isoformat() \
                            or day_date > horizon.isoformat():
                        continue
                    open_days.append(day)
            window_start += timedelta(days=27)
            time_mod.sleep(self.request_delay)

        # Horaires de chaque jour ouvert
        seen: set[str] = set()
        for day in open_days:
            day_date = day["date"]
            if day_date in seen:
                continue
            seen.add(day_date)
            resp = session.get(
                f"{self._api_base}/rdv/api/creneau",
                params=self._api_params(date=day_date),
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json() or []:
                slot = self._slot_from_api(day_date, item)
                if slot:
                    result.slots.append(slot)
            time_mod.sleep(self.request_delay)

        result.slots.sort(key=lambda s: (s.date, s.time))

    def _slot_from_api(self, day_date: str, item: dict) -> Slot | None:
        """Convertit une entrée de /rdv/api/creneau en Slot."""
        hour = str(item.get("hour") or item.get("heure_from") or "").strip()
        if not hour:
            return None
        price = item.get("final_price", item.get("price"))
        price = float(price) if price is not None else None
        base_price = None
        promo = item.get("promo")
        if isinstance(promo, dict) and promo.get("start_price") is not None:
            base_price = float(promo["start_price"])
        extra = {"source": "api"}
        if item.get("controleur_id"):
            extra["controleur_id"] = str(item["controleur_id"])
        if item.get("heure_to"):
            extra["heure_to"] = normalize_time(str(item["heure_to"])[:5])
        if isinstance(promo, dict) and promo.get("promo_type"):
            extra["promo_type"] = promo["promo_type"]
        return self._make_slot(day_date, normalize_time(hour[:5]), price, extra=extra,
                               base_price=base_price,
                               agenda_id=str(item.get("controleur_id", "")))

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
        result = ScrapeResult(center_id=self.center_id, slots=[])

        try:
            self._scrape_api(result)
        except (requests.RequestException, ValueError, KeyError) as exc:
            result.errors.append(f"Mode API en échec ({exc}), "
                                 "bascule sur le mode navigateur.")

        if not result.slots and self.browser_fallback:
            self._scrape_browser(result)

        prices = [s.price for s in result.slots if s.price is not None]
        result.min_price = min(prices) if prices else None
        result.max_price = max(prices) if prices else None
        return result

    # ------------------------------------------------------ mode navigateur

    def _scrape_browser(self, result: ScrapeResult) -> None:
        from playwright.sync_api import sync_playwright

        captured: list[dict] = []

        launch_kwargs: dict = {"headless": self.headless}
        # Permet d'utiliser un Chromium déjà installé (ex. CTSPY_CHROMIUM=/usr/bin/chromium)
        # au lieu de celui téléchargé par `playwright install chromium`.
        if os.environ.get("CTSPY_CHROMIUM"):
            launch_kwargs["executable_path"] = os.environ["CTSPY_CHROMIUM"]
        # Chromium ignore les variables d'environnement proxy : on les relaie
        # explicitement (réseaux d'entreprise, sandboxes...).
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

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
                        slot.extra["source"] = "browser-" + source
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

    # ----------------------------------------------------------- extraction

    def _make_slot(self, day: str, hour: str, price: float | None,
                   extra: dict | None = None, base_price: float | None = None,
                   agenda_id: str = "") -> Slot:
        return Slot(
            center_id=self.center_id,
            date=day,
            time=hour,
            price=price,
            base_price=base_price,
            agenda_id=agenda_id,
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
