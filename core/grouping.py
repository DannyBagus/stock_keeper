"""Varianten-Gruppen für den Webshop: sauberen Gruppennamen aus dem Artikel ableiten.

Artikel mit gleichem `variant_group` bilden im Shop EIN Produkt (Auswahl per
Grösse/Farbe); der Shop gruppiert nach `slugify(variant_group)`. Der Name wird
datengetrieben abgeleitet — aus dem Artikelnamen werden die artikeleigenen Werte
`size` und `color`, Artikelnummern und Klammer-Zusätze entfernt; **Zehen-Art
(open/closed toe) bleibt erhalten**, da sie Teil der Produkt-Identität ist.
"""
import re
from collections import Counter

from django.utils.text import slugify

# Angehängte/enthaltene Artikelnummern wie "17.02.01.05.1".
_ARTICLE_NR = re.compile(r"\b\d+(?:[.\-]\d+)+\b")
# Klammer-Zusätze, meist Grösse/Farbe: "(S Plus normal, Skin)".
_PARENS = re.compile(r"\([^)]*\)")
# Mehrfache Trenner/Whitespace einsammeln.
_SEPARATORS = re.compile(r"[\s,;/·]+")


def strip_token(text: str, token: str) -> str:
    """Entfernt `token` case-insensitiv als ganzes Wort aus text.

    Nur an Wortgrenzen — sonst löscht Grösse "S" jedes "s" ("Still" → "till").
    Zwischen den Teilen eines Tokens sind Trenner beliebig, damit "75 B" auch
    "75B" und "L Plus normal" auch "L Plus, normal" trifft.
    """
    parts = [re.escape(t) for t in _SEPARATORS.split((token or "").strip()) if t]
    if not parts:
        return text
    pattern = r"(?<!\w)" + r"[\s,;/·]*".join(parts) + r"(?!\w)"
    return re.sub(pattern, " ", text, flags=re.IGNORECASE)


def clean(text: str) -> str:
    """Übrig gebliebene Trenner glätten."""
    return _SEPARATORS.sub(" ", text).strip(" ,;-·/.")


def base_name(product) -> str:
    """Leitet einen sauberen Gruppen-Namen aus dem Produktnamen ab."""
    name = product.name or ""
    name = _PARENS.sub(" ", name)
    name = _ARTICLE_NR.sub(" ", name)
    # Produkteigene Grösse/Farbe entfernen (datengetrieben, kein Rate-Vokabular).
    name = strip_token(name, product.size)
    name = strip_token(name, product.color)
    return clean(name) or (product.name or "").strip()


def derive_variant_group(product) -> str:
    """Gruppenname für einen Artikel ohne `variant_group`.

    Trifft der abgeleitete Name (per Slug, wie ihn der Shop vergleicht) eine
    bestehende Gruppe, wird deren Schreibweise übernommen — der Artikel landet so
    als Variante im bestehenden Shop-Produkt, ohne dass der Titel flackert.
    """
    base = base_name(product)
    key = slugify(base)
    existing = (
        type(product)._default_manager.exclude(pk=product.pk).exclude(variant_group="")
        .values_list("variant_group", flat=True)
    )
    spellings = Counter(g for g in existing if slugify(g) == key)
    return spellings.most_common(1)[0][0] if spellings else base
