FROM python:3.12-slim

ENV TZ=Europe/Moscow \
    APP_TIMEZONE=Europe/Moscow \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Pillow ships no Cyrillic-capable font and python:*-slim ships no fonts at all, so the
# vote board (vote_image.py) would render every Russian name as boxes without this. -core
# is the ~1MB subset: the four DejaVu Sans faces, not the whole family.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Informational only -- Railway (and most PaaS hosts) route their generated domain to
# whatever port the app actually binds (see PORT in .env.example / vote_web.py), not to
# whatever's declared here. No-op if WEBAPP_PUBLIC_URL/PORT are never set.
EXPOSE 8080

# /poststats is its own product: the owner completes Telegram's normal login in the
# browser on their own deployment, with no shared Telegram session or configuration
# variables. A Railway Volume mounted at DATA_DIR is required to retain that login.
CMD ["python", "poststats_server.py"]
