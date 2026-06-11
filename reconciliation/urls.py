from django.urls import path
from . import views

app_name = 'reconciliation'

urlpatterns = [
    path('', views.reconciliation_list, name='list'),
    path('new/', views.reconciliation_start, name='start'),
    path('<int:pk>/review/', views.reconciliation_review, name='review'),
    path('<int:pk>/complete/', views.reconciliation_complete, name='complete'),
    path('<int:pk>/pdf/', views.reconciliation_pdf, name='pdf'),
    path('<int:pk>/item/<int:item_pk>/resolve/', views.resolve_item, name='resolve_item'),
    path('<int:pk>/item/<int:item_pk>/create-sale/', views.create_sale_for_item, name='create_sale_for_item'),
    path('<int:pk>/item/<int:item_pk>/align-sale/', views.align_sale_to_sumup, name='align_sale_to_sumup'),
]
