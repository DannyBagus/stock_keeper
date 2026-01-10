import hmac
import hashlib
import base64
import json
from io import BytesIO
from decimal import Decimal
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
import barcode 
from barcode.writer import ImageWriter

from .models import Product, Sale, SaleItem, PurchaseOrder, PurchaseOrderItem
from core.models import Supplier
from .utils import render_to_pdf, send_invoice_email, generate_invoice_pdf, generate_qr_code_svg
from .forms import AccountingReportForm, EanLabelForm, MwstReportForm

# --- POS VIEWS ---

@staff_member_required
def pos_view(request):
    context = admin.site.each_context(request)
    context.update({'title': 'Kasse (POS)'})
    return render(request, 'commerce/pos.html', context)

@staff_member_required
def purchase_pos_view(request):
    suppliers = Supplier.objects.all().order_by('name')
    context = admin.site.each_context(request)
    context.update({
        'suppliers': suppliers,
        'title': 'Bestellung erfassen'
    })
    return render(request, 'commerce/purchase_pos.html', context)

@staff_member_required
@require_GET
def api_search_product(request):
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'results': []})
    products = Product.objects.filter(
        Q(ean=query) | Q(sku__iexact=query) | Q(name__icontains=query)
    ).select_related('vat', 'supplier')[:10]
    results = []
    for p in products:
        display_name = str(p)
        results.append({
            'id': p.id,
            'name': display_name,
            'ean': p.ean,
            'sku': p.sku, # SKU hinzufügen für Frontend-Logik (Diverses)
            'price': float(p.sales_price),
            'cost': float(p.cost_price),
            'stock': p.stock_quantity,
            'track_stock': p.track_stock,
            'vat_rate': float(p.vat.rate) if p.vat else 0.0,
            'supplier_id': p.supplier.id if p.supplier else None,
            'supplier_name': p.supplier.name if p.supplier else "Unbekannt"
        })
    return JsonResponse({'results': results})

# --- CHECKOUT LOGIK ---

@staff_member_required
@require_POST
@transaction.atomic
def api_checkout(request):
    try:
        data = json.loads(request.body)
        items = data.get('items', [])
        payment_method = data.get('payment_method', 'CASH')
        customer_data = data.get('customer', None)

        if not items:
            return JsonResponse({'success': False, 'error': 'Warenkorb leer'})

        # 1. Sale erstellen
        sale = Sale.objects.create(
            date=timezone.now(),
            payment_method=payment_method,
            status=Sale.Status.COMPLETED,
            created_by=request.user,
            channel=Sale.SalesChannel.POS
        )

        total_gross = Decimal('0.00')

        # 2. Items hinzufügen
        for item in items:
            product = Product.objects.get(id=item['id'])
            qty = int(item['qty'])
            
            # Preis ermitteln:
            # Standard: Preis aus DB
            # Ausnahme: 'custom_price' im Request UND Produkt SKU ist 'DIVERSES'
            custom_price_raw = item.get('custom_price')
            
            # Wir holen den Preis standardmässig vom Produkt
            unit_price = product.sales_price
            
            # Wenn es DIVERSES ist und ein Custom Price mitkommt:
            if product.sku == 'DIVERSES' and custom_price_raw is not None:
                try:
                    unit_price = Decimal(str(custom_price_raw))
                except:
                    pass # Fallback auf Produktpreis (0.00) bei Fehler

            # WICHTIG: Die VAT Rate muss explizit ermittelt und übergeben werden.
            # Wenn unit_price gesetzt ist, greift die automatische Ermittlung im Model.save() oft nicht.
            vat_rate = product.vat.rate if product.vat else Decimal('0.00')

            # SaleItem erstellen (Audit Log passiert automatisch im SaleItem.save())
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=qty,
                unit_price_gross=unit_price, # Hier nutzen wir den (evtl. manuellen) Preis
                vat_rate=vat_rate # <--- BUGFIX: Explizit übergeben
            )

            total_gross += (unit_price * qty)

        # 3. Totals berechnen
        sale.total_amount_gross = total_gross
        
        # Exakte Netto-Berechnung
        total_net = Decimal('0.00')
        # Items neu laden um sicherzugehen, dass wir die gespeicherten Daten haben
        for s_item in sale.items.all():
            vr = s_item.vat_rate or Decimal('0.00')
            # Netto = Brutto / (1 + Steuersatz)
            net_price = s_item.total_price_gross / (Decimal('1.00') + (vr / Decimal('100.00')))
            total_net += net_price

        sale.total_amount_net = round(total_net, 2)
        sale.save()

        # PDF URL Logic (Standard: Thermo-Bon)
        pdf_url = f"/commerce/sale/{sale.id}/pdf/" 

        # 4. Spezifische Logik für RECHNUNG
        if payment_method == 'INVOICE' and customer_data:
            success, info = send_invoice_email(sale, customer_data)
            if not success:
                print(f"Mail Error: {info}")
            
            # Optional: Falls man direkt das A4 PDF zurückgeben will
            # pdf_url = f"/commerce/sale/{sale.id}/invoice-pdf/"

        return JsonResponse({
            'success': True, 
            'sale_id': sale.id,
            'pdf_url': pdf_url
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)})

# --- PDF VIEWS ---

@staff_member_required
def sale_receipt_pdf_view(request, sale_id):
    """ Thermodrucker Quittung """
    sale = get_object_or_404(Sale, id=sale_id)
    response = render_to_pdf(
        'commerce/sale_receipt_pdf.html',
        {
            'sale': sale,
            'items': sale.items.all(),
        }
    )
    if isinstance(response, HttpResponse) and response.status_code == 200:
        filename = f"Quittung_{sale.id}_{sale.date.strftime('%Y%m%d')}.pdf"
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    else:
        return HttpResponse("Fehler beim Generieren des PDFs", status=500)

@staff_member_required
def sale_invoice_pdf_view(request, sale_id):
    """ A4 Rechnung manuell herunterladen """
    sale = get_object_or_404(Sale, id=sale_id)
    
    # Dummy Kunde für manuellen Nachdruck (da wir keine Kundendaten im Sale speichern)
    customer_dummy = {
        'first_name': 'Kunde',
        'last_name': '(Nachdruck)',
        'address': '---',
        'zip_code': '----',
        'city': '---',
        'email': ''
    }
    
    try:
        from .utils import format_swiss_qr_content
        qr_payload = format_swiss_qr_content(sale, customer_dummy)
        qr_svg = generate_qr_code_svg(qr_payload)
    except Exception as e:
        qr_svg = None
        print(f"QR Error: {e}")
    
    pdf_content = generate_invoice_pdf(sale, customer_dummy, qr_svg)
    response = HttpResponse(pdf_content, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="Rechnung_{sale.id}.pdf"'
    return response

# --- PURCHASE CHECKOUT ---

@staff_member_required
@require_POST
@transaction.atomic
def api_purchase_checkout(request):
    try:
        data = json.loads(request.body)
        supplier_id = data.get('supplier_id')
        cart_items = data.get('items', [])
        if not supplier_id:
            return JsonResponse({'error': 'Lieferant fehlt'}, status=400)
        if not cart_items:
            return JsonResponse({'error': 'Bestellliste ist leer'}, status=400)
        supplier = Supplier.objects.get(pk=supplier_id)
        po = PurchaseOrder.objects.create(
            supplier=supplier,
            created_by=request.user,
            status=PurchaseOrder.Status.DRAFT,
            is_booked=False
        )
        for item in cart_items:
            product = Product.objects.get(pk=item['id'])
            qty = int(item['qty'])
            cost_price = product.cost_price
            PurchaseOrderItem.objects.create(
                order=po,
                product=product,
                quantity=qty,
                unit_price=cost_price,
            )
        return JsonResponse({
            'success': True,
            'order_id': po.id,
            'redirect_url': f'/admin/commerce/purchaseorder/{po.id}/change/'
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

# --- WEBHOOKS ---

def verify_shopify_webhook(request):
    secret = settings.SHOPIFY_WEBHOOK_SECRET.encode('utf-8')
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256')
    if not hmac_header:
        return False
    digest = hmac.new(secret, request.body, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed_hmac, hmac_header)

@csrf_exempt
@require_POST
def shopify_webhook(request):
    """
    Empfängt 'orders/paid' Events von Shopify.
    """
    if hasattr(settings, 'SHOPIFY_WEBHOOK_SECRET') and settings.SHOPIFY_WEBHOOK_SECRET and not verify_shopify_webhook(request):
        return HttpResponseForbidden("Invalid Signature")
    try:
        data = json.loads(request.body)
        shopify_order_id = str(data.get('id'))
        if Sale.objects.filter(transaction_id=shopify_order_id).exists():
            return HttpResponse("Order already processed", status=200)
        
        User = get_user_model()
        system_user = None
        try:
            system_user = User.objects.get(username='shopify_bot')
        except User.DoesNotExist:
            pass

        sale = Sale.objects.create(
            payment_method=Sale.PaymentMethod.SHOPIFY_PAYMENTS,
            transaction_id=shopify_order_id,
            channel=Sale.SalesChannel.WEB,
            created_by=system_user
        )
        for line_item in data.get('line_items', []):
            sku = line_item.get('sku')
            quantity = line_item.get('quantity')
            price = Decimal(line_item.get('price'))
            product = None
            if sku:
                product = Product.objects.filter(sku=sku).first()
            if not product:
                print(f"WARNUNG: Shopify Produkt mit SKU {sku} nicht in DB gefunden.")
                continue
            
            # Auch hier explizit VAT setzen!
            vat_rate = product.vat.rate if product.vat else Decimal('0.00')
            
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=quantity,
                unit_price_gross=price,
                vat_rate=vat_rate
            )
        sale.calculate_totals()
        return HttpResponse("Webhook received and processed", status=200)
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return HttpResponse("Internal Server Error", status=500)

# --- REPORTS ---

@staff_member_required
def accounting_report_view(request):
    """
    Umsatz-Report: Filterbar nach Datum, Kategorien UND Zahlungsmethoden.
    """
    if request.method == 'POST':
        form = AccountingReportForm(request.POST)
        if form.is_valid():
            start_date = form.cleaned_data['start_date']
            end_date = form.cleaned_data['end_date']
            categories = form.cleaned_data['categories']
            payment_methods = form.cleaned_data['payment_methods'] # NEU: Liste der gewählten Codes
            
            # 1. Sale Items holen (für Kategorie-Stats)
            items_qs = SaleItem.objects.filter(
                sale__date__date__gte=start_date,
                sale__date__date__lte=end_date
            ).select_related('product', 'product__category', 'sale')
            
            if categories:
                items_qs = items_qs.filter(product__category__in=categories)
            
            # NEU: Filter nach Zahlungsmethode (auf Sale-Ebene via Relation)
            if payment_methods:
                items_qs = items_qs.filter(sale__payment_method__in=payment_methods)
            
            # Aggregation
            category_stats = {}
            total_period_gross = Decimal('0.00')
            for item in items_qs:
                cat_name = item.product.category.name if item.product.category else "Ohne Kategorie"
                if cat_name not in category_stats:
                    category_stats[cat_name] = {'gross': Decimal('0.00'), 'net': Decimal('0.00'), 'vat': Decimal('0.00')}
                
                qty = item.quantity
                gross = item.unit_price_gross * qty if item.unit_price_gross else Decimal('0.00')
                vat_rate = item.vat_rate or Decimal('0.00')
                divisor = Decimal('1.00') + (vat_rate / Decimal('100.00'))
                net = gross / divisor
                vat_amt = gross - net
                
                category_stats[cat_name]['gross'] += gross
                category_stats[cat_name]['net'] += net
                category_stats[cat_name]['vat'] += vat_amt
                total_period_gross += gross
            
            # 2. Sales Liste holen
            sales_qs = Sale.objects.filter(date__date__gte=start_date, date__date__lte=end_date).order_by('date')
            
            if categories:
                sales_qs = sales_qs.filter(items__product__category__in=categories).distinct()
            
            # NEU: Filter nach Zahlungsmethode für die Sales-Liste
            if payment_methods:
                sales_qs = sales_qs.filter(payment_method__in=payment_methods)
            
            # "Schöne" Namen für die gewählten Methoden für das PDF aufbereiten
            payment_methods_display = []
            if payment_methods:
                # Hole alle Choices aus dem Model als Dict
                choices_dict = dict(Sale.PaymentMethod.choices)
                for pm_code in payment_methods:
                    payment_methods_display.append(choices_dict.get(pm_code, pm_code))

            context = {
                'start_date': start_date, 
                'end_date': end_date, 
                'category_stats': category_stats, 
                'sales_list': sales_qs, 
                'total_period_gross': total_period_gross, 
                'generation_date': timezone.now(),
                # Für Anzeige im PDF
                'selected_payment_methods': payment_methods_display 
            }
            
            response = render_to_pdf('commerce/accounting_report_pdf.html', context)
            if isinstance(response, HttpResponse) and response.status_code == 200:
                filename = f"Umsatzliste_{start_date}_{end_date}.pdf"
                response['Content-Disposition'] = f'inline; filename="{filename}"'
                return response
            else:
                return HttpResponse("Fehler beim Generieren des PDFs", status=500)
    else:
        form = AccountingReportForm()
    
    context = admin.site.each_context(request)
    context.update({'form': form, 'title': 'Umsatzliste & Buchhaltungs-Report'})
    return render(request, 'commerce/accounting_report_form.html', context)

@staff_member_required
def ean_label_view(request):
    if request.method == 'POST':
        form = EanLabelForm(request.POST)
        if form.is_valid():
            categories = form.cleaned_data['categories']
            products = Product.objects.filter(is_active=True).exclude(ean='')
            if categories:
                products = products.filter(category__in=categories)
            products = products.order_by('category__name', 'name')
            product_list = []
            for p in products:
                if not p.ean or not p.ean.isdigit(): continue
                try:
                    ean_class = barcode.get_barcode_class('ean13')
                    buffer = BytesIO()
                    my_ean = ean_class(p.ean, writer=ImageWriter())
                    my_ean.write(buffer, options={"write_text": False, "module_height": 8.0, "quiet_zone": 1.0})
                    image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    product_list.append({'name': p.name, 'price': p.sales_price, 'ean_text': p.ean, 'category': p.category.name if p.category else "-", 'barcode_image': f"data:image/png;base64,{image_base64}"})
                except Exception:
                    product_list.append({'name': p.name, 'price': p.sales_price, 'ean_text': p.ean, 'category': p.category.name if p.category else "-", 'barcode_image': None})
            context = {'product_list': product_list, 'generation_date': timezone.now()}
            response = render_to_pdf('commerce/ean_label_pdf.html', context)
            if isinstance(response, HttpResponse) and response.status_code == 200:
                filename = f"Scanliste_{timezone.now().strftime('%Y-%m-%d')}.pdf"
                response['Content-Disposition'] = f'inline; filename="{filename}"'
                return response
            else:
                return HttpResponse("Fehler beim Generieren des PDFs", status=500)
    else:
        form = EanLabelForm()
    context = admin.site.each_context(request)
    context.update({'form': form, 'title': 'Scan-Liste / Etiketten drucken'})
    return render(request, 'commerce/ean_label_form.html', context)

@staff_member_required
def mwst_report_view(request):
    if request.method == 'POST':
        form = MwstReportForm(request.POST)
        if form.is_valid():
            start_date = form.cleaned_data['start_date']
            end_date = form.cleaned_data['end_date']
            sale_items = SaleItem.objects.filter(sale__date__date__gte=start_date, sale__date__date__lte=end_date)
            
            ziffer_200_total = Decimal('0.00')
            norm_base = norm_tax = red_base = red_tax = spec_base = spec_tax = Decimal('0.00')
            
            for item in sale_items:
                gross = item.total_price_gross
                ziffer_200_total += gross
                rate = item.vat_rate or Decimal('0.00')
                divisor = Decimal('1.00') + (rate / Decimal('100.00'))
                net = gross / divisor
                tax = gross - net
                if rate >= Decimal('7.0'): norm_base += net; norm_tax += tax
                elif rate >= Decimal('3.0'): spec_base += net; spec_tax += tax
                elif rate > Decimal('0.0'): red_base += net; red_tax += tax
            
            purchase_items = PurchaseOrderItem.objects.filter(order__date__gte=start_date, order__date__lte=end_date, order__status=PurchaseOrder.Status.RECEIVED)
            ziffer_400_vorsteuer = Decimal('0.00')
            for p_item in purchase_items:
                net = p_item.total_price
                rate = p_item.vat_rate or Decimal('0.00')
                ziffer_400_vorsteuer += net * (rate / Decimal('100.00'))
            
            total_geschuldete_steuer = norm_tax + red_tax + spec_tax
            ziffer_500_zahllast = total_geschuldete_steuer - ziffer_400_vorsteuer
            
            context = {'start_date': start_date, 'end_date': end_date, 'generation_date': timezone.now(), 'ziffer_200': ziffer_200_total, 'ziffer_289': Decimal('0.00'), 'ziffer_299': ziffer_200_total, 'norm_base': norm_base, 'norm_tax': norm_tax, 'red_base': red_base, 'red_tax': red_tax, 'spec_base': spec_base, 'spec_tax': spec_tax, 'total_output_tax': total_geschuldete_steuer, 'ziffer_400': ziffer_400_vorsteuer, 'ziffer_500': ziffer_500_zahllast}
            response = render_to_pdf('commerce/mwst_report_pdf.html', context)
            if isinstance(response, HttpResponse) and response.status_code == 200:
                filename = f"MWST_Abrechnung_{start_date}_{end_date}.pdf"
                response['Content-Disposition'] = f'inline; filename="{filename}"'
                return response
            else:
                return HttpResponse("Fehler beim Generieren des PDFs", status=500)
    else:
        form = MwstReportForm()
    context = admin.site.each_context(request)
    context.update({'form': form, 'title': 'MWST Abrechnungshilfe (ESTV)'})
    return render(request, 'commerce/mwst_report_form.html', context)