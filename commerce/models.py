from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from core.models import Product, Supplier # Stellen Sie sicher, dass core.models importiert ist
from decimal import Decimal # Decimal muss importiert werden

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
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    
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
            return # Schon erledigt
        
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
    # Wir speichern den Einkaufspreis zum Zeitpunkt der Bestellung
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    
    # NEU: MwSt Satz zum Zeitpunkt der Bestellung
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="MwSt Satz zum Zeitpunkt des Kaufs (z.B. 8.10)")


    @property
    def total_price(self):
        return self.quantity * self.unit_price
    
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        # Wenn neu, dann VAT Rate vom Produkt holen
        if is_new and self.vat_rate == Decimal('0.00'):
            # Stellt sicher, dass wir den Satz vom Vat Modell ziehen
            self.vat_rate = self.product.vat.rate if self.product.vat else Decimal('0.00')

        super().save(*args, **kwargs)


# --- VERKAUF (Sale) ---

class Sale(models.Model):
    date = models.DateTimeField(default=timezone.now)
    # Optional: Kunde, falls bekannt
    # customer = ... 
    
    # Summen cachen wir hier, damit wir nicht immer rechnen müssen
    total_amount_net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    def __str__(self):
        return f"Sale-{self.id} | {self.date.date()}"

    def calculate_totals(self):
        # Einfache Hilfsmethode um Summen zu aktualisieren
        total_gross = sum(item.total_price_gross for item in self.items.all())
        # Netto Logik müsste man basierend auf den Steuersätzen der Items berechnen
        # Vereinfacht hier:
        self.total_amount_gross = total_gross
        self.save()

class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    
    # Snapshot der Preise beim Verkauf
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), null=True, blank=True)
    unit_price_gross = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True) # Auch den Preis absichern

    @property
    def total_price_gross(self):
        return self.quantity * self.unit_price_gross

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        # Wenn neu, dann Preis und VAT vom Produkt holen
        if is_new and not self.unit_price_gross:
            self.unit_price_gross = self.product.sales_price
            self.vat_rate = self.product.vat.rate if self.product.vat else Decimal('0.00')
        
        super().save(*args, **kwargs)

        # Bestand reduzieren (einfache Logik)
        if is_new:
            self.product.stock_quantity -= self.quantity
            self.product.save()