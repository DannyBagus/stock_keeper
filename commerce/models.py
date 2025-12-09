from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from core.models import Product, Supplier 
from decimal import Decimal 

# --- EINKAUF (Purchase) ---

class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Entwurf'
        ORDERED = 'ORDERED', 'Bestellt (beim Lieferanten)'
        RECEIVED = 'RECEIVED', 'Ware eingegangen'
        CANCELLED = 'CANCELLED', 'Storniert'

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='orders')
    date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    
    # Wer hat es bestellt?
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True) # Blank erlaubt
    
    # Rechnung vom Lieferanten
    invoice_document = models.FileField(upload_to='invoices_incoming/', blank=True, null=True)
    
    # Buchhaltung
    is_booked = models.BooleanField(default=False, help_text="In Buchhaltung übertragen?")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PO-{self.id} | {self.supplier}"

    @transaction.atomic
    def mark_as_received(self):
        """
        Bucht den Bestand aller Items auf die Produkte, wenn Status auf RECEIVED wechselt.
        """
        if self.status == self.Status.RECEIVED:
            return 
        
        self.status = self.Status.RECEIVED
        self.save()

        # Bestand buchen
        for item in self.items.all():
            product = item.product
            product.stock_quantity += item.quantity
            product.save()

class PurchaseOrderItem(models.Model):
    order = models.ForeignKey(PurchaseOrder, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    
    # KORREKTUR: unit_price darf leer sein (wird automatisch gefüllt)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # MwSt Satz zum Zeitpunkt der Bestellung (für Historie)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="MwSt Satz (z.B. 8.10)")

    @property
    def total_price(self):
        # Fallback falls unit_price noch None ist
        price = self.unit_price or Decimal('0.00')
        return self.quantity * price
    
    def save(self, *args, **kwargs):
        # 1. Preis automatisch vom Produkt holen (Einkaufspreis), falls leer
        if self.unit_price is None:
            self.unit_price = self.product.cost_price

        # 2. MwSt automatisch holen, falls 0.00
        # Hinweis: Beim PurchaseOrder geht es oft um den Vorsteuerabzug, 
        # hier nehmen wir vereinfacht den Satz vom Produkt an.
        if self.vat_rate == Decimal('0.00') and self.product.vat:
            self.vat_rate = self.product.vat.rate

        super().save(*args, **kwargs)


# --- VERKAUF (Sale) ---

class Sale(models.Model):
    date = models.DateTimeField(default=timezone.now)
    total_amount_net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    def __str__(self):
        return f"Sale-{self.id} | {self.date.date()}"

    def calculate_totals(self):
        total_gross = sum(item.total_price_gross for item in self.items.all())
        self.total_amount_gross = total_gross
        self.save()

class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    
    unit_price_gross = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), null=True, blank=True)

    @property
    def total_price_gross(self):
        qty = self.quantity or 0
        price = self.unit_price_gross or Decimal('0.00')
        return qty * price

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new and not self.unit_price_gross:
            self.unit_price_gross = self.product.sales_price
            self.vat_rate = self.product.vat.rate if self.product.vat else Decimal('0.00')
        
        super().save(*args, **kwargs)

        if is_new:
            self.product.stock_quantity -= self.quantity
            self.product.save()