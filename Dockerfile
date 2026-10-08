# Sawazi API, staff console and member app in one small image. See docs/DEPLOY.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY sawazi ./sawazi

# Never run as root.
RUN useradd --system --uid 10001 --no-create-home sawazi
USER sawazi

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# Migrate once, then start the workers. Only the reverse proxy (Caddy) can reach this port, so the client
# address it forwards is trusted; the Daraja and USSD IP allow-lists depend on it.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn sawazi.api:app --host 0.0.0.0 --port 8000 --workers ${SAWAZI_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
