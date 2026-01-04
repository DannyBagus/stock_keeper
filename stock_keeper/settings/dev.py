from .base import *
import os
from dotenv import load_dotenv

load_dotenv() # Lädt Variablen aus einer .env Datei (optional für Dev, gut für Passwörter)

DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'stock_keeper'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '3306'),
    }
}

# Für HTMX/Alpine Entwicklung hilfreich: Console Email Backend
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Send Mail settings für PROD Testing
# EMAIL_HOST = 'mail.infomaniak.com'
# EMAIL_PORT = '465'
# EMAIL_HOST_USER = 'admin@mileja.ch'
# EMAIL_HOST_PASSWORD = ''  # Setze hier dein Passwort oder lade es aus einer Umgebungsvariable
# EMAIL_USE_SSL = True
# EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
# DEFAULT_FROM_EMAIL = 'admin@mileja.ch'


# Shopify API Key
SHOPIFY_WEBHOOK_SECRET = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')