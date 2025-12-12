from django.shortcuts import render, redirect
from django.contrib import admin 
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, F, ExpressionWrapper, DecimalField, OuterRef, Subquery
from django.db.models.functions import TruncMonth, Coalesce
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponse
from decimal import Decimal
import json
from .models import Product, Category, StockMovement
from .forms import InventoryReportForm
from commerce.models import Sale, PurchaseOrder
from commerce.utils import render_to_pdf 

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
    

# Inventarliste Report
@staff_member_required
def inventory_report_view(request):
    if request.method == 'POST':
        form = InventoryReportForm(request.POST)
        if form.is_valid():
            report_date = form.cleaned_data['date']
            categories = form.cleaned_data['categories']
            only_positive = form.cleaned_data['only_positive']
            
            # Basis-Query
            products = Product.objects.filter(track_stock=True)
            
            if categories:
                products = products.filter(category__in=categories)
            
            # LOGIK-WECHSEL: Historischen Bestand berechnen
            # Wir suchen für jedes Produkt die letzte Bewegung, die <= dem Stichtag war.
            # Der Wert 'stock_after' dieser Bewegung war der Bestand an jenem Abend.
            latest_movement = StockMovement.objects.filter(
                product=OuterRef('pk'),
                created_at__date__lte=report_date
            ).order_by('-created_at', '-id').values('stock_after')[:1]

            products = products.annotate(
                # Coalesce wandelt None (keine Bewegung gefunden) in 0 um
                calculated_stock=Coalesce(Subquery(latest_movement), 0)
            )
            
            # Filterung basierend auf dem HISTORISCHEN Bestand
            if only_positive:
                products = products.filter(calculated_stock__gt=0)
                
            # Berechnung des Lagerwerts (Menge * Einkaufspreis)
            products = products.annotate(
                total_value=ExpressionWrapper(
                    F('calculated_stock') * F('cost_price'),
                    output_field=DecimalField()
                )
            ).order_by('category__name', 'name')
            
            # Gesamtsumme berechnen
            total_inventory_value = products.aggregate(Sum('total_value'))['total_value__sum'] or Decimal('0.00')
            
            context = {
                'product_list': products,
                'report_date': report_date,
                'generation_date': timezone.now(),
                'total_inventory_value': total_inventory_value,
                'categories': ", ".join([c.name for c in categories]) if categories else "Alle"
            }
            
            response = render_to_pdf('core/inventory_report_pdf.html', context)
            if isinstance(response, HttpResponse) and response.status_code == 200:
                filename = f"Inventarliste_{report_date}.pdf"
                response['Content-Disposition'] = f'inline; filename="{filename}"'
                return response
            else:
                return HttpResponse("Fehler beim Generieren des PDFs", status=500)
    else:
        form = InventoryReportForm()
        
    context = admin.site.each_context(request)
    context.update({
        'form': form,
        'title': 'Inventarliste (Bilanz)'
    })
    return render(request, 'core/inventory_report_form.html', context)