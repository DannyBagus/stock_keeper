"""
Matching-Engine v2: verbindet ein SumUp-Auszahlungs-Ledger exakt mit Stock Keeper.

Schlüssel ist ausschliesslich der SumUp transaction_code, den die POS-Kasse
als Sale.transaction_id speichert. Keine Betrag/Zeit-Heuristik mehr — sie hat
in der Vergangenheit Refunds als Zahlungen und Zahlungen der falschen
Transaktion zugeordnet.

Die Beträge für die Buchhaltung kommen immer aus SumUp. Stock Keeper liefert
den Kanal (Laden / Café) und wird auf Konsistenz geprüft:

  MATCHED     genau ein aktiver SK-Sale, Betrag identisch
  GAP         genau ein Sale, Betrag weicht ab           → prüfen
  DUPLICATE   mehrere aktive Sales zum selben Code         → Duplikat stornieren
  ONLY_SUMUP  kein Sale                                    → nacherfassen / Kanal setzen
  REFUND      Rückerstattung (Erlösminderung), Kanal vom Original-Sale
  ONLY_SK     SK-Sale (SUMUP) im Zeitraum ohne SumUp-Zahlung → Zahlungsart / Storno
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from commerce.models import Sale
from .ledger import PayoutLedger
from .models import ReconciliationItem, SumUpPayout

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo('Europe/Zurich')
CAFE_CATEGORY_KEYWORDS = ['café', 'cafe', 'kaffee', 'coffee', 'snack', 'getränk', 'food']
AMOUNT_TOLERANCE = Decimal('0.01')
RECON_IDEM_PREFIX = 'recon-item-'

Item = ReconciliationItem
Status = ReconciliationItem.MatchStatus


def detect_channel(sale: Sale | None) -> str:
    if sale is None:
        return Item.Channel.UNKNOWN
    categories = [
        item.product.category.name.lower()
        for item in sale.items.select_related('product__category').all()
        if item.product and item.product.category
    ]
    if not categories:
        return Item.Channel.UNKNOWN
    cafe_count = sum(1 for cat in categories if any(kw in cat for kw in CAFE_CATEGORY_KEYWORDS))
    if cafe_count / len(categories) > 0.5:
        return Item.Channel.CAFE
    return Item.Channel.LADEN


def _primary_sale(sales: list[Sale]) -> Sale:
    """Bei Duplikaten: der an der Kasse erfasste Sale (nicht die Recon-Nachbuchung)."""
    def key(s):
        return ((s.idempotency_key or '').startswith(RECON_IDEM_PREFIX), s.date, s.id)
    return sorted(sales, key=key)[0]


def _line_item(payout, line, sales) -> ReconciliationItem:
    item = Item(
        payout=payout,
        sumup_tx_id=line.transaction_id,
        sumup_tx_code=line.transaction_code,
        sumup_amount=line.gross,
        sumup_fee=line.fee,
        sumup_net=line.net,
        sumup_refund_deduction=line.deduction,
        sumup_timestamp=line.timestamp,
        match_tier=Item.MatchTier.EXACT,
        resolution=Item.Resolution.PENDING,
    )
    active = [s for s in sales if s.status == Sale.Status.COMPLETED]
    candidates = active or sales

    if not candidates:
        item.match_tier = Item.MatchTier.NO_MATCH
        item.match_status = Status.ONLY_SUMUP
        item.gap_amount = line.gross or Decimal('0')
        item.gap_pct = Decimal('100')
        item.channel = Item.Channel.UNKNOWN
        return item

    sale = _primary_sale(candidates)
    item.sale = sale
    item.sk_amount = sale.total_amount_gross
    item.sk_timestamp = sale.date
    item.channel = detect_channel(sale)

    if len(active) > 1:
        item.match_status = Status.DUPLICATE
        others = sorted(s.id for s in active if s.id != sale.id)
        item.resolution_note = 'Duplikate: ' + ', '.join(f'#{i}' for i in others)
        return item

    gap = abs((sale.total_amount_gross or Decimal('0')) - (line.gross or Decimal('0')))
    item.gap_amount = gap
    if line.gross and gap > AMOUNT_TOLERANCE:
        item.match_status = Status.GAP
        item.gap_pct = (gap / line.gross * 100).quantize(Decimal('0.01'))
    else:
        item.match_status = Status.MATCHED
    return item


def run_matching(payout: SumUpPayout, ledger: PayoutLedger,
                 known_payment_codes: set[str]) -> list[ReconciliationItem]:
    """
    Erstellt (ungespeichert) alle ReconciliationItems einer Auszahlung.

    known_payment_codes: alle erfolgreichen SumUp-Zahlungscodes im geladenen
        Zeitfenster — ein SK-Sale mit einem dieser Codes ist kein ONLY_SK, auch
        wenn er (noch) nicht in dieser Auszahlung steckt.
    """
    codes = [l.transaction_code for l in ledger.lines]
    refund_codes = [r.refunded_code for r in ledger.refunds]
    sales_by_code: dict[str, list[Sale]] = {}
    for s in (Sale.objects.filter(transaction_id__in=set(codes) | set(refund_codes))
              .prefetch_related('items__product__category')):
        sales_by_code.setdefault(s.transaction_id, []).append(s)

    items = [_line_item(payout, line, sales_by_code.get(line.transaction_code, []))
             for line in ledger.lines]

    for refund in ledger.refunds:
        sales = sales_by_code.get(refund.refunded_code, [])
        sale = _primary_sale(sales) if sales else None
        channel = detect_channel(sale)
        items.append(Item(
            payout=payout,
            sale=sale,
            sk_amount=sale.total_amount_gross if sale else None,
            sk_timestamp=sale.date if sale else None,
            sumup_tx_code=refund.refunded_code,
            sumup_amount=refund.amount,
            sumup_timestamp=datetime.combine(refund.refund_date, time(12), LOCAL_TZ),
            match_tier=Item.MatchTier.EXACT,
            match_status=Status.REFUND,
            channel=channel,
            resolution=(Item.Resolution.ACCEPTED if channel != Item.Channel.UNKNOWN
                        else Item.Resolution.PENDING),
            resolution_note=f'Abgezogen von {refund.deducted_from_code} am '
                            f'{refund.refund_date:%d.%m.%Y}',
        ))

    items.extend(_only_sk_items(payout, ledger, known_payment_codes))
    return items


def _only_sk_items(payout, ledger, known_payment_codes):
    """SK-Sales (SUMUP, aktiv) im Zeitraum der Auszahlung ohne SumUp-Zahlung."""
    if not ledger.period_start:
        return []
    start = datetime.combine(ledger.period_start, time.min, LOCAL_TZ)
    end = datetime.combine(ledger.period_end + timedelta(days=1), time.min, LOCAL_TZ)
    already_flagged = set(
        Item.objects.filter(match_status=Status.ONLY_SK, payout__engine_version__gte=2)
        .exclude(payout__status=SumUpPayout.Status.SUPERSEDED)
        .exclude(payout=payout)
        .values_list('sale_id', flat=True)
    )
    out = []
    for sale in (Sale.objects.filter(payment_method=Sale.PaymentMethod.SUMUP,
                                     status=Sale.Status.COMPLETED,
                                     date__gte=start, date__lt=end)
                 .prefetch_related('items__product__category')):
        if sale.transaction_id and sale.transaction_id in known_payment_codes:
            continue
        if sale.id in already_flagged:
            continue
        out.append(Item(
            payout=payout,
            sale=sale,
            sk_amount=sale.total_amount_gross,
            sk_timestamp=sale.date,
            sumup_tx_code=sale.transaction_id or '',
            match_tier=Item.MatchTier.NO_MATCH,
            match_status=Status.ONLY_SK,
            gap_amount=sale.total_amount_gross,
            gap_pct=Decimal('100'),
            channel=detect_channel(sale),
            resolution=Item.Resolution.PENDING,
        ))
    return out
