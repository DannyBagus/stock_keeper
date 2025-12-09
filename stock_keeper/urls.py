from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from core import views as core_views

urlpatterns = [
    path('', core_views.dashboard_view, name='home'),
    path('admin/', admin.site.urls),
    
    # Apps einbinden
    path('core/', include('core.urls')),
    path('commerce/', include('commerce.urls')), # NEU: Commerce URLs
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)