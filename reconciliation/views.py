import logging
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.utils import timezone

from .models import SumUpPayout, ReconciliationItem
from .forms import PayoutStartForm
from .sumup_client import SumUpClient, SumUpAPIError
from .matching import run_matching
from .pdf import generate_voucher_pdf

logger = logging.getLogger(__name__)


@staff_member_required
def reconciliation_list(request):
    payouts = SumUpPayout.objects.all()
    context = admin.site.each_context(request)
    context.update({
        'title': 'SumUp Abgleich',
        'payouts': payouts,
    })
    return render(request, 'reconciliation/payout_list.html', context)


@staff_member_required
def reconciliation_start(request):
    if request.method == 'POST':
        form = PayoutStartForm(request.POST)
        if form.is_valid():
            return _process_payout(request, form)
    else:
        form = PayoutStartForm()

    context = admin.site.each_context(request)
    context.update({
        'title': 'Neuer SumUp Abgleich',
        'form': form,
    })
    return render(request, 'reconciliation/start_form.html', context)


@transaction.atomic
def _process_payout(request, form):
    """Erstellt Payout, ruft SumUp API auf, führt Matching durch."""
    payout = SumUpPayout(
        bank_credit_amount=form.cleaned_data['bank_credit_amount'],
        bank_credit_date=form.cleaned_data['bank_credit_date'],
        created_by=request.user,
        status=SumUpPayout.Status.DRAFT,
    )
    payout.save()

    try:
        client = SumUpClient()

        # Alle Payouts für dieses Gutschriftsdatum finden
        sumup_payouts = client.find_payouts_for_credit(
            payout.bank_credit_amount,
            payout.bank_credit_date
        )

        if not sumup_payouts:
            messages.warning(
                request,
                f"Kein passendes SumUp-Auszahlungsdatum für CHF {payout.bank_credit_amount} "
                f"um {payout.bank_credit_date} gefunden. Bitte Betrag und Datum prüfen."
            )
            payout.delete()
            return redirect('reconciliation:start')

        # Beträge aggregieren
        total_net = sum(Decimal(str(p.get('amount', 0))) for p in sumup_payouts)
        total_fees = sum(Decimal(str(p.get('fee', 0))) for p in sumup_payouts)
        payout.sumup_net_amount = total_net
        payout.sumup_fees_amount = total_fees
        payout.sumup_gross_amount = total_net + total_fees
        payout.sumup_payout_id = sumup_payouts[0].get('date', '')

        # Gebühren-Mapping: transaction_code → fee
        payout_fees = {
            p['transaction_code']: Decimal(str(p.get('fee', 0)))
            for p in sumup_payouts
        }

        # Transaktionen laden und Periode ermitteln
        transactions, period_start, period_end = client.get_payout_transactions(sumup_payouts)
        payout.period_start = period_start
        payout.period_end = period_end
        payout.save()

        if not period_start or not period_end:
            messages.warning(request, "Periode konnte nicht ermittelt werden.")
            return redirect('reconciliation:review', pk=payout.pk)

        # Matching durchführen
        items = run_matching(payout, transactions, payout_fees=payout_fees)
        ReconciliationItem.objects.bulk_create(items)

        payout.status = SumUpPayout.Status.IN_REVIEW
        payout.save()

        matched = sum(1 for i in items if i.match_status == 'MATCHED')
        total = len(items)
        messages.success(request, f"Abgleich erstellt: {matched}/{total} Transaktionen gematched.")

    except SumUpAPIError as e:
        logger.error(f"SumUp API Fehler: {e}")
        messages.error(request, f"SumUp API Fehler: {e}")
        payout.delete()
        return redirect('reconciliation:start')

    return redirect('reconciliation:review', pk=payout.pk)


@staff_member_required
def reconciliation_review(request, pk):
    payout = get_object_or_404(SumUpPayout, pk=pk)
    items = payout.items.select_related('sale__created_by').all()

    # Zusammenfassung berechnen
    matched_items = [i for i in items if i.match_status == 'MATCHED']
    only_sumup = [i for i in items if i.match_status == 'ONLY_SUMUP']
    only_sk = [i for i in items if i.match_status == 'ONLY_SK']
    gap_items = [i for i in items if i.match_status == 'GAP']

    total_matched = sum(i.sumup_amount or Decimal(0) for i in matched_items)
    total_fees = sum(i.sumup_fee or Decimal(0) for i in items if i.sumup_fee)
    laden_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in matched_items if i.channel == 'LADEN'
    )
    cafe_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in matched_items if i.channel == 'CAFE'
    )

    context = admin.site.each_context(request)
    context.update({
        'title': f'Abgleich: {payout}',
        'payout': payout,
        'items': items,
        'matched_count': len(matched_items),
        'only_sumup_count': len(only_sumup),
        'only_sk_count': len(only_sk),
        'gap_count': len(gap_items),
        'total_matched': total_matched,
        'total_fees': total_fees,
        'laden_total': laden_total,
        'cafe_total': cafe_total,
    })
    return render(request, 'reconciliation/review.html', context)


@staff_member_required
def resolve_item(request, pk, item_pk):
    """AJAX-Endpunkt: Diskrepanz auflösen."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    payout = get_object_or_404(SumUpPayout, pk=pk)
    item = get_object_or_404(ReconciliationItem, pk=item_pk, payout=payout)

    resolution = request.POST.get('resolution', '')
    note = request.POST.get('resolution_note', '')

    if resolution not in dict(ReconciliationItem.Resolution.choices):
        return JsonResponse({'error': 'Ungültige Resolution'}, status=400)

    item.resolution = resolution
    item.resolution_note = note
    item.resolved_at = timezone.now()

    # Bei Zahlungsart-Korrektur: Sale aktualisieren
    if resolution == 'PAYMENT_TYPE_CHANGED' and item.sale:
        new_method = request.POST.get('new_payment_method', '')
        if new_method:
            item.sale.payment_method = new_method
            item.sale.save()

    # Bei Sale-Löschung: Sale stornieren (Refund = Ware zurückbuchen)
    if resolution == 'SALE_DELETED' and item.sale:
        item.sale.refund(user=request.user)
        item.resolution_note = f"Storniert via Reconciliation (Sale #{item.sale.id})"

    item.save()
    return JsonResponse({'status': 'ok', 'resolution': item.get_resolution_display()})


@staff_member_required
def reconciliation_complete(request, pk):
    if request.method != 'POST':
        return redirect('reconciliation:review', pk=pk)

    payout = get_object_or_404(SumUpPayout, pk=pk)

    # Auto-accept alle MATCHED items die noch PENDING sind
    payout.items.filter(
        match_status=ReconciliationItem.MatchStatus.MATCHED,
        resolution=ReconciliationItem.Resolution.PENDING
    ).update(
        resolution=ReconciliationItem.Resolution.ACCEPTED,
        resolved_at=timezone.now()
    )

    payout.status = SumUpPayout.Status.COMPLETED
    payout.completed_at = timezone.now()
    payout.save()

    messages.success(request, "Abgleich abgeschlossen. PDF wird generiert.")
    return redirect('reconciliation:pdf', pk=payout.pk)


@staff_member_required
def reconciliation_pdf(request, pk):
    payout = get_object_or_404(SumUpPayout, pk=pk)
    return generate_voucher_pdf(payout)
