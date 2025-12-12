from django import forms
from django.utils import timezone
from .models import Category

class InventoryReportForm(forms.Form):
    date = forms.DateField(
        label="Stichtag (Anzeige auf Beleg)",
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        initial=timezone.now().date(),
        help_text="Das Datum, das auf der Liste gedruckt wird (normalerweise der 31.12. oder heute)."
    )
    
    categories = forms.ModelMultipleChoiceField(
        label="Kategorien (Leer lassen für Alle)",
        queryset=Category.objects.all(),
        widget=forms.SelectMultiple(attrs={'class': 'form-control', 'style': 'height: 150px;'}),
        required=False
    )
    
    only_positive = forms.BooleanField(
        label="Nur Produkte mit Bestand > 0 anzeigen",
        initial=False,
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'custom-control-input'})
    )