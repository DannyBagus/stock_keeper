"""Hermetic settings for the test suite / offline makemigrations (SQLite)."""
from .base import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = ['testserver', 'localhost']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

SHOPIFY_WEBHOOK_SECRET = ''
WEBSHOP_API_TOKEN = 'test-webshop-token'
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
