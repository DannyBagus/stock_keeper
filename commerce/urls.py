from django.urls import path
from . import views

urlpatterns = [
    # POS (Sales)
    path('pos/', views.pos_view, name='pos'),
    
    # NEU: Purchase UI
    path('purchase/', views.purchase_pos_view, name='purchase_pos'),

    # API Endpoints
    path('api/search/', views.api_search_product, name='api_product_search'),
    path('api/checkout/', views.api_checkout, name='api_pos_checkout'),
    
    # NEU: API für Purchase Checkout
    path('api/purchase-checkout/', views.api_purchase_checkout, name='api_purchase_checkout'),
    
    # PDF
    path('sale/<int:sale_id>/pdf/', views.sale_receipt_pdf_view, name='sale_pdf'),
]