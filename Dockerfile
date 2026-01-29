# WICHTIG: Wir pinnen auf "bookworm" (Debian 12) für Stabilität
FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV RUNNING_IN_DOCKER=true

# 1. System-Updates & Abhängigkeiten
# libgconf-2-4 wurde entfernt.
# libgbm1 und libasound2 hinzugefügt (wichtig für Headless Chrome!)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    make \
    gcc \
    libglib2.0-0 \
    libnss3 \
    libfontconfig1 \
    libxrender1 \
    libxtst6 \
    libxi6 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 2. Google Chrome Stable installieren
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable

# 3. Python Dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 4. Code kopieren & Ordner erstellen
COPY . .
RUN mkdir -p /app/downloads

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]