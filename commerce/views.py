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

        # FIX: created_by setzen!
        sale = Sale.objects.create(
            payment_method=payment_method,
            created_by=request.user 
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
        
        # Purchase Order erstellen
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