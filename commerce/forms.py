from django import forms
from django.forms.models import BaseInlineFormSet, ModelForm
from decimal import Decimal
from django.core.exceptions import ObjectDoesNotExist
from .models import PurchaseOrder # Import hinzufügen
from core.widgets import DragAndDropFileWidget 

class SaleItemFormSet(BaseInlineFormSet):
    """
    Stellt sicher, dass die VAT Rate für neue SaleItems gesetzt wird.
    """
    def clean(self):
        super().clean()
        
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue

            # Wir nutzen cleaned_data, da instance.product bei neuen Objekten
            # noch nicht sicher verfügbar ist und einen RelatedObjectDoesNotExist Fehler wirft.
            product = form.cleaned_data.get('product')
            
            # Überspringen, wenn kein Produkt gewählt wurde oder das Formular gelöscht wird
            if not product or form.cleaned_data.get('DELETE'):
                continue

            instance = form.instance
            
            # Setze VAT Rate, falls leer (für IntegrityError Schutz)
            if not instance.vat_rate and product.vat:
                instance.vat_rate = product.vat.rate
            
            # Setze Preis, falls leer
            if instance.unit_price_gross is None:
                instance.unit_price_gross = product.sales_price
                
# NEU: Das Formular für PurchaseOrder
class PurchaseOrderForm(ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = '__all__'
        widgets = {
            'invoice_document': DragAndDropFileWidget(),
        }