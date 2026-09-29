FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RECOVERY_DB=/data/recovery.db

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# SQLite schema is created idempotently on startup (see db.init_db).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
