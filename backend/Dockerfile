# The API and the scheduler tick. Build from the repository root:
#   docker build -f backend/Dockerfile -t prayog-api .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
COPY steward steward
COPY cli cli
RUN pip install --quiet --progress-bar off -r backend/requirements.txt -e ./steward

COPY backend backend
RUN useradd --system --uid 10001 --home-dir /nonexistent prayog \
 && mkdir -p /var/lib/prayog/artifacts && chown -R prayog /var/lib/prayog
USER prayog
ENV PRAYOG_ARTIFACT_ROOT=/var/lib/prayog/artifacts
WORKDIR /app/backend

HEALTHCHECK --interval=15s --timeout=5s --retries=4 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
