from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.contrib import messages
from .models import PurchaseOrder, PurchaseOrderItem, Sale, SaleItem 
from .utils import render_to_pdf
from core.models import Product, Supplier # Muss für die Inlines und Actions vorhanden sein
from .forms import SaleItemFormSet # NEU: Import des Custom FormSets

# --- Inlines (Tabellen innerhalb der Hauptmaske) ---

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1 
    autocomplete_fields = ['product'] 
    fields = ('product', 'quantity', 'unit_price') 


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    autocomplete_fields = ['product']
    
    # NEU: Weist das Custom Formularset zu, das vat_rate setzt
    formset = SaleItemFormSet
    
    # Vereinfachte Felder, da die VAT Rate vom FormSet/Hook gesetzt wird
    fields = ('product', 'quantity', 'unit_price_gross')
    # Wir könnten vat_rate als readonly hinzufügen, aber es wird nicht als Feld übergeben, 
    # um den IntegrityError zu vermeiden.


# --- Haupt Admin für Bestellaufträge ---

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    actions = ['action_mark_as_received', 'action_generate_pdf']
    
    list_display = ('id', 'supplier', 'date', 'status', 'total_items_count', 'is_booked')
    list_filter = ('status', 'date', 'is_booked')
    inlines = [PurchaseOrderItemInline]
    
    # KORREKTUR: Methode MUSS self als Argument haben und INNERHALB der Klasse sein
    def total_items_count(self, obj):
        return obj.items.count()
    total_items_count.short_description = "Anzahl Positionen"
    
    # Steuert, welche Felder schreibgeschützt sind (verhindert manuelles Buchen)
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in [obj.Status.ORDERED, obj.Status.RECEIVED]:
            return self.readonly_fields + ('status',)
        return self.readonly_fields


    # Action 1: Bestand buchen (unverändert)
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
            
    # Action 2: PDF Generierung (unverändert)
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