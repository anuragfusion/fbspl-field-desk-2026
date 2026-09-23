"""Client workbook ingestion — a direct port of Phase 1's tested JavaScript (§10.5).

The rules here are NOT invented: header scoring, fuzzy column resolution, row
grouping by company, derived priority and health tone all mirror
FBSPL_Field_Desk_AppliedNet2026.html so that this tool and the Battlecards tool
read one workbook identically. Change the rules in both places or in neither.
"""

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Iterable

from openpyxl import load_workbook
from psycopg2.extras import execute_values

from .db import bump_version, new_id, now_iso

# --- primitives (ported 1:1) -------------------------------------------------

_UNKNOWN = re.compile(r"^(n/?a|na|none|nil|-|--|tbd|unknown)$", re.I)


def norm_hdr(s: Any) -> str:
    return re.sub(r"\s+", " ", str("" if s is None else s)).lower().strip()


def is_unknown(v: Any) -> bool:
    return not v or bool(_UNKNOWN.match(str(v).strip()))


def cell(row: list, idx: int | None):
    """JS `cell()`: dates pass through, everything else is trimmed text."""
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    v = row[idx]
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v
    return str(v).strip()


def lines(s: Any) -> list[str]:
    out = []
    for ln in str(s or "").replace("\r\n", "\n").split("\n"):
        ln = re.sub(r"^\s*[-•*]\s*", "", ln).strip()
        if ln:
            out.append(ln)
    return out


def find_col(hdrs: list[str], eq: Iterable[str] = (), starts: Iterable[str] = (),
             has: Iterable[str] = ()) -> int:
    """Exact match first, then prefix, then substring — same order as the JS."""
    for i, h in enumerate(hdrs):
        if h in eq:
            return i
    for i, h in enumerate(hdrs):
        if any(h.startswith(p) for p in starts):
            return i
    for i, h in enumerate(hdrs):
        if any(k in h for k in has):
            return i
    return -1


def score_headers(hdrs: list[str]) -> int:
    """Picks the right tab out of a multi-sheet workbook. Threshold is 3."""
    s = 0
    if find_col(hdrs, eq=["company"], has=["company"]) >= 0:
        s += 3
    if find_col(hdrs, has=["client name"]) >= 0:
        s += 3
    if find_col(hdrs, has=["account manager"]) >= 0:
        s += 2
    if find_col(hdrs, eq=["fte"], has=["fte"]) >= 0:
        s += 1
    if find_col(hdrs, has=["account health"]) >= 0:
        s += 1
    if find_col(hdrs, has=["key poc name"]) >= 0:
        s += 1
    return s


def health_tone(health: Any, sentiment: Any) -> str:
    h, s = str(health or "").lower(), str(sentiment or "").lower()
    if re.search(r"red|risk|at risk|critical|poor|escalat", h):
        return "crit"
    if re.search(r"negative|unhappy|dissatisf|vocal", s):
        return "crit"
    if re.search(r"amber|yellow|watch|moderate|average", h):
        return "warn"
    if re.search(r"green|good|healthy|strong|excellent", h):
        return "good"
    if re.search(r"positive|advocate|happy|satisf", s):
        return "good"
    return ""


def flag_of(txt: Any) -> str:
    t = str(txt or "").lower()
    if re.search(r"red|vocal|direct|escalat|churn|risk", t):
        return "crit"
    if re.search(r"green|advocate|champion|referenceable|promoter", t):
        return "good"
    return ""


_FALSY_ACTIVITY = {"", "0", "no", "n", "na", "n/a", "false", "-"}


def truthy_activity(v: Any) -> bool:
    """Mirrors the JS `truthy()` used to mark a P&C/EB service column as in scope."""
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v > 0
    return str(v).strip().lower() not in _FALSY_ACTIVITY


def activity_count(v: Any) -> int | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return int(n) if n > 0 else None


def find_activity_columns(raw_headers: list, hdrs: list[str]) -> list[dict]:
    """Any column headed "P&C - <service>" or "EB - <service>" is scope-of-service,
    not a mapped field — same convention the Battlecards tool uses (§ svcGroup)."""
    cols = []
    for i, h in enumerate(hdrs):
        hn = h.replace("–", "-").replace("—", "-")  # en/em dash -> hyphen
        if hn.startswith("p&c -"):
            line = "P&C"
        elif hn.startswith("eb -"):
            line = "EB"
        else:
            continue
        raw = str(raw_headers[i] if i < len(raw_headers) and raw_headers[i] is not None else "")
        raw = raw.replace("–", "-").replace("—", "-")
        label = raw.split("-", 1)[1].strip() if "-" in raw else raw.strip()
        cols.append({"idx": i, "line": line, "label": label or h})
    return cols


def _months_since(start: Any) -> int | None:
    if not start:
        return None
    d = start if isinstance(start, (datetime, date)) else None
    if d is None:
        try:
            d = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    days = (date.today() - d).days
    return int(days / 30.44) if days >= 0 else None


def tenure_from(start: Any) -> str:
    mo = _months_since(start)
    if mo is None:
        return ""
    if mo < 12:
        return f"{mo} mo with FBSPL"
    y, r = divmod(mo, 12)
    return f"{y} yr{'s' if y > 1 else ''}" + (f" {r} mo" if r else "") + " with FBSPL"


def is_new_client(start: Any) -> bool:
    mo = _months_since(start)
    return mo is not None and mo < 12


def money(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        n = float(re.sub(r"[^0-9.\-]", "", str(v)) or 0)
    except ValueError:
        return str(v)
    if not n:
        return str(v)
    if n >= 1e6:
        return f"${n / 1e6:.0f}M" if n >= 1e7 else f"${n / 1e6:.1f}M"
    if n >= 1e3:
        return f"${round(n / 1e3):.0f}k"
    return f"${n:.0f}"


def fmt_dateish(v: Any) -> str:
    if isinstance(v, (datetime, date)):
        return v.strftime("%b %-d, %Y")
    return str(v)


def build_cues(c: dict, start: Any, ai_level: Any, growth: Any) -> list[str]:
    """"How to play it at the booth" — situational cues derived the same way the
    Battlecards tool's `cues()` does, kept separate from the editorial
    talking_points/avoid_points so an admin's own notes are never overwritten."""
    sig = c["signals"]
    out: list[str] = []
    total_contacts = len(c["pocs"])
    advocates = c.get("_advocates", 0)
    vocal = c.get("_vocal", 0)

    if sig.get("newClient"):
        when = fmt_dateish(start) if start and not is_unknown(start) else ""
        out.append(
            f"Newly onboarded{f' (since {when})' if when else ''} — an early relationship; "
            "the booth is a chance to build trust face-to-face and set up expansion.")
    if sig.get("healthTone") == "crit":
        am = c.get("account_manager") or "the account manager"
        out.append(f"At-risk on last audit — handle carefully and loop in {am}.")
    if vocal > 0:
        out.append(
            f"{vocal} contact{'s are' if vocal > 1 else ' is'} vocal/direct — "
            "listen first, acknowledge feedback before pitching.")
    if advocates > 0 and vocal == 0 and advocates == total_contacts and total_contacts > 0:
        subject = ("The key contact is a strong advocate" if total_contacts == 1
                   else "Every contact is an advocate")
        out.append(f"{subject} — strong candidate for a testimonial, reference, or case study.")
    elif advocates > 0:
        out.append(
            f"{advocates} advocate{'s' if advocates > 1 else ''} on this account — "
            "natural reference/testimonial ask.")

    acts = sig.get("activities") or []
    on_count = sum(1 for a in acts if a["on"])
    if acts and on_count < len(acts):
        out.append(
            f"Only {on_count} of {len(acts)} services in scope — "
            "land-and-expand whitespace across the P&C / EB lifecycle.")
    if re.search(r"high|strong|very", str(ai_level or ""), re.I):
        out.append("AI interest is high — bring the automation / AI story.")
    if is_unknown(growth) and is_unknown(ai_level):
        out.append("Growth plans and AI interest aren't on file — good discovery questions for the booth.")
    return out[:4]


# --- column map (ported 1:1 from the JS `C = {...}`) -------------------------

COLUMN_SPEC: dict[str, dict[str, list[str]]] = {
    "company":   {"eq": ["company"], "has": ["company name", "company"]},
    "client":    {"has": ["client name"]},
    "priority":  {"eq": ["priority"], "has": ["priority"]},
    "status":    {"eq": ["status"]},
    "health":    {"has": ["account health"]},
    "remarks":   {"eq": ["remarks"], "has": ["remarks"]},
    "errors":    {"has": ["error"]},
    "start":     {"has": ["start date"]},
    "fte":       {"eq": ["fte"]},
    "billUsd":   {"has": ["billing (usd)", "billing(usd)", "(usd)"]},
    "city":      {"eq": ["city"]},
    "state":     {"eq": ["state"]},
    "country":   {"eq": ["country"]},
    "am":        {"has": ["account manager"]},
    "team":      {"has": ["team name"]},
    "um":        {"has": ["um name"]},
    "owner":     {"has": ["booth owner", "fbspl owner", "event owner"]},
    "ams":       {"has": ["product name", "ams"]},
    "inhouse":   {"has": ["in-house team count", "total in-house"]},
    "growth":    {"starts": ["growth plans"], "has": ["growth plans"]},
    "aiLevel":   {"starts": ["ai interest level"], "has": ["ai interest level"]},
    "aiDet":     {"has": ["opportunity details", "ai interest / opportunity"]},
    "feedback":  {"has": ["most recent client feedback", "client feedback"]},
    "fbDate":    {"has": ["feedback date"]},
    "sentiment": {"has": ["feedback sentiment", "sentiment"]},
    "book":      {"has": ["book of business"]},
    "plPct":     {"has": ["personal lines %"]},
    "clPct":     {"has": ["commercial lines %"]},
    "ebPct":     {"has": ["employee benefits (eb) %", "eb) %", "eb %"]},
    "revenue":   {"has": ["company revenue", "revenue"]},
    "pocName":   {"has": ["key poc name", "poc name"]},
    "pocTitle":  {"has": ["title/designation", "poc - title", "designation"]},
    "pocCmt":    {"has": ["comment and indicator", "poc note"]},
    "hobbies":   {"has": ["hobbies", "personal interest"]},
    "staff":     {"has": ["staff alias"]},
    "avoid":     {"has": ["do not", "avoid", "sensitive"]},
}


def resolve_columns(hdrs: list[str]) -> dict[str, int]:
    return {k: find_col(hdrs, **{kk: v for kk, v in spec.items()})
            for k, spec in COLUMN_SPEC.items()}


def _row_hash(row: list) -> str:
    payload = "|".join("" if v is None else
                       (v.isoformat() if isinstance(v, (datetime, date)) else str(v).strip())
                       for v in row)
    return hashlib.sha256(payload.encode()).hexdigest()


# --- ingestion ---------------------------------------------------------------

def ingest_rows(rows: list[list]) -> list[dict]:
    """rows[0] is the header row. Returns one dict per company, POCs merged."""
    if len(rows) < 2:
        return []
    hdrs = [norm_hdr(h) for h in rows[0]]
    C = resolve_columns(hdrs)
    name_col = C["company"] if C["company"] >= 0 else C["client"]
    if name_col < 0:
        raise ValueError('No "Company" or "Client Name" column in that sheet')
    act_cols = find_activity_columns(rows[0], hdrs)

    by_key: dict[str, dict] = {}
    order: list[str] = []

    for row in rows[1:]:
        nm = str(cell(row, name_col) or "").strip()
        if not nm or is_unknown(nm):
            continue
        key = nm.lower()

        if key not in by_key:
            health = cell(row, C["health"])
            sentiment = cell(row, C["sentiment"])
            start = cell(row, C["start"])
            fte = cell(row, C["fte"])
            ai_level = cell(row, C["aiLevel"])
            growth = cell(row, C["growth"])
            ai_det = cell(row, C["aiDet"])
            feedback = cell(row, C["feedback"])
            errs = cell(row, C["errors"])
            tone = health_tone(health, sentiment)
            ten = tenure_from(start)

            c: dict = {
                "name": nm,
                "account_manager": cell(row, C["am"]),
                "owner": cell(row, C["owner"]) or cell(row, C["um"]) or "",
                "product": cell(row, C["ams"]),
                "location": ", ".join(x for x in (cell(row, C["city"]), cell(row, C["state"]))
                                      if x and not is_unknown(x)),
                "talking_points": [],
                "avoid_points": [],
                "pocs": [],
                "tags": [],
                "source_row_hash": _row_hash(row),
                "_activities": {},
                "_advocates": 0,
                "_vocal": 0,
                "_start_raw": start,
                "_ai_level_raw": ai_level,
                "_growth_raw": growth,
            }

            # Where we stand
            summary: list[str] = []
            if not is_unknown(cell(row, C["remarks"])):
                summary.append(str(cell(row, C["remarks"])))
            if not is_unknown(fte):
                team = cell(row, C["team"])
                summary.append(f"{fte} FTE with FBSPL" + (f" ({team})" if team else "") + ".")
            if not is_unknown(health):
                tail = "" if is_unknown(sentiment) else f"; last feedback read as {sentiment}"
                summary.append(f"Account health: {health}{tail}.")
            if ten:
                summary.append(ten + (" — still inside the first year."
                                      if is_new_client(start) else "."))
            if not is_unknown(feedback):
                when = cell(row, C["fbDate"])
                stamp = f" ({fmt_dateish(when)})" if when else ""
                summary.append(f'Most recent feedback{stamp}: "{feedback}"')
            c["summary"] = " ".join(summary)

            # Key points to make
            if not is_unknown(growth):
                c["talking_points"] += lines(growth)
            if not is_unknown(ai_det):
                c["talking_points"] += lines(ai_det)
            if not is_unknown(ai_level) and re.search(r"high|strong|very", str(ai_level), re.I):
                c["talking_points"].append(
                    "Flagged high AI interest — bring a concrete automation example, not a deck.")
            if is_new_client(start):
                c["talking_points"].append(
                    "Inside the first year with FBSPL — ask how onboarding actually landed.")

            # Do not raise
            if C["avoid"] >= 0 and not is_unknown(cell(row, C["avoid"])):
                c["avoid_points"] += lines(cell(row, C["avoid"]))
            if not is_unknown(errs) and not re.match(r"^(0|none)$", str(errs), re.I):
                c["avoid_points"].append(
                    f"Open error/quality items on record ({errs}) — let them raise it first.")
            if tone == "crit":
                c["avoid_points"].append(
                    "Account is flagged at risk — do not pitch new scope before hearing them out.")

            c["signals"] = {
                "health": health, "healthTone": tone, "sentiment": sentiment,
                "flag": flag_of(cell(row, C["pocCmt"]) or health or sentiment),
                "fte": "" if is_unknown(fte) else fte,
                "tenure": ten, "newClient": is_new_client(start),
                "billing": "" if is_unknown(cell(row, C["billUsd"])) else money(cell(row, C["billUsd"])),
                "book": "" if is_unknown(cell(row, C["book"])) else money(cell(row, C["book"])),
                "revenue": "" if is_unknown(cell(row, C["revenue"])) else money(cell(row, C["revenue"])),
                "pl": cell(row, C["plPct"]), "cl": cell(row, C["clPct"]), "eb": cell(row, C["ebPct"]),
                "ai": "" if is_unknown(ai_level) else ai_level,
                "team": cell(row, C["team"]), "um": cell(row, C["um"]),
                "inhouse": "" if is_unknown(cell(row, C["inhouse"])) else cell(row, C["inhouse"]),
                "status": cell(row, C["status"]), "staff": cell(row, C["staff"]),
            }

            # Priority: an explicit column always wins, otherwise derive it.
            pv = str(cell(row, C["priority"]) or "").lower()
            if pv:
                c["priority"] = ("must" if re.search(r"must|^1$|high|p1", pv)
                                 else "watch" if re.search(r"watch|low|p3", pv) else "target")
            elif tone == "crit" or re.search(r"high|strong|very", str(ai_level), re.I):
                c["priority"] = "must"
            elif tone == "good" and is_unknown(ai_level):
                c["priority"] = "watch"
            else:
                c["priority"] = "target"

            if c["signals"]["ai"]:
                c["tags"].append(f"AI interest: {c['signals']['ai']}")
            if c["product"]:
                c["tags"].append(str(c["product"]))
            if c["signals"]["newClient"]:
                c["tags"].append("New client")

            by_key[key] = c
            order.append(key)

        # Scope of service: every row for the company can light up different
        # P&C/EB columns, so this aggregates across the whole group like POCs do.
        c = by_key[key]
        for ac in act_cols:
            v = cell(row, ac["idx"])
            entry_key = (ac["line"], ac["label"])
            entry = c["_activities"].get(entry_key)
            if entry is None:
                entry = {"line": ac["line"], "label": ac["label"], "on": False, "count": None}
                c["_activities"][entry_key] = entry
            if truthy_activity(v):
                entry["on"] = True
                n = activity_count(v)
                if n is not None:
                    entry["count"] = max(entry["count"] or 0, n)

        # POCs merge across every row belonging to this company.
        pn = str(cell(row, C["pocName"]) or "").strip()
        if pn and not is_unknown(pn) and not any(p["name"] == pn for p in c["pocs"]):
            pc = cell(row, C["pocCmt"])
            hob = cell(row, C["hobbies"])
            note = " · ".join(x for x in (pc,
                                          f"Outside work: {hob}" if hob and not is_unknown(hob) else "")
                              if x and not is_unknown(x))
            c["pocs"].append({"name": pn, "title": cell(row, C["pocTitle"]) or "", "note": note})
            poc_tone = flag_of(pc)
            if poc_tone == "good":
                c["_advocates"] += 1
            elif poc_tone == "crit":
                c["_vocal"] += 1

    for k in order:
        c = by_key[k]
        c["signals"]["activities"] = list(c.pop("_activities").values())
        c["signals"]["cues"] = build_cues(
            c, c["_start_raw"], c["_ai_level_raw"], c["_growth_raw"])
        del c["_start_raw"], c["_ai_level_raw"], c["_growth_raw"], c["_advocates"], c["_vocal"]

    return [by_key[k] for k in order]


def pick_sheet(path) -> tuple[str, list[list]]:
    """Scores every sheet against its first five candidate header rows (§10.5)."""
    wb = load_workbook(path, data_only=True, read_only=True)
    best, best_score, best_name = None, -1, ""
    for name in wb.sheetnames:
        rows = [list(r) for r in wb[name].iter_rows(values_only=True)]
        rows = [r for r in rows if any(v not in (None, "") for v in r)]
        if not rows:
            continue
        for top in range(min(5, len(rows))):
            sc = score_headers([norm_hdr(h) for h in rows[top]])
            if sc > best_score:
                best_score, best, best_name = sc, rows[top:], name
    wb.close()
    if best is None or best_score < 3:
        raise ValueError("No sheet with a Company / Client Name column")
    return best_name, best


def unmapped_columns(rows: list[list]) -> list[str]:
    hdrs = [norm_hdr(h) for h in rows[0]]
    used = {i for i in resolve_columns(hdrs).values() if i >= 0}
    used |= {ac["idx"] for ac in find_activity_columns(rows[0], hdrs)}
    return [rows[0][i] for i in range(len(hdrs)) if i not in used and hdrs[i]]


# --- preview / commit (§10.5 two-step) ---------------------------------------

def plan_import(conn, event_id: str, clients: list[dict]) -> dict:
    """Dry run. Returns the counts an admin confirms before anything is written."""
    existing = {r["name"].lower(): r for r in conn.execute(
        "SELECT id, name, source_row_hash FROM clients WHERE event_id = %s", (event_id,))}
    new = updated = unchanged = 0
    for c in clients:
        prev = existing.get(c["name"].lower())
        if prev is None:
            new += 1
        elif prev["source_row_hash"] == c["source_row_hash"]:
            unchanged += 1
        else:
            updated += 1
    return {"new": new, "updated": updated, "unchanged": unchanged, "total": len(clients)}


def apply_import(conn, event_id: str, clients: list[dict]) -> dict:
    """Commit. Idempotent on source_row_hash — re-importing the identical
    workbook produces zero updates (§12.4).

    Batched via execute_values: on a remote Postgres (Supabase), a round trip
    per row — the original shape of this function — took as long as
    (row count) x (network latency), long enough to hit gunicorn's worker
    timeout on a large workbook. This does the whole commit in three round
    trips total regardless of row count: one upsert for every changed client,
    one bulk delete of their old POCs, one bulk insert of the new ones."""
    version = bump_version(conn, event_id)
    existing = {r["name"].lower(): r for r in conn.execute(
        "SELECT id, name, source_row_hash FROM clients WHERE event_id = %s", (event_id,))}
    counts = {"new": 0, "updated": 0, "unchanged": 0}

    changed: list[tuple[dict, str]] = []   # (client, cid)
    for c in clients:
        prev = existing.get(c["name"].lower())
        if prev and prev["source_row_hash"] == c["source_row_hash"]:
            counts["unchanged"] += 1
            continue
        cid = prev["id"] if prev else new_id()
        counts["updated" if prev else "new"] += 1
        changed.append((c, cid))

    if changed:
        ts = now_iso()
        cur = conn.cursor()
        execute_values(
            cur,
            "INSERT INTO clients (id, event_id, name, priority, owner, account_manager,"
            " location, product, tags, summary, talking_points, avoid_points, signals,"
            " source_row_hash, version, created_at, updated_at) VALUES %s"
            " ON CONFLICT (id) DO UPDATE SET"
            " name=EXCLUDED.name, priority=EXCLUDED.priority, owner=EXCLUDED.owner,"
            " account_manager=EXCLUDED.account_manager, location=EXCLUDED.location,"
            " product=EXCLUDED.product, tags=EXCLUDED.tags, summary=EXCLUDED.summary,"
            " talking_points=EXCLUDED.talking_points, avoid_points=EXCLUDED.avoid_points,"
            " signals=EXCLUDED.signals, source_row_hash=EXCLUDED.source_row_hash,"
            " version=EXCLUDED.version, updated_at=EXCLUDED.updated_at",
            [(cid, event_id, c["name"], c["priority"], c["owner"], c["account_manager"],
              c["location"], c["product"], json.dumps(c["tags"]), c["summary"],
              json.dumps(c["talking_points"]), json.dumps(c["avoid_points"]),
              json.dumps(c["signals"], default=str), c["source_row_hash"], version, ts, ts)
             for c, cid in changed])

        cur.execute("DELETE FROM client_pocs WHERE client_id = ANY(%s)",
                    ([cid for _, cid in changed],))

        poc_rows = [(new_id(), cid, event_id, p["name"], p["title"], p["note"], i, version)
                    for c, cid in changed for i, p in enumerate(c["pocs"])]
        if poc_rows:
            execute_values(
                cur,
                "INSERT INTO client_pocs (id, client_id, event_id, name, title, note,"
                " sort_order, version) VALUES %s",
                poc_rows)

    counts["version"] = version
    counts["total"] = len(clients)
    return counts


def import_workbook(conn, event_id: str, path, commit: bool = False) -> dict:
    sheet, rows = pick_sheet(path)
    clients = ingest_rows(rows)
    result = apply_import(conn, event_id, clients) if commit \
        else plan_import(conn, event_id, clients)
    result["sheet"] = sheet
    result["unmapped_columns"] = unmapped_columns(rows)
    result["committed"] = commit
    return result
