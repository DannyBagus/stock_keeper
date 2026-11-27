# Task: Automatische Produktbilder via EAN für stock-keeper

## Ziel
Für alle Produkte im stock-keeper Django-Projekt, die noch kein Bild haben (`image` ist leer),
soll automatisch ein Bild gesucht, heruntergeladen und in der DB gespeichert werden.

## Architektur
- **Django-Container:** `stock_keeper_web`
- **App / Model:** `core.models.Product`
- **Image-Feld:** `Product.image` (ImageField, `upload_to='products/'`)
- **EAN-Logik:**
  - EAN beginnt mit `29` → intern generiert → **kein** Open*Facts-Lookup, direkt Produktname-Suche
  - EAN beginnt mit anderem Prefix → echter Hersteller-Barcode → Open*Facts zuerst
- **Quellen:** Open Food Facts → Open Beauty Facts → Open Products Facts → DuckDuckGo Images

---

## Schritt 1: Abhängigkeiten im Container installieren

```bash
docker exec stock_keeper_web pip install requests Pillow duckduckgo_search
```

---

## Schritt 2: Script erstellen, in den Container kopieren und ausführen

Erstelle die Datei `fetch_product_images.py` mit dem Inhalt weiter unten, dann:

```bash
docker cp fetch_product_images.py stock_keeper_web:/tmp/fetch_product_images.py
docker exec stock_keeper_web python /tmp/fetch_product_images.py
```

---

## Das Script: `fetch_product_images.py`

```python
#!/usr/bin/env python
"""
Automatische Produktbilder für stock-keeper / Supportelle by Mileja.

Suchstrategie:
  - EAN beginnt mit "29" (interner Barcode):
      → Direkt DuckDuckGo mit Produktname
  - EAN ist echter Hersteller-Barcode:
      → Open Food Facts → Open Beauty Facts → Open Products Facts
      → Fallback: DuckDuckGo mit Produktname

Ergebnis:
  - Bild wird nach MEDIA_ROOT/products/ gespeichert
  - Product.image wird in der DB gesetzt
  - Report: /tmp/image_fetch_report.json
"""

import os
import sys
import json
import time
import hashlib
import requests
import django

# ── Django Setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stock_keeper.settings')
django.setup()

from django.conf import settings
from django.core.files.base import ContentFile
from core.models import Product

# ── Konfiguration ──────────────────────────────────────────────────────────────
MEDIA_ROOT     = settings.MEDIA_ROOT
REPORT_PATH    = '/tmp/image_fetch_report.json'
REQUEST_DELAY  = 1.5   # Sekunden zwischen API-Calls (rate limiting)
MIN_IMAGE_SIZE = 5_000  # Bytes — kleinere Bilder werden verworfen

OPEN_FACTS_APIS = [
    'https://world.openfoodfacts.org/api/v0/product/{ean}.json',
    'https://world.openbeautyfacts.org/api/v0/product/{ean}.json',
    'https://world.openproductsfacts.org/api/v0/product/{ean}.json',
]

HEADERS = {
    'User-Agent': 'StockKeeper-Supportelle/1.0 (supportelle@mileja.ch)'
}

os.makedirs(os.path.join(MEDIA_ROOT, 'products'), exist_ok=True)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def log(msg):
    print(msg, flush=True)


def is_internal_ean(ean: str) -> bool:
    """EANs mit Prefix 29 sind intern generiert — kein Hersteller-Lookup möglich."""
    return str(ean).startswith('29')


def download_image(url: str):
    """
    Lädt ein Bild herunter.
    Gibt (bytes, extension) zurück oder (None, None) bei Fehler.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip()
        if not content_type.startswith('image/'):
            log(f"    ⚠ Kein Bild-Content-Type: {content_type}")
            return None, None
        data = resp.content
        if len(data) < MIN_IMAGE_SIZE:
            log(f"    ⚠ Bild zu klein ({len(data)} Bytes), übersprungen")
            return None, None
        ext_map = {
            'image/jpeg': 'jpg',
            'image/png': 'png',
            'image/webp': 'webp',
            'image/gif': 'gif',
        }
        ext = ext_map.get(content_type, 'jpg')
        return data, ext
    except Exception as e:
        log(f"    ⚠ Download-Fehler: {e}")
        return None, None


def save_image_to_product(product, image_data: bytes, ext: str) -> str:
    """Speichert Bilddaten im ImageField und gibt den gespeicherten Pfad zurück."""
    hash_id = hashlib.md5(str(product.pk).encode()).hexdigest()[:10]
    filename = f"{hash_id}_{product.pk}.{ext}"
    product.image.save(filename, ContentFile(image_data), save=True)
    return product.image.name


def search_open_facts(ean: str):
    """Sucht in Open*Facts APIs nach Produktbild via EAN. Gibt URL oder None zurück."""
    for api_template in OPEN_FACTS_APIS:
        url = api_template.format(ean=ean)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            time.sleep(REQUEST_DELAY)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get('status') != 1:
                continue
            p = data.get('product', {})
            image_url = (
                p.get('image_front_url') or
                p.get('image_url') or
                p.get('image_front_small_url')
            )
            if image_url:
                source_name = api_template.split('/')[2]
                log(f"    ✓ Open*Facts Treffer ({source_name})")
                return image_url
        except Exception as e:
            log(f"    ⚠ Open*Facts Fehler: {e}")
    return None


def search_duckduckgo(product_name: str):
    """DuckDuckGo Image Search als Fallback. Gibt URL oder None zurück."""
    try:
        from duckduckgo_search import DDGS
        query = f"{product_name} Produkt"
        with DDGS() as ddgs:
            results = list(ddgs.images(
                query,
                max_results=5,
                type_image='photo',
            ))
        time.sleep(REQUEST_DELAY)
        if results:
            url = results[0].get('image')
            log(f"    ✓ DuckDuckGo Treffer (Query: '{query}')")
            return url
        log(f"    ✗ DuckDuckGo: Keine Ergebnisse")
    except Exception as e:
        log(f"    ⚠ DuckDuckGo Fehler: {e}")
    return None


# ── Hauptprogramm ──────────────────────────────────────────────────────────────

def main():
    # Alle aktiven Produkte ohne Bild (image='' oder image=None)
    products_empty = list(Product.objects.filter(image='', is_active=True))
    products_null  = list(Product.objects.filter(image__isnull=True, is_active=True))
    seen = set()
    unique_products = []
    for p in products_empty + products_null:
        if p.pk not in seen:
            seen.add(p.pk)
            unique_products.append(p)

    total = len(unique_products)
    internal_count = sum(1 for p in unique_products if is_internal_ean(p.ean))

    log(f"\n{'='*60}")
    log(f"stock-keeper Produktbilder-Fetch")
    log(f"Produkte ohne Bild:          {total}")
    log(f"  davon interne EANs (29..): {internal_count}  → direkt DuckDuckGo")
    log(f"  davon echte EANs:          {total - internal_count}  → Open*Facts zuerst")
    log(f"{'='*60}\n")

    report = {
        'total': total,
        'success_open_facts': [],
        'success_duckduckgo': [],
        'failed': [],
    }

    for i, product in enumerate(unique_products, 1):
        ean  = str(product.ean).strip()
        name = product.name
        log(f"[{i}/{total}] {name} (EAN: {ean})")

        image_url = None
        source    = None

        if is_internal_ean(ean):
            log(f"    → Interne EAN (29..), suche per Produktname...")
            image_url = search_duckduckgo(name)
            if image_url:
                source = 'duckduckgo'
        else:
            image_url = search_open_facts(ean)
            if image_url:
                source = 'open_facts'
            else:
                log(f"    → Kein Open*Facts Treffer, versuche DuckDuckGo...")
                image_url = search_duckduckgo(name)
                if image_url:
                    source = 'duckduckgo'

        if not image_url:
            log(f"    ✗ Kein Bild gefunden\n")
            report['failed'].append({
                'id': product.pk, 'name': name, 'ean': ean,
                'internal_ean': is_internal_ean(ean)
            })
            continue

        image_data, ext = download_image(image_url)
        if not image_data:
            log(f"    ✗ Download fehlgeschlagen\n")
            report['failed'].append({
                'id': product.pk, 'name': name, 'ean': ean, 'url': image_url
            })
            continue

        try:
            saved_path = save_image_to_product(product, image_data, ext)
            log(f"    ✓ Gespeichert: {saved_path}\n")
            entry = {'id': product.pk, 'name': name, 'ean': ean, 'path': saved_path}
            if source == 'open_facts':
                report['success_open_facts'].append(entry)
            else:
                report['success_duckduckgo'].append(entry)
        except Exception as e:
            log(f"    ✗ Speicher-Fehler: {e}\n")
            report['failed'].append({
                'id': product.pk, 'name': name, 'ean': ean, 'error': str(e)
            })

    # ── Abschlussbericht ──────────────────────────────────────────────────────
    success_total = len(report['success_open_facts']) + len(report['success_duckduckgo'])
    log(f"\n{'='*60}")
    log(f"Fertig! {success_total}/{total} Bilder gefunden")
    log(f"  ✓ Open*Facts:     {len(report['success_open_facts'])}")
    log(f"  ✓ DuckDuckGo:     {len(report['success_duckduckgo'])}")
    log(f"  ✗ Nicht gefunden: {len(report['failed'])}")
    log(f"{'='*60}")
    log(f"Vollständiger Report: {REPORT_PATH}\n")

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if report['failed']:
        log("Produkte ohne Bild (manuell nachtragen):")
        for p in report['failed']:
            marker = "[intern]" if p.get('internal_ean') else "[EAN]"
            log(f"  {marker} [{p['id']}] {p['name']}")


if __name__ == '__main__':
    main()
```

---

## Schritt 3: Report auslesen

```bash
docker exec stock_keeper_web cat /tmp/image_fetch_report.json | python3 -m json.tool
```

---

## Schritt 4 (optional): Aktuellen Stand prüfen

```bash
docker exec stock_keeper_web python manage.py shell -c "
from core.models import Product
total  = Product.objects.count()
mit    = Product.objects.exclude(image='').exclude(image__isnull=True).count()
ohne   = total - mit
intern = Product.objects.filter(ean__startswith='29').count()
print(f'Produkte total:          {total}')
print(f'  mit Bild:              {mit}')
print(f'  ohne Bild:             {ohne}')
print(f'  interne EANs (29..):   {intern}')
"
```

---

## Erwartete Ausgabe

```
============================================================
stock-keeper Produktbilder-Fetch
Produkte ohne Bild:          52
  davon interne EANs (29..): 38  → direkt DuckDuckGo
  davon echte EANs:          14  → Open*Facts zuerst
============================================================

[1/52] Lansinoh Stilleinlagen (EAN: 4002542037346)
    ✓ Open*Facts Treffer (world.openbeautyfacts.org)
    ✓ Gespeichert: products/a3f2b1c4_12.jpg

[2/52] Medela Calma Sauger (EAN: 2900000000123)
    → Interne EAN (29..), suche per Produktname...
    ✓ DuckDuckGo Treffer (Query: 'Medela Calma Sauger Produkt')
    ✓ Gespeichert: products/b7e9a2f1_13.webp
...
```
