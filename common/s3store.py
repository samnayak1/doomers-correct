
from __future__ import annotations

import gzip
import io
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


def _key() -> str:
    return "/".join(p for p in (config.S3_PREFIX, "db/jobs.db.gz") if p).strip("/")


def backup_db(path: str | Path) -> str | None:
    """Gzip the SQLite file to S3 so a later invocation can pick up where this left off."""
    if not enabled() or not Path(path).exists():
        return None
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(Path(path).read_bytes())
    k = _key()
    client().put_object(Bucket=config.S3_BUCKET, Key=k, Body=buf.getvalue(),
                        ContentType="application/gzip", CacheControl="no-cache")
    return k


def restore_db(path: str | Path) -> bool:
    """Pull the backup down if there is no local database yet."""
    if not enabled() or Path(path).exists():
        return False
    try:
        obj = client().get_object(Bucket=config.S3_BUCKET, Key=_key())
    except Exception:
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(gzip.decompress(obj["Body"].read()))
    return True
