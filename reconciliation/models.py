from decimal import Decimal

from django.db import models
from django.conf import settings
from commerce.models import Sale

# Toleranz für die Schlusssummen-Prüfung (Bankgutschrift vs. abgerechnetem Total)
BALANCE_TOLERANCE = Decimal('0.05')


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

    @property
    def booking_summary(self):
        """
        Zentrale Aggregation für Review-Screen und PDF-Buchungsbeleg.

        Erlöse werden mit dem *vollen* Transaktionsbetrag gebucht (sumup_amount).
        Der von SumUp tatsächlich ausbezahlte Betrag ergibt sich aus:

            Bankgutschrift = Σ Brutto − Σ eigene Gebühren − Σ Refund-Abzüge

        Refund-Abzüge (sumup_refund_deduction) entstehen, wenn SumUp bei einer
        Rückerstattung die nicht erstattete Bearbeitungsgebühr von der
        Auszahlungszeile einer (anderen) Transaktion abzieht. Sie sind faktisch
        eine zusätzliche Gebühr und werden separat ausgewiesen.

        net_delta = (Laden + Café + Unbekannt − Gebühren − Refund-Abzüge) − Bankgutschrift.
        Ein Wert ≠ 0 bedeutet: der Abgleich geht nicht auf (z. B. nicht erfasste
        SumUp-Transaktion).
        """
        items = list(self.items.all())

        def booked_amount(i):
            # Voller Transaktions-Bruttobetrag; Fallback auf SK-Betrag.
            if i.sumup_amount is not None:
                return i.sumup_amount
            return i.sk_amount or Decimal('0')

        booked = [i for i in items if i.match_status in ('MATCHED', 'GAP')]
        laden = sum((booked_amount(i) for i in booked if i.channel == 'LADEN'), Decimal('0'))
        cafe = sum((booked_amount(i) for i in booked if i.channel == 'CAFE'), Decimal('0'))
        unknown = sum((booked_amount(i) for i in booked if i.channel == 'UNKNOWN'), Decimal('0'))
        fees = sum((i.sumup_fee for i in items if i.sumup_fee), Decimal('0'))
        refund_deductions = sum(
            (i.sumup_refund_deduction for i in items if i.sumup_refund_deduction), Decimal('0')
        )

        computed_net = laden + cafe + unknown - fees - refund_deductions
        net_delta = None
        if self.bank_credit_amount is not None:
            net_delta = computed_net - self.bank_credit_amount

        # Zeilen mit Refund-Abzug (nicht erstattete Gebühr einer Rückerstattung)
        deduction_lines = [
            i for i in items if i.sumup_refund_deduction and i.sumup_refund_deduction > Decimal('0')
        ]

        matched = [i for i in items if i.match_status == 'MATCHED']
        gap = [i for i in items if i.match_status == 'GAP']
        only_sumup = [i for i in items if i.match_status == 'ONLY_SUMUP']
        only_sk = [i for i in items if i.match_status == 'ONLY_SK']

        # net_matches: das Nettototal deckt sich mit der Bankgutschrift.
        # is_balanced: zusätzlich keine offenen Positionen. Refund-Abzüge sind
        # erklärte Gebühren (eigene Kachel) und gelten NICHT als offener Punkt.
        net_matches = net_delta is not None and abs(net_delta) <= BALANCE_TOLERANCE
        has_open_items = bool(gap or only_sumup or only_sk)
        is_balanced = net_matches and not has_open_items

        return {
            'laden_total': laden,
            'cafe_total': cafe,
            'unknown_total': unknown,
            'total_fees': fees,
            'refund_deductions': refund_deductions,
            'deduction_lines': deduction_lines,
            'computed_net': computed_net,
            'net_delta': net_delta,
            'matched_count': len(matched),
            'gap_count': len(gap),
            'only_sumup_count': len(only_sumup),
            'only_sk_count': len(only_sk),
            'total_matched': sum((i.sumup_amount or Decimal('0') for i in matched), Decimal('0')),
            'net_matches': net_matches,
            'has_open_items': has_open_items,
            'is_balanced': is_balanced,
        }


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
    sumup_refund_deduction = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Refund-Abzug auf dieser Auszahlungszeile "
                  "(nicht erstattete Gebühr einer Rückerstattung)"
    )
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
