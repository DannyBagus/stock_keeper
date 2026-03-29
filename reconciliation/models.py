from django.db import models
from django.conf import settings
from commerce.models import Sale


class SumUpPayout(models.Model):
    """
    Repräsentiert eine SumUp-Auszahlung (Gutschrift auf Bankkonto).
    Die Periode wird automatisch aus dem SumUp Payouts-API ermittelt.
    """
    # Vom Buchhalter eingetragen
    bank_credit_amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        verbose_name="Gutschrift auf Bankkonto (CHF)"
    )
    bank_credit_date = models.DateField(verbose_name="Datum Bankgutschrift")

    # Aus SumUp API ermittelt
    sumup_payout_id = models.CharField(max_length=100, blank=True)
    period_start = models.DateField(null=True, blank=True, verbose_name="Abrechnungsperiode von")
    period_end = models.DateField(null=True, blank=True, verbose_name="Abrechnungsperiode bis")
    sumup_gross_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Bruttoumsatz (vor Gebühren)"
    )
    sumup_fees_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Gebühren"
    )
    sumup_net_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name="SumUp Nettobetrag (= Bankgutschrift)"
    )

    # Status
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Entwurf'
        IN_REVIEW = 'IN_REVIEW', 'In Prüfung'
        COMPLETED = 'COMPLETED', 'Abgeschlossen'

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-bank_credit_date']
        verbose_name = "SumUp Auszahlung"
        verbose_name_plural = "SumUp Auszahlungen"

    def __str__(self):
        return f"SumUp Auszahlung {self.bank_credit_date} – CHF {self.bank_credit_amount}"

    @property
    def period_label(self):
        if self.period_start and self.period_end:
            return f"{self.period_start.strftime('%d.%m.%Y')} – {self.period_end.strftime('%d.%m.%Y')}"
        return "Periode unbekannt"

    @property
    def delta(self):
        """Differenz zwischen Bankgutschrift und SumUp-Nettobetrag."""
        if self.sumup_net_amount:
            return self.bank_credit_amount - self.sumup_net_amount
        return None


class ReconciliationItem(models.Model):
    """
    Eine einzelne Transaktionszeile im Abgleich.
    Kann auf eine Sale in SK, eine SumUp-Txn, oder beides zeigen.
    """
    payout = models.ForeignKey(SumUpPayout, on_delete=models.CASCADE, related_name='items')

    # Stock Keeper Seite
    sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True)
    sk_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sk_timestamp = models.DateTimeField(null=True, blank=True)

    # SumUp Seite
    sumup_tx_id = models.CharField(max_length=100, blank=True)
    sumup_tx_code = models.CharField(max_length=100, blank=True)
    sumup_foreign_tx_id = models.CharField(
        max_length=128, blank=True,
        help_text="= Sale.id, falls beim Checkout übergeben"
    )
    sumup_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sumup_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sumup_timestamp = models.DateTimeField(null=True, blank=True)

    # Matching-Ergebnis
    class MatchTier(models.TextChoices):
        EXACT = 'EXACT', 'Exakt (foreign_tx_id)'
        AMOUNT_TIME = 'AMOUNT_TIME', 'Betrag + Zeit (±2min)'
        AMOUNT_DATE = 'AMOUNT_DATE', 'Betrag + Tag'
        NO_MATCH = 'NO_MATCH', 'Kein Match'

    class MatchStatus(models.TextChoices):
        MATCHED = 'MATCHED', 'Abgeglichen'
        ONLY_SUMUP = 'ONLY_SUMUP', 'Nur SumUp (fehlt in SK)'
        ONLY_SK = 'ONLY_SK', 'Nur SK (falscher Zahlungstyp?)'
        GAP = 'GAP', 'Betragsdifferenz > Toleranz'

    match_tier = models.CharField(max_length=20, choices=MatchTier.choices, blank=True)
    match_status = models.CharField(max_length=20, choices=MatchStatus.choices)
    gap_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gap_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Kategorie (für Buchungssatz-Split)
    class Channel(models.TextChoices):
        LADEN = 'LADEN', 'Laden (Supportelle)'
        CAFE = 'CAFE', 'Café'
        UNKNOWN = 'UNKNOWN', 'Unbekannt'

    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.UNKNOWN)

    # Auflösung
    class Resolution(models.TextChoices):
        PENDING = 'PENDING', 'Ausstehend'
        ACCEPTED = 'ACCEPTED', 'Akzeptiert'
        PAYMENT_TYPE_CHANGED = 'PAYMENT_TYPE_CHANGED', 'Zahlungsart korrigiert'
        SALE_DELETED = 'SALE_DELETED', 'Sale gelöscht/storniert'
        SALE_ADDED = 'SALE_ADDED', 'In SK nacherfasst'
        MANUAL = 'MANUAL', 'Manuell bearbeitet'
        IGNORED = 'IGNORED', 'Ignoriert'

    resolution = models.CharField(
        max_length=30, choices=Resolution.choices, default=Resolution.PENDING
    )
    resolution_note = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['sumup_timestamp', 'sk_timestamp']

    def __str__(self):
        return f"{self.match_status} | CHF {self.sumup_amount or self.sk_amount}"
