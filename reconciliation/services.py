"""
Orchestrierung Engine v2: SumUp-Auszahlung → Abgleich in Stock Keeper.

Kein manuelles Erfassen von Betrag/Datum mehr: Auszahlungen werden über ihre
SumUp-Referenz (= Bankmitteilung) identifiziert.
"""

import logging
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from .ledger import build_ledgers, group_by_reference
from .matching import run_matching
from .models import ReconciliationItem, SumUpPayout
from .sumup_client import SumUpClient, load_ledger_inputs

logger = logging.getLogger(__name__)


class ReconciliationError(Exception):
    pass


def list_sumup_payouts(client: SumUpClient, since: date) -> list[dict]:
    """Alle Bankgutschriften seit `since`, mit verknüpftem Abgleich (falls vorhanden)."""
    entries = client.get_payout_entries(since, timezone.localdate() + timedelta(days=1))
    existing = {
        p.sumup_reference: p
        for p in SumUpPayout.objects.filter(sumup_reference__isnull=False)
        .exclude(status=SumUpPayout.Status.SUPERSEDED)
    }
    legacy = {
        (p.bank_credit_date, p.bank_credit_amount): p
        for p in SumUpPayout.objects.filter(engine_version=1)
        .exclude(status=SumUpPayout.Status.SUPERSEDED)
    }
    rows = []
    for ref, info in group_by_reference(entries).items():
        payout = existing.get(ref) or legacy.get((info['date'], info['amount']))
        rows.append({'reference': ref, **info, 'payout': payout})
    return sorted(rows, key=lambda r: r['date'], reverse=True)


def _load_ledger(client, reference: str, payout_date: date):
    entries, txns = load_ledger_inputs(client, payout_date)
    ledger = build_ledgers(entries, txns).get(reference)
    if ledger is not None and not ledger.is_complete:
        # Ältere Zahlungen in der Auszahlung → Transaktionsfenster vergrössern
        txns = client.get_transactions_in_window(payout_date - timedelta(days=400), payout_date)
        ledger = build_ledgers(entries, txns).get(reference)
    return ledger, txns


@transaction.atomic
def create_reconciliation(reference: str, payout_date: date, user=None,
                          client: SumUpClient | None = None) -> SumUpPayout:
    """
    Erstellt den Abgleich für eine SumUp-Auszahlung. Ein bestehender, nicht
    abgeschlossener v1-Abgleich derselben Gutschrift wird als SUPERSEDED markiert.
    """
    if SumUpPayout.objects.filter(sumup_reference=reference).exists():
        raise ReconciliationError(f"Abgleich für {reference} existiert bereits.")

    client = client or SumUpClient()
    ledger, txns = _load_ledger(client, reference, payout_date)
    if ledger is None:
        raise ReconciliationError(f"Auszahlung {reference} bei SumUp nicht gefunden.")

    for old in SumUpPayout.objects.filter(
        engine_version=1, bank_credit_date=ledger.payout_date,
        bank_credit_amount=ledger.bank_amount,
    ).exclude(status=SumUpPayout.Status.SUPERSEDED):
        old.status = SumUpPayout.Status.SUPERSEDED
        old.save(update_fields=['status'])
        logger.info("Abgleich #%s (v1) durch Neuabgleich %s ersetzt", old.pk, reference)

    payout = SumUpPayout.objects.create(
        sumup_reference=reference,
        sumup_payout_id=reference,
        engine_version=2,
        bank_credit_amount=ledger.bank_amount,
        bank_credit_date=ledger.payout_date,
        sumup_net_amount=ledger.bank_amount,
        sumup_fees_amount=ledger.fee_total + ledger.extra_fee,
        sumup_gross_amount=ledger.gross_total,
        refunds_amount=ledger.refund_total,
        extra_fees_amount=ledger.extra_fee,
        period_start=ledger.period_start,
        period_end=ledger.period_end,
        warnings='\n'.join(ledger.warnings),
        created_by=user,
        status=SumUpPayout.Status.IN_REVIEW,
    )
    known_codes = {
        t.get('transaction_code') for t in txns
        if t.get('type') == 'PAYMENT' and t.get('status') == 'SUCCESSFUL'
    }
    ReconciliationItem.objects.bulk_create(run_matching(payout, ledger, known_codes))
    return payout


def sync_new_payouts(since: date, user=None, client: SumUpClient | None = None):
    """Legt für jede noch nicht abgeglichene Auszahlung seit `since` einen Abgleich an."""
    client = client or SumUpClient()
    created = []
    for row in list_sumup_payouts(client, since):
        payout = row['payout']
        if payout is not None and (payout.engine_version >= 2
                                   or payout.status == SumUpPayout.Status.COMPLETED):
            continue
        created.append(create_reconciliation(row['reference'], row['date'],
                                             user=user, client=client))
    return created
