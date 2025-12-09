from django.db import models
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from decimal import Decimal

class Category(models.Model):
    name = models.CharField(max_length=100)
    
    # NEU: Prefix für die SKU Generierung (1-9...)
    sku_prefix = models.PositiveIntegerField(unique=True, editable=False, null=True)
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Automatische Vergabe des Prefixes, falls noch nicht gesetzt
        if not self.sku_prefix:
            # Höchsten existierenden Prefix finden
            max_prefix = Category.objects.aggregate(Max('sku_prefix'))['sku_prefix__max'] or 0
            self.sku_prefix = max_prefix + 1
        super().save(*args, **kwargs)

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
    # SKU ist blank=True, damit wir es im Code generieren können, wenn leer
    sku = models.CharField(max_length=50, unique=True, blank=True, help_text="Wird automatisch generiert (z.B. 10001)")
    
    # Eigenschaften
    size = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=50, blank=True)
    
    # Bestand & Preis
    stock_quantity = models.IntegerField(default=0)
    unit = models.CharField(max_length=3, choices=Unit.choices, default=Unit.PIECE)
    
    # Preise
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Einkaufspreis (Netto)")
    sales_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Verkaufspreis (Brutto)")
    vat = models.ForeignKey(Vat, on_delete=models.SET_NULL, null=True)

    # Bild
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    
    # Archivierung statt Löschen (für Datenintegrität)
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.sku or self.ean})"

    def save(self, *args, **kwargs):
        # SKU Generierungs-Logik
        if not self.sku and self.category:
            # 1. Sicherstellen, dass die Kategorie einen Prefix hat
            if not self.category.sku_prefix:
                self.category.save() # Das triggert die Prefix-Erstellung in Category.save()
                self.category.refresh_from_db()
            
            prefix = str(self.category.sku_prefix)
            
            # 2. Letztes Produkt dieser Kategorie finden, um hochzuzählen
            # Wir suchen nach SKUs, die mit diesem Prefix beginnen
            last_product = Product.objects.filter(
                category=self.category,
                sku__startswith=prefix
            ).exclude(id=self.id).order_by('sku').last()
            
            new_sequence = 1
            if last_product and last_product.sku:
                # Versuchen, den numerischen Teil (Suffix) zu extrahieren
                try:
                    # Wir nehmen an: Prefix + 4 Stellen (z.B. Prefix '1' -> '10001')
                    # Wir schneiden den Prefix ab und parsen den Rest
                    suffix = last_product.sku[len(prefix):]
                    if suffix.isdigit():
                        new_sequence = int(suffix) + 1
                except ValueError:
                    pass # Fallback auf 1, falls SKU Format manuell verpfuscht wurde
            
            # 3. Formatierung: Prefix + 4-stellige Nummer (mit Nullen aufgefüllt)
            # z.B. Prefix 1, Seq 1 -> 10001
            # z.B. Prefix 5, Seq 23 -> 50023
            self.sku = f"{prefix}{new_sequence:04d}"
            
        super().save(*args, **kwargs)