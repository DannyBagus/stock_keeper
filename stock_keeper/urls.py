from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('core/', include('core.urls')),
    path('', admin.site.urls),
]

# Im Development-Modus (DEBUG=True) liefert Django User-Uploads direkt aus.
# In Produktion übernimmt das später Nginx/Docker.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)