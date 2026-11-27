"""
SumUp-Auszahlungs-Ledger (reine Rechenlogik, kein ORM).

Datengrundlage ist ausschliesslich das SumUp Payouts-API
(GET /v0.1/me/financials/payouts) plus die Transaktionshistorie für den
Bruttobetrag jeder Zahlung. SumUp ist die Ground Truth für das Geld;
Stock Keeper liefert später nur den Kanal-Split (Laden / Café).

Aufbau der Payouts-API (verifiziert an Echtdaten 2025-06 … 2026-11):

  type=PAYOUT            Eine Zeile pro ausbezahlter Zahlung.
                         transaction_code = Zahlung, amount = Netto, fee = Gebühr,
                         reference = Bankmitteilung "SUMUP PIDxxxx PAYOUT ddmmyy".
                         Alle Zeilen einer Bankgutschrift teilen diese reference.
  type=REFUND_DEDUCTION  Abzug wegen einer Rückerstattung.
                         transaction_code = Zahlung, von deren Auszahlungszeile
                         abgezogen wurde; reference = rückerstattete Zahlung.

Pro Auszahlungszeile gilt:  Abzug = Brutto − Gebühr − Netto.
Ein Abzug ist entweder
  * eine Rückerstattung einer AUSBEZAHLTEN Zahlung  → Erlösminderung, oder
  * die nicht erstattete Gebühr einer Rückerstattung, deren Zahlung nie
    ausbezahlt wurde (Voll-Refund vor Auszahlung) → zusätzliche Gebühr.
Eine Rückerstattung zählt nur als Erlösminderung, wenn die rückerstattete
Zahlung selbst in dieser oder einer früheren Auszahlung enthalten war —
sonst war ihr Umsatz nie Teil einer Gutschrift.

Damit gilt pro Auszahlung exakt:
    Bankgutschrift = Σ Brutto − Σ Rückerstattungen − Σ Gebühren − Zusatzgebühren
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

ZERO = Decimal('0.00')


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal('0.01'))


def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except ValueError:
        return None


@dataclass
class PayoutLine:
    transaction_code: str
    net: Decimal
    fee: Decimal
    gross: Decimal | None = None          # aus Transaktionshistorie
    timestamp: datetime | None = None     # Zahlungszeitpunkt (UTC)
    transaction_id: str = ''              # SumUp-Transaktions-UUID

    @property
    def deduction(self) -> Decimal:
        if self.gross is None:
            return ZERO
        return self.gross - self.fee - self.net


@dataclass
class Refund:
    refunded_code: str        # rückerstattete Zahlung
    deducted_from_code: str   # Auszahlungszeile, von der abgezogen wurde
    amount: Decimal
    refund_date: date


@dataclass
class PayoutLedger:
    reference: str
    payout_date: date
    lines: list[PayoutLine] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    # REFUND_DEDUCTION-Einträge, die keine Erlösminderung sind (Info)
    fee_deductions: list[Refund] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bank_amount(self) -> Decimal:
        return sum((l.net for l in self.lines), ZERO)

    @property
    def gross_total(self) -> Decimal:
        return sum((l.gross or ZERO for l in self.lines), ZERO)

    @property
    def fee_total(self) -> Decimal:
        return sum((l.fee for l in self.lines), ZERO)

    @property
    def refund_total(self) -> Decimal:
        return sum((r.amount for r in self.refunds), ZERO)

    @property
    def deduction_total(self) -> Decimal:
        return sum((l.deduction for l in self.lines), ZERO)

    @property
    def extra_fee(self) -> Decimal:
        """Abzüge, die keine Erlösminderung sind (nicht erstattete Refund-Gebühren)."""
        return self.deduction_total - self.refund_total

    @property
    def is_complete(self) -> bool:
        return all(l.gross is not None for l in self.lines)

    @property
    def period_start(self) -> date | None:
        ts = [l.timestamp for l in self.lines if l.timestamp]
        return min(ts).date() if ts else None

    @property
    def period_end(self) -> date | None:
        ts = [l.timestamp for l in self.lines if l.timestamp]
        return max(ts).date() if ts else None


def build_ledgers(payout_entries: list[dict], transactions: list[dict]) -> dict[str, PayoutLedger]:
    """
    Baut für jede Bankgutschrift (reference) ein Ledger.

    payout_entries: Rohdaten des Payouts-API — müssen auch frühere Auszahlungen
        enthalten, damit erkennbar ist, ob eine rückerstattete Zahlung schon
        ausbezahlt war.
    transactions: Transaktionshistorie (PAYMENT/REFUND, beliebiger Status),
        mindestens für alle Zahlungen der betrachteten Auszahlungen.
    """
    payments = {}
    for t in transactions:
        if t.get('type') == 'PAYMENT' and t.get('status') == 'SUCCESSFUL':
            payments[t.get('transaction_code')] = t

    payout_rows = [e for e in payout_entries if e.get('type') == 'PAYOUT']
    deduction_rows = [e for e in payout_entries if e.get('type') == 'REFUND_DEDUCTION']

    ledgers: dict[str, PayoutLedger] = {}
    code_to_ledger: dict[str, PayoutLedger] = {}
    for row in sorted(payout_rows, key=lambda r: r.get('date', '')):
        ref = row.get('reference') or f"PAYOUT {row.get('date')}"
        ledger = ledgers.get(ref)
        if ledger is None:
            ledger = ledgers[ref] = PayoutLedger(
                reference=ref, payout_date=date.fromisoformat(row['date']),
            )
        code = row.get('transaction_code', '')
        txn = payments.get(code)
        line = PayoutLine(
            transaction_code=code,
            net=_d(row.get('amount')),
            fee=_d(row.get('fee')),
            gross=_d(txn['amount']) if txn else None,
            timestamp=parse_ts(txn.get('timestamp')) if txn else None,
            transaction_id=(txn or {}).get('id', ''),
        )
        if txn is None:
            ledger.warnings.append(
                f"Zahlung {code} nicht in der SumUp-Transaktionshistorie gefunden — "
                f"Bruttobetrag unbekannt."
            )
        ledger.lines.append(line)
        code_to_ledger[code] = ledger

    for row in deduction_rows:
        ledger = code_to_ledger.get(row.get('transaction_code', ''))
        if ledger is None:
            # Abzug auf einer Zahlung, die nie ausbezahlt wurde (Voll-Refund
            # vor Auszahlung) — wirkt auf keine Bankgutschrift.
            continue
        refunded_code = row.get('reference') or row.get('transaction_code')
        refund = Refund(
            refunded_code=refunded_code,
            deducted_from_code=row.get('transaction_code', ''),
            amount=_d(row.get('amount')),
            refund_date=date.fromisoformat(row['date']),
        )
        refunded_ledger = code_to_ledger.get(refunded_code)
        was_paid_out = (
            refunded_ledger is not None
            and refunded_ledger.payout_date <= ledger.payout_date
        )
        if was_paid_out:
            ledger.refunds.append(refund)
        else:
            ledger.fee_deductions.append(refund)

    for ledger in ledgers.values():
        if not ledger.is_complete:
            continue
        extra = ledger.extra_fee
        if extra < ZERO:
            ledger.warnings.append(
                f"Rückerstattungen ({ledger.refund_total}) übersteigen die Abzüge "
                f"({ledger.deduction_total}) — bitte SumUp-Daten prüfen."
            )
        elif extra > ZERO and not (ledger.refunds or ledger.fee_deductions):
            ledger.warnings.append(
                f"Abzug von CHF {extra} ohne Rückerstattungs-Eintrag bei SumUp — "
                f"Ursache unklar (Chargeback?)."
            )
    return ledgers


def group_by_reference(payout_entries: list[dict]) -> dict[str, dict]:
    """Übersicht aller Bankgutschriften: reference → {date, amount, count}."""
    out = defaultdict(lambda: {'date': None, 'amount': ZERO, 'count': 0})
    for e in payout_entries:
        if e.get('type') != 'PAYOUT':
            continue
        ref = e.get('reference') or f"PAYOUT {e.get('date')}"
        out[ref]['date'] = date.fromisoformat(e['date'])
        out[ref]['amount'] += _d(e.get('amount'))
        out[ref]['count'] += 1
    return dict(out)
