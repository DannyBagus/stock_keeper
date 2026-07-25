from urllib.parse import urlencode

from django.contrib import admin
from django.db.models import Q
from django.utils.html import format_html
from django.utils.safestring import mark_safe
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



class HasImageFilter(admin.SimpleListFilter):
    """Arbeitsliste: Produkte mit / ohne hochgeladenes Bild filtern."""
    title = "Bild vorhanden"
    parameter_name = 'has_image'

    def lookups(self, request, model_admin):
        return (('no', 'Nein (Bild fehlt)'), ('yes', 'Ja'))

    def queryset(self, request, queryset):
        missing = Q(image='') | Q(image__isnull=True)
        if self.value() == 'no':
            return queryset.filter(missing)
        if self.value() == 'yes':
            return queryset.exclude(missing)
        return queryset


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'sku', 'has_image', 'stock_quantity', 'track_stock', 'unit', 'sales_price', 'category', 'supplier')
    list_filter = (HasImageFilter, 'category', 'supplier', 'unit', 'is_active', 'track_stock')
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

    @admin.display(description='Bild', boolean=True, ordering='image')
    def has_image(self, obj):
        return bool(obj.image)

    def get_fieldsets(self, request, obj=None):
        """Den Google-Suchbutton nur einblenden, wenn noch kein Bild hinterlegt ist."""
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None or obj.image:
            return fieldsets

        return [
            (
                name,
                {**opts, 'fields': tuple(opts['fields']) + ('image_search_links',)}
                if 'image' in opts.get('fields', ()) else opts,
            )
            for name, opts in fieldsets
        ]

    def get_readonly_fields(self, request, obj=None):
        return tuple(super().get_readonly_fields(request, obj)) + ('image_search_links',)

    @admin.display(description='Bild suchen')
    def image_search_links(self, obj):
        """Absprung in die Google-Bildersuche mit den Produktdaten als Suchbegriff."""
        if obj is None or obj.pk is None:
            return "-"

        terms = [
            obj.supplier.name if obj.supplier else '',
            obj.name,
            obj.category.name if obj.category else '',
        ]
        query = " ".join(t.strip() for t in terms if t and t.strip())

        buttons = [format_html(
            '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener" href="{}">'
            '<i class="fas fa-search"></i> Bilder bei Google suchen</a>',
            f"https://www.google.com/search?{urlencode({'q': query, 'tbm': 'isch'})}",
        )]

        if obj.ean:
            buttons.append(format_html(
                '<a class="btn btn-sm btn-outline-secondary ml-2" target="_blank" rel="noopener" href="{}">'
                '<i class="fas fa-barcode"></i> Suche mit EAN</a>',
                f"https://www.google.com/search?{urlencode({'q': f'{query} {obj.ean}'.strip(), 'tbm': 'isch'})}",
            ))

        return format_html(
            '<div>{}<p class="help mt-2 mb-0">Suchbegriff: {}</p></div>',
            mark_safe("".join(buttons)), query,
        )

    # Diese Methode sorgt für das Pre-Filling via URL-Parameter
    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        # Wenn 'ean' in der URL ist, fügen wir es zu den Initial-Daten hinzu
        if 'ean' in request.GET:
            initial['ean'] = request.GET.get('ean')
        return initial

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