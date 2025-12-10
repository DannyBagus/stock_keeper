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
    fk_name = 'product' # Expliziter FK, falls es mehrere gäbe
    extra = 0 # Keine leeren Zeilen für neue Einträge
    
    # Wir machen das Log schreibgeschützt, damit niemand die Historie verfälscht
    readonly_fields = ('created_at', 'user', 'movement_type', 'quantity', 'stock_after', 'source_link', 'notes')
    fields = ('created_at', 'user', 'movement_type', 'quantity', 'stock_after', 'source_link', 'notes')
    
    # Verhindert das Löschen/Hinzufügen von Logs direkt am Produkt
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False

    def source_link(self, obj):
        """
        Erstellt einen klickbaren Link zum Ursprungsobjekt (Sale oder PurchaseOrder).
        """
        if obj.content_object:
            # Holt App-Label und Model-Name für den Admin-URL-Lookup
            # z.B. 'commerce', 'sale'
            app_label = obj.content_type.app_label
            model_name = obj.content_type.model
            
            try:
                # Baut die URL: admin:commerce_sale_change
                url = reverse(f'admin:{app_label}_{model_name}_change', args=[obj.object_id])
                return format_html('<a href="{}">{} #{}</a>', url, model_name.capitalize(), obj.object_id)
            except Exception:
                return f"{model_name} #{obj.object_id}"
        return "-"
    source_link.short_description = "Beleg / Ursprung"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'stock_quantity', 'track_stock', 'unit', 'sales_price', 'category', 'supplier')
    list_filter = ('category', 'supplier', 'unit', 'track_stock', 'is_active')
    search_fields = ('name', 'sku', 'ean', 'description')
    
    list_editable = ('sales_price',) 
    
    # HIER binden wir die Historie als Tab ein
    inlines = [StockMovementInline]
    
    fieldsets = (
        ('Basisdaten', {
            'fields': ('name', 'description', 'category', 'supplier', 'is_active')
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

    def save_model(self, request, obj, form, change):
        """
        Erkennt manuelle Bestandsänderungen im Admin und schreibt einen Audit-Log Eintrag.
        """
        if change:
            # Update-Fall: Wir holen uns den alten Zustand aus der DB
            try:
                old_obj = Product.objects.get(pk=obj.pk)
                diff = obj.stock_quantity - old_obj.stock_quantity
                
                if diff != 0:
                    # Manuelle Korrektur erfassen
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
        else:
            # Neuerstellungs-Fall: Wenn direkt mit Bestand angelegt wird
            if obj.stock_quantity != 0:
                # Da das Produkt erst nach super().save_model eine ID hat, 
                # müssen wir hier aufpassen oder es danach machen.
                # Aber StockMovement braucht eine Produkt-ID.
                # Strategie: Erst speichern, dann Log schreiben.
                pass 

        # Das eigentliche Speichern des Produkts
        super().save_model(request, obj, form, change)

        # Nachträgliche Behandlung für NEUE Produkte (da sie jetzt eine ID haben)
        if not change and obj.stock_quantity != 0:
             StockMovement.objects.create(
                product=obj,
                quantity=obj.stock_quantity,
                stock_after=obj.stock_quantity,
                movement_type=StockMovement.Type.INITIAL,
                user=request.user,
                notes="Initialbestand bei Erstellung"
            )