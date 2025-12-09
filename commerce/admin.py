from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.contrib import messages
from .models import PurchaseOrder, PurchaseOrderItem, Sale, SaleItem 
from .utils import render_to_pdf
from core.models import Product, Supplier 
from .forms import SaleItemFormSet 

# --- Inlines (Tabellen innerhalb der Hauptmaske) ---

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1 
    autocomplete_fields = ['product'] 
    
    # Preis ist optional (wird im Model automatisch gesetzt), kann aber hier editiert werden
    fields = ('product', 'quantity', 'unit_price') 


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    autocomplete_fields = ['product']
    
    # WICHTIG: Custom FormSet, um VAT Rate automatisch zu setzen und DB Fehler zu verhindern
    formset = SaleItemFormSet
    
    # Vereinfachte Felder für die Kasse (VAT Rate wird im Hintergrund gesetzt)
    fields = ('product', 'quantity', 'unit_price_gross')


# --- Haupt Admin für Bestellaufträge ---

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    actions = ['action_mark_as_received', 'action_generate_pdf']
    inlines = [PurchaseOrderItemInline]
    
    list_display = ('id', 'supplier', 'date', 'status', 'total_items_count', 'is_booked')
    list_filter = ('status', 'date', 'is_booked')

    # 1. Felder konfigurieren: Dynamisch je nach Ansicht (Erstellen vs. Bearbeiten)
    def get_fields(self, request, obj=None):
        if obj is None:
            # Ansicht: NEU ERSTELLEN (Initial)
            # Nur Supplier und Datum anzeigen. Der Rest wird automatisch gesetzt.
            return ('supplier', 'date')
        else:
            # Ansicht: BEARBEITEN
            # Alle relevanten Felder anzeigen
            return ('supplier', 'date', 'status', 'created_by', 'invoice_document', 'is_booked')

    # 2. Automatische Felder setzen beim Speichern
    def save_model(self, request, obj, form, change):
        # Wenn es eine neue Bestellung ist (keine ID)
        if not obj.pk:
            obj.created_by = request.user
            obj.status = PurchaseOrder.Status.DRAFT
            obj.is_booked = False
        
        super().save_model(request, obj, form, change)

    # 3. Readonly Logik (KORRIGIERT)
    def get_readonly_fields(self, request, obj=None):
        if obj:
            # Status sperren NUR wenn 'Ware eingegangen' oder 'Storniert'
            # Im Status 'Bestellt' (ORDERED) bleibt es bearbeitbar.
            if obj.status in [PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED]:
                return ('status', 'created_by', 'is_booked') 
            
            # Im normalen Edit-Modus soll 'created_by' und 'is_booked' meist nur informativ sein
            return ('created_by', 'is_booked')
            
        return ()

    def total_items_count(self, obj):
        return obj.items.count()
    total_items_count.short_description = "Anzahl Positionen"

    # Action 1: Bestand buchen
    @admin.action(description='Ware als eingegangen markieren (Bestand buchen)')
    def action_mark_as_received(self, request, queryset):
        count = 0
        for order in queryset:
            if order.status != PurchaseOrder.Status.RECEIVED:
                order.mark_as_received()
                count += 1
        
        if count > 0:
            messages.success(request, f"{count} Bestellungen erfolgreich gebucht. Bestand wurde aktualisiert.")
        else:
            messages.warning(request, "Keine offenen Bestellungen zum Buchen ausgewählt.")
            
    # Action 2: PDF Generierung
    @admin.action(description='Bestellauftrag als PDF exportieren (nur 1 wählen)')
    def action_generate_pdf(self, request, queryset):
        if queryset.count() != 1:
            messages.error(request, "Bitte wählen Sie genau EINEN Bestellauftrag für den PDF-Export aus.")
            return redirect(request.META['HTTP_REFERER'])

        order = queryset.first()
        
        response = render_to_pdf(
            'commerce/purchase_order_pdf.html',
            {
                'order': order,
                'items': order.items.all(),
            }
        )
        
        if isinstance(response, HttpResponse) and response.status_code == 200:
            filename = f"Bestellauftrag_{order.id}_{order.supplier.name}.pdf"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        else:
            return response

# --- Haupt Admin für Verkäufe ---

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    actions = ['action_generate_receipt'] 
    
    list_display = ('id', 'date', 'total_amount_gross')
    list_filter = ('date',)
    inlines = [SaleItemInline] 
    readonly_fields = ('total_amount_net', 'total_amount_gross')
    
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.calculate_totals()
        
    # Action zur Quittungsgenerierung
    @admin.action(description='Quittung/Beleg als PDF exportieren (nur 1 wählen)')
    def action_generate_receipt(self, request, queryset):
        if queryset.count() != 1:
            messages.error(request, "Bitte wählen Sie genau EINEN Verkauf für den PDF-Export aus.")
            return redirect(request.META['HTTP_REFERER'])

        sale = queryset.first()
        
        response = render_to_pdf(
            'commerce/sale_receipt_pdf.html',
            {
                'sale': sale,
                'items': sale.items.all(),
            }
        )
        
        if isinstance(response, HttpResponse) and response.status_code == 200:
            filename = f"Quittung_{sale.id}_{sale.date.strftime('%Y%m%d')}.pdf"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        else:
            return response