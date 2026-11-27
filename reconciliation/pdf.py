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

import io
import os
from django.http import HttpResponse
from django.template.loader import get_template
from django.conf import settings
from xhtml2pdf import pisa


def render_reconciliation_pdf(template_src, context_dict):
    """Konvertiert ein Django-Template in ein PDF-Dokument."""
    template = get_template(template_src)
    html = template.render(context_dict)

    result = io.BytesIO()

    def link_callback(uri, rel):
        if uri.startswith(settings.STATIC_URL):
            path = os.path.join(settings.STATIC_ROOT, uri.replace(settings.STATIC_URL, ""))
            return path
        if uri.startswith(settings.MEDIA_URL):
            path = os.path.join(settings.MEDIA_ROOT, uri.replace(settings.MEDIA_URL, ""))
            return path
        return uri

    pisa_status = pisa.CreatePDF(
        html,
        dest=result,
        link_callback=link_callback
    )

    if pisa_status.err:
        return HttpResponse('PDF-Generierung fehlgeschlagen', status=500)

    return HttpResponse(result.getvalue(), content_type='application/pdf')


def voucher_pdf_bytes(payout) -> bytes | None:
    """Beleg v2 als PDF-Bytes (für Ablage/KLARA-Upload)."""
    response = generate_voucher_pdf(payout)
    return response.content if response.status_code == 200 else None


def generate_voucher_pdf(payout):
    """Generiert den Buchungsbeleg als PDF-Response."""
    if payout.engine_version >= 2:
        from .booking import LINE_STATUSES
        from .models import ReconciliationItem
        items = list(payout.items.select_related('sale').order_by('sumup_timestamp', 'pk'))
        context = {
            'payout': payout,
            'line_items': [i for i in items if i.match_status in LINE_STATUSES],
            'refund_items': [i for i in items
                             if i.match_status == ReconciliationItem.MatchStatus.REFUND],
        }
        context.update(payout.booking_summary)
        return render_reconciliation_pdf('reconciliation/voucher_pdf_v2.html', context)

    items = payout.items.all().order_by('sumup_timestamp', 'sk_timestamp')

    summary = payout.booking_summary
    discrepancy_count = (
        summary['gap_count'] + summary['only_sumup_count'] + summary['only_sk_count']
    )

    context = {
        'payout': payout,
        'items': items,
        'net_amount': summary['computed_net'],
        'discrepancy_count': discrepancy_count,
    }
    context.update(summary)

    return render_reconciliation_pdf('reconciliation/voucher_pdf.html', context)
