from django.urls import path
from . import views

urlpatterns = [
    # Tools
    path('scanner/', views.scanner_view, name='scanner'),
    path('inventory/', views.inventory_view, name='inventory'),
    
    # Reports
    path('inventory-report/', views.inventory_report_view, name='inventory_report'), 

    # API
    path('api/inventory-correct/', views.api_inventory_correct, name='api_inventory_correct'),
]