#!/bin/sh

# Stoppt das Skript bei Fehlern
set -e

echo "Sammle statische Dateien..."
python manage.py collectstatic --noinput --clear

echo "Führe Datenbank-Migrationen aus..."
python manage.py migrate

echo "Starte Gunicorn Server..."
# Wir binden an 0.0.0.0:8000 damit es von außen erreichbar ist
exec gunicorn stock_keeper.wsgi:application --bind 0.0.0.0:8000 --workers 3