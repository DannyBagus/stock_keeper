from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.contrib import messages
from .models import PurchaseOrder, PurchaseOrderItem, Sale, SaleItem 
from .utils import render_to_pdf
from core.models import Product, Supplier 
from .forms import SaleItemFormSet, PurchaseOrderForm

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
    form = PurchaseOrderForm
    actions = ['action_mark_as_received', 'action_generate_pdf']
    inlines = [PurchaseOrderItemInline]
    
    # NEU: 'created_by' zur Liste hinzugefügt
    list_display = ('id', 'supplier', 'date', 'status', 'total_items_count', 'created_by', 'is_booked')
    list_filter = ('status', 'date', 'is_booked', 'created_by')

    # 1. Felder konfigurieren
    def get_fields(self, request, obj=None):
        if obj is None:
            # Initial: Nur Supplier und Datum
            return ('supplier', 'date')
        else:
            # Bearbeiten: Alle Felder
            return ('supplier', 'date', 'status', 'created_by', 'invoice_document', 'is_booked')

    # 2. Automatische Felder setzen
    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
            obj.status = PurchaseOrder.Status.DRAFT
            obj.is_booked = False
        super().save_model(request, obj, form, change)

    # 3. Readonly Logik (KORRIGIERT)
    def get_readonly_fields(self, request, obj=None):
        if obj:
            # FIX: 'is_booked' wurde hier entfernt, damit es immer editierbar bleibt!
            
            # Status sperren wenn finalisiert
            if obj.status in [PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED]:
                return ('status', 'created_by') 
            
            # Im normalen Edit-Modus ist nur der Ersteller fix
            return ('created_by',)
            
        return ()

    def total_items_count(self, obj):
        return obj.items.count()
    total_items_count.short_description = "Anzahl Positionen"

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

# --- Sale Admin (MIT STORNO) ---

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    # NEU: Refund Action hinzufügen
    actions = ['action_generate_receipt', 'action_refund_sale'] 
    
    # NEU: Status anzeigen
    list_display = ('id', 'date', 'total_amount_gross', 'payment_method', 'channel', 'status', 'created_by')
    list_filter = ('date', 'payment_method', 'channel', 'status', 'created_by')
    inlines = [SaleItemInline] 
    readonly_fields = ('total_amount_net', 'total_amount_gross', 'transaction_id', 'created_by', 'channel', 'status')
    
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.calculate_totals()

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        
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

    # NEU: Die Storno-Action
    @admin.action(description='Verkauf stornieren / Ware retournieren')
    def action_refund_sale(self, request, queryset):
        count = 0
        for sale in queryset:
            # Nur stornieren, wenn noch nicht storniert
            if sale.status != Sale.Status.REFUNDED:
                sale.refund(user=request.user)
                count += 1
        
        if count > 0:
            self.message_user(request, f"{count} Verkäufe erfolgreich storniert. Ware wurde zurückgebucht.", messages.SUCCESS)
        else:
            self.message_user(request, "Keine stornierbaren Verkäufe ausgewählt.", messages.WARNING)