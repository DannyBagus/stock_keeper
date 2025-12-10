from django.db import models
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

class Category(models.Model):
    name = models.CharField(max_length=100)
    
    # Prefix für die SKU Generierung (1-9...)
    sku_prefix = models.PositiveIntegerField(unique=True, editable=False, null=True)
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Automatische Vergabe des Prefixes, falls noch nicht gesetzt
        if not self.sku_prefix:
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
    sku = models.CharField(max_length=50, unique=True, blank=True, help_text="Wird automatisch generiert (z.B. 10001)")
    
    # Eigenschaften
    size = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=50, blank=True)
    
    # Schalter für Lagerführung
    track_stock = models.BooleanField(
        default=True, 
        verbose_name="Lagerbestand führen",
        help_text="Deaktivieren für Dienstleistungen oder Gastronomie-Produkte (z.B. Kaffee)."
    )
    
    # Bestand & Preis
    # WICHTIG: Dieser Wert ist nun ein "Cache". Die Wahrheit liegt in den StockMovements.
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
            if not self.category.sku_prefix:
                self.category.save()
                self.category.refresh_from_db()
            
            prefix = str(self.category.sku_prefix)
            
            last_product = Product.objects.filter(
                category=self.category,
                sku__startswith=prefix
            ).exclude(id=self.id).order_by('sku').last()
            
            new_sequence = 1
            if last_product and last_product.sku:
                try:
                    suffix = last_product.sku[len(prefix):]
                    if suffix.isdigit():
                        new_sequence = int(suffix) + 1
                except ValueError:
                    pass 
            
            self.sku = f"{prefix}{new_sequence:04d}"
            
        super().save(*args, **kwargs)

    @transaction.atomic
    def adjust_stock(self, quantity, movement_type, user=None, reference=None, notes=""):
        """
        Zentrale Methode um den Bestand zu ändern.
        Erstellt automatisch einen Eintrag im Audit Log (StockMovement).
        
        Args:
            quantity (int): Veränderung (+ für Eingang, - für Ausgang)
            movement_type (str): Art der Bewegung (siehe StockMovement.Type)
            user (User, optional): Wer hat es ausgelöst?
            reference (Model, optional): Das Objekt, das die Änderung ausgelöst hat (Sale, PO)
            notes (str, optional): Freitext Notiz
        """
        # Wenn Lagerführung deaktiviert ist, brechen wir hier ab (oder loggen nur)
        if not self.track_stock:
            return None
        
        # 1. Bestand aktualisieren
        self.stock_quantity += quantity
        self.save()
        
        # 2. Audit Eintrag erstellen
        movement = StockMovement(
            product=self,
            quantity=quantity,
            stock_after=self.stock_quantity, # Snapshot des neuen Bestands
            movement_type=movement_type,
            user=user,
            notes=notes
        )
        
        # Wenn eine Referenz (z.B. Sale oder PurchaseOrder) übergeben wurde, verknüpfen wir sie generisch
        if reference:
            movement.content_object = reference
            
        movement.save()
        return movement


class StockMovement(models.Model):
    class Type(models.TextChoices):
        INITIAL = 'INITIAL', _('Initialbestand')
        PURCHASE = 'PURCHASE', _('Wareneingang')
        SALE = 'SALE', _('Verkauf')
        CORRECTION = 'CORRECTION', _('Manuelle Korrektur')
        RETURN = 'RETURN', _('Retoure')
        DAMAGED = 'DAMAGED', _('Bruch/Verlust')

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='movements')
    
    # Die Veränderung (+10 oder -5)
    quantity = models.IntegerField(help_text="Veränderung des Bestands (positiv oder negativ)")
    
    # Der Bestand NACH dieser Bewegung (für schnelle Historien-Ansicht ohne Rechnen)
    stock_after = models.IntegerField(help_text="Bestand nach der Bewegung")
    
    movement_type = models.CharField(max_length=20, choices=Type.choices)
    
    # Audit Trail
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.CharField(max_length=255, blank=True)

    # Generic Relation: Kann auf Sale, PurchaseOrder oder jedes andere Modell zeigen
    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Lagerbewegung"
        verbose_name_plural = "Lagerbewegungen"

    def __str__(self):
        return f"{self.created_at.date()} | {self.product.name} | {self.quantity} ({self.get_movement_type_display()})"