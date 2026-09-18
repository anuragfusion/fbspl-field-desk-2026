"""Rollback bridge: emit Phase 1's native briefing-pack format.

Phase 1 (the single-file HTML tool) remains the lifeboat for Applied Net 2026.
Falling back to it is only a real option if the data can get there in one click,
so this produces exactly the `fbspl-an26-briefing-pack` v1 structure its own
importer already reads.

Note the direction of travel. §11 says never migrate Phase 1's hashes INTO
Phase 2 — they are not password hashes in any meaningful sense. Going the other
way we have the opposite problem: argon2 is one-way, so we cannot recover
anyone's password to re-hash it. Each exported account therefore gets a fresh
temporary password, returned in plaintext to the admin running the export so it
can be handed out over Teams.
"""

import base64
import json
import secrets

from .storage import open_path

WORDS = ["harbor", "meridian", "cascade", "summit", "anchor", "beacon", "quarry", "juniper"]


def _int32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def _b36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def phase1_hash(user: str, password: str) -> str:
    """Byte-for-byte port of Phase 1's hashPass(), including its JavaScript
    32-bit integer semantics. Verified against the original in
    tests/test_phase1_bridge.py by running the real JS under Node.

    This is NOT cryptography and Phase 1 says so itself — it exists only so the
    fallback tool accepts the accounts we hand it.
    """
    s = f"fbspl:an26:{user.lower()}:{password}"
    codes = [ord(ch) for ch in s]
    h1, h2 = 5381, 52711
    for r in range(1200):
        for c in codes:
            h1 = _int32(_int32(_int32(h1) << 5) + h1) ^ _int32(c)
            h2 = _int32(_int32(_int32(h2) << 5) + h2) ^ _int32(c + r)
        h1 &= 0xFFFFFFFF          # JS: h1 = h1 >>> 0
        h2 &= 0xFFFFFFFF
    return "h1:" + _b36(h1) + _b36(h2)


def temp_password() -> str:
    return f"{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


def _jsonl(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def build_pack(conn, event_id: str, packed_by: str = "", include_files: bool = True) -> dict:
    """Returns (pack, credentials). `credentials` is the plaintext list the admin
    must distribute — it is deliberately NOT inside the pack file, because the
    pack gets shared in a Teams channel."""
    ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        raise LookupError(event_id)

    credentials = []
    users = []
    for u in conn.execute(
        "SELECT u.* FROM users u JOIN event_members m ON m.user_id = u.id"
        " WHERE m.event_id = ? AND u.disabled_at IS NULL", (event_id,)
    ):
        pw = temp_password()
        credentials.append({"username": u["username"], "name": u["full_name"],
                            "role": u["role"], "temporary_password": pw})
        users.append({
            "id": u["id"], "user": u["username"], "name": u["full_name"], "role": u["role"],
            "hash": phase1_hash(u["username"], pw), "disabled": False, "seed": False,
            "added": u["created_at"], "lastIn": u["last_login_at"],
        })

    pocs_by_client: dict[str, list] = {}
    for p in conn.execute(
        "SELECT * FROM client_pocs WHERE event_id = ? ORDER BY sort_order", (event_id,)
    ):
        pocs_by_client.setdefault(p["client_id"], []).append(
            {"name": p["name"] or "", "title": p["title"] or "", "note": p["note"] or ""})

    clients = [{
        "id": c["id"], "name": c["name"], "priority": c["priority"] or "target",
        "owner": c["owner"] or "", "am": c["account_manager"] or "",
        "location": c["location"] or "", "product": c["product"] or "",
        "tags": _jsonl(c["tags"], []), "summary": c["summary"] or "",
        "say": _jsonl(c["talking_points"], []),        # Phase 1 calls them "say"
        "avoid": _jsonl(c["avoid_points"], []),
        "pocs": pocs_by_client.get(c["id"], []),
        "sig": _jsonl(c["signals"], {}),
    } for c in conn.execute("SELECT * FROM clients WHERE event_id = ?", (event_id,))]

    meetings = [{
        "id": m["id"], "date": m["meeting_date"] or "", "time": m["start_time"] or "",
        "dur": f"{m['duration_minutes']} min" if m["duration_minutes"] else "",
        "title": m["title"] or "", "clientId": m["client_id"], "location": m["location"] or "",
        "owner": m["owner"] or "", "attendees": m["attendees"] or "",
        "status": "planned" if m["status"] == "scheduled" else (m["status"] or "planned"),
        "notes": m["notes"] or "",
    } for m in conn.execute("SELECT * FROM meetings WHERE event_id = ?", (event_id,))]

    authors = {u["id"]: u["full_name"] for u in conn.execute("SELECT id, full_name FROM users")}
    updates = [{
        "id": u["id"], "title": u["title"], "body": u["body"] or "", "level": u["level"],
        "pinned": bool(u["pinned"]), "action": bool(u["is_action"]), "doneAt": None,
        "clientId": u["client_id"], "author": authors.get(u["author_id"], "Office"),
        "ts": u["created_at"],
    } for u in conn.execute("SELECT * FROM updates WHERE event_id = ?", (event_id,))]

    docs, files = [], []
    for d in conn.execute("SELECT * FROM documents WHERE event_id = ?", (event_id,)):
        docs.append({"id": d["id"], "name": d["filename"], "type": d["mime_type"],
                     "size": d["size_bytes"], "clientId": d["client_id"],
                     "note": d["note"] or "", "added": d["created_at"]})
        if include_files:
            try:
                raw = open_path(d["storage_key"]).read_bytes()
                files.append({"id": d["id"], "b64": base64.b64encode(raw).decode()})
            except FileNotFoundError:
                pass          # metadata still travels; the file is simply absent

    pack = {
        "kind": "fbspl-an26-briefing-pack", "v": 1,
        "exported": conn.execute("SELECT datetime('now')").fetchone()[0] + "Z",
        "packVersion": ev["version"], "packUpdated": None, "packBy": packed_by,
        "users": users, "clients": clients, "docs": docs,
        "meetings": meetings, "updates": updates, "files": files,
    }
    return pack, credentials
