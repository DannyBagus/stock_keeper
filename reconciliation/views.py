import logging
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.utils import timezone

from commerce.models import Sale, SaleItem
from core.models import Product
from .models import SumUpPayout, ReconciliationItem
from .forms import PayoutStartForm
from .sumup_client import SumUpClient, SumUpAPIError
from .matching import run_matching, detect_channel
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
        # Tatsächlich abgerechneter Bruttobetrag pro Transaktion (Netto + Gebühr).
        # Weicht von txn.amount ab, wenn eine Teilrückerstattung abgezogen wurde.
        payout_settled = {
            p['transaction_code']: (
                Decimal(str(p.get('amount', 0))) + Decimal(str(p.get('fee', 0)))
            )
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
        items = run_matching(
            payout, transactions,
            payout_fees=payout_fees,
            payout_settled=payout_settled,
        )
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

    summary = payout.booking_summary

    context = admin.site.each_context(request)
    context.update({
        'title': f'Abgleich: {payout}',
        'payout': payout,
        'items': items,
    })
    context.update(summary)
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
@transaction.atomic
def create_sale_for_item(request, pk, item_pk):
    """
    Erstellt einen nachträglichen SumUp-Sale für einen ONLY_SUMUP ReconciliationItem.
    Default-Produkt: SKU 'DIVERSES'. Mit optionalem product_id kann ein
    konkretes Produkt gewählt werden.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    payout = get_object_or_404(SumUpPayout, pk=pk)
    item = get_object_or_404(ReconciliationItem, pk=item_pk, payout=payout)

    if item.match_status != ReconciliationItem.MatchStatus.ONLY_SUMUP:
        return JsonResponse({'error': 'Sale-Nachbuchung nur für "Nur SumUp"-Zeilen möglich.'}, status=400)
    if item.sale_id:
        return JsonResponse({'error': 'Für diese Zeile existiert bereits ein Sale.'}, status=400)
    if not item.sumup_amount:
        return JsonResponse({'error': 'SumUp-Betrag fehlt auf der Zeile.'}, status=400)

    product_id = request.POST.get('product_id', '').strip()
    if product_id:
        try:
            product = Product.objects.get(pk=int(product_id))
        except (Product.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Produkt nicht gefunden.'}, status=404)
    else:
        product = Product.objects.filter(sku='DIVERSES').first()
        if not product:
            return JsonResponse(
                {'error': 'Produkt mit SKU "DIVERSES" fehlt. Bitte im Admin anlegen.'},
                status=400,
            )

    sumup_amount = Decimal(str(item.sumup_amount))
    sale_date = item.sumup_timestamp or timezone.now()
    tx_code = item.sumup_tx_code or item.sumup_tx_id or None
    idem_key = f'recon-item-{item.pk}'

    existing = Sale.objects.filter(idempotency_key=idem_key).first()
    if existing:
        sale = existing
    else:
        sale = Sale.objects.create(
            date=sale_date,
            payment_method=Sale.PaymentMethod.SUMUP,
            channel=Sale.SalesChannel.POS,
            status=Sale.Status.COMPLETED,
            created_by=request.user,
            transaction_id=tx_code,
            idempotency_key=idem_key,
        )
        vat_rate = product.vat.rate if product.vat else Decimal('0.00')
        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=1,
            unit_price_gross=sumup_amount,
            vat_rate=vat_rate,
        )
        sale.total_amount_gross = sumup_amount
        if vat_rate and vat_rate > 0:
            sale.total_amount_net = (
                sumup_amount / (Decimal('1.00') + (vat_rate / Decimal('100.00')))
            ).quantize(Decimal('0.01'))
        else:
            sale.total_amount_net = sumup_amount
        sale.save()

    item.sale = sale
    item.sk_amount = sumup_amount
    item.sk_timestamp = sale.date
    item.match_status = ReconciliationItem.MatchStatus.MATCHED
    item.match_tier = ReconciliationItem.MatchTier.EXACT
    item.gap_amount = Decimal('0')
    item.gap_pct = Decimal('0')
    item.channel = detect_channel(sale)
    item.resolution = ReconciliationItem.Resolution.SALE_ADDED
    item.resolution_note = f"Sale #{sale.id} nacherfasst ({product.sku})"
    item.resolved_at = timezone.now()
    item.save()

    return JsonResponse({
        'status': 'ok',
        'sale_id': sale.id,
        'product_sku': product.sku,
        'product_name': str(product),
        'resolution': item.get_resolution_display(),
    })


def _live_settled_for_item(item):
    """
    Holt live aus der SumUp-API den tatsächlich abgerechneten Betrag
    (Payout-Netto + Gebühr) für die Transaktion dieses Items.
    Gibt Decimal zurück oder None, wenn die Transaktion nicht gefunden wird.
    """
    if not item.sumup_tx_code:
        return None
    payout = item.payout
    client = SumUpClient()
    sumup_payouts = client.find_payouts_for_credit(
        payout.bank_credit_amount, payout.bank_credit_date
    )
    for p in sumup_payouts:
        if p.get('transaction_code') == item.sumup_tx_code:
            return Decimal(str(p.get('amount', 0))) + Decimal(str(p.get('fee', 0)))
    return None


@staff_member_required
def align_sale_to_sumup(request, pk, item_pk):
    """
    Gleicht einen Verkauf an den tatsächlich von SumUp abgerechneten Betrag an.

    Eine SumUp-Teilrückerstattung reduziert nur das Payout-Netto, nicht den
    ursprünglichen Verkaufsbetrag. Diese View bucht die Differenz als negative
    Korrektur-Position (Produkt SKU "SUMUP-REFUND", lagerneutral) zum MwSt-Satz
    des Verkaufs, sodass Umsatz und MwSt in Stock Keeper der Abrechnung entsprechen.

    GET  → Live-Abgleich mit SumUp, liefert die aktuelle Differenz (ohne Änderung).
    POST → bucht die Korrektur-Position und markiert die Zeile als erledigt.
    """
    payout = get_object_or_404(SumUpPayout, pk=pk)
    item = get_object_or_404(ReconciliationItem, pk=item_pk, payout=payout)

    if not item.sale_id:
        return JsonResponse({'error': 'Zeile hat keinen verknüpften Verkauf.'}, status=400)

    sale = item.sale

    try:
        settled = _live_settled_for_item(item)
    except SumUpAPIError as e:
        return JsonResponse({'error': f'SumUp API Fehler: {e}'}, status=502)

    if settled is None:
        return JsonResponse({'error': 'Transaktion in SumUp nicht gefunden.'}, status=404)

    sk_gross = Decimal(str(sale.total_amount_gross))
    delta = (sk_gross - settled).quantize(Decimal('0.01'))

    sale_items = list(sale.items.all())
    dominant_rate = Decimal('0.00')
    if sale_items:
        dominant_rate = (
            max(sale_items, key=lambda it: it.total_price_gross).vat_rate or Decimal('0.00')
        )
    mixed_rates = len({(it.vat_rate or Decimal('0.00')) for it in sale_items}) > 1
    already_done = item.resolution != ReconciliationItem.Resolution.PENDING

    if request.method != 'POST':
        return JsonResponse({
            'sale_id': sale.id,
            'sumup_tx_code': item.sumup_tx_code,
            'sk_gross': str(sk_gross),
            'settled': str(settled),
            'delta': str(delta),
            'vat_rate': str(dominant_rate),
            'mixed_rates': mixed_rates,
            'already_done': already_done,
        })

    # ── POST: Korrektur anwenden ──
    if already_done:
        return JsonResponse({'error': 'Diese Zeile wurde bereits bearbeitet.'}, status=400)
    if delta <= Decimal('0.00'):
        return JsonResponse({'error': 'Keine positive Differenz zu korrigieren.'}, status=400)

    with transaction.atomic():
        refund_product, _created = Product.objects.get_or_create(
            sku='SUMUP-REFUND',
            defaults={
                'name': 'SumUp-Rückerstattung (Korrektur)',
                'cost_price': Decimal('0.00'),
                'sales_price': Decimal('0.00'),
                'track_stock': False,
                'is_active': True,
            },
        )
        # Negative Korrektur-Position; lagerneutral (track_stock=False)
        SaleItem.objects.create(
            sale=sale,
            product=refund_product,
            quantity=1,
            unit_price_gross=(-delta),
            vat_rate=dominant_rate,
        )
        # Totale (Brutto + Netto) aus allen Positionen neu berechnen
        total_gross = sum((it.total_price_gross for it in sale.items.all()), Decimal('0.00'))
        total_net = Decimal('0.00')
        for it in sale.items.all():
            rate = it.vat_rate or Decimal('0.00')
            total_net += it.total_price_gross / (Decimal('1.00') + rate / Decimal('100.00'))
        sale.total_amount_gross = total_gross
        sale.total_amount_net = total_net.quantize(Decimal('0.01'))
        sale.save()

        # Reconciliation-Zeile gilt jetzt als abgeglichen (SK == abgerechnet)
        item.sk_amount = settled
        item.gap_amount = Decimal('0.00')
        item.gap_pct = Decimal('0.00')
        item.resolution = ReconciliationItem.Resolution.MANUAL
        item.resolution_note = (
            f"SumUp-Teilrückerstattung CHF {delta} als Korrektur-Position gebucht "
            f"(Verkauf #{sale.id}, MwSt {dominant_rate}%)."
        )
        item.resolved_at = timezone.now()
        item.save()

    return JsonResponse({
        'status': 'ok',
        'sale_id': sale.id,
        'correction_amount': str(delta),
        'new_gross': str(total_gross),
        'vat_rate': str(dominant_rate),
    })


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

    return JsonResponse({'status': 'ok'})


@staff_member_required
def reconciliation_pdf(request, pk):
    payout = get_object_or_404(SumUpPayout, pk=pk)
    return generate_voucher_pdf(payout)
