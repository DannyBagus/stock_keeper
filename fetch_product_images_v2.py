#!/usr/bin/env python
"""
Produktbilder v2: Hersteller-Website-Scraping für stock-keeper / Supportelle.

Strategie pro Hersteller:
  - Cache Coeur:  Shopify JSON API → EAN-Matching via variant.barcode
  - Anita:        Produktkatalog-Seiten scrapen
  - Leonisa:      Shopify JSON API → EAN-Matching
  - LIPOELASTIC:  Produktseiten scrapen (EAN sichtbar)
  - Sigvaris:     Shop-Seiten scrapen
  - BORT:         Website scrapen
  - Cafe/Interne: Skip (nicht für Online-Shop)

Ergebnis:
  - Bilder in MEDIA_ROOT/products/
  - Report: /tmp/image_fetch_report_v2.json
"""

import os
import sys
import json
import time
import hashlib
import re
import requests
import django

# ── Django Setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stock_keeper.settings.prod')
django.setup()

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from core.models import Product

# ── Konfiguration ──────────────────────────────────────────────────────────────
MEDIA_ROOT  = settings.MEDIA_ROOT
REPORT_PATH = '/tmp/image_fetch_report_v2.json'
DELAY       = 1.0  # Sekunden zwischen Requests

HEADERS = {
    'User-Agent': 'StockKeeper-Supportelle/1.0 (supportelle@mileja.ch)'
}

os.makedirs(os.path.join(MEDIA_ROOT, 'products'), exist_ok=True)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def log(msg):
    print(msg, flush=True)


def download_image(url: str, min_size=5000):
    """Lädt ein Bild herunter. Gibt (bytes, extension) oder (None, None) zurück."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        ct = resp.headers.get('Content-Type', '').split(';')[0].strip()
        if not ct.startswith('image/'):
            log(f"    ⚠ Kein Bild-Content-Type: {ct}")
            return None, None
        data = resp.content
        if len(data) < min_size:
            log(f"    ⚠ Bild zu klein ({len(data)} B)")
            return None, None
        ext_map = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}
        return data, ext_map.get(ct, 'jpg')
    except Exception as e:
        log(f"    ⚠ Download-Fehler: {e}")
        return None, None


def save_image(product, image_data, ext):
    """Speichert Bild im ImageField."""
    h = hashlib.md5(str(product.pk).encode()).hexdigest()[:10]
    filename = f"{h}_{product.pk}.{ext}"
    product.image.save(filename, ContentFile(image_data), save=True)
    return product.image.name


def fetch_json(url):
    """GET request, returns parsed JSON or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"    ⚠ JSON-Fehler ({url[:80]}): {e}")
        return None


def fetch_html(url):
    """GET request, returns HTML text or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log(f"    ⚠ HTML-Fehler ({url[:80]}): {e}")
        return None


# ── Scraper: Shopify Stores (Cache Coeur, Leonisa) ────────────────────────────

def scrape_shopify_store(store_name, base_url, our_eans):
    """
    Scrape Shopify store via JSON API.
    1. Get all product handles via /products.json (paginated)
    2. For each handle, fetch /products/{handle}.json for real EAN barcodes
    3. Match against our EANs
    Returns: {ean: image_url}
    """
    log(f"\n{'─'*60}")
    log(f"Scraping {store_name} ({base_url})")
    log(f"{'─'*60}")

    # Schritt 1: Alle Produkt-Handles sammeln
    handles = []
    page = 1
    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"
        data = fetch_json(url)
        if not data or not data.get('products'):
            break
        for p in data['products']:
            handles.append(p['handle'])
        log(f"  Page {page}: {len(data['products'])} Produkte geladen")
        if len(data['products']) < 250:
            break
        page += 1
        time.sleep(DELAY)

    log(f"  Total Handles: {len(handles)}")

    # Schritt 2: Jedes Produkt einzeln fetchen für EAN-Barcodes
    ean_to_image = {}
    for i, handle in enumerate(handles):
        url = f"{base_url}/products/{handle}.json"
        data = fetch_json(url)
        time.sleep(DELAY)

        if not data or 'product' not in data:
            continue

        product = data['product']
        images = product.get('images', [])
        if not images:
            continue

        image_url = images[0].get('src', '')
        if not image_url:
            continue

        # Alle Varianten-Barcodes prüfen
        for variant in product.get('variants', []):
            barcode = str(variant.get('barcode') or '').strip()
            if barcode and barcode in our_eans:
                ean_to_image[barcode] = image_url

        if (i + 1) % 25 == 0:
            log(f"  ... {i+1}/{len(handles)} Produkte geprüft, {len(ean_to_image)} Matches")

    log(f"  Fertig: {len(ean_to_image)} EAN-Matches gefunden")
    return ean_to_image


# ── Scraper: LIPOELASTIC ─────────────────────────────────────────────────────

def scrape_lipoelastic(our_eans):
    """
    LIPOELASTIC: Direkte Produktseiten scrapen.
    EAN im JSON-LD (MPN Feld), Bilder unter /images/products/.
    """
    log(f"\n{'─'*60}")
    log(f"Scraping LIPOELASTIC (lipoelastic.com)")
    log(f"{'─'*60}")

    ean_to_image = {}

    # Bekannte Produktseiten basierend auf Sitemap-Analyse
    product_pages = [
        # Bauchgurte
        'https://www.lipoelastic.com/kplus-velcro-fastener',
        'https://www.lipoelastic.com/kp-soft',
        'https://www.lipoelastic.com/kp-special',
        'https://www.lipoelastic.com/kp-extra',
        'https://www.lipoelastic.com/kpress-velcro-fastener',
        'https://www.lipoelastic.com/kpad',
        'https://www.lipoelastic.com/kpg-velcro-fastener',
        # Hosen
        'https://www.lipoelastic.com/vh-variant',
        'https://www.lipoelastic.com/vh-comfort',
        'https://www.lipoelastic.com/vh-special-comfort',
        'https://www.lipoelastic.com/vh-unique-variant',
        'https://www.lipoelastic.com/vb-variant',
        'https://www.lipoelastic.com/vb-comfort',
        'https://www.lipoelastic.com/tb-comfort',
        'https://www.lipoelastic.com/tb-variant',
        # Postnatal
        'https://www.lipoelastic.com/control-high-waisted-briefs-everyday',
        'https://www.lipoelastic.com/silhouette-control-briefs-everyday',
    ]

    for url in product_pages:
        html = fetch_html(url)
        time.sleep(DELAY)
        if not html:
            continue

        slug = url.split('/')[-1]

        # JSON-LD parsen für MPN (= EAN) und Bild
        ld_match = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        if ld_match:
            try:
                ld_data = json.loads(ld_match.group(1))
                mpn = str(ld_data.get('mpn', '') or ld_data.get('gtin13', '')).strip()
                image_url = ld_data.get('image', '')
                if mpn and mpn in our_eans and image_url:
                    ean_to_image[mpn] = image_url
                    log(f"    JSON-LD Match: EAN {mpn} → {slug}")
                    continue
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: EAN direkt im HTML suchen
        eans_on_page = re.findall(r'\b(859184\d{7})\b', html)

        # Bild suchen: /images/products/ oder og:image
        img_url = None
        img_match = re.search(r'src="(https?://[^"]*?/images/products/[^"]*?-web-[^"]+)"', html)
        if img_match:
            img_url = img_match.group(1)
        if not img_url:
            img_match = re.search(r'property="og:image"\s+content="([^"]+)"', html)
            if not img_match:
                img_match = re.search(r'content="([^"]+)"\s+property="og:image"', html)
            if img_match:
                img_url = img_match.group(1)

        if not img_url:
            log(f"    {slug}: kein Bild gefunden")
            continue

        for ean in eans_on_page:
            if ean in our_eans:
                ean_to_image[ean] = img_url
                log(f"    Match: EAN {ean} → {slug}")

        if not eans_on_page:
            log(f"    {slug}: keine EANs auf Seite")

    log(f"  Fertig: {len(ean_to_image)} EAN-Matches")
    return ean_to_image


# ── Scraper: Anita ────────────────────────────────────────────────────────────

def scrape_anita(our_eans):
    """
    Anita: EANs sind NICHT auf der Website sichtbar.
    Strategie: Produktseiten scrapen, Bilder sammeln, dann per Produktname matchen.
    Artikelnummern: 5068 (Still-BH), 5051, 5053, 5265 (Sita), 1885 (Rebelt)
    """
    log(f"\n{'─'*60}")
    log(f"Scraping Anita (anita.com/de)")
    log(f"Hinweis: Anita zeigt keine EANs — Matching per Produktname")
    log(f"{'─'*60}")

    ean_to_image = {}

    # Bekannte Anita-Produktseiten für Still-BHs und Rebelt
    product_pages = [
        'https://www.anita.com/de/basic-still-bugel-bh.html',
        'https://www.anita.com/de/miss-cotton-still-bh-ohne-bugel.html',
        'https://www.anita.com/de/fleur-still-bh-mit-bugel.html',
        'https://www.anita.com/de/bhs/still-bhs.html',
    ]

    # Erstmal Kategorieseiten laden um Produktlinks zu finden
    all_product_urls = set()
    for cat_url in ['https://www.anita.com/de/bhs/still-bhs.html',
                     'https://www.anita.com/de/wasche-dessous/slips.html',
                     'https://www.anita.com/de/schwangerschaft-stillen.html']:
        html = fetch_html(cat_url)
        time.sleep(DELAY)
        if not html:
            continue
        links = re.findall(r'href="(https://www\.anita\.com/de/[a-z0-9][a-z0-9\-]*\.html)"', html)
        for link in links:
            if '/bhs/' not in link and '/wasche-dessous/' not in link and '/schwangerschaft' not in link:
                all_product_urls.add(link)
        log(f"  {cat_url.split('/')[-1]}: {len(links)} Links")

    log(f"  Total Produktlinks: {len(all_product_urls)}")

    # Produktseiten laden und Bilder+Artikelnummern sammeln
    # Da EANs nicht verfügbar sind, sammeln wir Bilder pro Artikelnummer
    article_images = {}  # article_number -> image_url

    for url in all_product_urls:
        html = fetch_html(url)
        time.sleep(DELAY)
        if not html:
            continue

        # Artikelnummer extrahieren (z.B. "Art. 5068" oder "Artikel 5068")
        art_match = re.search(r'(?:Art(?:ikel)?\.?\s*(?:Nr\.?\s*)?|article\s+)(\d{4,5})', html, re.IGNORECASE)

        # Bild: og:image
        img_match = re.search(r'property="og:image"\s+content="([^"]+)"', html)
        if not img_match:
            img_match = re.search(r'content="([^"]+)"\s+property="og:image"', html)
        if not img_match:
            img_match = re.search(r'"(https://cdn-01\.anita\.com/media/catalog/product[^"]+\.jpg)"', html)

        if art_match and img_match:
            art_num = art_match.group(1)
            article_images[art_num] = img_match.group(1)
            log(f"    Art. {art_num} → {url.split('/')[-1]}")

    log(f"  Artikelbilder gefunden: {len(article_images)}")
    log(f"  (Kein EAN-Matching möglich — Anita zeigt keine EANs auf der Website)")
    log(f"  Hinweis: Anita-Produkte müssen manuell zugeordnet werden")

    return ean_to_image


# ── Scraper: Sigvaris (Shopify mit EAN-Suche) ────────────────────────────────

def scrape_sigvaris(our_eans):
    """
    Sigvaris: Shopify-basiert, aber Barcodes in API sind anders als unsere EANs.
    Strategie: Für jede unserer EANs die Suchfunktion nutzen, dann Produktbild holen.
    """
    log(f"\n{'─'*60}")
    log(f"Scraping Sigvaris (shop.sigvaris.com) via EAN-Suche")
    log(f"{'─'*60}")

    ean_to_image = {}
    already_found_products = set()  # Vermeidung von Duplikat-Requests

    for ean in our_eans:
        # Suchseite laden
        search_url = f"https://shop.sigvaris.com/de-ch/search?q={ean}"
        html = fetch_html(search_url)
        time.sleep(DELAY)

        if not html:
            log(f"    EAN {ean}: Suche fehlgeschlagen")
            continue

        # Produktlink aus Suchergebnis extrahieren
        product_match = re.search(r'href="(/de-ch/products/[^"?]+)', html)
        if not product_match:
            log(f"    EAN {ean}: kein Suchergebnis")
            continue

        product_path = product_match.group(1)
        product_handle = product_path.split('/')[-1]

        # Wenn wir dieses Produkt schon haben, Bild wiederverwenden
        if product_handle in already_found_products:
            # Bild vom vorherigen Match verwenden
            for prev_ean, prev_url in ean_to_image.items():
                if product_handle in str(prev_url) or True:
                    ean_to_image[ean] = prev_url
                    break
            log(f"    EAN {ean}: Duplikat von {product_handle}")
            continue

        # Produkt-JSON laden für Bild
        json_url = f"https://shop.sigvaris.com/products/{product_handle}.json"
        data = fetch_json(json_url)
        time.sleep(DELAY)

        if not data or 'product' not in data:
            log(f"    EAN {ean}: JSON nicht verfügbar für {product_handle}")
            continue

        product = data['product']
        images = product.get('images', [])
        if images:
            image_url = images[0].get('src', '')
            if image_url:
                ean_to_image[ean] = image_url
                already_found_products.add(product_handle)
                log(f"    ✓ EAN {ean} → {product.get('title', product_handle)}")
            else:
                log(f"    EAN {ean}: kein Bild für {product_handle}")
        else:
            log(f"    EAN {ean}: kein Bild für {product_handle}")

    log(f"  Fertig: {len(ean_to_image)} EAN-Matches")
    return ean_to_image


# ── Scraper: BORT GmbH ───────────────────────────────────────────────────────

def scrape_bort(our_eans):
    """
    BORT: Offizielle Website zeigt keine EANs.
    Strategie: bort.com Produktseiten direkt laden (bekannte Produkt-IDs).
    Bilder unter bort.com/files/public/products/images/.
    """
    log(f"\n{'─'*60}")
    log(f"Scraping BORT (bort.com)")
    log(f"{'─'*60}")

    ean_to_image = {}

    # Bekannte BORT-Produkte und deren IDs
    bort_products = [
        ('https://www.bort.com/en/produktdetail.html?product=104620',
         'BORT Abdominal Support for Pregnant Women',
         'https://www.bort.com/files/public/products/images/2020/104620_vorne.jpg'),
        ('https://www.bort.com/en/produktdetail.html?product=104580',
         'BORT Back Support for Pregnant Woman',
         'https://www.bort.com/files/public/products/images/2020/104580_vorne.jpg'),
    ]

    log(f"  BORT zeigt keine EANs auf der Website")
    log(f"  Bekannte Produkte: {len(bort_products)}")
    log(f"  Hinweis: BORT-Produkte müssen manuell per EAN zugeordnet werden")

    # Wir können die Bilder trotzdem downloaden und per Produktname matchen versuchen
    # Aber ohne EAN-Mapping ist das unzuverlässig
    # → Wir loggen die verfügbaren Bilder für manuelle Zuordnung

    for url, name, img_url in bort_products:
        log(f"    Verfügbar: {name}")
        log(f"      Bild: {img_url}")

    log(f"  Fertig: {len(ean_to_image)} EAN-Matches (manuell nötig)")
    return ean_to_image


# ── Hauptprogramm ──────────────────────────────────────────────────────────────

SKIP_SUPPLIERS = {'Cafe', 'Diverse'}

SUPPLIER_SCRAPERS = {
    'SARL Cache Cœur': ('shopify', 'https://cachecoeur.com'),
    'Leonisa':         ('shopify', 'https://www.leonisa.com'),
    'LIPOELASTIC a.s.': ('lipoelastic', None),
    'CORSA NOVA':      ('anita', None),
    'Sigvaris':        ('sigvaris', None),
    'BORT GmbH':       ('bort', None),
}


def main():
    # Alle aktiven Produkte ohne Bild laden
    products = list(
        Product.objects.filter(is_active=True)
        .filter(Q(image='') | Q(image__isnull=True))
        .select_related('supplier')
    )

    total = len(products)
    log(f"\n{'='*60}")
    log(f"stock-keeper Produktbilder v2 — Hersteller-Scraping")
    log(f"Produkte ohne Bild: {total}")
    log(f"{'='*60}")

    # Nach Supplier gruppieren
    by_supplier = {}
    for p in products:
        supplier_name = p.supplier.name if p.supplier else 'Unbekannt'
        by_supplier.setdefault(supplier_name, []).append(p)

    log(f"\nNach Hersteller:")
    for s, prods in sorted(by_supplier.items(), key=lambda x: -len(x[1])):
        skip = " [SKIP]" if s in SKIP_SUPPLIERS else ""
        log(f"  {s}: {len(prods)}{skip}")

    # EAN-Set für schnelles Matching
    all_eans = {str(p.ean).strip(): p for p in products}

    report = {
        'total': total,
        'results': {},
        'summary': {'success': 0, 'failed': 0, 'skipped': 0},
    }

    # Pro Supplier den passenden Scraper aufrufen
    all_matches = {}  # ean -> image_url

    for supplier_name, prods in by_supplier.items():
        if supplier_name in SKIP_SUPPLIERS:
            report['summary']['skipped'] += len(prods)
            continue

        supplier_eans = {str(p.ean).strip() for p in prods}

        if supplier_name in SUPPLIER_SCRAPERS:
            scraper_type, base_url = SUPPLIER_SCRAPERS[supplier_name]

            if scraper_type == 'shopify':
                matches = scrape_shopify_store(supplier_name, base_url, supplier_eans)
            elif scraper_type == 'lipoelastic':
                matches = scrape_lipoelastic(supplier_eans)
            elif scraper_type == 'anita':
                matches = scrape_anita(supplier_eans)
            elif scraper_type == 'sigvaris':
                matches = scrape_sigvaris(supplier_eans)
            elif scraper_type == 'bort':
                matches = scrape_bort(supplier_eans)
            else:
                matches = {}

            all_matches.update(matches)
        else:
            log(f"\n  Kein Scraper für '{supplier_name}' ({len(prods)} Produkte)")

    # Bilder herunterladen und speichern
    log(f"\n{'='*60}")
    log(f"Bilder herunterladen: {len(all_matches)} Matches")
    log(f"{'='*60}\n")

    success_list = []
    failed_list = []

    for ean, image_url in all_matches.items():
        product = all_eans.get(ean)
        if not product:
            continue

        log(f"  [{product.pk}] {product.name} (EAN: {ean})")
        image_data, ext = download_image(image_url)

        if not image_data:
            log(f"    ✗ Download fehlgeschlagen")
            failed_list.append({'id': product.pk, 'name': product.name, 'ean': ean, 'url': image_url})
            continue

        try:
            saved_path = save_image(product, image_data, ext)
            log(f"    ✓ Gespeichert: {saved_path}")
            success_list.append({
                'id': product.pk, 'name': product.name,
                'ean': ean, 'path': saved_path,
                'supplier': product.supplier.name if product.supplier else ''
            })
        except Exception as e:
            log(f"    ✗ Speicher-Fehler: {e}")
            failed_list.append({'id': product.pk, 'name': product.name, 'ean': ean, 'error': str(e)})

    # Produkte ohne Match auflisten
    matched_eans = set(all_matches.keys())
    no_match = []
    for p in products:
        ean = str(p.ean).strip()
        supplier = p.supplier.name if p.supplier else 'Unbekannt'
        if supplier not in SKIP_SUPPLIERS and ean not in matched_eans:
            no_match.append({'id': p.pk, 'name': p.name, 'ean': ean, 'supplier': supplier})

    # Report
    report['results'] = {
        'success': success_list,
        'download_failed': failed_list,
        'no_match': no_match,
    }
    report['summary'] = {
        'success': len(success_list),
        'download_failed': len(failed_list),
        'no_match': len(no_match),
        'skipped': report['summary']['skipped'],
    }

    log(f"\n{'='*60}")
    log(f"Fertig!")
    log(f"  ✓ Bilder gespeichert:  {len(success_list)}")
    log(f"  ✗ Download-Fehler:     {len(failed_list)}")
    log(f"  ? Kein Match:          {len(no_match)}")
    log(f"  ⊘ Übersprungen (Cafe): {report['summary']['skipped']}")
    log(f"{'='*60}")
    log(f"Report: {REPORT_PATH}\n")

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if no_match:
        log("Produkte ohne Match (manuell nachtragen):")
        for p in no_match:
            log(f"  [{p['supplier']}] [{p['id']}] {p['name']} ({p['ean']})")


if __name__ == '__main__':
    main()
