from django.shortcuts import render, redirect
from django.contrib import admin 
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse
import json
from .models import Product, Category, StockMovement
from commerce.models import Sale, PurchaseOrder

@staff_member_required
def scanner_view(request):
    """
    Zeigt den Scanner an und verarbeitet gescannte EANs.
    """
    if request.method == "POST":
        ean = request.POST.get('ean')
        if ean:
            try:
                product = Product.objects.get(ean=ean)
                messages.success(request, f"Produkt '{product.name}' gefunden.")
                return redirect(f'/admin/core/product/{product.id}/change/')
            except Product.DoesNotExist:
                messages.warning(request, f"Produkt mit EAN {ean} nicht gefunden. Neues Produkt anlegen?")
                return redirect(f'/admin/core/product/add/?ean={ean}')
    
    context = admin.site.each_context(request)
    return render(request, 'core/scanner.html', context)

@staff_member_required
def dashboard_view(request):
    """
    Landing Page Dashboard mit KPIs und Charts.
    """
    # 1. Verkaufserlös pro Monat
    sales_qs = Sale.objects.annotate(month=TruncMonth('date'))\
        .values('month')\
        .annotate(total=Sum('total_amount_gross'))\
        .order_by('month')
    
    months = []
    revenues = []
    
    for entry in sales_qs:
        if entry['month']:
            months.append(entry['month'].strftime('%b %Y'))
            revenues.append(float(entry['total'] or 0))

    # 2. Produkte nach Kategorie
    cat_qs = Category.objects.annotate(p_count=Count('products')).filter(p_count__gt=0)
    cat_labels = [c.name for c in cat_qs]
    cat_data = [c.p_count for c in cat_qs]

    # 3. Pendente Bestellungen
    pending_orders = PurchaseOrder.objects.exclude(
        status__in=[PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED]
    ).order_by('date')[:5]

    # 4. KPIs
    # Total aller Produkte (auch inaktive oder ohne Tracking, als Info)
    total_products = Product.objects.count()
    
    # FIX: Nur Produkte zählen, die auch gelagert werden (track_stock=True)
    low_stock = Product.objects.filter(
        stock_quantity__lt=5, 
        track_stock=True
    ).count()
    
    context = admin.site.each_context(request)
    context.update({
        'months_json': json.dumps(months),
        'revenues_json': json.dumps(revenues),
        'cat_labels_json': json.dumps(cat_labels),
        'cat_data_json': json.dumps(cat_data),
        'pending_orders': pending_orders,
        'total_products': total_products,
        'low_stock': low_stock,
        'title': 'Dashboard' 
    })
    
    return render(request, 'core/dashboard.html', context)


@staff_member_required
def inventory_view(request):
    """
    Zeigt das Inventur-Interface (Stock Take).
    """
    context = admin.site.each_context(request)
    context.update({'title': 'Inventur / Lagerkorrektur'})
    return render(request, 'core/inventory.html', context)


@staff_member_required
@require_POST
@transaction.atomic
def api_inventory_correct(request):
    """
    Führt die Bestandskorrektur durch.
    """
    try:
        data = json.loads(request.body)
        product_id = data.get('product_id')
        counted_qty = data.get('counted_qty')
        reason = data.get('reason', 'Inventur / Stock Take') # <--- NEU: Grund auslesen
        
        if product_id is None or counted_qty is None:
            return JsonResponse({'error': 'Fehlende Daten'}, status=400)

        product = Product.objects.get(pk=product_id)
        
        if not product.track_stock:
            return JsonResponse({'error': 'Für dieses Produkt wird kein Lager geführt.'}, status=400)

        # Differenz berechnen
        current_qty = product.stock_quantity
        # counted (8) - current (10) = -2
        diff = int(counted_qty) - current_qty
        
        if diff != 0:
            product.adjust_stock(
                quantity=diff,
                movement_type=StockMovement.Type.CORRECTION,
                user=request.user,
                notes=reason
            )
            
        return JsonResponse({
            'success': True,
            'product_name': product.name,
            'old_stock': current_qty,
            'new_stock': product.stock_quantity,
            'diff': diff
        })

    except Product.DoesNotExist:
        return JsonResponse({'error': 'Produkt nicht gefunden'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)