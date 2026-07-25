"""Setzt `variant_group` auf Produkten gemäss einer geprüften CSV (Backfill).

Erwartet eine CSV mit den Spalten `sku` und `proposed_group` (oder
`variant_group`) — z.B. die von `suggest_groups` erzeugte und vom Operator
korrigierte Datei. Idempotent; mit `--dry-run` nur Bericht, keine Änderung.

    python manage.py apply_groups --from vorschlag.csv [--dry-run]
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Product


class Command(BaseCommand):
    help = "Setzt variant_group je Produkt gemäss geprüfter CSV (sku, proposed_group)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="src", required=True, help="Pfad zur geprüften CSV.")
        parser.add_argument("--dry-run", action="store_true", help="Nur berichten, nichts schreiben.")

    def handle(self, *args, **opts):
        try:
            fh = open(opts["src"], newline="", encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"CSV nicht lesbar: {exc}")

        updated = unchanged = missing = skipped = 0
        with fh:
            reader = csv.DictReader(fh)
            group_col = "proposed_group" if "proposed_group" in (reader.fieldnames or []) else "variant_group"
            if "sku" not in (reader.fieldnames or []) or group_col not in (reader.fieldnames or []):
                raise CommandError("CSV braucht die Spalten 'sku' und 'proposed_group' (oder 'variant_group').")

            with transaction.atomic():
                for row in reader:
                    sku = (row.get("sku") or "").strip()
                    group = (row.get(group_col) or "").strip()
                    if not sku:
                        skipped += 1
                        continue
                    product = Product.objects.filter(sku=sku).first()
                    if not product:
                        missing += 1
                        self.stderr.write(f"  unbekannte SKU übersprungen: {sku}")
                        continue
                    if product.variant_group == group:
                        unchanged += 1
                        continue
                    if not opts["dry_run"]:
                        product.variant_group = group
                        product.save(update_fields=["variant_group"])
                    updated += 1
                if opts["dry_run"]:
                    transaction.set_rollback(True)

        prefix = "[dry-run] " if opts["dry_run"] else ""
        self.stdout.write(
            f"{prefix}{updated} aktualisiert, {unchanged} unverändert, "
            f"{missing} unbekannte SKU, {skipped} ohne SKU."
        )
