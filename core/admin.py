from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Product, Category, Supplier, Vat, StockMovement

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku_prefix')
    search_fields = ('name',)

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'website', 'contact_person')
    search_fields = ('name', 'email')

@admin.register(Vat)
class VatAdmin(admin.ModelAdmin):
    list_display = ('name', 'rate', 'is_default')
    list_editable = ('is_default',)

# --- AUDIT LOG INLINE ---
class StockMovementInline(admin.TabularInline):
    model = StockMovement
    fk_name = 'product' 
    extra = 0 
    
    readonly_fields = ('created_at', 'user', 'movement_type', 'quantity', 'stock_after', 'source_link', 'notes')
    fields = ('created_at', 'user', 'movement_type', 'quantity', 'stock_after', 'source_link', 'notes')
    
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False

    def source_link(self, obj):
        if obj.content_object:
            app_label = obj.content_type.app_label
            model_name = obj.content_type.model
            try:
                url = reverse(f'admin:{app_label}_{model_name}_change', args=[obj.object_id])
                return format_html('<a href="{}">{} #{}</a>', url, model_name.capitalize(), obj.object_id)
            except Exception:
                return f"{model_name} #{obj.object_id}"
        return "-"
    source_link.short_description = "Beleg / Ursprung"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    # KORREKTUR: Wir nutzen 'display_name' statt 'name'
    list_display = ('display_name', 'sku', 'stock_quantity', 'track_stock', 'unit', 'sales_price', 'category', 'supplier')
    list_filter = ('category', 'supplier', 'unit', 'is_active', 'track_stock')
    search_fields = ('name', 'sku', 'ean', 'description')
    
    list_editable = ('sales_price',) 
    
    inlines = [StockMovementInline]
    
    fieldsets = (
        ('Basisdaten', {
            'fields': ('name', 'description', 'category', 'supplier', 'is_active', 'track_stock')
        }),
        ('Identifikation', {
            'fields': ('ean', 'sku')
        }),
        ('Eigenschaften', {
            'fields': ('size', 'color', 'image')
        }),
        ('Lager & Preis', {
            'fields': ('stock_quantity', 'unit', 'cost_price', 'sales_price', 'vat')
        }),
    )

    # Diese Methode erzwingt die Nutzung Ihrer __str__ Formatierung
    @admin.display(description='Produktbezeichnung', ordering='name')
    def display_name(self, obj):
        return str(obj)

    def save_model(self, request, obj, form, change):
        """
        Erkennt manuelle Bestandsänderungen im Admin und schreibt einen Audit-Log Eintrag.
        """
        if change:
            try:
                old_obj = Product.objects.get(pk=obj.pk)
                if obj.track_stock: 
                    diff = obj.stock_quantity - old_obj.stock_quantity
                    
                    if diff != 0:
                        StockMovement.objects.create(
                            product=obj,
                            quantity=diff,
                            stock_after=obj.stock_quantity,
                            movement_type=StockMovement.Type.CORRECTION,
                            user=request.user,
                            notes="Manuelle Anpassung im Admin"
                        )
            except Product.DoesNotExist:
                pass

        super().save_model(request, obj, form, change)

        if not change and obj.stock_quantity != 0 and obj.track_stock:
             StockMovement.objects.create(
                product=obj,
                quantity=obj.stock_quantity,
                stock_after=obj.stock_quantity,
                movement_type=StockMovement.Type.INITIAL,
                user=request.user,
                notes="Initialbestand bei Erstellung"
            )