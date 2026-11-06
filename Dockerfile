FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    NOTICE_DIR=/notice \
    PORT=8742 \
    PUID=1000 \
    PGID=1000

# tzdata so the TZ variable works for auto-remove times
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /data /notice

EXPOSE 8742
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8742'), timeout=4)"

# Starts as root, fixes ownership of /data and the /notice folder to PUID:PGID,
# then drops to that user and runs the app (see app/entrypoint.py).
ENTRYPOINT ["python", "-m", "app.entrypoint"]
