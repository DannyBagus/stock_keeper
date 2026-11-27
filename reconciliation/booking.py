"""
Buchungsvorschlag (Engine v2) nach der Durchlaufkonto-Methode.

  A) Umsatzbuchung je Kalendermonat der Zahlungen (Buchungsdatum = Monatsende):
       Haben 3000  Kartenumsatz Laden / Café         (Brutto aus SumUp)
       Soll  3800  Rückerstattungen Laden / Café
       Soll  6000  SumUp-Gebühren (+ nicht erstattete Refund-Gebühren)
       Soll  1101  Forderung gegenüber SumUp         (= Saldo)
  B) Bankgutschrift (Buchungsdatum = Auszahlungstag), im KLARA-Bankabgleich:
       Soll  1020  Bank / Haben 1101                 (= Payout-Netto)

Nach A + B ist der Saldo von 1101 für diese Auszahlung exakt 0.
Konten und Tags sind über settings.SUMUP_ACCOUNTING übersteuerbar.
"""

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from .models import BALANCE_TOLERANCE, ReconciliationItem

LOCAL_TZ = ZoneInfo('Europe/Zurich')
ZERO = Decimal('0.00')

DEFAULT_ACCOUNTING = {
    'clearing_account': 1101,   # Forderungen gegenüber Payment-Service-Provider
    'bank_account': 1020,
    'revenue_account': 3000,
    'refund_account': 3800,     # Erlösminderungen
    'fee_account': 6000,        # Übriger betrieblicher Aufwand
    'tags': {
        'LADEN': 'Erlös SupportElle',
        'CAFE': 'Erlös Gastro',
        'UNKNOWN': 'Erlös unbekannt',
        'fees': 'SumUp Gebühren',
        'clearing': 'SumUp',
        'bank': 'Bankabgleich',
    },
}

CHANNEL_LABELS = {'LADEN': 'Laden', 'CAFE': 'Café', 'UNKNOWN': 'Kanal unbekannt'}
Status = ReconciliationItem.MatchStatus
LINE_STATUSES = (Status.MATCHED, Status.GAP, Status.DUPLICATE, Status.ONLY_SUMUP)


def accounting_config() -> dict:
    cfg = {**DEFAULT_ACCOUNTING, **getattr(settings, 'SUMUP_ACCOUNTING', {})}
    cfg['tags'] = {**DEFAULT_ACCOUNTING['tags'], **cfg.get('tags', {})}
    return cfg


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _local_month(ts, fallback: date) -> date:
    if ts is None:
        return _month_end(fallback)
    return _month_end(ts.astimezone(LOCAL_TZ).date())


def _short_ref(payout) -> str:
    ref = payout.sumup_reference or ''
    return ref.split()[1] if ref.startswith('SUMUP ') and len(ref.split()) > 1 else ref


def booking_proposal(payout, items=None) -> dict:
    cfg = accounting_config()
    tags = cfg['tags']
    items = list(items if items is not None else payout.items.all())
    fallback = payout.period_end or payout.bank_credit_date

    revenue = defaultdict(lambda: defaultdict(lambda: ZERO))
    refunds = defaultdict(lambda: defaultdict(lambda: ZERO))
    fees = defaultdict(lambda: ZERO)
    for i in items:
        if i.match_status in LINE_STATUSES:
            month = _local_month(i.sumup_timestamp, fallback)
            revenue[month][i.channel] += i.sumup_amount or ZERO
            fees[month] += i.sumup_fee or ZERO
        elif i.match_status == Status.REFUND:
            month = _local_month(i.sumup_timestamp, fallback)
            refunds[month][i.channel] += i.sumup_amount or ZERO
    if payout.extra_fees_amount:
        month = _month_end(fallback)
        fees[month] += payout.extra_fees_amount

    ref = _short_ref(payout)
    bookings = []
    for month in sorted(set(revenue) | set(refunds) | set(fees)):
        label = f"{month:%m/%Y}"
        lines = []
        for channel in ('LADEN', 'CAFE', 'UNKNOWN'):
            amount = revenue[month].get(channel, ZERO)
            if amount:
                lines.append({
                    'account': cfg['revenue_account'], 'side': 'CR', 'amount': amount,
                    'text': f"SumUp Kartenumsatz {CHANNEL_LABELS[channel]} {label} ({ref})",
                    'tag': tags[channel],
                })
        for channel in ('LADEN', 'CAFE', 'UNKNOWN'):
            amount = refunds[month].get(channel, ZERO)
            if amount:
                lines.append({
                    'account': cfg['refund_account'], 'side': 'DR', 'amount': amount,
                    'text': f"SumUp Rückerstattungen {CHANNEL_LABELS[channel]} {label} ({ref})",
                    'tag': tags[channel],
                })
        if fees[month]:
            lines.append({
                'account': cfg['fee_account'], 'side': 'DR', 'amount': fees[month],
                'text': f"SumUp Transaktionsgebühren {label} ({ref})",
                'tag': tags['fees'],
            })
        credit = sum((l['amount'] for l in lines if l['side'] == 'CR'), ZERO)
        debit = sum((l['amount'] for l in lines if l['side'] == 'DR'), ZERO)
        clearing = credit - debit
        if clearing:
            lines.insert(0, {
                'account': cfg['clearing_account'],
                'side': 'DR' if clearing > 0 else 'CR',
                'amount': abs(clearing),
                'text': f"SumUp Abrechnung {label} ({ref}) – Gutschrift "
                        f"{payout.bank_credit_date:%d.%m.%Y}",
                'tag': tags['clearing'],
            })
        bookings.append({'date': month, 'label': label, 'lines': lines,
                         'clearing': clearing})

    clearing_total = sum((b['clearing'] for b in bookings), ZERO)
    bank_booking = {
        'date': payout.bank_credit_date,
        'lines': [
            {'account': cfg['bank_account'], 'side': 'DR', 'amount': payout.bank_credit_amount,
             'text': payout.sumup_reference or 'SumUp Gutschrift', 'tag': tags['bank']},
            {'account': cfg['clearing_account'], 'side': 'CR', 'amount': payout.bank_credit_amount,
             'text': f"SumUp Gutschrift {payout.bank_credit_date:%d.%m.%Y} ({ref})",
             'tag': tags['clearing']},
        ],
    }
    return {
        'bookings': bookings,
        'bank_booking': bank_booking,
        'clearing_total': clearing_total,
        'clearing_balance': clearing_total - payout.bank_credit_amount,
    }


def ledger_summary(payout) -> dict:
    items = list(payout.items.select_related('sale').all())
    lines = [i for i in items if i.match_status in LINE_STATUSES]
    refund_items = [i for i in items if i.match_status == Status.REFUND]

    def by_channel(rows, channel):
        return sum((i.sumup_amount or ZERO for i in rows if i.channel == channel), ZERO)

    laden, cafe, unknown = (by_channel(lines, c) for c in ('LADEN', 'CAFE', 'UNKNOWN'))
    refunds_total = sum((i.sumup_amount or ZERO for i in refund_items), ZERO)
    line_fees = sum((i.sumup_fee or ZERO for i in lines), ZERO)
    total_fees = line_fees + (payout.extra_fees_amount or ZERO)
    computed_net = laden + cafe + unknown - refunds_total - total_fees
    net_delta = computed_net - payout.bank_credit_amount

    def is_open(i):
        if i.match_status in (Status.MATCHED, Status.REFUND):
            return i.channel == ReconciliationItem.Channel.UNKNOWN
        if i.resolution == ReconciliationItem.Resolution.PENDING:
            return True
        return i.match_status != Status.ONLY_SK and i.channel == ReconciliationItem.Channel.UNKNOWN

    open_items = [i for i in items if is_open(i)]
    warnings = [w for w in (payout.warnings or '').splitlines() if w.strip()]
    net_matches = abs(net_delta) <= BALANCE_TOLERANCE
    proposal = booking_proposal(payout, items)

    counts = defaultdict(int)
    for i in items:
        counts[i.match_status] += 1

    return {
        'engine_version': 2,
        'laden_total': laden,
        'cafe_total': cafe,
        'unknown_total': unknown,
        'gross_total': laden + cafe + unknown,
        'refunds_total': refunds_total,
        'line_fees': line_fees,
        'extra_fees': payout.extra_fees_amount or ZERO,
        'total_fees': total_fees,
        'computed_net': computed_net,
        'net_delta': net_delta,
        'net_matches': net_matches,
        'matched_count': counts[Status.MATCHED],
        'gap_count': counts[Status.GAP],
        'duplicate_count': counts[Status.DUPLICATE],
        'only_sumup_count': counts[Status.ONLY_SUMUP],
        'only_sk_count': counts[Status.ONLY_SK],
        'refund_count': counts[Status.REFUND],
        'open_items': open_items,
        'open_count': len(open_items),
        'has_open_items': bool(open_items),
        'warnings': warnings,
        'is_balanced': net_matches and not open_items and not warnings,
        'proposal': proposal,
        # Kompatibilität mit v1-Templates
        'refund_deductions': payout.extra_fees_amount or ZERO,
        'deduction_lines': [],
        'total_matched': sum((i.sumup_amount or ZERO for i in lines
                              if i.match_status == Status.MATCHED), ZERO),
    }
