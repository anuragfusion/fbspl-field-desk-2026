"""Admin CLI. The seeded admin is created here, not by a web signup page.

    python -m app.cli create-admin --username admin --name "Office admin"
    python -m app.cli create-event --name "Applied Net 2026" --starts 2026-09-28 --ends 2026-10-01
    python -m app.cli add-user --username priya --name "Priya Nair" --role field
    python -m app.cli add-member --event <id> --username priya
    python -m app.cli reset-password --username priya
    python -m app.cli list-users
"""

import argparse
import getpass
import secrets
import sys

from . import auth as A
from .db import audit, db, init_db, new_id, now_iso

WORDS = ["harbor", "meridian", "cascade", "summit", "anchor", "beacon", "quarry", "juniper"]


def suggest_password() -> str:
    return f"{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


def _ask_password(prompt="Password (blank to generate): ") -> tuple[str, bool]:
    p = getpass.getpass(prompt)
    if not p:
        p = suggest_password()
        return p, True
    if len(p) < 8:
        sys.exit("Password must be at least 8 characters.")
    if p != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    return p, False


def _create_user(conn, username, name, role, password, must_change):
    if conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
        sys.exit(f"Username already taken: {username}")
    uid = new_id()
    conn.execute(
        "INSERT INTO users (id, username, full_name, role, password_hash,"
        " must_change_password, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid, username, name or username, role, A.hash_password(password),
         1 if must_change else 0, now_iso(), now_iso()))
    audit(conn, None, "user.create", "user", uid, {"username": username, "role": role})
    return uid


def cmd_create_admin(args):
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled_at IS NULL").fetchone():
            sys.exit("An admin already exists. Use add-user or reset-password.")
        password, generated = _ask_password()
        _create_user(conn, args.username, args.name, "admin", password, must_change=False)
    print(f"Admin created: {args.username}")
    if generated:
        print(f"Password: {password}   <- store this now, it is not shown again")


def cmd_add_user(args):
    with db() as conn:
        password, generated = _ask_password()
        _create_user(conn, args.username, args.name, args.role, password, must_change=True)
    print(f"User created: {args.username} ({args.role}); must change password on first sign-in.")
    if generated:
        print(f"Temporary password: {password}   <- send over Teams, not in the briefing pack")


def cmd_reset_password(args):
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                           (args.username,)).fetchone()
        if not row:
            sys.exit(f"No such user: {args.username}")
        password, generated = _ask_password()
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0,"
            " last_failed_at = NULL, locked_until = NULL, updated_at = ? WHERE id = ?",
            (A.hash_password(password), now_iso(), row["id"]))
        n = A.revoke_all_sessions(conn, row["id"])   # §12.2
        audit(conn, None, "user.reset_password", "user", row["id"], {"sessions_revoked": n})
    print(f"Password reset for {args.username}; {n} session(s) revoked.")
    if generated:
        print(f"Temporary password: {password}")


def cmd_create_event(args):
    with db() as conn:
        eid = new_id()
        conn.execute(
            "INSERT INTO events (id, name, venue, city, country, starts_on, ends_on, timezone)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, args.name, args.venue, args.city, args.country,
             args.starts, args.ends, args.timezone))
        # Every admin is a member so the event shows up without extra setup.
        for u in conn.execute("SELECT id FROM users WHERE role='admin'").fetchall():
            conn.execute("INSERT INTO event_members (id, event_id, user_id, event_role, added_at)"
                         " VALUES (?,?,?,?,?)", (new_id(), eid, u["id"], "organiser", now_iso()))
    print(eid)


def cmd_add_member(args):
    with db() as conn:
        u = conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                         (args.username,)).fetchone()
        if not u:
            sys.exit(f"No such user: {args.username}")
        if not conn.execute("SELECT 1 FROM events WHERE id = ?", (args.event,)).fetchone():
            sys.exit(f"No such event: {args.event}")
        conn.execute(
            "INSERT OR IGNORE INTO event_members (id, event_id, user_id, event_role, added_at)"
            " VALUES (?,?,?,?,?)", (new_id(), args.event, u["id"], args.event_role, now_iso()))
    print(f"{args.username} added to event {args.event} as {args.event_role}")


def cmd_import_workbook(args):
    """Two-step by default: preview, then re-run with --commit.

    An import that silently rewrites 300 client briefs the night before an event
    is a bad afternoon, so the default here is the dry run.
    """
    from .importer import import_workbook
    with db() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id = ?", (args.event,)).fetchone():
            sys.exit(f"No such event: {args.event}")
        res = import_workbook(conn, args.event, args.file, commit=args.commit)
    print(f"sheet:      {res['sheet']}")
    print(f"clients:    {res['total']}")
    print(f"  new:      {res['new']}")
    print(f"  updated:  {res['updated']}")
    print(f"  unchanged:{res['unchanged']}")
    if res["unmapped_columns"]:
        print("unmapped columns (ignored): " + ", ".join(str(c) for c in res["unmapped_columns"]))
    print("COMMITTED — version " + str(res["version"]) if args.commit
          else "DRY RUN — nothing written. Re-run with --commit to apply.")


def cmd_export_phase1(args):
    """Rollback lever. Writes the pack Phase 1 imports, and prints the temporary
    credentials to THIS terminal — they are deliberately not in the file, because
    the file gets shared in a Teams channel."""
    import json as _json
    from .phase1 import build_pack
    with db() as conn:
        pack, creds = build_pack(conn, args.event, packed_by=args.by,
                                 include_files=not args.no_files)
    with open(args.out, "w") as fh:
        _json.dump(pack, fh)
    print(f"wrote {args.out}")
    print(f"  {len(pack['clients'])} clients · {len(pack['meetings'])} meetings · "
          f"{len(pack['updates'])} updates · {len(pack['docs'])} documents"
          f" ({len(pack['files'])} with file bytes)")
    if creds:
        print("\nTemporary passwords for the Phase 1 fallback — send over Teams, not with the pack:")
        for c in creds:
            print(f"  {c['username']:<16} {c['temporary_password']:<18} ({c['role']})")


def cmd_list_users(args):
    with db() as conn:
        for r in conn.execute("SELECT username, full_name, role, disabled_at, last_login_at"
                              " FROM users ORDER BY role, username").fetchall():
            flag = " [disabled]" if r["disabled_at"] else ""
            print(f"{r['username']:<20} {r['role']:<6} {r['full_name']}{flag}")


def main():
    init_db()
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("create-admin"); a.add_argument("--username", required=True)
    a.add_argument("--name", default=""); a.set_defaults(fn=cmd_create_admin)

    a = sub.add_parser("add-user"); a.add_argument("--username", required=True)
    a.add_argument("--name", default=""); a.add_argument("--role", default="field",
                                                         choices=["admin", "field"])
    a.set_defaults(fn=cmd_add_user)

    a = sub.add_parser("reset-password"); a.add_argument("--username", required=True)
    a.set_defaults(fn=cmd_reset_password)

    a = sub.add_parser("create-event"); a.add_argument("--name", required=True)
    a.add_argument("--venue", default=""); a.add_argument("--city", default="")
    a.add_argument("--country", default=""); a.add_argument("--starts", default="")
    a.add_argument("--ends", default=""); a.add_argument("--timezone", default="America/New_York")
    a.set_defaults(fn=cmd_create_event)

    a = sub.add_parser("add-member"); a.add_argument("--event", required=True)
    a.add_argument("--username", required=True)
    a.add_argument("--event-role", default="attendee", choices=["organiser", "attendee"],
                   dest="event_role")
    a.set_defaults(fn=cmd_add_member)

    a = sub.add_parser("import-workbook"); a.add_argument("--event", required=True)
    a.add_argument("--file", required=True)
    a.add_argument("--commit", action="store_true", help="apply it; omit for a dry run")
    a.set_defaults(fn=cmd_import_workbook)

    a = sub.add_parser("export-phase1"); a.add_argument("--event", required=True)
    a.add_argument("--out", default="FBSPL-briefing-pack.json")
    a.add_argument("--by", default="Office admin")
    a.add_argument("--no-files", action="store_true", help="text only; much smaller")
    a.set_defaults(fn=cmd_export_phase1)

    a = sub.add_parser("list-users"); a.set_defaults(fn=cmd_list_users)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
