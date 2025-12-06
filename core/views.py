from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from .models import Product

@staff_member_required
def scanner_view(request):
    """
    Zeigt den Scanner an und verarbeitet gescannte EANs.
    """
    if request.method == "POST":
        ean = request.POST.get('ean')
        if ean:
            try:
                # 1. Produkt suchen
                product = Product.objects.get(ean=ean)
                messages.success(request, f"Produkt '{product.name}' gefunden.")
                
                # 2. Redirect zur Admin-Bearbeiten-Seite dieses Produkts
                return redirect(f'/core/product/{product.id}/change/')
                
            except Product.DoesNotExist:
                messages.warning(request, f"Produkt mit EAN {ean} nicht gefunden. Neues Produkt anlegen?")
                
                # 3. Redirect zur Erstellen-Seite (EAN vorausgefüllt via GET Parameter)
                return redirect(f'/core/product/add/?ean={ean}')
    
    return render(request, 'core/scanner.html')