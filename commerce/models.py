from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from core.models import Product, Supplier, StockMovement # Wichtig: StockMovement für Audit
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
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    invoice_document = models.FileField(upload_to='invoices_incoming/', blank=True, null=True)
    is_booked = models.BooleanField(default=False, help_text="In Buchhaltung übertragen?")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PO-{self.id} | {self.supplier}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        should_book_stock = False
        
        if self.pk: 
            try:
                old_obj = PurchaseOrder.objects.get(pk=self.pk)
                if old_obj.status != self.Status.RECEIVED and self.status == self.Status.RECEIVED:
                    should_book_stock = True
            except PurchaseOrder.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        if should_book_stock:
            self._process_stock_arrival()

    def _process_stock_arrival(self):
        """
        Interne Hilfsmethode: Bucht den Bestand via adjust_stock (Audit Log).
        Aktualisiert VORHER den Durchschnittspreis (Moving Average).
        """
        for item in self.items.all():
            product = item.product
            
            # 1. Durchschnittspreis aktualisieren (bevor der Bestand erhöht wird!)
            # Wir nutzen den Preis aus der Bestellung (unit_price)
            if item.unit_price and item.unit_price > 0:
                product.update_moving_average_price(item.quantity, item.unit_price)

            # 2. Bestand erhöhen & Audit Log schreiben
            product.adjust_stock(
                quantity=item.quantity,
                movement_type=StockMovement.Type.PURCHASE,
                user=self.created_by,
                reference=self, 
                notes=f"Wareneingang Bestellung #{self.id}"
            )

    @transaction.atomic
    def mark_as_received(self):
        if self.status != self.Status.RECEIVED:
            self.status = self.Status.RECEIVED
            self.save() 

class PurchaseOrderItem(models.Model):
    order = models.ForeignKey(PurchaseOrder, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="MwSt Satz (z.B. 8.10)")

    @property
    def total_price(self):
        price = self.unit_price or Decimal('0.00')
        return self.quantity * price
    
    def save(self, *args, **kwargs):
        if self.unit_price is None:
            self.unit_price = self.product.cost_price

        if self.vat_rate == Decimal('0.00') and self.product.vat:
            self.vat_rate = self.product.vat.rate

        super().save(*args, **kwargs)


# --- VERKAUF (Sale) ---

class Sale(models.Model):
    date = models.DateTimeField(default=timezone.now)
    total_amount_net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Verkaufsstatus
    class Status(models.TextChoices):
        COMPLETED = 'COMPLETED', 'Abgeschlossen'
        REFUNDED = 'REFUNDED', 'Storniert / Retourniert'

    # Zahlungsarten (Wie fließt das Geld?)
    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Barzahlung'
        SUMUP = 'SUMUP', 'SumUp (Karte)'
        SHOPIFY_PAYMENTS = 'SHOPIFY', 'Shopify Payments' # Für Online
        TWINT = 'TWINT', 'Twint'
        INVOICE = 'INVOICE', 'Rechnung'

    # Vertriebskanal (Wo wurde gekauft?)
    class SalesChannel(models.TextChoices):
        POS = 'POS', 'Ladenlokal (Kasse)'
        WEB = 'WEB', 'Online Shop (Shopify)'
        MANUAL = 'MANUAL', 'Manuell / Telefon'

    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    transaction_id = models.CharField(max_length=100, blank=True, null=True, help_text="Transaktions-ID von SumUp/Twint")
    channel = models.CharField(
        max_length=10, 
        choices=SalesChannel.choices, 
        default=SalesChannel.POS,
        help_text="Über welchen Kanal wurde dieser Verkauf getätigt?"
    )
    
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.COMPLETED)
    
    # Optional: User für Audit Log
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"Sale-{self.id} | {self.date.date()}"

    def calculate_totals(self):
        total_gross = sum(item.total_price_gross for item in self.items.all())
        self.total_amount_gross = total_gross
        self.save()

    @transaction.atomic
    def refund(self, user=None):
        """
        Storniert den Verkauf und bucht die Ware zurück ins Lager.
        """
        if self.status == self.Status.REFUNDED:
            return # Bereits storniert
        
        # 1. Status ändern
        self.status = self.Status.REFUNDED
        self.save()
        
        # 2. Ware zurückbuchen
        for item in self.items.all():
            # Wir nutzen adjust_stock mit positiver Quantity (Rückbuchung)
            # und dem Typ RETURN
            item.product.adjust_stock(
                quantity=item.quantity, # Positiv = Eingang
                movement_type=StockMovement.Type.RETURN,
                user=user or self.created_by,
                reference=self,
                notes=f"Storno Verkauf #{self.id}"
            )

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

        # Bestand reduzieren bei neuem Verkauf (mit Audit Log)
        if is_new:
            self.product.adjust_stock(
                quantity=-self.quantity, # Negativ für Abgang
                movement_type=StockMovement.Type.SALE,
                user=self.sale.created_by if hasattr(self.sale, 'created_by') else None,
                reference=self.sale, # Link zum Sale für das Audit-Log
                notes=f"Verkauf #{self.sale.id}"
            )