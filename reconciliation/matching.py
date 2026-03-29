"""
Matching-Engine: Verbindet SumUp-Transaktionen mit Stock Keeper Sales.

Matching-Hierarchie (Tier 1 zuerst, dann Fallbacks):

  Tier 1 — EXACT:       sumup.client_transaction_id == str(sale.id)
  Tier 2 — AMOUNT_TIME: sumup.amount == sale.total_amount_gross UND |Δt| ≤ 2 Minuten
  Tier 3 — AMOUNT_DATE: sumup.amount == sale.total_amount_gross UND gleicher Tag
  Tier 4 — NO_MATCH:    Keine Übereinstimmung → als Diskrepanz kennzeichnen

Hinweis: Aktuell übergibt die POS-Kasse keine Sale-ID an SumUp (nur Kauf-{timestamp}),
daher funktioniert Tier 1 nicht für historische Daten. Matching läuft über Tier 2/3.

Gebühren: Die SumUp Transactions API liefert kein fee_amount pro Transaktion.
Die Gebühren kommen aus dem Payout-Objekt (payout.fee) und werden separat zugeordnet.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from commerce.models import Sale
from .models import ReconciliationItem, SumUpPayout

logger = logging.getLogger(__name__)

CAFE_CATEGORY_KEYWORDS = ['café', 'cafe', 'kaffee', 'coffee', 'snack', 'getränk', 'food']
AMOUNT_TOLERANCE = Decimal('0.01')
TIME_WINDOW_MINUTES = 2
FEE_TOLERANCE_PCT = Decimal('3.0')


def detect_channel(sale: Sale | None) -> str:
    if sale is None:
        return ReconciliationItem.Channel.UNKNOWN

    try:
        categories = [
            item.product.category.name.lower()
            for item in sale.items.select_related('product__category').all()
            if item.product and item.product.category
        ]
    except Exception:
        return ReconciliationItem.Channel.UNKNOWN

    if not categories:
        return ReconciliationItem.Channel.UNKNOWN

    cafe_count = sum(
        1 for cat in categories
        if any(kw in cat for kw in CAFE_CATEGORY_KEYWORDS)
    )

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


def run_matching(payout: SumUpPayout, sumup_transactions: list[dict],
                 payout_fees: dict[str, Decimal] = None) -> list[ReconciliationItem]:
    """
    Hauptfunktion: Führt das Matching für einen Payout durch.

    Args:
        payout: SumUpPayout-Objekt
        sumup_transactions: Liste von SumUp-Transaktionsdaten
        payout_fees: Mapping von transaction_code → fee (aus Payouts)
    """
    if not payout.period_start or not payout.period_end:
        raise ValueError("Payout hat keine Periode — bitte zuerst Periode ermitteln")

    payout_fees = payout_fees or {}

    # Stock Keeper Sales für den Zeitraum laden
    sk_sales = list(
        Sale.objects.filter(
            payment_method='SUMUP',
            status='COMPLETED',
            date__date__gte=payout.period_start,
            date__date__lte=payout.period_end,
        ).prefetch_related('items__product__category')
    )

    logger.info(
        f"Matching: {len(sumup_transactions)} SumUp Txn vs. {len(sk_sales)} SK Sales "
        f"({payout.period_start} – {payout.period_end})"
    )

    items = []
    matched_sale_ids = set()
    matched_sumup_ids = set()

    # ── Tier 1: Exakter Match via client_transaction_id ──────────────────────
    sale_by_id = {str(s.id): s for s in sk_sales}

    for txn in sumup_transactions:
        foreign_id = str(txn.get('client_transaction_id', '') or '').strip()
        if foreign_id and foreign_id in sale_by_id:
            sale = sale_by_id[foreign_id]
            sk_amount = Decimal(str(sale.total_amount_gross))
            sumup_amount = Decimal(str(txn.get('amount', 0)))
            gap = abs(sk_amount - sumup_amount)
            gap_pct = compute_gap_pct(sk_amount, sumup_amount)
            tx_code = txn.get('transaction_code', '')
            fee = payout_fees.get(tx_code, Decimal(0))

            status = (
                ReconciliationItem.MatchStatus.GAP
                if gap_pct > FEE_TOLERANCE_PCT
                else ReconciliationItem.MatchStatus.MATCHED
            )

            item = ReconciliationItem(
                payout=payout,
                sale=sale,
                sk_amount=sk_amount,
                sk_timestamp=sale.date,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=tx_code,
                sumup_foreign_tx_id=foreign_id,
                sumup_amount=sumup_amount,
                sumup_fee=fee,
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
            matched_sumup_ids.add(txn.get('transaction_code'))

    # ── Tier 2 + 3: Betrag + Zeit / Betrag + Tag ─��────────────────────────────
    unmatched_sales = [s for s in sk_sales if s.id not in matched_sale_ids]
    unmatched_txns = [t for t in sumup_transactions if t.get('transaction_code') not in matched_sumup_ids]

    for txn in unmatched_txns:
        sumup_amount = Decimal(str(txn.get('amount', 0)))
        sumup_ts = parse_sumup_timestamp(txn.get('timestamp'))
        tx_code = txn.get('transaction_code', '')
        fee = payout_fees.get(tx_code, Decimal(0))
        matched_sale = None
        tier = None

        for sale in unmatched_sales:
            if sale.id in matched_sale_ids:
                continue
            sk_amount = Decimal(str(sale.total_amount_gross))
            if abs(sk_amount - sumup_amount) > AMOUNT_TOLERANCE:
                continue

            # Tier 2: ±2 Minuten
            if sumup_ts and sale.date:
                try:
                    delta = abs((sumup_ts - sale.date.astimezone(timezone.utc)).total_seconds())
                    if delta <= TIME_WINDOW_MINUTES * 60:
                        matched_sale = sale
                        tier = ReconciliationItem.MatchTier.AMOUNT_TIME
                        break
                except Exception:
                    pass

            # Tier 3: Gleicher Tag
            if sumup_ts and sale.date:
                if sumup_ts.date() == sale.date.date():
                    matched_sale = sale
                    tier = ReconciliationItem.MatchTier.AMOUNT_DATE

        if matched_sale:
            sk_amount = Decimal(str(matched_sale.total_amount_gross))
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
                sk_timestamp=matched_sale.date,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=tx_code,
                sumup_amount=sumup_amount,
                sumup_fee=fee,
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
            matched_sumup_ids.add(tx_code)
        else:
            # Nur SumUp — kein SK-Eintrag
            item = ReconciliationItem(
                payout=payout,
                sumup_tx_id=txn.get('id', ''),
                sumup_tx_code=tx_code,
                sumup_amount=sumup_amount,
                sumup_fee=fee,
                sumup_timestamp=sumup_ts,
                match_tier=ReconciliationItem.MatchTier.NO_MATCH,
                match_status=ReconciliationItem.MatchStatus.ONLY_SUMUP,
                gap_amount=sumup_amount,
                gap_pct=Decimal('100'),
                channel=ReconciliationItem.Channel.UNKNOWN,
                resolution=ReconciliationItem.Resolution.PENDING,
            )
            items.append(item)

    # ── SK Sales ohne SumUp-Treffer ─────────────────────────────────────────
    for sale in unmatched_sales:
        if sale.id in matched_sale_ids:
            continue
        sk_amount = Decimal(str(sale.total_amount_gross))
        item = ReconciliationItem(
            payout=payout,
            sale=sale,
            sk_amount=sk_amount,
            sk_timestamp=sale.date,
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
