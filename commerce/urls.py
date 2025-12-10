from django.urls import path
from . import views

urlpatterns = [
    # POS
    path('pos/', views.pos_view, name='pos'),
    path('purchase/', views.purchase_pos_view, name='purchase_pos'),

    # Reports & Tools
    path('accounting-report/', views.accounting_report_view, name='accounting_report'),
    path('ean-labels/', views.ean_label_view, name='ean_labels'), # NEU

    # API Endpoints
    path('api/search/', views.api_search_product, name='api_product_search'),
    path('api/checkout/', views.api_checkout, name='api_pos_checkout'),
    path('api/purchase-checkout/', views.api_purchase_checkout, name='api_purchase_checkout'),
    
    # PDF & Webhooks
    path('sale/<int:sale_id>/pdf/', views.sale_receipt_pdf_view, name='sale_pdf'),
    path('webhooks/shopify/orders-paid/', views.shopify_webhook, name='shopify_webhook'),
]