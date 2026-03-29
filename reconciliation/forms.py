from django import forms


class PayoutStartForm(forms.Form):
    bank_credit_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        label="Gutschrift auf Bankkonto (CHF)",
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'placeholder': 'z.B. 1234.56',
        })
    )
    bank_credit_date = forms.DateField(
        label="Datum Bankgutschrift",
        widget=forms.DateInput(attrs={
            'class': 'form-control',
            'type': 'date',
        })
    )
