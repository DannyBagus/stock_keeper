from django.urls import path
from . import views

urlpatterns = [
    path('scanner/', views.scanner_view, name='scanner'),
]