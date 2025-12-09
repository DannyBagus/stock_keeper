from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.contrib import messages
from .models import PurchaseOrder, PurchaseOrderItem, Sale, SaleItem 
from .utils import render_to_pdf
from core.models import Product, Supplier 
from .forms import SaleItemFormSet 

# --- Inlines ---

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1 
    autocomplete_fields = ['product'] 
    fields = ('product', 'quantity', 'unit_price') 

class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    autocomplete_fields = ['product']
    formset = SaleItemFormSet
    fields = ('product', 'quantity', 'unit_price_gross')

# --- Purchase Order Admin ---

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    actions = ['action_mark_as_received', 'action_generate_pdf']
    inlines = [PurchaseOrderItemInline]
    
    list_display = ('id', 'supplier', 'date', 'status', 'total_items_count', 'is_booked')
    list_filter = ('status', 'date', 'is_booked')
    
    # Readonly wenn Status abgeschlossen (Sicherheits-Feature behalten wir)
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in [PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.RECEIVED]:
            return ('status', 'created_by', 'is_booked') 
        return ()

    def total_items_count(self, obj):
        return obj.items.count()
    total_items_count.short_description = "Anzahl Positionen"

    # Actions bleiben gleich...
    @admin.action(description='Ware als eingegangen markieren (Bestand buchen)')
    def action_mark_as_received(self, request, queryset):
        count = 0
        for order in queryset:
            if order.status != PurchaseOrder.Status.RECEIVED:
                order.mark_as_received()
                count += 1
        if count > 0:
            messages.success(request, f"{count} Bestellungen erfolgreich gebucht.")
        else:
            messages.warning(request, "Keine offenen Bestellungen ausgewählt.")
            
    @admin.action(description='Bestellauftrag als PDF exportieren')
    def action_generate_pdf(self, request, queryset):
        if queryset.count() != 1:
            messages.error(request, "Bitte wählen Sie genau EINEN Bestellauftrag aus.")
            return redirect(request.META['HTTP_REFERER'])
        order = queryset.first()
        response = render_to_pdf('commerce/purchase_order_pdf.html', {'order': order, 'items': order.items.all()})
        if isinstance(response, HttpResponse) and response.status_code == 200:
            filename = f"Bestellauftrag_{order.id}_{order.supplier.name}.pdf"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        else:
            return response

# --- Sale Admin ---
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
        
    @admin.action(description='Quittung als PDF exportieren')
    def action_generate_receipt(self, request, queryset):
        if queryset.count() != 1:
            messages.error(request, "Bitte wählen Sie genau EINEN Verkauf aus.")
            return redirect(request.META['HTTP_REFERER'])
        sale = queryset.first()
        response = render_to_pdf('commerce/sale_receipt_pdf.html', {'sale': sale, 'items': sale.items.all()})
        if isinstance(response, HttpResponse) and response.status_code == 200:
            filename = f"Quittung_{sale.id}_{sale.date.strftime('%Y%m%d')}.pdf"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        else:
            return response