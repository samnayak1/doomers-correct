"""S3 persistence.

Everything the site serves is also published to S3 as plain gzipped JSON, so the
data is retrievable without the app - and so a replaced EC2 box can rebuild its
SQLite file from the bucket.  If S3_BUCKET is unset the whole module degrades to
no-ops and the app keeps working purely on local SQLite.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

from . import config

_client = None


def enabled() -> bool:
    return bool(config.S3_BUCKET)


def client():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("s3", region_name=config.AWS_REGION)
    return _client


def key(*parts: str) -> str:
    return "/".join([config.S3_PREFIX, *[p.strip("/") for p in parts]]).strip("/")


def public_url(k: str) -> str:
    return f"https://{config.S3_BUCKET}.s3.{config.AWS_REGION}.amazonaws.com/{k}"


def put_json(k: str, obj, *, gzip_body: bool = True, cache_seconds: int = 300) -> str | None:
    if not enabled():
        return None
    body = json.dumps(obj, separators=(",", ":"), default=str).encode()
    extra = {"ContentType": "application/json", "CacheControl": f"public, max-age={cache_seconds}"}
    if gzip_body:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(body)
        body = buf.getvalue()
        extra["ContentEncoding"] = "gzip"
    client().put_object(Bucket=config.S3_BUCKET, Key=k, Body=body, **extra)
    return k


def get_json(k: str):
    if not enabled():
        return None
    try:
        obj = client().get_object(Bucket=config.S3_BUCKET, Key=k)
    except Exception:
        return None
    raw = obj["Body"].read()
    if obj.get("ContentEncoding") == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def put_file(k: str, path: str | Path, content_type: str = "application/octet-stream") -> str | None:
    if not enabled():
        return None
    client().put_object(Bucket=config.S3_BUCKET, Key=k, Body=Path(path).read_bytes(), ContentType=content_type)
    return k


def backup_db(path: str | Path) -> str | None:
    """Gzip the SQLite file to S3 so a fresh instance can restore history."""
    if not enabled() or not Path(path).exists():
        return None
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(Path(path).read_bytes())
    k = key("db", "jobs.db.gz")
    client().put_object(Bucket=config.S3_BUCKET, Key=k, Body=buf.getvalue(),
                        ContentType="application/gzip", CacheControl="no-cache")
    return k


def restore_db(path: str | Path) -> bool:
    """Pull the SQLite backup down if we have no local database yet."""
    if not enabled() or Path(path).exists():
        return False
    try:
        obj = client().get_object(Bucket=config.S3_BUCKET, Key=key("db", "jobs.db.gz"))
    except Exception:
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(gzip.decompress(obj["Body"].read()))
    return True


def write_manifest(countries: dict) -> str | None:
    """A small index so anyone can discover the data files without listing the bucket."""
    manifest = {
        "name": "Are doomers correct?",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "bucket": config.S3_BUCKET,
        "countries": countries,
        "license": "Data is provided as-is for public use.",
    }
    k = key("manifest.json")
    put_json(k, manifest, gzip_body=False, cache_seconds=60)
    return k
