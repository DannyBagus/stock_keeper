from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
import json
from .models import Product, Category
# Importiere Modelle aus der commerce App
# Wir nutzen apps.get_model um zirkuläre Imports zu vermeiden, falls nötig, 
# aber hier sollte der direkte Import funktionieren.
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
    
    return render(request, 'core/scanner.html')

@staff_member_required
def dashboard_view(request):
    """
    Landing Page Dashboard mit KPIs und Charts.
    """
    # 1. Verkaufserlös pro Monat (Chart Daten)
    # Wir betrachten alle Verkäufe
    sales_qs = Sale.objects.annotate(month=TruncMonth('date'))\
        .values('month')\
        .annotate(total=Sum('total_amount_gross'))\
        .order_by('month')
    
    months = []
    revenues = []
    
    for entry in sales_qs:
        if entry['month']:
            months.append(entry['month'].strftime('%b %Y'))
            # Decimal muss zu float konvertiert werden für JSON/JS
            revenues.append(float(entry['total'] or 0))

    # 2. Produkte nach Kategorie (Pie Chart Daten)
    cat_qs = Category.objects.annotate(p_count=Count('products')).filter(p_count__gt=0)
    cat_labels = [c.name for c in cat_qs]
    cat_data = [c.p_count for c in cat_qs]

    # 3. Pendente Bestellungen (Tabelle)
    # Alles was NICHT RECEIVED und NICHT CANCELLED ist
    pending_orders = PurchaseOrder.objects.exclude(
        status__in=[PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED]
    ).order_by('date')[:5] # Nur die ältesten 5 anzeigen

    # 4. KPIs
    total_products = Product.objects.count()
    low_stock = Product.objects.filter(stock_quantity__lt=5).count()
    
    context = {
        'months_json': json.dumps(months),
        'revenues_json': json.dumps(revenues),
        'cat_labels_json': json.dumps(cat_labels),
        'cat_data_json': json.dumps(cat_data),
        'pending_orders': pending_orders,
        'total_products': total_products,
        'low_stock': low_stock,
    }
    
    return render(request, 'core/dashboard.html', context)