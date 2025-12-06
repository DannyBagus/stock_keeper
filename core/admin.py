from django.contrib import admin
from .models import Product, Category, Supplier, Vat

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'website', 'contact_person')
    search_fields = ('name', 'email')

@admin.register(Vat)
class VatAdmin(admin.ModelAdmin):
    list_display = ('name', 'rate', 'is_default')
    list_editable = ('is_default',)

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'stock_quantity', 'unit', 'sales_price', 'category', 'supplier')
    list_filter = ('category', 'supplier', 'unit')
    search_fields = ('name', 'sku', 'ean', 'description')
    
    # Das macht die Liste super mächtig: Direktes Bearbeiten
    list_editable = ('stock_quantity', 'sales_price')
    
    # Schöne Gruppierung im Formular
    fieldsets = (
        ('Basisdaten', {
            'fields': ('name', 'description', 'category', 'supplier')
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