# The API only reads SQLite, so it deliberately has no numpy/pandas/jobspy.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && SP="$(python -c 'import site;print(site.getsitepackages()[0])')" \
 && find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} +

COPY common/ /app/common/
COPY api/__init__.py api/main.py /app/api/

ENV PYTHONPATH=/app DATA_DIR=/data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8000/api/health > /dev/null || exit 1

# One worker: this is a read-only API on a 1 GB box, and a second process would
# double the resident footprint for no throughput we need.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log", "--timeout-keep-alive", "20"]
