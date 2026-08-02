FROM python:3.12-slim

ENV TZ=Europe/Moscow \
    APP_TIMEZONE=Europe/Moscow \
    PYTHONUNBUFFERED=1

WORKDIR /app

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
