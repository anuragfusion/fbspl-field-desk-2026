"""Document bytes in Supabase Storage, content-addressed.

Object storage in the spec (§11 assumption 3). Originally a local-disk folder;
now a private Supabase Storage bucket, since Render's free tier wipes local
disk on every restart/redeploy but Supabase Storage persists like the DB does.
put_bytes()/signed_url() are the only two functions that know about the HTTP
calls — everything else in the app just asks for a storage_key.
"""

import hashlib
import os
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

MAX_BYTES = 50 * 1024 * 1024        # per-file cap (§9.6)

_SIGNED_URL_TTL_SECONDS = 60         # long enough for a browser to start the download


class StorageNotConfigured(Exception):
    pass


def _config() -> tuple[str, str, str]:
    # Read lazily, not at import time: this module can be imported before
    # app.db's load_dotenv() has run, which would otherwise freeze these as
    # empty strings even with a correct .env.
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "documents")
    if not url or not key:
        raise StorageNotConfigured(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set to store documents.")
    return url, key, bucket


def _headers(key: str) -> dict:
    # The service_role key bypasses Storage RLS — required since the bucket is
    # private and this server, not the end user, is the one talking to Supabase.
    # It must never reach the browser; it only ever lives in server-side env vars.
    return {"Authorization": f"Bearer {key}", "apikey": key}


def ensure_bucket() -> None:
    """Idempotent: creates the bucket if it doesn't exist yet. Safe to call on
    every startup, same spirit as STORAGE_DIR.mkdir() before this migration."""
    url, key, bucket = _config()
    r = httpx.post(f"{url}/storage/v1/bucket", headers=_headers(key),
                   json={"id": bucket, "name": bucket, "public": False}, timeout=10)
    if r.status_code not in (200, 201) and "already exists" not in r.text:
        raise RuntimeError(f"Could not create/verify Supabase Storage bucket: {r.text}")


# §12.5: uploads are rejected on SNIFFED content, never on the declared type.
_MAGIC = [
    (b"%PDF-", "application/pdf", {".pdf"}),
    (b"\x89PNG\r\n\x1a\n", "image/png", {".png"}),
    (b"\xff\xd8\xff", "image/jpeg", {".jpg", ".jpeg"}),
]
_OOXML = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class RejectedUpload(Exception):
    pass


def sniff(data: bytes, filename: str) -> str:
    """Returns the real MIME type or raises. The client's content-type is ignored."""
    if not data:
        raise RejectedUpload("The file is empty.")
    if len(data) > MAX_BYTES:
        raise RejectedUpload(f"File exceeds the {MAX_BYTES // (1024 * 1024)} MB limit.")

    ext = Path(filename).suffix.lower()

    for magic, mime, exts in _MAGIC:
        if data.startswith(magic):
            if ext and ext not in exts:
                raise RejectedUpload(
                    f"Contents look like {mime} but the name ends in {ext}.")
            return mime

    if data.startswith(b"PK\x03\x04"):
        # Every OOXML file is a zip carrying [Content_Types].xml. Checking that is
        # real sniffing; trusting the extension alone is not.
        if ext not in _OOXML:
            raise RejectedUpload("Zip archives are not accepted.")
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                if "[Content_Types].xml" not in z.namelist():
                    raise RejectedUpload("Not a valid Office document.")
        except zipfile.BadZipFile:
            raise RejectedUpload("Not a valid Office document.")
        return _OOXML[ext]

    raise RejectedUpload("Only PDF, PNG, JPEG, DOCX, XLSX and PPTX files are accepted.")


def put_bytes(data: bytes) -> tuple[str, int]:
    """Content-addressed write. Returns (sha256, size). Identical files dedupe.

    Upload is idempotent (upsert): re-uploading the same digest is a cheap no-op
    on Supabase's side, so no existence check is needed before writing — unlike
    the old local-disk version, a network round trip either way, so skipping the
    check just avoids one for no benefit."""
    url, key, bucket = _config()
    digest = hashlib.sha256(data).hexdigest()
    r = httpx.post(f"{url}/storage/v1/object/{bucket}/{digest}",
                   headers={**_headers(key), "x-upsert": "true",
                            "Content-Type": "application/octet-stream"},
                   content=data, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase Storage upload failed: {r.status_code} {r.text}")
    return digest, len(data)


def get_bytes(storage_key: str) -> bytes:
    """A direct authenticated download — for the phase1 briefing-pack export,
    which embeds file bytes as base64 rather than linking to them. Raises
    FileNotFoundError if the object is missing."""
    url, key, bucket = _config()
    r = httpx.get(f"{url}/storage/v1/object/{bucket}/{storage_key}",
                  headers=_headers(key), timeout=30)
    if r.status_code == 404:
        raise FileNotFoundError(storage_key)
    if r.status_code != 200:
        raise RuntimeError(f"Supabase Storage download failed: {r.status_code} {r.text}")
    return r.content


def signed_url(storage_key: str) -> str:
    """A short-lived URL the browser downloads the file from directly — the
    server never proxies the bytes. Raises FileNotFoundError if the object is
    missing (e.g. it was deleted, or the bucket was ever cleared)."""
    url, key, bucket = _config()
    r = httpx.post(f"{url}/storage/v1/object/sign/{bucket}/{storage_key}",
                   headers=_headers(key), json={"expiresIn": _SIGNED_URL_TTL_SECONDS}, timeout=10)
    if r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower()):
        raise FileNotFoundError(storage_key)
    if r.status_code != 200:
        raise RuntimeError(f"Supabase Storage sign failed: {r.status_code} {r.text}")
    return f"{url}/storage/v1{r.json()['signedURL']}"
