# NOT Alpine, deliberately - this is the one container that cannot be.
#
# JobSpy depends on tls-client, which is a Go library shipped as a prebuilt
# c-shared object and loaded with ctypes at import time. Go's c-shared builds use
# the initial-exec TLS model, and musl's loader cannot relocate initial-exec TLS
# in a library opened with dlopen after start-up. Every route was tried and all
# of them fail on Alpine:
#
#   * the wheel's musl build (tls-client-amd64.so)
#       -> OSError: free: initial-exec TLS resolves to dynamic definition
#   * the wheel's glibc build plus gcompat
#       -> OSError: seteuid: initial-exec TLS resolves to dynamic definition
#   * LD_PRELOAD, so it loads at start-up rather than via dlopen
#       -> segfault
#
# The remaining option would be stubbing tls_client out and forcing JobSpy onto
# plain requests, which loses the TLS fingerprinting that Indeed and Glassdoor
# check - trading a working scraper for a smaller base image. Not worth it.
#
# api and web are still Alpine. Only this one is Debian, and the memory cap in
# docker-compose.yml is what actually protects the 1 GB box - the base image is
# a disk-size question, not a memory one (measured RSS difference is a few MB).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: the schedule is local-time, and slim ships no zoneinfo database.
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
