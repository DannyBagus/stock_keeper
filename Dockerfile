# Basis-Image: Schlankes Python 3.11
FROM python:3.11-slim

# Umgebungsvariablen setzen
# PYTHONDONTWRITEBYTECODE: Verhindert .pyc Dateien
# PYTHONUNBUFFERED: Log-Ausgaben sofort anzeigen (wichtig für Docker Logs)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Arbeitsverzeichnis im Container setzen
WORKDIR /app

# System-Abhängigkeiten installieren
# Wir kombinieren hier:
# 1. MySQL Client (default-libmysqlclient-dev)
# 2. Build Tools (build-essential, pkg-config)
# 3. WeasyPrint & PDF Abhängigkeiten (Pango, Cairo, Fonts)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-libmysqlclient-dev \
       build-essential \
       pkg-config \
       # PDF / WeasyPrint Libraries
       libcairo2-dev \
       libpango-1.0-0 \
       libpangoft2-1.0-0 \
       libgdk-pixbuf-2.0-0 \
       shared-mime-info \
       python3-cffi \
       python3-brotli \
       libxml2-dev \
       libxslt1-dev \
       # Schriftarten für PDF Generierung (WICHTIG für WeasyPrint)
       fonts-liberation \
       fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# Abhängigkeiten installieren
# Wir kopieren erst nur die requirements, um Docker-Caching zu nutzen
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN pip install gunicorn

# Den Rest des Codes kopieren
COPY . /app/

# Entrypoint Skript ausführbar machen
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Port freigeben
EXPOSE 8000

# Start-Befehl via Entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]