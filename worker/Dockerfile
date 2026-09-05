FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1


RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && SP="$(python -c 'import site;print(site.getsitepackages()[0])')" \
    # Drop the Windows/macOS tls-client binaries we can never use (~47 MB).
 && rm -f "$SP/tls_client/dependencies/"*.dll \
          "$SP/tls_client/dependencies/"*.dylib \
          "$SP/tls_client/dependencies/tls-client-arm64.so" \
 && find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find "$SP" -name 'tests' -type d -prune -exec rm -rf {} + \
    # Fail the build loudly here rather than at midnight in production.
 && python -c "import jobspy, tls_client, pandas, numpy, boto3; print('import check OK')"

COPY common/ /app/common/
COPY scripts/ /app/scripts/
COPY worker/scheduler.py /app/

ENV PYTHONPATH=/app DATA_DIR=/data
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD pgrep -f scheduler.py > /dev/null || exit 1

CMD ["python", "-u", "/app/scheduler.py"]
