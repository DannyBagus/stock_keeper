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
from decimal import Decimal


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


def generate_voucher_pdf(payout):
    """Generiert den Buchungsbeleg als PDF-Response."""
    items = payout.items.all().order_by('sumup_timestamp', 'sk_timestamp')

    laden_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in items
        if i.channel == 'LADEN' and i.match_status == 'MATCHED'
    )
    cafe_total = sum(
        i.sk_amount or i.sumup_amount or Decimal(0)
        for i in items
        if i.channel == 'CAFE' and i.match_status == 'MATCHED'
    )
    total_fees = sum(i.sumup_fee or Decimal(0) for i in items if i.sumup_fee)
    net_amount = laden_total + cafe_total - total_fees

    context = {
        'payout': payout,
        'items': items,
        'laden_total': laden_total,
        'cafe_total': cafe_total,
        'total_fees': total_fees,
        'net_amount': net_amount,
        'matched_count': sum(1 for i in items if i.match_status == 'MATCHED'),
        'discrepancy_count': sum(1 for i in items if i.match_status != 'MATCHED'),
    }

    return render_reconciliation_pdf('reconciliation/voucher_pdf.html', context)
