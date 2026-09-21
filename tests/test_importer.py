"""Workbook ingestion — the rules ported from Phase 1 (§10.5) and §12.4's criteria.

Fixture columns are copied verbatim from Phase 1's own CSV template, so a
divergence here is a real port bug rather than a fixture artefact.
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fielddesk-imp-")
os.environ["FIELDDESK_DB"] = str(Path(_TMP) / "imp.db")
os.environ["FIELDDESK_STORAGE"] = str(Path(_TMP) / "storage")

from openpyxl import Workbook                                          # noqa: E402

from app.db import db, init_db, new_id                                 # noqa: E402
from app.importer import (build_cues, find_activity_columns,           # noqa: E402
                          health_tone, import_workbook, ingest_rows,
                          money, norm_hdr, pick_sheet, score_headers,
                          tenure_from, truthy_activity)

HEADERS = [
    "Company", "Priority", "Booth Owner", "Account Manager", "UM Name", "Team Name",
    "City", "State", "Product Name", "FTE", "Start Date", "Account Health",
    "Most Recent Client Feedback", "Feedback Date", "Feedback Sentiment", "Errors",
    "Remarks", "Growth Plans", "AI Interest Level", "AI Interest / Opportunity Details",
    "Book of Business", "Company Revenue", "Personal Lines %", "Commercial Lines %",
    "Employee Benefits (EB) %", "Do Not Raise", "Key POC Name", "Title/Designation",
    "Comment and Indicator", "Hobbies",
]

MERIDIAN = [
    "Meridian Insurance Group", "Must", "Priya Nair", "R. Shah", "S. Iyer", "Team Atlas",
    "Columbus", "OH", "Applied Epic", "42", date(2021, 4, 12), "Green",
    "Turnaround on endorsements has been excellent", date(2026, 8, 14), "Positive", "0",
    "Renewal signed Aug 2026, three-year term.",
    "Opening a commercial lines desk in Q1\nWants to double policy-checking volume",
    "High", "Loss-run extraction and renewal packet assembly",
    "18000000", "24000000", "55", "40", "5",
    "The March invoice dispute — still with finance",
    "Dana Whitfield", "Operations Director", "Decision maker, wants detail not slides",
    "Cycling, jazz",
]
# Second row for the same company — only the POC differs. Must merge, not duplicate.
MERIDIAN_POC2 = MERIDIAN[:26] + ["Marc Ellis", "Producer", "Warm, referred two agencies", ""]

CASCADE = [
    "Cascade Risk Advisors", "", "", "S. Mehta", "", "", "Seattle", "WA", "Applied Epic",
    "8", date(2026, 6, 1), "Red — at risk", "Escalated twice this quarter", date(2026, 9, 1),
    "Vocal", "4", "", "", "", "", "", "", "", "", "", "", "Rosa Alvarez", "Principal", "", "",
]
# No priority column value, good health, no AI interest -> derived "watch"
HARBORLINE = [
    "Harborline Agency Partners", "", "", "S. Mehta", "", "", "Tampa", "FL", "EZLynx",
    "6", date(2019, 1, 10), "Green", "", "", "", "0", "", "", "", "", "", "", "", "", "",
    "", "", "", "", "",
]


def build_workbook(path, rows, sheet_name="Client Master", noise_sheet=True):
    wb = Workbook()
    if noise_sheet:
        # A decoy tab that must lose the header-scoring contest.
        first = wb.active
        first.title = "Instructions"
        first.append(["This tab explains how to fill the workbook"])
        first.append(["Step 1", "Step 2"])
        ws = wb.create_sheet(sheet_name)
    else:
        ws = wb.active
        ws.title = sheet_name
    ws.append(["FBSPL client master — internal"])   # a banner row above the header
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


class PrimitiveTests(unittest.TestCase):
    def test_health_tone_precedence(self):
        self.assertEqual(health_tone("Red — at risk", "Positive"), "crit")
        self.assertEqual(health_tone("Green", "Vocal"), "crit", "negative sentiment overrides")
        self.assertEqual(health_tone("Amber", ""), "warn")
        self.assertEqual(health_tone("Green", "Positive"), "good")
        self.assertEqual(health_tone("", ""), "")

    def test_money_matches_phase1_rounding(self):
        self.assertEqual(money("18000000"), "$18M")     # >=1e7 -> no decimal
        self.assertEqual(money("1500000"), "$1.5M")     # <1e7  -> one decimal
        self.assertEqual(money("18000"), "$18k")
        self.assertEqual(money("250"), "$250")
        self.assertEqual(money(""), "")

    def test_tenure_wording(self):
        self.assertIn("with FBSPL", tenure_from(date(2021, 4, 12)))
        self.assertEqual(tenure_from(""), "")

    def test_header_scoring_beats_a_decoy_sheet(self):
        self.assertGreaterEqual(score_headers([h.lower() for h in HEADERS]), 3)
        self.assertLess(score_headers(["step 1", "step 2"]), 3)


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.path = Path(_TMP) / "book.xlsx"
        build_workbook(cls.path, [MERIDIAN, MERIDIAN_POC2, CASCADE, HARBORLINE])
        sheet, rows = pick_sheet(cls.path)
        cls.sheet = sheet
        cls.clients = ingest_rows(rows)
        cls.by_name = {c["name"]: c for c in cls.clients}

    def test_picks_the_data_sheet_and_skips_the_banner_row(self):
        self.assertEqual(self.sheet, "Client Master")
        self.assertEqual(len(self.clients), 3, "two Meridian rows must group into one client")

    def test_explicit_priority_column_wins(self):
        self.assertEqual(self.by_name["Meridian Insurance Group"]["priority"], "must")

    def test_priority_derived_from_at_risk_health(self):
        self.assertEqual(self.by_name["Cascade Risk Advisors"]["priority"], "must")
        self.assertEqual(self.by_name["Cascade Risk Advisors"]["signals"]["healthTone"], "crit")

    def test_priority_derived_as_watch_when_healthy_and_no_ai_interest(self):
        self.assertEqual(self.by_name["Harborline Agency Partners"]["priority"], "watch")

    def test_pocs_merge_across_rows_of_the_same_company(self):
        pocs = self.by_name["Meridian Insurance Group"]["pocs"]
        self.assertEqual([p["name"] for p in pocs], ["Dana Whitfield", "Marc Ellis"])
        self.assertIn("Outside work: Cycling, jazz", pocs[0]["note"])

    def test_talking_points_combine_growth_ai_detail_and_the_ai_nudge(self):
        tp = self.by_name["Meridian Insurance Group"]["talking_points"]
        self.assertIn("Opening a commercial lines desk in Q1", tp)
        self.assertIn("Wants to double policy-checking volume", tp)
        self.assertIn("Loss-run extraction and renewal packet assembly", tp)
        self.assertTrue(any("high AI interest" in t for t in tp))

    def test_avoid_points_carry_the_sheet_value_and_the_derived_risk_line(self):
        meridian = self.by_name["Meridian Insurance Group"]["avoid_points"]
        self.assertIn("The March invoice dispute — still with finance", meridian)
        self.assertFalse(any("Open error" in a for a in meridian), "0 errors must not warn")

        cascade = self.by_name["Cascade Risk Advisors"]["avoid_points"]
        self.assertTrue(any("flagged at risk" in a for a in cascade))
        self.assertTrue(any("Open error/quality items" in a for a in cascade), "4 errors must warn")

    def test_summary_is_assembled_from_the_sheet(self):
        s = self.by_name["Meridian Insurance Group"]["summary"]
        self.assertIn("Renewal signed Aug 2026", s)
        self.assertIn("42 FTE with FBSPL (Team Atlas)", s)
        self.assertIn("Account health: Green", s)

    def test_signals_carry_the_money_columns(self):
        sig = self.by_name["Meridian Insurance Group"]["signals"]
        self.assertEqual(sig["book"], "$18M")
        self.assertEqual(sig["revenue"], "$24M")
        self.assertEqual(sig["ai"], "High")
        self.assertEqual((sig["pl"], sig["cl"], sig["eb"]), ("55", "40", "5"))


ACTIVITY_HEADERS = HEADERS + [
    "P&C - Policy Checking", "P&C – Renewals", "EB - Enrollment",
]
# Meridian: row 1 lights up policy checking only; row 2 (same company, second POC)
# also lights up renewals with a count. EB - Enrollment stays off both rows ->
# whitespace. Escalated contact -> "vocal", second contact -> "advocate".
MERIDIAN_ACT1 = MERIDIAN + ["Yes", "", ""]
MERIDIAN_ACT2 = MERIDIAN_POC2 + ["Yes", "3", ""]
# Cascade: single row, single advocate contact, everything off -> full whitespace cue.
CASCADE_ACT = CASCADE[:26] + [
    "Alvaro Reyes", "Owner", "Champion, referenceable", "Sailing",
] + ["", "", ""]


class ActivitiesAndCuesTests(unittest.TestCase):
    """Scope-of-service parsing and the "how to play it at the booth" cues,
    the two panels the Battlecards tool renders that this importer must feed."""

    @classmethod
    def setUpClass(cls):
        init_db()
        cls.path = Path(_TMP) / "activities.xlsx"
        build_workbook(cls.path, [MERIDIAN_ACT1, MERIDIAN_ACT2, CASCADE_ACT],
                       sheet_name="Client Master")
        cls._headers_backup = list(HEADERS)
        # build_workbook always appends the module-level HEADERS row; swap it
        # for the activity-bearing header list for just this workbook.
        wb_headers = ACTIVITY_HEADERS
        from openpyxl import load_workbook as _lw
        wb = _lw(cls.path)
        ws = wb["Client Master"]
        for i, h in enumerate(wb_headers, start=1):
            ws.cell(row=2, column=i, value=h)
        wb.save(cls.path)

        sheet, rows = pick_sheet(cls.path)
        cls.clients = ingest_rows(rows)
        cls.by_name = {c["name"]: c for c in cls.clients}

    def test_finds_pc_and_eb_activity_columns_by_prefix(self):
        hdrs = [norm_hdr(h) for h in ACTIVITY_HEADERS]
        cols = find_activity_columns(ACTIVITY_HEADERS, hdrs)
        self.assertEqual([(c["line"], c["label"]) for c in cols],
                         [("P&C", "Policy Checking"), ("P&C", "Renewals"), ("EB", "Enrollment")])

    def test_truthy_activity_rules(self):
        self.assertTrue(truthy_activity("Yes"))
        self.assertTrue(truthy_activity(3))
        self.assertFalse(truthy_activity("No"))
        self.assertFalse(truthy_activity("0"))
        self.assertFalse(truthy_activity(""))
        self.assertFalse(truthy_activity(None))

    def test_activities_aggregate_across_rows_of_the_same_company(self):
        acts = {a["label"]: a for a in self.by_name["Meridian Insurance Group"]["signals"]["activities"]}
        self.assertTrue(acts["Policy Checking"]["on"])
        self.assertTrue(acts["Renewals"]["on"], "second row for the same company must still count")
        self.assertEqual(acts["Renewals"]["count"], 3)
        self.assertFalse(acts["Enrollment"]["on"])

    def test_whitespace_cue_fires_when_not_every_service_is_in_scope(self):
        cues = self.by_name["Cascade Risk Advisors"]["signals"]["cues"]
        self.assertTrue(any("0 of 3 services in scope" in c for c in cues))

    def test_at_risk_cue_names_the_account_manager(self):
        cues = self.by_name["Cascade Risk Advisors"]["signals"]["cues"]
        self.assertTrue(any("handle carefully and loop in S. Mehta" in c for c in cues))

    def test_single_advocate_contact_gets_the_testimonial_cue(self):
        cues = self.by_name["Cascade Risk Advisors"]["signals"]["cues"]
        self.assertTrue(any("strong candidate for a testimonial" in c for c in cues))

    def test_build_cues_caps_at_four(self):
        c = {
            "signals": {"newClient": True, "healthTone": "crit", "activities": [
                {"line": "P&C", "label": "A", "on": False, "count": None}]},
            "pocs": [{"name": "X"}], "account_manager": "R. Shah",
            "_advocates": 0, "_vocal": 1,
        }
        cues = build_cues(c, date(2026, 6, 1), "High", "")
        self.assertLessEqual(len(cues), 4)


class CommitTests(unittest.TestCase):
    """§10.5 two-step preview->commit and §12.4 idempotency."""

    def setUp(self):
        init_db()
        self.path = Path(_TMP) / "commit.xlsx"
        build_workbook(self.path, [MERIDIAN, MERIDIAN_POC2, CASCADE, HARBORLINE])
        self.event_id = new_id()
        with db() as conn:
            conn.execute("DELETE FROM client_pocs")
            conn.execute("DELETE FROM clients")
            conn.execute("DELETE FROM events")
            conn.execute("INSERT INTO events (id, name) VALUES (?,?)",
                         (self.event_id, "Applied Net 2026"))

    def test_preview_writes_nothing(self):
        with db() as conn:
            plan = import_workbook(conn, self.event_id, self.path, commit=False)
            self.assertEqual((plan["new"], plan["updated"], plan["unchanged"]), (3, 0, 0))
            self.assertFalse(plan["committed"])
            n = conn.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
        self.assertEqual(n, 0, "cancelling at the preview stage must leave the DB untouched")

    def test_commit_then_reimport_is_idempotent(self):
        with db() as conn:
            first = import_workbook(conn, self.event_id, self.path, commit=True)
        self.assertEqual(first["new"], 3)

        with db() as conn:
            second = import_workbook(conn, self.event_id, self.path, commit=True)
            n = conn.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
            pocs = conn.execute("SELECT COUNT(*) c FROM client_pocs").fetchone()["c"]
        # §12.4: "re-importing the identical workbook produces zero updates"
        self.assertEqual((second["new"], second["updated"]), (0, 0))
        self.assertEqual(second["unchanged"], 3)
        self.assertEqual(n, 3, "re-import must not duplicate clients")
        self.assertEqual(pocs, 3, "re-import must not duplicate POCs")

    def test_changed_row_is_detected_as_an_update_and_bumps_the_version(self):
        with db() as conn:
            import_workbook(conn, self.event_id, self.path, commit=True)
            v1 = conn.execute("SELECT version FROM events WHERE id=?",
                              (self.event_id,)).fetchone()["version"]

        changed = list(MERIDIAN)
        changed[16] = "Renewal signed; expansion under discussion."
        build_workbook(self.path, [changed, MERIDIAN_POC2, CASCADE, HARBORLINE])

        with db() as conn:
            res = import_workbook(conn, self.event_id, self.path, commit=True)
            v2 = conn.execute("SELECT version FROM events WHERE id=?",
                              (self.event_id,)).fetchone()["version"]
            row = conn.execute("SELECT summary FROM clients WHERE name=?",
                               ("Meridian Insurance Group",)).fetchone()
        self.assertEqual(res["updated"], 1)
        self.assertEqual(res["unchanged"], 2)
        self.assertGreater(v2, v1, "a publish must advance the sync version")
        self.assertIn("expansion under discussion", row["summary"])

    def test_unmapped_columns_are_reported_to_the_admin(self):
        path = Path(_TMP) / "extra.xlsx"
        wb_headers = HEADERS + ["Some Column We Do Not Know"]
        from openpyxl import Workbook as WB
        wb = WB(); ws = wb.active; ws.title = "Client Master"
        ws.append(["banner"]); ws.append(wb_headers); ws.append(MERIDIAN + ["x"])
        wb.save(path)
        with db() as conn:
            plan = import_workbook(conn, self.event_id, path, commit=False)
        self.assertIn("Some Column We Do Not Know", plan["unmapped_columns"])


if __name__ == "__main__":
    unittest.main()
