FROM python:3.12-slim

ENV TZ=Europe/Moscow \
    APP_TIMEZONE=Europe/Moscow \
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

# The listener is the only headless entry point -- gui.py needs a display and isn't
# meant to run on a server. Config comes entirely from environment variables (Railway's
# dashboard, or a local .env for other hosts); TELEGRAM_SESSION_STRING avoids needing a
# persistent session file/volume (see generate_session_string.py).
CMD ["python", "listener.py"]
