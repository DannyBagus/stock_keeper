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


class KlaraPostingError(Exception):
    pass


CLAIM = 'IN_ARBEIT'


def can_post_to_klara(user) -> bool:
    """
    Explizit vergebene Berechtigung (Benutzer oder Gruppe). Superuser-Status
    allein reicht bewusst NICHT — mehrere Ladenmitarbeiterinnen sind Superuser.
    """
    from django.contrib.auth.models import Permission
    from django.db.models import Q
    if not (user and user.is_active and user.is_staff):
        return False
    return Permission.objects.filter(
        Q(user=user) | Q(group__user=user),
        codename='post_to_klara', content_type__app_label='reconciliation',
    ).exists()


def post_to_klara(payout_pk: int, user) -> list[int]:
    """
    Bucht die Umsatzbuchung(en) eines grünen v2-Abgleichs in KLARA.

    Ablauf (damit nie doppelt gebucht wird und kein KLARA-Ergebnis verloren geht):
      1. kurze DB-Transaktion: prüfen + Abgleich mit CLAIM sperren (committed)
      2. KLARA-Kontoblatt 1101 auf vorhandene Buchung dieser Auszahlung prüfen
      3. buchen; jede erhaltene Buchungs-ID sofort speichern
      4. bei Fehler ohne gebuchte ID: Sperre lösen; mit ID: IDs bleiben stehen
    """
    from .booking import accounting_config
    from .klara import KlaraClient, KlaraError, find_existing_bookings

    if not can_post_to_klara(user):
        raise KlaraPostingError("Dir fehlt die Berechtigung «SumUp-Abgleich in KLARA buchen».")
    with transaction.atomic():
        payout = SumUpPayout.objects.select_for_update().get(pk=payout_pk)
        if payout.engine_version < 2:
            raise KlaraPostingError("Nur für Abgleiche nach neuer Methode möglich.")
        if payout.klara_booking_ids == CLAIM:
            raise KlaraPostingError("Buchung läuft bereits — bitte Seite neu laden.")
        if payout.is_booked_in_klara:
            raise KlaraPostingError(f"Bereits in KLARA gebucht (Buchung {payout.klara_booking_ids}).")
        summary = payout.booking_summary
        if not summary['is_balanced']:
            raise KlaraPostingError(
                f"Abgleich geht nicht auf ({summary['open_count']} offene Position(en)).")
        bookings = summary['proposal']['bookings']
        if summary['proposal']['clearing_balance'] != 0 or not bookings:
            raise KlaraPostingError("Buchungsvorschlag ist nicht ausgeglichen.")
        payout.klara_booking_ids = CLAIM
        payout.save(update_fields=['klara_booking_ids'])

    def store(ids):
        SumUpPayout.objects.filter(pk=payout.pk).update(klara_booking_ids=','.join(map(str, ids)))

    ids = []
    try:
        client = KlaraClient()
        existing = find_existing_bookings(client, payout, bookings,
                                          accounting_config()['clearing_account'])
        if existing:
            store(existing)
            raise KlaraPostingError(
                f"In KLARA existiert bereits eine Buchung dieser Auszahlung "
                f"({', '.join(map(str, existing))}) — nichts neu gebucht.")
        comment = (f"SumUp-Abgleich v2, Stock Keeper #{payout.pk} ({payout.sumup_reference}). "
                   f"Freigegeben von {user.get_full_name() or user.get_username()}. "
                   f"Gegenbuchung Gutschrift {payout.bank_credit_date:%d.%m.%Y} im Bankabgleich auf 1101.")
        for booking in bookings:
            ids.append(client.create_booking(booking, comment))
            store(ids)
    except KlaraError as e:
        if not ids:
            SumUpPayout.objects.filter(pk=payout.pk, klara_booking_ids=CLAIM).update(klara_booking_ids='')
            raise KlaraPostingError(str(e))
        raise KlaraPostingError(
            f"Teilweise gebucht ({', '.join(map(str, ids))}), dann Fehler: {e}. "
            f"Bitte in KLARA prüfen und den Rest von Hand buchen.")

    now = timezone.now()
    with transaction.atomic():
        payout = SumUpPayout.objects.select_for_update().get(pk=payout.pk)
        payout.klara_booked_at = now
        payout.klara_booked_by = user
        if payout.status == SumUpPayout.Status.IN_REVIEW:
            payout.items.filter(
                match_status=ReconciliationItem.MatchStatus.MATCHED,
                resolution=ReconciliationItem.Resolution.PENDING,
            ).update(resolution=ReconciliationItem.Resolution.ACCEPTED, resolved_at=now)
            payout.status = SumUpPayout.Status.COMPLETED
            payout.completed_at = now
        payout.save()
    logger.info("Abgleich #%s in KLARA gebucht: %s (durch %s)", payout.pk, ids, user)
    return ids
