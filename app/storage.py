"""Document bytes on local disk, content-addressed.

Object storage in the spec (§11 assumption 3) is a folder here. At 100 documents
per event that is the right shape; swapping in Blob/S3 later means replacing
put_bytes/open_path and nothing else.
"""

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

from .db import STORAGE_DIR

MAX_BYTES = 50 * 1024 * 1024        # per-file cap (§9.6)

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
    """Content-addressed write. Returns (sha256, size). Identical files dedupe."""
    digest = hashlib.sha256(data).hexdigest()
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = STORAGE_DIR / digest
    if not path.exists():
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.rename(path)             # atomic; a reader never sees a half-written file
    return digest, len(data)


def open_path(storage_key: str) -> Path:
    path = STORAGE_DIR / storage_key
    if not path.is_file():
        raise FileNotFoundError(storage_key)
    return path
