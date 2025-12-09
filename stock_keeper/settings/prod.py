from .base import *
import os

# --- Production specific settings ---

DEBUG = False
# Lesen der ALLOWED_HOSTS und SECRET_KEY aus der Umgebungsvariable (aus .env.prod)
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '').split(',')
SECRET_KEY = os.environ.get('SECRET_KEY', 'default-prod-key-change-me')


# --- Whitenoise Konfiguration ---
# Whitenoise dient dazu, statische Dateien (CSS/JS) im Produktionsmodus 
# über Gunicorn auszuliefern, da Django selbst das bei DEBUG=False nicht macht.

MIDDLEWARE = [
    # Whitenoise muss direkt nach SecurityMiddleware kommen!
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # NEU: Whitenoise Middleware
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Wichtig: Konfiguriert Whitenoise, um komprimierte und gemanifestete Dateien zu nutzen
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'


# --- NEU: ZUSÄTZLICHE PRODUKTIONS-SICHERHEIT ---
# Diese Einstellungen sind entscheidend für den Betrieb hinter einem Proxy (wie Docker)
# und für die korrekte Cookie-Behandlung in Prod.

# 1. SSL Header: Teilt Django mit, dass es hinter einem HTTPS-Proxy läuft.
#    Auch wenn wir lokal kein echtes HTTPS nutzen, kann dies Worker-Timeouts beheben.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# 2. Cookie Security: Setzt Session- und CSRF-Cookies nur über HTTPS (Standard für Prod)
SESSION_COOKIE_SECURE = False # VORÜBERGEHEND False lassen, da wir lokal HTTP nutzen!
CSRF_COOKIE_SECURE = False    # VORÜBERGEHEND False lassen, da wir lokal HTTP nutzen!

# HINWEIS: Sobald Sie die App mit einem echten HTTPS-Zertifikat betreiben, 
# MÜSSEN diese beiden Einstellungen auf True gesetzt werden!

# 3. HSTS (wichtig für die Sicherheit in Prod, aber für das lokale Setup unkritisch)
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True


# Database settings
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'stock_keeper'),
        'USER': os.getenv('DB_USER', 'stock_keeper'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'change_me'),
        'HOST': os.getenv('DB_HOST', 'db'),
        'PORT': os.getenv('DB_PORT', '3306'),
    }
}

# Shopify API Key
SHOPIFY_WEBHOOK_SECRET = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')