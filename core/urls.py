from django.urls import path
from . import views

urlpatterns = [
    # Bestehender Scanner (kann evtl. entfernt werden, wenn Inventory besser ist)
    path('scanner/', views.scanner_view, name='scanner'),
    
    # NEU: Inventur UI
    path('inventory/', views.inventory_view, name='inventory'),
    
    # NEU: API für Korrektur
    path('api/inventory-correct/', views.api_inventory_correct, name='api_inventory_correct'),
]