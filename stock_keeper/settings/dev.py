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


# Shopify API Key
SHOPIFY_WEBHOOK_SECRET = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')