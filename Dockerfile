# Basis-Image: Schlankes Python 3.11
FROM python:3.11-slim

# Umgebungsvariablen setzen
# PYTHONDONTWRITEBYTECODE: Verhindert .pyc Dateien
# PYTHONUNBUFFERED: Log-Ausgaben sofort anzeigen (wichtig für Docker Logs)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Arbeitsverzeichnis im Container setzen
WORKDIR /app

# System-Abhängigkeiten für mysqlclient und PDF-Libraries (pycairo, lxml) installieren
# build-essential und pkg-config werden zum Kompilieren benötigt
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       default-libmysqlclient-dev \
       build-essential \
       pkg-config \
       # NEUE ABHÄNGIGKEITEN FÜR PDF (pycairo und lxml/xhtml2pdf)
       libcairo2-dev \
       libxml2-dev \
       libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Abhängigkeiten installieren
# Wir kopieren erst nur die requirements, um Docker-Caching zu nutzen
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN pip install gunicorn

# Den Rest des Codes kopieren
COPY . /app/

# Entrypoint Skript ausführbar machen (erstellen wir gleich)
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Port freigeben
EXPOSE 8000

# Start-Befehl via Entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]