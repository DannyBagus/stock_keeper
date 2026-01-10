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
    fields = ('product', 'quantity', 'unit_price_gross', 'vat_rate')

# --- Purchase Order Admin ---

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    form = PurchaseOrderForm
    actions = ['action_mark_as_received', 'action_generate_pdf']
    inlines = [PurchaseOrderItemInline]
    
    list_display = ('id', 'supplier', 'date', 'status', 'total_items_count', 'created_by', 'is_booked')
    list_filter = ('status', 'date', 'is_booked', 'created_by')

    def get_fields(self, request, obj=None):
        if obj is None:
            return ('supplier', 'date')
        else:
            return ('supplier', 'date', 'status', 'created_by', 'invoice_document', 'is_booked')

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
            obj.status = PurchaseOrder.Status.DRAFT
            obj.is_booked = False
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            if obj.status in [PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED]:
                return ('status', 'created_by') 
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

# --- Sale Admin ---

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    actions = ['action_generate_receipt', 'action_refund_sale'] 
    
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

    # NEU: Die Storno-Action mit intelligenter Info-Ausgabe
    @admin.action(description='Verkauf stornieren / Ware retournieren')
    def action_refund_sale(self, request, queryset):
        count = 0
        refund_instructions = []
        
        for sale in queryset:
            # Nur stornieren, wenn noch nicht storniert
            if sale.status != Sale.Status.REFUNDED:
                sale.refund(user=request.user)
                count += 1
                
                # Spezifische Hinweise je nach Zahlungsmethode generieren
                if sale.payment_method == Sale.PaymentMethod.SUMUP:
                    # Bei SumUp haben wir keine ID, daher Hinweis auf manuelle Suche via Betrag/Zeit
                    refund_instructions.append(f"SumUp (Sale #{sale.id}, {sale.total_amount_gross} CHF)")
                elif sale.transaction_id:
                    # Bei Shopify (oder anderen) haben wir eine ID
                    refund_instructions.append(f"Ext. Ref: {sale.transaction_id}")
        
        if count > 0:
            msg = f"{count} Verkäufe erfolgreich storniert. Ware wurde zurückgebucht."
            if refund_instructions:
                msg += f" WICHTIG: Bitte folgende Rückerstattungen manuell im Zahlungsanbieter (App/Portal) vornehmen: {', '.join(refund_instructions)}"
            
            self.message_user(request, msg, messages.SUCCESS)
        else:
            self.message_user(request, "Keine stornierbaren Verkäufe ausgewählt.", messages.WARNING)