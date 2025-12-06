from django.db import models
from django.utils.translation import gettext_lazy as _
from decimal import Decimal # Decimal muss importiert werden

class Category(models.Model):
    name = models.CharField(max_length=100)
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

class Supplier(models.Model):
    name = models.CharField(max_length=200)
    website = models.URLField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    contact_person = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name

class Vat(models.Model):
    rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Prozentsatz, z.B. 19.00")
    name = models.CharField(max_length=50, help_text="z.B. MwSt. Standard")
    is_default = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.name} ({self.rate}%)"

class Product(models.Model):
    class Unit(models.TextChoices):
        PIECE = 'PCS', _('Stück')
        KG = 'KG', _('Kilogramm')
        LITER = 'L', _('Liter')

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name='products')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, related_name='products')
    
    # Identifikation
    ean = models.CharField(max_length=13, unique=True, help_text="Barcode / EAN")
    sku = models.CharField(max_length=50, unique=True, blank=True, help_text="Interne Artikelnummer")
    
    # Eigenschaften
    size = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=50, blank=True)
    
    # Bestand & Preis
    stock_quantity = models.IntegerField(default=0)
    unit = models.CharField(max_length=3, choices=Unit.choices, default=Unit.PIECE)
    
    # Preise immer Decimal!
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Einkaufspreis (Netto)")
    sales_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Verkaufspreis (Brutto)")
    vat = models.ForeignKey(Vat, on_delete=models.SET_NULL, null=True)

    # Bild für die App später
    image = models.ImageField(upload_to='products/', blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.sku or self.ean})"