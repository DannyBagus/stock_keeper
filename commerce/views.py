import hmac
import hashlib
import base64
import json
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
from decimal import Decimal
import json
from .models import Product, Sale, SaleItem, PurchaseOrder, PurchaseOrderItem
from core.models import Supplier
from .utils import render_to_pdf

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
        results.append({
            'id': p.id,
            'name': p.name,
            'ean': p.ean,
            'price': float(p.sales_price), 
            'cost': float(p.cost_price),   
            'stock': p.stock_quantity,
            'vat_rate': float(p.vat.rate) if p.vat else 0.0,
            'supplier_id': p.supplier.id if p.supplier else None,
            'supplier_name': p.supplier.name if p.supplier else "Unbekannt"
        })
    return JsonResponse({'results': results})

@staff_member_required
@require_POST
@transaction.atomic
def api_checkout(request):
    try:
        data = json.loads(request.body)
        cart_items = data.get('items', [])
        payment_method = data.get('payment_method', 'CASH')
        
        if not cart_items:
            return JsonResponse({'error': 'Warenkorb ist leer'}, status=400)

        sale = Sale.objects.create(
            payment_method=payment_method,
            created_by=request.user,
            # WICHTIG: Explizit POS setzen (auch wenn es Default ist)
            channel=Sale.SalesChannel.POS 
        )

        for item in cart_items:
            product = Product.objects.get(pk=item['id'])
            qty = int(item['qty'])
            
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=qty,
                unit_price_gross=product.sales_price,
                vat_rate=product.vat.rate if product.vat else Decimal('0.00')
            )

        sale.calculate_totals()
        
        return JsonResponse({
            'success': True, 
            'sale_id': sale.id,
            'pdf_url': f'/commerce/sale/{sale.id}/pdf/' 
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@staff_member_required
def sale_receipt_pdf_view(request, sale_id):
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
                    
        # 1. Versuchen, den System-User zu laden
        User = get_user_model()
        system_user = None
        try:
            system_user = User.objects.get(username='shopify_bot')
        except User.DoesNotExist:
            # Fallback: Wenn User noch nicht existiert, nehmen wir None 
            # oder erstellen ihn on-the-fly (weniger empfohlen wegen Seiteneffekten)
            pass

        # FIX: Channel auf WEB setzen!
        sale = Sale.objects.create(
            payment_method=Sale.PaymentMethod.SHOPIFY_PAYMENTS, # Nutzt den Wert 'SHOPIFY'
            transaction_id=shopify_order_id,
            channel=Sale.SalesChannel.WEB, # <--- WICHTIG: Online Kanal setzen
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

            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=quantity,
                unit_price_gross=price,
                vat_rate=product.vat.rate if product.vat else Decimal('0.00')
            )

        sale.calculate_totals()
        
        return HttpResponse("Webhook received and processed", status=200)

    except Exception as e:
        print(f"Error processing webhook: {e}")
        return HttpResponse("Internal Server Error", status=500)