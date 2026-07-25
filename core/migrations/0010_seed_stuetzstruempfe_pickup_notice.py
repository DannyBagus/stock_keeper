"""Seed pickup-only + customer notice for the Stützstrümpfe/Schenkelstrümpfe
categories (measurement in-store required before purchase). Idempotent: matches
by name (case-insensitive); does nothing if the category doesn't exist. The
operator can edit both values later in the admin (StockKeeper is source of truth).
"""
from django.db import migrations

NOTICE = (
    "Um deine Grösse bestimmen zu können, bitten wir dich zu unseren "
    "Öffnungszeiten bei SupportElle vorbei zu schauen.\n\n"
    "Dort können wir deine Beine ausmessen und dir auch direkt die Verordnung "
    "für die Krankenkasse ausstellen.\n\n"
    "Wenn dein gewünschtes Produkt an Lager ist, kannst du es direkt mitnehmen.\n"
    "Bei Bestellung dauert es 3-4 Werktage."
)

TARGET_NAMES = ["Stützstrümpfe", "Schenkelstrümpfe", "Stützstrumpfhosen"]


def seed(apps, schema_editor):
    Category = apps.get_model("core", "Category")
    for name in TARGET_NAMES:
        for cat in Category.objects.filter(name__iexact=name):
            cat.pickup_only = True
            cat.store_notice = NOTICE
            cat.save(update_fields=["pickup_only", "store_notice"])


def unseed(apps, schema_editor):
    Category = apps.get_model("core", "Category")
    for name in TARGET_NAMES:
        for cat in Category.objects.filter(name__iexact=name):
            cat.pickup_only = False
            cat.store_notice = ""
            cat.save(update_fields=["pickup_only", "store_notice"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_category_pickup_only_category_store_notice"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
