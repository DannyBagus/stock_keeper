"""Schlägt Varianten-Gruppen für bestehende Produkte vor (Backfill-Hilfe).

Reine Leseoperation: erzeugt eine CSV zum Prüfen/Korrigieren, verändert NICHTS.
Der Vorschlag entsteht datengetrieben — aus dem Produktnamen werden die
produkteigenen Werte `size` und `color`, angehängte Artikelnummern und
Klammer-Zusätze entfernt; **Zehen-Art (open/closed toe, offene/geschlossene
Zehen) bleibt erhalten**, da sie Teil der Produkt-Identität ist.

Ablauf:  python manage.py suggest_groups --output vorschlag.csv
         # Operator prüft/korrigiert die Spalte `proposed_group`
         python manage.py apply_groups --from vorschlag.csv
"""
import csv
import re
import sys

from django.core.management.base import BaseCommand

from core.models import Product

# Angehängte/enthaltene Artikelnummern wie "17.02.01.05.1".
_ARTICLE_NR = re.compile(r"\b\d+(?:[.\-]\d+)+\b")
# Klammer-Zusätze, meist Grösse/Farbe: "(S Plus normal, Skin)".
_PARENS = re.compile(r"\([^)]*\)")
# Mehrfache Trenner/Whitespace einsammeln.
_SEPARATORS = re.compile(r"[\s,;/·]+")


def _strip_token(text: str, token: str) -> str:
    """Entfernt `token` case-insensitiv als ganzes Wort/Teilstring aus text."""
    token = (token or "").strip()
    if not token:
        return text
    return re.sub(re.escape(token), " ", text, flags=re.IGNORECASE)


def base_name(product: Product) -> str:
    """Leitet einen sauberen Gruppen-Namen aus dem Produktnamen ab."""
    name = product.name or ""
    name = _PARENS.sub(" ", name)
    name = _ARTICLE_NR.sub(" ", name)
    # Produkteigene Grösse/Farbe entfernen (datengetrieben, kein Rate-Vokabular).
    name = _strip_token(name, product.size)
    name = _strip_token(name, product.color)
    # Übrig gebliebene Trenner glätten.
    name = _SEPARATORS.sub(" ", name).strip(" ,;-·/")
    return name or (product.name or "").strip()


class Command(BaseCommand):
    help = "Schlägt Varianten-Gruppen als CSV vor (Review vor apply_groups)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", "-o", default="-",
            help="Ziel-CSV (Default: stdout).",
        )
        parser.add_argument(
            "--only-ungrouped", action="store_true",
            help="Nur Produkte ohne gesetzte variant_group vorschlagen.",
        )

    def handle(self, *args, **opts):
        qs = Product.objects.all().select_related("category").order_by("name", "sku")
        if opts["only_ungrouped"]:
            qs = qs.filter(variant_group="")

        rows = []
        for p in qs:
            rows.append({
                "sku": p.sku,
                "current_name": p.name,
                "proposed_group": p.variant_group or base_name(p),
                "size": p.size,
                "color": p.color,
                "category": p.category.name if p.category else "",
            })
        # Nach vorgeschlagener Gruppe sortieren, damit zusammengehörige Zeilen
        # in der CSV beieinanderstehen (leichteres Review).
        rows.sort(key=lambda r: (r["proposed_group"].casefold(), r["current_name"]))

        fields = ["sku", "current_name", "proposed_group", "size", "color", "category"]
        out = sys.stdout if opts["output"] == "-" else open(opts["output"], "w", newline="", encoding="utf-8")
        try:
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        finally:
            if out is not sys.stdout:
                out.close()

        groups = len({r["proposed_group"].casefold() for r in rows})
        self.stderr.write(
            f"{len(rows)} Produkte → {groups} vorgeschlagene Gruppen. "
            f"Bitte 'proposed_group' prüfen, dann: apply_groups --from <datei>.csv"
        )
