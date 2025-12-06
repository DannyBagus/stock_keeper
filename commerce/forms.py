from django import forms
from django.forms.models import BaseInlineFormSet
from decimal import Decimal

# WICHTIG: Die Modelle müssen importiert werden, falls sie benötigt werden (hier nicht direkt, aber zur Sicherheit)
# from .models import SaleItem, Product 

class SaleItemFormSet(BaseInlineFormSet):
    """
    Stellt sicher, dass die VAT Rate für neue SaleItems gesetzt wird, 
    bevor Django versucht, sie in der Datenbank zu speichern (IntegrityError vermeiden).
    """
    def clean(self):
        super().clean()
        
        # Iteriert durch alle Formulare (Items) im Formularset
        for form in self.forms:
            # Nur für Formulare, die hinzugefügt oder geändert wurden
            if form.has_changed() or self.initial_forms:
                instance = form.instance
                product = instance.product
                
                # Prüfen, ob das vat_rate Feld Null ist, obwohl es nicht Null sein darf
                if not instance.vat_rate and product and product.vat:
                    # Setze den Wert, der im Model-Hook gesetzt werden sollte, 
                    # hier VOR der Datenbank-Validierung.
                    instance.vat_rate = product.vat.rate
                
                # Optional: Setzt den Preis, falls er im Formular nicht gesetzt wurde
                if not instance.unit_price_gross and product:
                    instance.unit_price_gross = product.sales_price