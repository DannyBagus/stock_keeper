# SUMUP_RECONCILIATION.md

Spezifikation für die neue Django-App `reconciliation/` im stock-keeper Projekt.
Dieses Dokument ist vollständig ausführbar durch Claude Code.

---

## Übersicht

**Ziel:** Monatliche Abstimmung zwischen SumUp-Auszahlungen und Stock Keeper Verkäufen,
mit automatischem Matching, Diskrepanz-Erkennung und PDF-Buchungsbeleg.

**Stack:** Wie bestehendes Projekt — Django 5.2, MySQL, Alpine.js, xhtml2pdf, Jazzmin

**Resultat für die Sachbearbeiterin:**
1. Formular: SumUp-Gutschrift eingeben → System ermittelt Periode automatisch
2. Abgleich-Screen: Matched / Diskrepanzen auf einen Blick
3. Aktionen: Vorschläge annehmen (Zahlungsart korrigieren, Buchung nacherfassen)
4. PDF-Buchungsbeleg: 2 Buchungssätze (Laden / Café) + Gebührenaufstellung

---

## Kritische Vorbedingung: SumUp foreign_tx_id

### Hintergrund
Stock Keeper übergibt beim Checkout an SumUp bereits eine Sale-ID. Laut SumUp API
Dokumentation heisst dieses Feld `foreign_tx_id` (auch `client_transaction_id`).
Dieses Feld erscheint in der SumUp Transaktionshistorie und ermöglicht **exaktes Matching**.

### Schritt 1: Prüfen ob foreign_tx_id bereits übergeben wird

```bash
docker exec stock_keeper_web python manage.py shell -c "
from commerce.models import Sale
s = Sale.objects.filter(payment_method='SUMUP').first()
print('Sale ID:', s.id)
print('Sale str:', str(s))
# SumUp-Feld prüfen: Gibt es ein sumup_tx_id Feld?
print('Fields:', [f.name for f in s._meta.get_fields()])
"
```

Prüfe auch die POS-Checkout-View in `commerce/views.py` auf den SumUp-Aufruf:
- Suche nach `foreign_tx_id`, `foreignTransactionId`, oder `client_transaction_id`
- Suche nach dem SumUp Checkout-API-Aufruf (POST zu `api.sumup.com/v0.1/checkouts`)

### Schritt 2a: Falls foreign_tx_id bereits übergeben wird
→ Feld `sumup_foreign_tx_id` zu `Sale` Model hinzufügen um den Rückgabewert zu speichern
→ Matching läuft über dieses Feld: **Tier 1 Match (exakt)**

### Schritt 2b: Falls foreign_tx_id NICHT übergeben wird
→ Beim Checkout-Aufruf `foreign_tx_id=str(sale.id)` ergänzen
→ Für historische Daten: Fallback auf Betrag+Zeit-Matching (Tier 2/3)
→ Migrations + Model-Anpassung wie in Schritt 2a

**Diese Prüfung muss VOR dem Rest der Implementation erfolgen.**

---

## Phase 1: Django App Grundstruktur

### 1.1 App erstellen

```bash
docker exec stock_keeper_web python manage.py startapp reconciliation
```

Dann `reconciliation` zu `INSTALLED_APPS` in `stock_keeper/settings/base.py` hinzufügen.

### 1.2 Models (`reconciliation/models.py`)

```python
from django.db import models
from django.conf import settings
from commerce.models import Sale


class SumUpPayout(models.Model):
    """
    Repräsentiert eine SumUp-Auszahlung (Gutschrift auf Bankkonto).
    Die Periode wird automatisch aus dem SumUp Payouts-API ermittelt.
    """
    # Vom Buchhalter eingetragen
    bank_credit_amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        verbose_name="Gutschrift auf Bankkonto (CHF)"
    )
    bank_credit_date = models.DateField(verbose_name="Datum Bankgutschrift")

    # Aus SumUp API ermittelt
    sumup_payout_id = models.CharField(max_length=100, blank=True)
    period_start = models.DateField(null=True, blank=True, verbose_name="Abrechnungsperiode von")
    period_end = models.DateField(null=True, blank=True, verbose_name="Abrechnungsperiode bis")
    sumup_gross_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Bruttoumsatz (vor Gebühren)"
    )
    sumup_fees_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Gebühren"
    )
    sumup_net_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Nettobetrag (= Bankgutschrift)"
    )

    # Status
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Entwurf'
        IN_REVIEW = 'IN_REVIEW', 'In Prüfung'
        COMPLETED = 'COMPLETED', 'Abgeschlossen'

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-bank_credit_date']
        verbose_name = "SumUp Auszahlung"
        verbose_name_plural = "SumUp Auszahlungen"

    def __str__(self):
        return f"SumUp Auszahlung {self.bank_credit_date} – CHF {self.bank_credit_amount}"

    @property
    def period_label(self):
        if self.period_start and self.period_end:
            return f"{self.period_start.strftime('%d.%m.%Y')} – {self.period_end.strftime('%d.%m.%Y')}"
        return "Periode unbekannt"

    @property
    def delta(self):
        """Differenz zwischen Bankgutschrift und SumUp-Nettobetrag."""
        if self.sumup_net_amount:
            return self.bank_credit_amount - self.sumup_net_amount
        return None


class ReconciliationItem(models.Model):
    """
    Eine einzelne Transaktionszeile im Abgleich.
    Kann auf eine Sale in SK, eine SumUp-Txn, oder beides zeigen.
    """
    payout = models.ForeignKey(SumUpPayout, on_delete=models.CASCADE, related_name='items')

    # Stock Keeper Seite
    sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True)
    sk_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sk_timestamp = models.DateTimeField(null=True, blank=True)

    # SumUp Seite
    sumup_tx_id = models.CharField(max_length=100, blank=True)
    sumup_tx_code = models.CharField(max_length=100, blank=True)  # z.B. "TEENSK4W2K"
    sumup_foreign_tx_id = models.CharField(
        max_length=128, blank=True,
        help_text="= Sale.id, falls beim Checkout übergeben"
    )
    sumup_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sumup_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sumup_timestamp = models.DateTimeField(null=True, blank=True)

    # Matching-Ergebnis
    class MatchTier(models.TextChoices):
        EXACT = 'EXACT', 'Exakt (foreign_tx_id)'
        AMOUNT_TIME = 'AMOUNT_TIME', 'Betrag + Zeit (±2min)'
        AMOUNT_DATE = 'AMOUNT_DATE', 'Betrag + Tag'
        NO_MATCH = 'NO_MATCH', 'Kein Match'

    class MatchStatus(models.TextChoices):
        MATCHED = 'MATCHED', '✓ Abgeglichen'
        ONLY_SUMUP = 'ONLY_SUMUP', '⚠ Nur SumUp (fehlt in SK)'
        ONLY_SK = 'ONLY_SK', '⚠ Nur SK (falscher Zahlungstyp?)'
        GAP = 'GAP', '✗ Betragsdifferenz > Toleranz'

    match_tier = models.CharField(max_length=20, choices=MatchTier.choices, blank=True)
    match_status = models.CharField(max_length=20, choices=MatchStatus.choices)
    gap_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gap_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Kategorie (für Buchungssatz-Split)
    class Channel(models.TextChoices):
        LADEN = 'LADEN', 'Laden (Supportelle)'
        CAFE = 'CAFE', 'Café'
        UNKNOWN = 'UNKNOWN', 'Unbekannt'

    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.UNKNOWN)

    # Auflösung
    class Resolution(models.TextChoices):
        PENDING = 'PENDING', 'Ausstehend'
        ACCEPTED = 'ACCEPTED', 'Akzeptiert'
        PAYMENT_TYPE_CHANGED = 'PAYMENT_TYPE_CHANGED', 'Zahlungsart korrigiert'
        SALE_ADDED = 'SALE_ADDED', 'In SK nacherfasst'
        MANUAL = 'MANUAL', 'Manuell bearbeitet'
        IGNORED = 'IGNORED', 'Ignoriert'

    resolution = models.CharField(
        max_length=30, choices=Resolution.choices, default=Resolution.PENDING
    )
    resolution_note = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['sumup_timestamp', 'sk_timestamp']

    def __str__(self):
        return f"{self.match_status} | CHF {self.sumup_amount or self.sk_amount}"
```

### 1.3 Migration

```bash
docker exec stock_keeper_web python manage.py makemigrations reconciliation
docker exec stock_keeper_web python manage.py migrate
```

---

## Phase 2: SumUp API Client (`reconciliation/sumup_client.py`)

```python
"""
SumUp API Client für stock-keeper Reconciliation.

Benötigte Endpunkte:
  GET /v0.1/me/financials/payouts          — Auszahlungsliste mit Periode
  GET /v0.1/me/transactions/history        — Alle Transaktionen (mit foreign_tx_id)

Auth: API Key aus settings.SUMUP_API_KEY
Scope: transactions.history (reicht für unsere Zwecke)
"""

import requests
import logging
from datetime import datetime, date
from decimal import Decimal
from django.conf import settings

logger = logging.getLogger(__name__)

SUMUP_BASE = 'https://api.sumup.com'


class SumUpClient:
    def __init__(self):
        self.api_key = settings.SUMUP_API_KEY
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        })

    def _get(self, path, params=None):
        url = f"{SUMUP_BASE}{path}"
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_payouts(self, start_date: date = None, end_date: date = None) -> list[dict]:
        """
        Ruft Auszahlungen ab.
        Gibt Liste von Payouts zurück, sortiert nach Datum (neueste zuerst).

        Jeder Payout enthält:
          - id: str
          - date: str (ISO8601)
          - amount: float (Nettobetrag = was auf Bankkonto kommt)
          - fee: float (abgezogene Gebühren)
          - status: str (z.B. "paid_out")
          - transaction_code: str (Referenz)
        """
        params = {'limit': 50}
        if start_date:
            params['start_date'] = start_date.isoformat()
        if end_date:
            params['end_date'] = end_date.isoformat()

        data = self._get('/v0.1/me/financials/payouts', params=params)
        return data.get('items', data) if isinstance(data, dict) else data

    def find_payout_for_amount(self, amount: Decimal, credit_date: date) -> dict | None:
        """
        Findet den Payout der zu einer Bankgutschrift passt.
        Sucht in einem ±7-Tage-Fenster um credit_date.

        Matching-Logik:
          1. Exakt: amount == payout.amount (Netto)
          2. Toleranz ±0.05 CHF (Rundungsdifferenzen)
        """
        from datetime import timedelta
        start = credit_date - timedelta(days=7)
        end = credit_date + timedelta(days=3)
        payouts = self.get_payouts(start_date=start, end_date=end)

        for payout in payouts:
            payout_amount = Decimal(str(payout.get('amount', 0)))
            diff = abs(payout_amount - amount)
            if diff <= Decimal('0.05'):
                return payout

        logger.warning(f"Kein SumUp Payout für CHF {amount} am {credit_date} gefunden")
        return None

    def get_transactions_for_payout(self, payout: dict) -> list[dict]:
        """
        Ruft alle Transaktionen für einen spezifischen Payout ab.

        Strategie:
          1. Falls payout['id'] vorhanden: Filter via payout_id Parameter
          2. Fallback: Zeitraum aus Payout-Daten ermitteln und nach oldest_time/newest_time filtern

        Jede Transaktion enthält:
          - id: str (SumUp interne ID)
          - transaction_code: str (z.B. "TEENSK4W2K")
          - foreign_transaction_id: str (= Sale.id, falls gesetzt)
          - amount: float
          - fee_amount: float
          - timestamp: str (ISO8601)
          - status: str ("SUCCESSFUL", "REFUNDED", ...)
          - product_summary: str
        """
        params = {
            'statuses': ['SUCCESSFUL', 'REFUNDED'],
            'limit': 100,
        }

        # Periode aus Payout ermitteln
        # SumUp Payout-Objekte enthalten start_date / end_date der Abrechnungsperiode
        period_start = payout.get('period', {}).get('start_date') or payout.get('start_date')
        period_end = payout.get('period', {}).get('end_date') or payout.get('end_date')

        if period_start:
            params['oldest_time'] = period_start
        if period_end:
            params['newest_time'] = period_end

        data = self._get('/v0.1/me/transactions/history', params=params)
        transactions = data.get('items', [])

        logger.info(f"SumUp: {len(transactions)} Transaktionen für Payout {payout.get('id')} geladen")
        return transactions

    def get_payout_period(self, payout: dict) -> tuple[date | None, date | None]:
        """
        Ermittelt die exakte Abrechnungsperiode eines Payouts.
        Gibt (period_start, period_end) als date-Objekte zurück.

        SumUp gibt die Periode in verschiedenen Formaten zurück — wir normalisieren hier.
        """
        def parse_date(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace('Z', '+00:00')).date()
            except Exception:
                try:
                    return date.fromisoformat(s[:10])
                except Exception:
                    return None

        # Verschiedene mögliche Felder im Payout-Objekt
        start = (
            parse_date(payout.get('period', {}).get('start_date')) or
            parse_date(payout.get('start_date')) or
            parse_date(payout.get('oldest_time'))
        )
        end = (
            parse_date(payout.get('period', {}).get('end_date')) or
            parse_date(payout.get('end_date')) or
            parse_date(payout.get('newest_time'))
        )

        # Fallback: Periode aus den Transaktions-Timestamps ableiten
        if not start or not end:
            try:
                txns = self.get_transactions_for_payout(payout)
                if txns:
                    timestamps = [
                        datetime.fromisoformat(t['timestamp'].replace('Z', '+00:00')).date()
                        for t in txns if t.get('timestamp')
                    ]
                    if timestamps:
                        start = start or min(timestamps)
                        end = end or max(timestamps)
            except Exception as e:
                logger.error(f"Fehler beim Ermitteln der Periode via Transaktionen: {e}")

        return start, end
```

---

## Phase 3: Matching Engine (`reconciliation/matching.py`)

```python
"""
Matching-Engine: Verbindet SumUp-Transaktionen mit Stock Keeper Sales.

Matching-Hierarchie (Tier 1 zuerst, dann Fallbacks):

  Tier 1 — EXACT:       sumup.foreign_transaction_id == str(sale.id)
  Tier 2 — AMOUNT_TIME: sumup.amount == sale.total_amount UND |Δt| ≤ 2 Minuten
  Tier 3 — AMOUNT_DATE: sumup.amount == sale.total_amount UND gleicher Tag
  Tier 4 — NO_MATCH:    Keine Übereinstimmung → als Diskrepanz kennzeichnen

Kategorisierung (Laden vs. Café):
  Basiert auf sale.items → product.category.name
  Falls alle Items aus Café-Kategorien → CAFE
  Sonst → LADEN
  Ohne Sale-Referenz → UNKNOWN (manuell klären)
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from commerce.models import Sale
from .models import ReconciliationItem, SumUpPayout

logger = logging.getLogger(__name__)

# Kategorien die als "Café" gelten (case-insensitive, partial match)
CAFE_CATEGORY_KEYWORDS = ['café', 'cafe', 'kaffee', 'coffee', 'snack', 'getränk', 'food']

# Toleranz für Betragsvergleich (Rundungsdifferenzen)
AMOUNT_TOLERANCE = Decimal('0.01')

# Max. Zeitdifferenz für Tier-2-Match
TIME_WINDOW_MINUTES = 2

# Gebührentoleranz für Gap-Erkennung (3%)
FEE_TOLERANCE_PCT = Decimal('3.0')


def detect_channel(sale: Sale | None) -> str:
    """
    Ermittelt ob ein Verkauf zum Laden oder Café gehört.
    Basiert auf den Produktkategorien der SaleItems.
    """
    if sale is None:
        return ReconciliationItem.Channel.UNKNOWN

    try:
        categories = [
            item.product.category.name.lower()
            for item in sale.items.select_related('product__category').all()
            if item.product.category
        ]
    except Exception:
        return ReconciliationItem.Channel.UNKNOWN

    if not categories:
        return ReconciliationItem.Channel.UNKNOWN

    cafe_count = sum(
        1 for cat in categories
        if any(kw in cat for kw in CAFE_CATEGORY_KEYWORDS)
    )

    # Wenn mehr als die Hälfte der Items Café-Kategorien sind → Café
    if cafe_count / len(categories) > 0.5:
        return ReconciliationItem.Channel.CAFE
    return ReconciliationItem.Channel.LADEN


def parse_sumup_timestamp(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        return None


def compute_gap_pct(sk_amount: Decimal, sumup_amount: Decimal) -> Decimal:
    if sk_amount == 0:
        return Decimal('100')
    return abs((sk_amount - sumup_amount) / sk_amount * 100).quantize(Decimal('0.01'))


def run_matching(payout: SumUpPayout, sumup_transactions: list[dict]) -> list[ReconciliationItem]:
    """
    Hauptfunktion: Führt das Matching für einen Payout durch.

    1. Lädt alle SK Sales im Abrechnungszeitraum
    2. Matched Tier 1 → 2 → 3
    3. Erstellt ReconciliationItem Einträge
    4. Gibt alle Items zurück (noch nicht gespeichert — Transaktion liegt beim Aufrufer)
    """
    if not payout.period_start or not payout.period_end:
        raise ValueError("Payout hat keine Periode — bitte zuerst Periode ermitteln")

    # Stock Keeper Sales für den Zeitraum laden
    from datetime import date
    sk_sales = list(
        Sale.objects.filter(
            payment_method='SUMUP',
            created_at__date__gte=payout.period_start,
            created_at__date__lte=payout.period_end,
        ).prefetch_related('items__product__category')
    )

    logger.info(
        f"Matching: {len(sumup_transactions)} SumUp Txn vs. {len(sk_sales)} SK Sales "
        f"({payout.period_start} – {payout.period_end})"
    )

    items = []
    matched_sale_ids = set()
    matched_sumup_ids = set()

    # ── Tier 1: Exakter Match via foreign_transaction_id ──────────────────────
    sale_by_id = {str(s.id): s for s in sk_sales}

    for txn in sumup_transactions:
        foreign_id = str(txn.get('foreign_transaction_id', '') or '').strip()
        if foreign_id and foreign_id in sale_by_id:
            sale = sale_by_id[foreign_id]
            sk_amount = sale.total_price  # Feld-Name ggf. anpassen
            sumup_amount = Decimal(str(txn.get('amount', 0)))
            gap = abs(sk_amount - sumup_amount)
            gap_pct = compute_gap_pct(sk_amount, sumup_amount)

            status = (
                ReconciliationItem.MatchStatus.GAP
                if gap_pct > FEE_TOLERANCE_PCT
                else ReconciliationItem.MatchStatus.MATCHED
            )

            item = ReconciliationItem(
                payout=payout,
                sale=sale,
                sk_amount=sk_amount,
                sk_timestamp=sale.created_at,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=txn.get('transaction_code', ''),
                sumup_foreign_tx_id=foreign_id,
                sumup_amount=sumup_amount,
                sumup_fee=Decimal(str(txn.get('fee_amount', 0) or 0)),
                sumup_timestamp=parse_sumup_timestamp(txn.get('timestamp')),
                match_tier=ReconciliationItem.MatchTier.EXACT,
                match_status=status,
                gap_amount=gap,
                gap_pct=gap_pct,
                channel=detect_channel(sale),
                resolution=ReconciliationItem.Resolution.PENDING,
            )
            items.append(item)
            matched_sale_ids.add(sale.id)
            matched_sumup_ids.add(txn.get('id'))

    # ── Tier 2 + 3: Betrag + Zeit / Betrag + Tag ──────────────────────────────
    unmatched_sales = [s for s in sk_sales if s.id not in matched_sale_ids]
    unmatched_txns = [t for t in sumup_transactions if t.get('id') not in matched_sumup_ids]

    for txn in unmatched_txns:
        sumup_amount = Decimal(str(txn.get('amount', 0)))
        sumup_ts = parse_sumup_timestamp(txn.get('timestamp'))
        matched_sale = None
        tier = None

        for sale in unmatched_sales:
            if sale.id in matched_sale_ids:
                continue
            sk_amount = Decimal(str(sale.total_price))
            if abs(sk_amount - sumup_amount) > AMOUNT_TOLERANCE:
                continue

            # Tier 2: ±2 Minuten
            if sumup_ts and sale.created_at:
                delta = abs((sumup_ts - sale.created_at.astimezone(timezone.utc)).total_seconds())
                if delta <= TIME_WINDOW_MINUTES * 60:
                    matched_sale = sale
                    tier = ReconciliationItem.MatchTier.AMOUNT_TIME
                    break

            # Tier 3: Gleicher Tag
            if sumup_ts and sale.created_at:
                if sumup_ts.date() == sale.created_at.date():
                    matched_sale = sale
                    tier = ReconciliationItem.MatchTier.AMOUNT_DATE
                    # Kein break — weiter suchen ob Tier 2 besser passt

        if matched_sale:
            sk_amount = Decimal(str(matched_sale.total_price))
            gap_pct = compute_gap_pct(sk_amount, sumup_amount)
            status = (
                ReconciliationItem.MatchStatus.GAP
                if gap_pct > FEE_TOLERANCE_PCT
                else ReconciliationItem.MatchStatus.MATCHED
            )
            item = ReconciliationItem(
                payout=payout,
                sale=matched_sale,
                sk_amount=sk_amount,
                sk_timestamp=matched_sale.created_at,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=txn.get('transaction_code', ''),
                sumup_amount=sumup_amount,
                sumup_fee=Decimal(str(txn.get('fee_amount', 0) or 0)),
                sumup_timestamp=sumup_ts,
                match_tier=tier,
                match_status=status,
                gap_amount=abs(sk_amount - sumup_amount),
                gap_pct=gap_pct,
                channel=detect_channel(matched_sale),
                resolution=ReconciliationItem.Resolution.PENDING,
            )
            items.append(item)
            matched_sale_ids.add(matched_sale.id)
            matched_sumup_ids.add(txn.get('id'))
        else:
            # Nur SumUp — kein SK-Eintrag
            item = ReconciliationItem(
                payout=payout,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=txn.get('transaction_code', ''),
                sumup_amount=sumup_amount,
                sumup_fee=Decimal(str(txn.get('fee_amount', 0) or 0)),
                sumup_timestamp=sumup_ts,
                match_tier=ReconciliationItem.MatchTier.NO_MATCH,
                match_status=ReconciliationItem.MatchStatus.ONLY_SUMUP,
                gap_amount=sumup_amount,
                gap_pct=Decimal('100'),
                channel=ReconciliationItem.Channel.UNKNOWN,
                resolution=ReconciliationItem.Resolution.PENDING,
            )
            items.append(item)

    # ── SK Sales ohne SumUp-Treffer → Zahlungsart prüfen ─────────────────────
    for sale in unmatched_sales:
        if sale.id in matched_sale_ids:
            continue
        sk_amount = Decimal(str(sale.total_price))
        item = ReconciliationItem(
            payout=payout,
            sale=sale,
            sk_amount=sk_amount,
            sk_timestamp=sale.created_at,
            match_tier=ReconciliationItem.MatchTier.NO_MATCH,
            match_status=ReconciliationItem.MatchStatus.ONLY_SK,
            gap_amount=sk_amount,
            gap_pct=Decimal('100'),
            channel=detect_channel(sale),
            resolution=ReconciliationItem.Resolution.PENDING,
        )
        items.append(item)

    logger.info(
        f"Matching abgeschlossen: {sum(1 for i in items if i.match_status == 'MATCHED')} matched, "
        f"{sum(1 for i in items if i.match_status != 'MATCHED')} Diskrepanzen"
    )
    return items
```

---

## Phase 4: Views (`reconciliation/views.py`)

Drei Views — alle `@staff_member_required`:

### 4.1 `reconciliation_start` — Formular

- Felder: `bank_credit_amount`, `bank_credit_date`
- Bei Submit: SumUp API aufrufen → Payout finden → Periode ermitteln → Matching ausführen
- Redirect zu `reconciliation_review`

### 4.2 `reconciliation_review` — Abgleich-Screen

Alpine.js-Tabelle mit:
- Zusammenfassung oben: Matched / Nur SumUp / Nur SK / Gap / Totale
- Zeilen eingefärbt nach Status (grün / orange / rot)
- Pro Diskrepanz: Vorschlag-Button (Zahlungsart ändern / Buchung nacherfassen)
- "Abschliessen" Button → generiert PDF

Layout der Tabelle:

```
Datum     | SK Sale | SK Betrag | SumUp Betrag | Gebühr | Match | Kanal  | Aktion
----------|---------|-----------|--------------|--------|-------|--------|-------
12.03.25  | #1042   | 85.50     | 85.50        | 1.44   | ✓ T1  | Laden  | –
12.03.25  | –       | –         | 42.00        | 0.71   | ⚠ –   | ?      | [Nacherfassen]
13.03.25  | #1047   | 28.00     | –            | –      | ⚠ –   | Café   | [Zahlungsart: Rechnung]
```

### 4.3 `reconciliation_complete` — PDF generieren

Ruft `generate_voucher_pdf()` auf, setzt Status auf COMPLETED.

---

## Phase 5: PDF-Buchungsbeleg (`reconciliation/pdf.py`)

```python
"""
Generiert den Buchungsbeleg für die Sachbearbeiterin.

Aufbau des Belegs:
  1. Header: Supportelle by Mileja, Abrechnungsperiode, Datum
  2. Buchungssätze:
     Buchungssatz 1: 1020 Bank / 3000 Erlös Laden "SumUp [Monat]"  CHF X
     Buchungssatz 2: 1020 Bank / 3000 Erlös Café  "SumUp [Monat]"  CHF Y
     (optional) Buchungssatz 3: 6800 SumUp-Gebühren / 1020 Bank    CHF Z
  3. Gebührenaufstellung: pro Transaktion mit fee_amount
  4. Abgleich-Zusammenfassung: matched / Diskrepanzen
  5. Differenz Bankgutschrift vs. SumUp-Netto (sollte 0 sein)
"""

from django.template.loader import render_to_string
from commerce.utils import render_to_pdf  # Bestehendes utility nutzen
from decimal import Decimal


def generate_voucher_pdf(payout):
    """Generiert den Buchungsbeleg als PDF-Response."""
    items = payout.items.all().order_by('sumup_timestamp', 'sk_timestamp')

    # Totale nach Kanal
    laden_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in items
        if i.channel == 'LADEN' and i.match_status == 'MATCHED'
    )
    cafe_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in items
        if i.channel == 'CAFE' and i.match_status == 'MATCHED'
    )
    total_fees = sum(i.sumup_fee or Decimal(0) for i in items)

    context = {
        'payout': payout,
        'items': items,
        'laden_total': laden_total,
        'cafe_total': cafe_total,
        'total_fees': total_fees,
        'matched_count': items.filter(match_status='MATCHED').count(),
        'discrepancy_count': items.exclude(match_status='MATCHED').count(),
    }

    return render_to_pdf('reconciliation/voucher_pdf.html', context)
```

PDF-Template erstellen: `reconciliation/templates/reconciliation/voucher_pdf.html`

Inhalt (HTML für xhtml2pdf):
- Corporate Header (Supportelle by Mileja)
- Buchungssätze als Tabelle
- Gebühren-Breakdown
- Hinweis auf offene Diskrepanzen falls vorhanden

---

## Phase 6: URL-Konfiguration

In `stock_keeper/urls.py` ergänzen:

```python
path('reconciliation/', include('reconciliation.urls')),
```

`reconciliation/urls.py`:

```python
from django.urls import path
from . import views

app_name = 'reconciliation'

urlpatterns = [
    path('', views.reconciliation_list, name='list'),
    path('new/', views.reconciliation_start, name='start'),
    path('<int:pk>/review/', views.reconciliation_review, name='review'),
    path('<int:pk>/complete/', views.reconciliation_complete, name='complete'),
    path('<int:pk>/pdf/', views.reconciliation_pdf, name='pdf'),
    path('<int:pk>/item/<int:item_pk>/resolve/', views.resolve_item, name='resolve_item'),
]
```

---

## Phase 7: Settings ergänzen

In `stock_keeper/settings/base.py`:

```python
# SumUp API
SUMUP_API_KEY = env('SUMUP_API_KEY', default='')
```

In `.env.prod`:

```env
SUMUP_API_KEY=sk_live_...
```

---

## Phase 8: Sale.total_price Feldname prüfen

Das Matching-Script referenziert `sale.total_price`. Prüfe den korrekten Feldnamen:

```bash
docker exec stock_keeper_web python manage.py shell -c "
from commerce.models import Sale
s = Sale.objects.filter(payment_method='SUMUP').first()
print('Fields:', [f.name for f in s._meta.get_fields()])
print('Total:', getattr(s, 'total_price', None) or getattr(s, 'total', None) or getattr(s, 'amount', None))
"
```

Feldname in `matching.py` und `pdf.py` entsprechend anpassen.

---

## Implementierungsreihenfolge für Claude Code

1. **Vorbedingung prüfen** (Phase 0): `foreign_tx_id` in POS-Checkout vorhanden?
2. **Models + Migration** (Phase 1): App erstellen, Models anlegen, migrieren
3. **SumUp Client** (Phase 2): API-Client implementieren, manuell testen
4. **Matching Engine** (Phase 3): Implementieren, Unit-Test mit echten Daten
5. **Views** (Phase 4): Start-Formular → Review-Screen → Complete
6. **PDF** (Phase 5): Template + Generator
7. **URLs + Settings** (Phase 6+7): Verdrahten

---

## Manueller API-Test (nach Settings-Konfiguration)

```bash
docker exec stock_keeper_web python manage.py shell -c "
from reconciliation.sumup_client import SumUpClient
from datetime import date

client = SumUpClient()

# 1. Payouts der letzten 3 Monate abrufen
payouts = client.get_payouts()
print(f'Payouts gefunden: {len(payouts)}')
for p in payouts[:3]:
    print(f'  Payout: {p}')

# 2. Ersten Payout nehmen und Periode ermitteln
if payouts:
    payout = payouts[0]
    start, end = client.get_payout_period(payout)
    print(f'Periode: {start} – {end}')

    # 3. Transaktionen abrufen
    txns = client.get_transactions_for_payout(payout)
    print(f'Transaktionen: {len(txns)}')
    for t in txns[:3]:
        print(f'  Txn: {t.get(\"transaction_code\")} | {t.get(\"amount\")} | foreign_id={t.get(\"foreign_transaction_id\")}')
"
```

---

## Bekannte Einschränkungen & Hinweise

1. **SumUp Payout-Periode:** Das genaue Format der Periodenfelder im Payout-Objekt
   muss beim ersten echten API-Call verifiziert werden — der Client hat mehrere Fallbacks.

2. **Café-Kategorien:** Die Keywords in `CAFE_CATEGORY_KEYWORDS` müssen auf die
   tatsächlichen Kategorienamen in Stock Keeper abgestimmt werden.

3. **Sale.total_price:** Feldname muss vor dem Matching geprüft werden (Phase 8).

4. **Historische Daten:** Falls `foreign_tx_id` bisher nicht übergeben wurde,
   sind ältere Verkäufe nur via Tier 2/3 matchbar. Ab Implementierung läuft alles via Tier 1.

5. **Direktbuchungen am SumUp Terminal:** Diese haben keine `foreign_tx_id` und landen
   als `ONLY_SUMUP` → manuell in SK nacherfassen via Resolve-Action.
