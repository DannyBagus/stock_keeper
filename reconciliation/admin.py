from django.contrib import admin
from .models import SumUpPayout, ReconciliationItem


class ReconciliationItemInline(admin.TabularInline):
    model = ReconciliationItem
    extra = 0
    readonly_fields = [
        'sale', 'sk_amount', 'sk_timestamp',
        'sumup_tx_id', 'sumup_tx_code', 'sumup_amount', 'sumup_fee', 'sumup_timestamp',
        'match_tier', 'match_status', 'gap_amount', 'channel',
    ]


@admin.register(SumUpPayout)
class SumUpPayoutAdmin(admin.ModelAdmin):
    list_display = ['bank_credit_date', 'bank_credit_amount', 'period_label', 'status']
    list_filter = ['status']
    inlines = [ReconciliationItemInline]


@admin.register(ReconciliationItem)
class ReconciliationItemAdmin(admin.ModelAdmin):
    list_display = ['payout', 'match_status', 'sk_amount', 'sumup_amount', 'channel', 'resolution']
    list_filter = ['match_status', 'channel', 'resolution']
