import io
import os
from django.http import HttpResponse
from django.template.loader import get_template
from xhtml2pdf import pisa
from django.conf import settings
from datetime import date 
from decimal import Decimal 


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