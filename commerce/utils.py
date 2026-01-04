import io
import os
import re
from django.http import HttpResponse
from django.template.loader import get_template, render_to_string
from xhtml2pdf import pisa
from django.conf import settings
from datetime import date 
from decimal import Decimal 

import segno
from django.core.mail import EmailMessage
from django.utils import timezone
from weasyprint import HTML, CSS


# Konstante für den MwSt-Satz (nur für die Anzeige als Fallback)
DEFAULT_MWST_SATZ = Decimal('0.081') 

def render_to_pdf(template_src, context_dict={}):
    """
    Konvertiert ein Django-Template in ein PDF-Dokument und führt die notwendige Brutto/Netto-Berechnung durch.
    Kann PurchaseOrder (PO) oder Sale-Objekte verarbeiten.
    """
    obj = context_dict.get('order') or context_dict.get('sale') # Objekt kann PO oder Sale sein
    
    if obj:
        # --- Spezifische Logik für PURCHASE ORDER ---
        if obj.__class__.__name__ == 'PurchaseOrder':
            order = obj
            
            total_cost_net = Decimal('0.00')
            total_mwst = Decimal('0.00')
            
            for item in order.items.all():
                total_cost_net += item.total_price
                item_vat_rate_percent = item.vat_rate or Decimal('0.00')
                item_vat_rate_decimal = item_vat_rate_percent / Decimal('100.00')
                total_mwst += item.total_price * item_vat_rate_decimal
            
            total_cost_gross = total_cost_net + total_mwst
            
            context_dict['total_cost_net'] = total_cost_net
            context_dict['total_mwst'] = total_mwst
            context_dict['total_cost_gross'] = total_cost_gross
            context_dict['mwst_rate_percent'] = f"{(DEFAULT_MWST_SATZ * 100):.1f}"

        # --- Spezifische Logik für SALE (Quittung) ---
        elif obj.__class__.__name__ == 'Sale':
            sale = obj
            
            # Die Summen werden vom Sale-Objekt übernommen, da sie dort bereits gecached sind
            context_dict['total_cost_gross'] = sale.total_amount_gross
            
            # Da die MwSt in den SaleItems gespeichert ist, rechnen wir die Netto- und MwSt-Summen neu,
            # um die detaillierte Aufschlüsselung zu zeigen.
            total_mwst = Decimal('0.00')
            total_cost_net = Decimal('0.00')
            
            for item in sale.items.all():
                item_vat_rate_percent = item.vat_rate or Decimal('0.00')
                item_vat_rate_decimal = item_vat_rate_percent / Decimal('100.00')
                
                # Brutto-Preis des Items
                item_total_gross = item.total_price_gross
                
                # Brutto zu Netto Berechnung
                divisor = Decimal('1.00') + item_vat_rate_decimal
                item_net_price = item_total_gross / divisor
                
                total_cost_net += item_net_price
                total_mwst += item_total_gross - item_net_price
            
            context_dict['total_cost_net'] = total_cost_net
            context_dict['total_mwst'] = total_mwst
            # MwSt-Satz für die Anzeige
            context_dict['mwst_rate_percent'] = f"{(DEFAULT_MWST_SATZ * 100):.1f}"


        # --- Gemeinsame Logik ---
        # Datums- und Benutzer-Filter im Code auflösen
        obj_date = obj.date if isinstance(obj.date, date) else obj.date.date()
        context_dict['formatted_date'] = obj_date.strftime("%d.%m.%Y")
        
        # 'created_by' existiert auf Sale nicht, nur auf PurchaseOrder
        if hasattr(obj, 'created_by') and obj.created_by:
             context_dict['formatted_creator'] = obj.created_by.get_full_name() or obj.created_by.username
        else:
             context_dict['formatted_creator'] = "System"
    
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
        html,                   # HTML-Quelle
        dest=result,            # Ziel-BytesIO-Objekt
        link_callback=link_callback
    )

    if pisa_status.err:
        return HttpResponse('Wir hatten einige Fehler <pre>%s</pre>' % html, status=500)
    
    return HttpResponse(result.getvalue(), content_type='application/pdf')


def clean_qr_text(text, max_length=70):
    """
    Bereinigt Text für Swiss QR Bill:
    - Ersetzt Zeilenumbrüche durch Leerzeichen
    - Entfernt nicht erlaubte Zeichen (vereinfacht)
    - Kürzt auf max_length
    """
    if not text:
        return ""
    # Zeilenumbrüche entfernen
    text = str(text).replace('\n', ' ').replace('\r', '')
    # Mehrfache Leerzeichen reduzieren
    text = re.sub(' +', ' ', text)
    return text[:max_length].strip()

def format_swiss_qr_content(sale, customer_data):
    """
    Erstellt den exakten String (Payload) für den Swiss QR Code
    gemäss SIX Implementation Guidelines (SPC 0200).
    """
    
    # 1. Header
    data = [
        "SPC",      # QRType
        "0200",     # Version
        "1"         # Coding (1 = UTF-8)
    ]

    # 2. Creditor (Wir)
    iban = "CH4109000000152652867" # Deine IBAN ohne Leerzeichen
    
    data.append(iban) 
    
    # Creditor Address (Combined Address 'K' oder Structured 'S')
    # Wir nutzen 'K' (Combined) da einfacher, wenn Strasse/Nr getrennt komplex ist,
    # aber SIX empfiehlt 'S'. Hier 'K' für Robustheit mit deinen Daten:
    data.extend([
        "K",                    # Address Type
        "Mileja GmbH",          # Name
        "Rittergasse 20",       # Strasse + Nr (Zeile 1)
        "4051 Basel",           # PLZ + Ort (Zeile 2)
        "",                     # Empty (Country, optional, bei K meist weggelassen oder inferred)
        "",                     # Empty
        "CH"                    # Country
    ])

    # 3. Ultimate Creditor (leer, da wir es selbst sind)
    data.extend(["", "", "", "", "", "", ""])

    # 4. Payment Amount
    # Format: 0.00 (Punkt als Dezimaltrenner)
    amount = "{:.2f}".format(sale.total_amount_gross)
    data.extend([
        amount,     # Amount
        "CHF"       # Currency
    ])

    # 5. Ultimate Debtor (Kunde)
    # Adresse des Kunden zusammenbauen
    c_name = clean_qr_text(f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}")
    c_address = clean_qr_text(customer_data.get('address', ''))
    c_zip_city = clean_qr_text(f"{customer_data.get('zip_code', '')} {customer_data.get('city', '')}")
    
    # Falls Pflichtfelder fehlen, füllen wir mit Platzhaltern, damit QR valide bleibt
    if not c_name: c_name = "Kunde"
    
    data.extend([
        "K",            # Address Type
        c_name,         # Name
        c_address,      # Strasse Zeile
        c_zip_city,     # PLZ Ort Zeile
        "", "",         # Empty
        "CH"            # Country (Annahme: Schweiz)
    ])

    # 6. Reference
    # Wir nutzen "NON" (Non-structured), da wir keine QRR-Referenz (mit spezieller ID) haben.
    # Bei 'NON' bleibt die Referenzzeile leer, die Info kommt in "Unstructured Message".
    data.extend([
        "NON", # Ref Type
        ""     # Reference
    ])

    # 7. Unstructured Message
    msg = clean_qr_text(f"Rechnung {sale.id}")
    data.append(msg)

    # 8. Trailer
    data.append("EPD") # End of Payment Data
    
    # 9. Additional Info (leer)
    data.extend(["", ""])

    # Zusammenfügen mit Newlines
    return "\n".join(data)

def generate_qr_code_svg(qr_data):
    """
    Erstellt den QR-Code als SVG-String mit segno.
    """
    qr = segno.make(qr_data, error='M', micro=False)
    
    buff = io.BytesIO()
    # scale=4 sorgt für gute Auflösung.
    # border=0 wichtig, da wir Rand CSS-seitig steuern
    qr.save(buff, kind='svg', scale=4, border=0)
    buff.seek(0)
    return buff.read().decode('utf-8')

def generate_invoice_pdf(sale, customer_data, qr_svg=None):
    """
    Generiert das PDF basierend auf dem HTML-Template.
    """
    # Wenn kein SVG übergeben wurde, generieren wir eines
    if not qr_svg:
        payload = format_swiss_qr_content(sale, customer_data)
        qr_svg = generate_qr_code_svg(payload)

    context = {
        'sale': sale,
        'customer': customer_data,
        'qr_code_svg': qr_svg,
        'STATIC_ROOT': settings.STATIC_ROOT,
        'items': sale.items.all(), # Items explizit übergeben für Template
        'total_cost_net': sale.total_amount_net,
        'total_cost_gross': sale.total_amount_gross,
        # MwSt Differenz berechnen für Anzeige
        'total_mwst': sale.total_amount_gross - sale.total_amount_net,
        # Annahme: Mischsteuersatz oder fix. Für Anzeige nehmen wir den häufigsten oder 8.1
        'mwst_rate_percent': '8.1', 
        'formatted_date': sale.date.strftime("%d.%m.%Y"),
        'formatted_creator': sale.created_by.username if sale.created_by else "System"
    }

    html_string = render_to_string('commerce/invoice_pdf.html', context)
    
    # base_url ist wichtig für statische Dateien
    html = HTML(string=html_string, base_url=settings.BASE_DIR)
    pdf_file = html.write_pdf()
    
    return pdf_file

def send_invoice_email(sale, customer_data):
    """
    Hauptfunktion: Generiert QR & PDF und sendet die E-Mail.
    """
    try:
        # 1. QR Payload generieren
        qr_payload = format_swiss_qr_content(sale, customer_data)
        qr_svg = generate_qr_code_svg(qr_payload)

        # 2. PDF generieren
        pdf_content = generate_invoice_pdf(sale, customer_data, qr_svg)

        # 3. E-Mail Body rendern
        email_body = render_to_string('commerce/invoice_mail.html', {
            'sale': sale,
            'customer': customer_data
        })

        # 4. E-Mail konfigurieren
        subject = f'Rechnung #{sale.id} - Mileja GmbH'
        to_email = [customer_data['email']]
        cc_email = ['info@mileja.ch']

        email = EmailMessage(
            subject,
            email_body,
            settings.DEFAULT_FROM_EMAIL,
            to_email,
            cc=cc_email,
        )
        email.content_subtype = "html"

        # 5. PDF anhängen
        filename = f"Rechnung_{sale.id}.pdf"
        email.attach(filename, pdf_content, 'application/pdf')

        # 6. Senden
        email.send()
        return True, "Gesendet"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)