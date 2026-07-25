# CTSpy — suivi des créneaux et tarifs de contrôle technique

CTSpy collecte automatiquement les **créneaux de rendez-vous disponibles et
leurs tarifs** sur des sites de prise de rendez-vous de contrôle technique,
les stocke dans une base **SQLite** (avec historique), et les présente via
une **CLI** et un **dashboard web**.

Deux familles de sites sont prises en charge :

| Scraper      | Plateforme                              | Exemple                     | Technique |
|--------------|------------------------------------------|-----------------------------|-----------|
| `rdvonline`  | RDV-Online / Karoil (control-v11.fr, …)  | CONTROL'V Villemoustaussou  | Appels HTTP directs à l'endpoint AJAX `Horaires.php` (rapide, sans navigateur) |
| `securitest` | Genilink (agenda2.securitest.org)        | AAB Carcassonne             | Appels directs à l'API JSON `/rdv/api/*` (repli Playwright si l'API évolue) |

## Installation

```bash
pip install -r requirements.txt
playwright install chromium        # uniquement pour les centres Securitest
```

> Si vous avez déjà un Chromium installé, vous pouvez éviter le téléchargement
> avec `export CTSPY_CHROMIUM=/chemin/vers/chromium`.

## Utilisation

```bash
# Lister les centres configurés
python -m ctspy centers

# Collecter les créneaux de tous les centres (fenêtre : weeks_ahead semaines)
python -m ctspy scrape

# Un seul centre, 2 semaines, navigateur visible (debug Playwright)
python -m ctspy scrape -c securitest-carcassonne-aab -w 2 --headful

# Afficher les créneaux du dernier relevé
python -m ctspy slots
python -m ctspy slots -c control-v11-villemoustaussou --date-from 2026-07-27

# Historique des relevés
python -m ctspy runs

# Dashboard web sur http://127.0.0.1:5000
python -m ctspy serve
```

Chaque exécution de `scrape` crée un **relevé** (run) horodaté ; les créneaux
sont rattachés au relevé, ce qui permet de suivre l'évolution des
disponibilités et des prix dans le temps (visible dans « Historique des
relevés » du dashboard, ou via `price_history` en SQL).

Le dashboard expose aussi une petite API JSON :
`GET /api/centers` et `GET /api/slots?center=<id>[&date_from=…&date_to=…]`.

## Configuration (`config.yaml`)

Options globales :

- `weeks_ahead` : nombre de semaines scannées à partir d'aujourd'hui (défaut 4) ;
- `request_delay` : délai de politesse entre deux requêtes vers un même site ;
- `database` : chemin de la base SQLite.

### Ajouter un centre RDV-Online (`scraper: rdvonline`)

Ces sites chargent le planning par un `POST scripts/genereDisplay/Horaires.php`
qui renvoie du JSON (fragment HTML de la grille + tarif minimum). Les
identifiants nécessaires se lisent dans le code source de la page de prise de
rendez-vous :

- `id_etab` : dans le JS de la page (`idEtab = 1393`) — c'est aussi le nombre
  au début de l'URL (`/1393-Villemoustaussou/rendez-vous`) ;
- `vehicle.id_tvehi` : type de véhicule (`11` = véhicule particulier) ;
- `vehicle.id_energie` : `data-id` des `<li>` du bloc « Choisir l'énergie »
  (5 = Essence, 6 = Diesel, 7 = GPL, 8 = Hybride/Électricité) ;
- `vehicle.id_tctrl` : `data-id` du bloc « Choisir un type de contrôle »
  (99 = CTP, 100 = visite complémentaire pollution sur ce centre).

Tout autre centre de la même plateforme (même si le nom de domaine diffère)
s'ajoute en copiant le bloc et en adaptant `base_url`, `booking_path` et les ids.

Détails d'implémentation : chaque créneau du fragment HTML est un élément
`.hourSlotsItem` portant `data-jour`, `data-horaire`, `data-tarif`,
`data-agenda`, et `data-tarif-base`/`data-id-promo` en cas de promotion — le
parseur (`ctspy/scrapers/rdvonline.py`) lit directement ces attributs. Quand
une semaine est vide, la réponse contient `prochaineDispo` et le scraper saute
directement à cette date.

### Ajouter un centre Securitest / Genilink (`scraper: securitest`)

- `url` : l'URL complète de l'agenda (avec `?origine=…&c=…`) ;
- `code_centre` : le code interne du centre, visible dans le HTML de la page
  (`.constant('codeCentre', 'S01102702')`) ;
- `api.type_rdv`, `api.type_veh`, `api.carb_veh` : identifiants du profil.

**Mode API (par défaut, validé en conditions réelles)** : le scraper charge la
page une fois (le serveur associe le centre à la session PHP), puis interroge
directement l'API JSON du site :

- `GET /rdv/api/config` → configuration du centre et liste `rdv_types`
  (1 = CTP contrôle technique périodique) ;
- `GET /rdv/api/types?typeId=1` → véhicules (1=VP, 2=VU, 7=Moto…) et
  carburants (1=Essence, 2=Diesel, 3=Gaz, 4=Hybride, 5=Electrique) ;
- `GET /rdv/api/calendar?dateFrom=…&dateTo=…&…&codeCentre=…` → jours ouverts
  et tarif du jour, par fenêtres de 27 jours ;
- `GET /rdv/api/creneau?date=…&…` → horaires du jour avec `price`,
  `final_price` et promotion éventuelle (ex. remise paiement en ligne `pel`).

Pour découvrir les identifiants d'un profil différent, appelez ces deux
premiers endpoints dans un navigateur après avoir ouvert la page de l'agenda.

**Mode navigateur (repli)** : si l'API échoue (évolution du site), le scraper
bascule sur Playwright et rejoue le parcours humain — `steps` liste les
libellés à cliquer à chaque étape, `next_button_texts` les boutons de
validation. Les échecs produisent des **artefacts de debug** (capture d'écran,
HTML, JSON interceptés) dans `debug/securitest/`, et `--headful` permet
d'observer le parcours en direct. Ce repli se désactive avec
`browser_fallback: false`.

## Planification (relevés réguliers)

Exemple de crontab pour un relevé toutes les 2 heures :

```cron
0 */2 * * * cd /chemin/vers/CTSpy && /usr/bin/python3 -m ctspy scrape >> scrape.log 2>&1
```

## Tests

```bash
python -m pytest tests/
```

Les tests valident le parseur RDV-Online sur une fixture HTML reproduisant la
structure réelle de `resultSlots`, les utilitaires de dates/prix, l'extraction
de créneaux depuis du JSON arbitraire (Securitest) et la couche SQLite.

## Bonnes pratiques

- `request_delay` impose une pause entre les requêtes : gardez une valeur
  raisonnable (≥ 1 s) pour ne pas charger les sites visés.
- Les données collectées sont des disponibilités publiques affichées à tout
  visiteur ; l'outil ne réserve rien et ne contourne aucune authentification.

## Architecture

```
ctspy/
├── config.py            # chargement de config.yaml
├── models.py            # Slot, ScrapeResult, parse_price, normalize_time
├── db.py                # SQLite : scrape_runs + slots, historique des prix
├── cli.py               # commandes centers / scrape / slots / runs / serve
├── scrapers/
│   ├── base.py          # interface commune
│   ├── rdvonline.py     # plateforme RDV-Online (HTTP direct)
│   └── securitest.py    # plateforme Genilink (Playwright)
└── web/
    ├── app.py           # Flask + API JSON
    └── templates/index.html
```
