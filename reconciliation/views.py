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
from datetime import date
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo('Europe/Zurich')

from .models import SumUpPayout, ReconciliationItem
from .sumup_client import SumUpClient, SumUpAPIError
from .matching import detect_channel
from .pdf import generate_voucher_pdf
from .services import (
    KlaraPostingError, ReconciliationError, can_post_to_klara, create_reconciliation,
    list_sumup_payouts, post_to_klara,
)

logger = logging.getLogger(__name__)


LIST_SINCE = date(2026, 1, 1)


@staff_member_required
def reconciliation_list(request):
    sumup_rows, sumup_error = [], None
    try:
        sumup_rows = list_sumup_payouts(SumUpClient(), LIST_SINCE)
    except SumUpAPIError as e:
        sumup_error = str(e)
    linked = {r['payout'].pk for r in sumup_rows if r['payout']}
    context = admin.site.each_context(request)
    context.update({
        'title': 'SumUp Abgleich',
        'sumup_rows': sumup_rows,
        'sumup_error': sumup_error,
        'other_payouts': SumUpPayout.objects.exclude(pk__in=linked),
    })
    return render(request, 'reconciliation/payout_list.html', context)


@staff_member_required
def reconciliation_start(request):
    """Erstellt den Abgleich für eine SumUp-Auszahlung (Referenz aus der Liste)."""
    if request.method != 'POST':
        return redirect('reconciliation:list')
    reference = request.POST.get('reference', '').strip()
    try:
        payout_date = date.fromisoformat(request.POST.get('date', ''))
        payout = create_reconciliation(reference, payout_date, user=request.user)
    except (ValueError, ReconciliationError, SumUpAPIError) as e:
        messages.error(request, f"Abgleich konnte nicht erstellt werden: {e}")
        return redirect('reconciliation:list')
    summary = payout.booking_summary
    if summary['is_balanced']:
        messages.success(request, "Abgleich erstellt — alles abgeglichen, bereit zum Abschluss.")
    else:
        messages.warning(request, f"Abgleich erstellt — {summary['open_count']} offene Position(en).")
    return redirect('reconciliation:review', pk=payout.pk)


@staff_member_required
def reconciliation_review(request, pk):
    payout = get_object_or_404(SumUpPayout, pk=pk)
    items = list(payout.items.select_related('sale__created_by').all())
    _attach_link_candidates(items)

    summary = payout.booking_summary

    context = admin.site.each_context(request)
    context.update({
        'title': f'Abgleich: {payout}',
        'payout': payout,
        'items': items,
    })
    context.update(summary)
    return render(request, 'reconciliation/review.html', context)


def _local_date(ts):
    return timezone.localtime(ts, LOCAL_TZ).date() if ts else None


def _attach_link_candidates(items):
    """
    Vorschläge für ONLY_SUMUP-Zeilen: SK-Sales ohne SumUp-Code (ONLY_SK) mit
    gleichem Betrag am gleichen Tag. Verknüpft wird nur auf Klick.
    """
    Status = ReconciliationItem.MatchStatus
    orphans = [i for i in items if i.match_status == Status.ONLY_SK
               and i.resolution == ReconciliationItem.Resolution.PENDING
               and i.sale and not i.sale.transaction_id]
    for item in items:
        item.link_candidates = []
        if item.match_status != Status.ONLY_SUMUP or item.resolution != 'PENDING':
            continue
        item.link_candidates = [
            o for o in orphans
            if o.sk_amount == item.sumup_amount
            and _local_date(o.sk_timestamp) == _local_date(item.sumup_timestamp)
        ]


@staff_member_required
@transaction.atomic
def link_sale(request, pk, item_pk):
    """Verknüpft eine SumUp-Zahlung (ONLY_SUMUP) mit einem SK-Sale ohne SumUp-Code."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    payout = get_object_or_404(SumUpPayout, pk=pk)
    item = get_object_or_404(ReconciliationItem, pk=item_pk, payout=payout,
                             match_status=ReconciliationItem.MatchStatus.ONLY_SUMUP)
    if payout.status != SumUpPayout.Status.IN_REVIEW:
        return JsonResponse({'error': 'Abgleich ist nicht in Prüfung.'}, status=400)
    sale = get_object_or_404(Sale.objects.select_for_update(), pk=request.POST.get('sale_id'))
    if sale.transaction_id:
        return JsonResponse({'error': f'Sale #{sale.id} hat bereits einen SumUp-Code.'}, status=400)
    if sale.status != Sale.Status.COMPLETED or sale.payment_method != Sale.PaymentMethod.SUMUP:
        return JsonResponse({'error': f'Sale #{sale.id} ist kein aktiver SumUp-Verkauf.'}, status=400)
    if Sale.objects.filter(transaction_id=item.sumup_tx_code, status=Sale.Status.COMPLETED).exists():
        return JsonResponse({'error': 'Für diese Zahlung existiert bereits ein Sale.'}, status=400)

    sale.transaction_id = item.sumup_tx_code
    sale.save(update_fields=['transaction_id'])

    now = timezone.now()
    item.sale = sale
    item.sk_amount = sale.total_amount_gross
    item.sk_timestamp = sale.date
    item.channel = detect_channel(sale)
    item.gap_amount = abs((sale.total_amount_gross or 0) - (item.sumup_amount or 0))
    item.gap_pct = Decimal('0')
    item.match_status = (ReconciliationItem.MatchStatus.MATCHED if item.gap_amount <= Decimal('0.01')
                         else ReconciliationItem.MatchStatus.GAP)
    item.resolution = ReconciliationItem.Resolution.MANUAL
    item.resolution_note = f"Mit Sale #{sale.id} verknüpft (SumUp-Code nachgetragen)"
    item.resolved_at = now
    item.save()
    payout.items.filter(match_status=ReconciliationItem.MatchStatus.ONLY_SK, sale=sale).update(
        resolution=ReconciliationItem.Resolution.MANUAL,
        resolution_note=f"Mit SumUp-Zahlung {item.sumup_tx_code} verknüpft",
        resolved_at=now,
    )
    return JsonResponse({'status': 'ok', 'sale_id': sale.id})


@staff_member_required
@transaction.atomic
def resolve_item(request, pk, item_pk):
    """
    AJAX-Endpunkt: Diskrepanz auflösen.

    resolution:
      ACCEPTED              GAP: SumUp-Betrag gilt (wird so gebucht)
      PAYMENT_TYPE_CHANGED  ONLY_SK: Zahlungsart korrigieren (new_payment_method)
      SALE_DELETED          ONLY_SK: Sale stornieren / DUPLICATE: Duplikate stornieren
      MANUAL                Kanal festlegen ohne SK-Nachbuchung (channel=LADEN|CAFE)
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    payout = get_object_or_404(SumUpPayout, pk=pk)
    item = get_object_or_404(ReconciliationItem, pk=item_pk, payout=payout)
    if payout.status in (SumUpPayout.Status.COMPLETED, SumUpPayout.Status.SUPERSEDED):
        return JsonResponse({'error': 'Abgleich ist abgeschlossen.'}, status=400)

    resolution = request.POST.get('resolution', '')
    note = request.POST.get('resolution_note', '')
    channel = request.POST.get('channel', '')
    Status = ReconciliationItem.MatchStatus

    if resolution not in dict(ReconciliationItem.Resolution.choices):
        return JsonResponse({'error': 'Ungültige Resolution'}, status=400)
    if channel and channel not in (ReconciliationItem.Channel.LADEN, ReconciliationItem.Channel.CAFE):
        return JsonResponse({'error': 'Ungültiger Kanal'}, status=400)
    if resolution == 'MANUAL' and not channel:
        return JsonResponse({'error': 'Bitte Kanal (Laden/Café) wählen.'}, status=400)

    if channel:
        item.channel = channel
        note = note or f"Kanal manuell: {item.get_channel_display()}"

    if resolution == 'PAYMENT_TYPE_CHANGED' and item.sale:
        new_method = request.POST.get('new_payment_method', '')
        if new_method not in dict(Sale.PaymentMethod.choices):
            return JsonResponse({'error': 'Ungültige Zahlungsart'}, status=400)
        item.sale.payment_method = new_method
        item.sale.save()
        note = note or f"Zahlungsart → {item.sale.get_payment_method_display()}"

    if resolution == 'SALE_DELETED':
        if item.match_status == Status.DUPLICATE:
            dupes = Sale.objects.filter(
                transaction_id=item.sumup_tx_code, status=Sale.Status.COMPLETED,
            ).exclude(pk=item.sale_id)
            ids = []
            for dupe in dupes:
                dupe.refund(user=request.user)
                ids.append(f'#{dupe.id}')
            note = f"Duplikat(e) storniert: {', '.join(ids) or '–'} (behalten: #{item.sale_id})"
        elif item.sale:
            item.sale.refund(user=request.user)
            note = f"Storniert via Reconciliation (Sale #{item.sale.id})"

    item.resolution = resolution
    item.resolution_note = note
    item.resolved_at = timezone.now()
    item.save()
    return JsonResponse({
        'status': 'ok',
        'resolution': item.get_resolution_display(),
        'channel': item.get_channel_display(),
    })


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

    if payout.status in (SumUpPayout.Status.COMPLETED, SumUpPayout.Status.SUPERSEDED):
        return JsonResponse({'error': 'Abgleich ist abgeschlossen.'}, status=400)
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
    tx_code = item.sumup_tx_code or None
    idem_key = f'recon-item-{item.pk}'

    # Kasse hat inzwischen gebucht → verknüpfen statt Duplikat anlegen
    existing = (
        (tx_code and Sale.objects.filter(transaction_id=tx_code,
                                         status=Sale.Status.COMPLETED).first())
        or Sale.objects.filter(idempotency_key=idem_key).first()
    )
    linked = existing is not None
    if linked:
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
    item.sk_amount = sale.total_amount_gross
    item.sk_timestamp = sale.date
    item.match_status = ReconciliationItem.MatchStatus.MATCHED
    item.match_tier = ReconciliationItem.MatchTier.EXACT
    item.gap_amount = Decimal('0')
    item.gap_pct = Decimal('0')
    item.channel = detect_channel(sale)
    item.resolution = ReconciliationItem.Resolution.SALE_ADDED
    item.resolution_note = (f"Mit Kassen-Sale #{sale.id} verknüpft" if linked
                            else f"Sale #{sale.id} nacherfasst ({product.sku})")
    item.resolved_at = timezone.now()
    item.save()

    return JsonResponse({
        'status': 'ok',
        'sale_id': sale.id,
        'product_sku': product.sku,
        'product_name': str(product),
        'resolution': item.get_resolution_display(),
    })


@staff_member_required
def reconciliation_complete(request, pk):
    if request.method != 'POST':
        return redirect('reconciliation:review', pk=pk)

    payout = get_object_or_404(SumUpPayout, pk=pk)
    if payout.status != SumUpPayout.Status.IN_REVIEW:
        return JsonResponse({'error': 'Abgleich ist nicht in Prüfung.'}, status=400)
    if payout.engine_version >= 2:
        summary = payout.booking_summary
        if not summary['is_balanced']:
            return JsonResponse({
                'error': f"Abgleich geht nicht auf: {summary['open_count']} offene Position(en), "
                         f"Differenz CHF {summary['net_delta']}.",
            }, status=400)

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


@staff_member_required
def klara_approve(request, pk):
    """
    Freigabe-Seite aus der Monats-Mail: zeigt den Buchungsvorschlag und bucht
    nach Klick direkt in KLARA (Berechtigung reconciliation.post_to_klara).
    """
    payout = get_object_or_404(SumUpPayout, pk=pk)
    can_post = can_post_to_klara(request.user)
    if request.method == 'POST':
        if not can_post:
            messages.error(request, "Dir fehlt die Berechtigung «SumUp-Abgleich in KLARA buchen».")
        else:
            try:
                ids = post_to_klara(payout.pk, request.user)
                messages.success(
                    request,
                    f"In KLARA gebucht (Buchung {', '.join(map(str, ids))}). "
                    f"Jetzt noch im KLARA-Bankabgleich die Gutschrift vom "
                    f"{payout.bank_credit_date:%d.%m.%Y} auf Konto 1101 zuordnen und das PDF anhängen.")
            except KlaraPostingError as e:
                messages.error(request, f"Nicht gebucht: {e}")
        return redirect('reconciliation:klara_approve', pk=payout.pk)

    payout.refresh_from_db()
    context = admin.site.each_context(request)
    context.update({
        'title': f'KLARA-Freigabe: {payout}',
        'payout': payout,
        'can_post': can_post,
    })
    if payout.engine_version >= 2:
        context.update(payout.booking_summary)
    return render(request, 'reconciliation/klara_approve.html', context)
