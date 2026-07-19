"""Interface en ligne de commande de CTSpy.

Exemples :
    python -m ctspy centers
    python -m ctspy scrape                     # tous les centres
    python -m ctspy scrape -c control-v11-villemoustaussou -w 2
    python -m ctspy slots -c control-v11-villemoustaussou
    python -m ctspy runs
    python -m ctspy serve --port 5000
"""

from __future__ import annotations

import click

from .config import load_config
from .db import Database
from .scrapers import get_scraper


@click.group()
@click.option("--config", "config_path", default=None,
              help="Chemin du fichier config.yaml (défaut : celui du projet).")
@click.pass_context
def main(ctx, config_path):
    """CTSpy — suivi des créneaux et tarifs de contrôle technique."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@main.command()
@click.pass_context
def centers(ctx):
    """Liste les centres configurés."""
    cfg = ctx.obj["config"]
    for center in cfg.centers:
        click.echo(f"{center['id']:40s} {center.get('name', ''):25s} "
                   f"{center.get('city', ''):20s} [{center.get('scraper')}]")


@main.command()
@click.option("-c", "--center", "center_id", default=None,
              help="Ne scraper qu'un centre (id de config.yaml).")
@click.option("-w", "--weeks", type=int, default=None,
              help="Nombre de semaines à couvrir (défaut : config.yaml).")
@click.option("--headful", is_flag=True,
              help="Affiche le navigateur pour les scrapers Playwright (debug).")
@click.pass_context
def scrape(ctx, center_id, weeks, headful):
    """Collecte les créneaux et les enregistre dans la base."""
    cfg = ctx.obj["config"]
    weeks = weeks or cfg.weeks_ahead
    targets = [cfg.center(center_id)] if center_id else cfg.centers
    if not targets:
        raise click.ClickException("Aucun centre configuré dans config.yaml.")

    db = Database(cfg.database)
    exit_error = False
    try:
        for center in targets:
            click.echo(f"==> {center['id']} ({center.get('name', '')}) ...")
            scraper = get_scraper(center, weeks_ahead=weeks,
                                  request_delay=cfg.request_delay)
            if headful and hasattr(scraper, "headless"):
                scraper.headless = False

            run_id = db.start_run(center["id"])
            try:
                result = scraper.scrape()
            except Exception as exc:
                db.finish_run(run_id, "error", 0, error=str(exc))
                click.echo(f"    ERREUR : {exc}", err=True)
                exit_error = True
                continue

            for warning in result.errors:
                click.echo(f"    avertissement : {warning}", err=True)

            if result.slots:
                db.insert_slots(run_id, result.slots)
                db.finish_run(run_id, "success", len(result.slots))
                price = f"à partir de {result.min_price:.2f} €" \
                    if result.min_price is not None else "tarif non détecté"
                click.echo(f"    {len(result.slots)} créneaux enregistrés ({price})")
            else:
                status = "error" if result.errors else "success"
                db.finish_run(run_id, status, 0,
                              error="; ".join(result.errors) or None)
                click.echo("    aucun créneau trouvé")
                exit_error = exit_error or bool(result.errors)
    finally:
        db.close()
    if exit_error:
        raise SystemExit(1)


@main.command()
@click.option("-c", "--center", "center_id", default=None,
              help="Filtrer sur un centre.")
@click.option("--date-from", default=None, help="Date min (YYYY-MM-DD).")
@click.option("--date-to", default=None, help="Date max (YYYY-MM-DD).")
@click.pass_context
def slots(ctx, center_id, date_from, date_to):
    """Affiche les créneaux du dernier relevé réussi."""
    cfg = ctx.obj["config"]
    db = Database(cfg.database)
    try:
        targets = [cfg.center(center_id)] if center_id else cfg.centers
        for center in targets:
            rows = db.latest_slots(center["id"], date_from, date_to)
            run = db.latest_successful_run(center["id"])
            header = f"{center.get('name', center['id'])} — {center.get('city', '')}"
            if run:
                header += f" (relevé du {run['started_at']})"
            click.echo(header)
            if not rows:
                click.echo("  aucun créneau enregistré\n")
                continue
            current_day = None
            for row in rows:
                if row["slot_date"] != current_day:
                    current_day = row["slot_date"]
                    click.echo(f"  {current_day}")
                price = f"{row['price']:.2f} €" if row["price"] is not None else "N.C."
                promo = ""
                if row["base_price"] is not None and row["price"] is not None \
                        and row["price"] < row["base_price"]:
                    promo = f"  (PROMO, au lieu de {row['base_price']:.2f} €)"
                click.echo(f"    {row['slot_time']}  {price}{promo}")
            click.echo("")
    finally:
        db.close()


@main.command()
@click.option("-c", "--center", "center_id", default=None)
@click.option("-n", "--limit", type=int, default=15)
@click.pass_context
def runs(ctx, center_id, limit):
    """Historique des relevés (runs de scraping)."""
    cfg = ctx.obj["config"]
    db = Database(cfg.database)
    try:
        for row in db.runs(center_id, limit):
            error = f"  {row['error']}" if row["error"] else ""
            click.echo(f"#{row['id']:<4} {row['started_at']}  {row['center_id']:40s} "
                       f"{row['status']:8s} {row['slots_found']:>4} créneaux{error}")
    finally:
        db.close()


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=5000, show_default=True)
@click.pass_context
def serve(ctx, host, port):
    """Démarre le dashboard web."""
    from .web.app import create_app
    app = create_app(ctx.obj["config"])
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
