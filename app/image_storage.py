"""Image-lead photo bytes, in their own private Supabase Storage bucket.

Kept apart from app/storage.py (documents) on purpose, so a change here can
never affect document upload or download.
"""

import os

import httpx

_SIGNED_URL_TTL_SECONDS = 60

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}

# ISO-BMFF major brands (bytes 8-12). AVIF and anything unlisted is rejected.
_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"}
_HEIF_BRANDS = {b"mif1", b"msf1", b"heif"}


class StorageNotConfigured(Exception):
    pass


class RejectedImage(Exception):
    pass


def sniff_image(data: bytes) -> str:
    """The real MIME type from the bytes themselves. Name and declared type are ignored."""
    if not data:
        raise RejectedImage("The file is empty.")
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
    raise RejectedImage("Only JPEG, PNG, WebP, HEIC and HEIF photos are accepted.")


def _config() -> tuple[str, str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_IMAGES_BUCKET", "images")
    if not url or not key:
        raise StorageNotConfigured(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set to store photos.")
    return url, key, bucket


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "apikey": key}


def ensure_bucket() -> None:
    url, key, bucket = _config()
    r = httpx.post(f"{url}/storage/v1/bucket", headers=_headers(key),
                   json={"id": bucket, "name": bucket, "public": False}, timeout=10)
    if r.status_code not in (200, 201) and "already exists" not in r.text:
        raise RuntimeError(f"Could not create/verify the images bucket: {r.text}")


def put(storage_key: str, data: bytes, mime: str) -> None:
    url, key, bucket = _config()
    r = httpx.post(f"{url}/storage/v1/object/{bucket}/{storage_key}",
                   headers={**_headers(key), "x-upsert": "true", "Content-Type": mime},
                   content=data, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Photo upload to storage failed: {r.status_code} {r.text}")


def signed_url(storage_key: str) -> str:
    url, key, bucket = _config()
    r = httpx.post(f"{url}/storage/v1/object/sign/{bucket}/{storage_key}",
                   headers=_headers(key), json={"expiresIn": _SIGNED_URL_TTL_SECONDS},
                   timeout=10)
    if r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower()):
        raise FileNotFoundError(storage_key)
    if r.status_code != 200:
        raise RuntimeError(f"Photo link signing failed: {r.status_code} {r.text}")
    return f"{url}/storage/v1{r.json()['signedURL']}"


def delete_many(storage_keys: list[str]) -> None:
    """Idempotent: keys that are already gone are not an error, so a retried
    delete after a partial failure succeeds."""
    if not storage_keys:
        return
    url, key, bucket = _config()
    r = httpx.request("DELETE", f"{url}/storage/v1/object/{bucket}",
                      headers=_headers(key), json={"prefixes": storage_keys}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Photo delete from storage failed: {r.status_code} {r.text}")
