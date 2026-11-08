# -slim, not -alpine: sqlite-vec ships glibc wheels only, and building its C
# extension against musl in this image is more moving parts than ~40 MB of disk.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Same uid as worker and db-replicate: they share the /data volume.
RUN useradd --system --uid 10001 --gid 0 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && SP="$(python -c 'import site;print(site.getsitepackages()[0])')" \
 && find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && python -c "import sqlite3, sqlite_vec; c = sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c); print('sqlite-vec', c.execute('select vec_version()').fetchone()[0])"

COPY common/ /app/common/
COPY api/__init__.py api/main.py /app/api/

ENV PYTHONPATH=/app DATA_DIR=/data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

# One worker: this is a read-only API on a 1 GB box, and a second process would
# double the resident footprint for no throughput we need.
USER 10001:0
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log", "--timeout-keep-alive", "20"]
