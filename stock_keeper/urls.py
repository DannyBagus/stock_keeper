from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve # WICHTIG: Importieren
from core import views as core_views

urlpatterns = [
    path('', core_views.dashboard_view, name='home'),
    path('admin/', admin.site.urls),
    
    # Apps einbinden
    path('core/', include('core.urls')),
    path('commerce/', include('commerce.urls')), # NEU: Commerce URLs
]

# --- MEDIEN DATEIEN IN PROD ---
# Damit Docker ohne Nginx auch Uploads ausliefert:
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {
        'document_root': settings.MEDIA_ROOT,
    }),
]

# Statische Dateien (CSS/JS) werden bereits von Whitenoise behandelt,
# aber lokal brauchen wir das hier weiterhin:
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)