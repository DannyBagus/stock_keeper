from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib import admin
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
    """Rendert die Haupt-Kassen-Oberfläche"""
    # FIX: Admin-Kontext laden
    context = admin.site.each_context(request)
    context.update({'title': 'Kasse (POS)'})
    return render(request, 'commerce/pos.html', context)

@staff_member_required
def purchase_pos_view(request):
    """Rendert die Einkaufs-Oberfläche"""
    suppliers = Supplier.objects.all().order_by('name')
    
    # FIX: Admin-Kontext laden und Daten hinzufügen
    context = admin.site.each_context(request)
    context.update({
        'suppliers': suppliers,
        'title': 'Bestellung erfassen'
    })
    
    return render(request, 'commerce/purchase_pos.html', context)

@staff_member_required
@require_GET
def api_search_product(request):
    """
    Sucht Produkte. Gibt jetzt auch cost_price und SUPPLIER zurück.
    """
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'results': []})

    # Wichtig: select_related('supplier') für Performance
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
            # NEU: Supplier Info für die Auto-Selektion
            'supplier_id': p.supplier.id if p.supplier else None,
            'supplier_name': p.supplier.name if p.supplier else "Unbekannt"
        })
    
    return JsonResponse({'results': results})

@staff_member_required
@require_POST
@transaction.atomic
def api_checkout(request):
    """
    Nimmt den JSON-Warenkorb entgegen und erstellt den Sale.
    """
    try:
        data = json.loads(request.body)
        cart_items = data.get('items', [])
        
        if not cart_items:
            return JsonResponse({'error': 'Warenkorb ist leer'}, status=400)

        # 1. Sale erstellen
        sale = Sale.objects.create(
            # Optional: User zuordnen, falls das Model es unterstützt (haben wir aktuell nicht im Sale Model, nur in PO)
            # created_by=request.user 
        )

        # 2. Items erstellen
        for item in cart_items:
            product = Product.objects.get(pk=item['id'])
            qty = int(item['qty'])
            
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=qty,
                # Wir nehmen den aktuellen Preis aus der DB zur Sicherheit, 
                # oder den vom Frontend übermittelten, falls Rabatte erlaubt wären.
                # Hier: DB Preis (Sicherheit).
                unit_price_gross=product.sales_price,
                vat_rate=product.vat.rate if product.vat else Decimal('0.00')
            )

        # Summen berechnen (Methode im Model)
        sale.calculate_totals()
        
        return JsonResponse({
            'success': True, 
            'sale_id': sale.id,
            'redirect_url': f'/admin/commerce/sale/{sale.id}/change/' # Oder direkt zum PDF?
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    
# Dedizierte PDF View für Verkäufe
@staff_member_required
def sale_receipt_pdf_view(request, sale_id):
    """
    Generiert das PDF für einen spezifischen Verkauf.
    Aufrufbar via URL: /commerce/sale/<id>/pdf/
    """
    sale = get_object_or_404(Sale, id=sale_id)
    
    # Nutzung der bestehenden Utility Funktion
    response = render_to_pdf(
        'commerce/sale_receipt_pdf.html',
        {
            'sale': sale,
            'items': sale.items.all(),
        }
    )
    
    if isinstance(response, HttpResponse) and response.status_code == 200:
        filename = f"Quittung_{sale.id}_{sale.date.strftime('%Y%m%d')}.pdf"
        # 'inline' öffnet es im Browser, 'attachment' würde es downloaden. 
        # Für POS ist 'inline' meist besser zum schnellen Drucken.
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    else:
        return HttpResponse("Fehler beim Generieren des PDFs", status=500)

@staff_member_required
@require_POST
@transaction.atomic
def api_checkout(request):
    """
    Nimmt den JSON-Warenkorb entgegen und erstellt den Sale.
    """
    try:
        data = json.loads(request.body)
        cart_items = data.get('items', [])
        
        if not cart_items:
            return JsonResponse({'error': 'Warenkorb ist leer'}, status=400)

        # 1. Sale erstellen
        sale = Sale.objects.create()

        # 2. Items erstellen
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
            # Hier geben wir jetzt die direkte PDF URL zurück!
            'pdf_url': f'/commerce/sale/{sale.id}/pdf/' 
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    

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