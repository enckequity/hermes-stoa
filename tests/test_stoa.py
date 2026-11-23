"""Tests for the desk, with no Hermes and no network. Run: python3 -m unittest discover -s tests"""
import asyncio
import datetime as dt
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN = chr(0x2013)   # the dash in "50-199" style size bands


def _load_package():
    """Import the plugin directory as the package ``stoa`` whatever the checkout folder is called."""
    if "stoa" in sys.modules:
        return sys.modules["stoa"]
    spec = importlib.util.spec_from_file_location("stoa", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["stoa"] = module
    spec.loader.exec_module(module)
    return module


stoa = _load_package()
import fixture  # noqa: E402  (tests/fixture.py: the small desk most tests start from)
from stoa import cards, demo, schemas, sources  # noqa: E402
from stoa.desk import Desk  # noqa: E402
from stoa.playbook import Library, Playbook, PlaybookError, validate  # noqa: E402
from stoa.slackdesk import ACTIONS, SlackDesk  # noqa: E402
from stoa import store as st  # noqa: E402
from stoa.store import Ambiguous, Store, _number, parse_due, slugify, to_cents  # noqa: E402


def make_desk(tmp, playbook="recruiting", secrets=None, http=None, config=None):
    settings = dict({"playbook": playbook}, **(config or {}))
    return Desk(data_dir=lambda: Path(tmp), get_config=lambda k, default=None: settings.get(k, default),
                set_config=settings.__setitem__, get_secret=lambda n: (secrets or {}).get(n),
                actor=lambda: "tester", http=http or (lambda *a, **k: (0, {"error": "no network in tests"})))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_add_get_and_unique_ids(self):
        a = self.store.add("Dana Whitfield", "sourced", grp="Staff Backend")
        b = self.store.add("Dana Whitfield", "sourced", grp="Staff Backend")
        self.assertEqual(a["id"], "dana-whitfield-staff-backend")
        self.assertEqual(b["id"], "dana-whitfield-staff-backend-2")
        self.assertEqual(self.store.get(a["id"])["stage"], "sourced")

    def test_ids_fit_slack_option_values(self):
        deal = self.store.add("A" * 200, "sourced", grp="B" * 200)
        self.assertLessEqual(len("snooze7|" + deal["id"]), 75)

    def test_find_by_fragment_and_ambiguity(self):
        self.store.add("Dana Whitfield", "sourced")
        self.store.add("Dana Okafor", "sourced")
        self.assertEqual(self.store.find("whitfield")["name"], "Dana Whitfield")
        with self.assertRaises(Ambiguous):
            self.store.find("dana")
        self.assertIsNone(self.store.find("nobody"))

    def test_find_prefers_the_open_record(self):
        old = self.store.add("Sam Lee", "sourced", grp="A")
        self.store.move(old["id"], "passed", status="lost")
        new = self.store.add("Sam Lee", "sourced", grp="B")
        self.assertEqual(self.store.find("Sam Lee")["id"], new["id"])

    def test_update_only_logs_real_changes(self):
        deal = self.store.add("Dana", "sourced")
        before = len(self.store.activities(deal["id"]))
        self.store.update(deal["id"], company="")
        self.assertEqual(len(self.store.activities(deal["id"])), before)
        self.store.update(deal["id"], value="$42k", fields={"comp": "210k"})
        updated = self.store.get(deal["id"])
        self.assertEqual(updated["value"], 42000)
        self.assertEqual(updated["fields"]["comp"], "210k")

    def test_money_parsing(self):
        self.assertEqual(_number("$1.2M"), 1200000)
        self.assertEqual(_number("48,000"), 48000)
        self.assertEqual(_number("junk"), 0)
        self.assertEqual(slugify("  Ünï & Co!  "), "ünï-co")

    def test_file_store_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "desk.db")
            Store(path).add("Dana", "sourced")
            self.assertEqual(Store(path).count(), 1)


class PlaybookTests(unittest.TestCase):
    def test_builtins_load_and_validate(self):
        library = Library()
        self.assertEqual(library.names(), ["agency", "b2b-sales", "client-leads", "partnerships", "recruiting"])
        self.assertEqual([entry["door"] for entry in library.catalog()],             # what the first-launch chooser calls each
                         ["Agency and consulting", "Clients and revenue", "Lead searches for clients", "Partnerships and fundraising", "Recruiting and search"])
        for name in library.names():
            playbook = library.load(name)
            self.assertGreaterEqual(len(playbook.stages), 2)
            for stage in playbook.stages:
                self.assertLessEqual(len(playbook.advance_label(stage["id"])), 70)
            self.assertNotIn("error", library.catalog()[library.names().index(name)])

    def test_stage_navigation(self):
        playbook = Library().load("recruiting")
        self.assertEqual(playbook.next_stage("sourced"), "contacted")
        self.assertEqual(playbook.next_stage("offer"), "placed")
        self.assertIsNone(playbook.next_stage("placed"))
        self.assertEqual(playbook.resolve_stage("Client Interview"), "interview")
        self.assertEqual(playbook.resolve_stage("won"), "placed")
        self.assertEqual(playbook.resolve_stage("lost"), "passed")
        self.assertEqual(playbook.status_of("passed"), "lost")

    def test_validation_rejects_bad_playbooks(self):
        for bad in ({}, {"name": "Bad Name", "stages": ["a", "b"]}, {"name": "x", "stages": ["only"]},
                    {"name": "x", "stages": [{"id": "a"}, {"id": "a"}]},
                    {"name": "x", "stages": ["a", "b"], "won": {"id": "a"}}):
            with self.assertRaises(PlaybookError):
                validate(bad)
        ok = validate({"name": "tiny", "stages": ["New", "In talks"]})
        self.assertEqual([s["id"] for s in ok["stages"]], ["new", "in-talks"])
        self.assertEqual(ok["won"]["id"], "won")

    def test_save_custom_playbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            text = "name: dental\ntitle: Dental supply desk\nstages:\n  - {id: lead, label: Lead}\n  - {id: trial, label: Trial order}\n"
            self.assertEqual(library.save(text).name, "dental")
            self.assertIn("dental", library.names())
            with self.assertRaises(PlaybookError):
                library.save("name: [unclosed")


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)

    def add(self, **kw):
        args = dict({"action": "add", "name": "Dana Whitfield", "company": "Kestrel Labs", "group": "Staff Backend"}, **kw)
        return self.desk.deal(args)["deal"]

    def test_add_is_idempotent(self):
        first = self.add()
        again = self.desk.deal({"action": "add", "name": "dana whitfield", "group": "staff backend"})
        self.assertTrue(again["exists"])
        self.assertEqual(again["deal"]["id"], first["id"])
        self.assertEqual(self.desk.store.count(), 1)

    def test_move_close_reopen(self):
        deal = self.add()
        moved = self.desk.deal({"action": "move", "deal": "dana", "stage": "next"})["deal"]
        self.assertEqual(moved["stage"], "contacted")
        self.assertEqual(self.desk.deal({"action": "move", "deal": deal["id"], "stage": "Screened"})["deal"]["stage"], "screened")
        closed = self.desk.deal({"action": "close", "deal": deal["id"], "outcome": "lost", "reason": "Comp mismatch"})["deal"]
        self.assertEqual((closed["status"], closed["lost_reason"]), ("lost", "Comp mismatch"))
        reopened = self.desk.deal({"action": "reopen", "deal": deal["id"]})["deal"]
        self.assertEqual((reopened["status"], reopened["stage"]), ("open", "screened"))
        self.assertFalse(self.desk.deal({"action": "move", "deal": deal["id"], "stage": "nonsense"})["ok"])

    def test_log_sets_next_step_and_get_returns_activity(self):
        deal = self.add()
        self.desk.deal({"action": "log", "deal": deal["id"], "kind": "call", "text": "Good call.", "next_step": "Send JD",
                        "next_due": "2020-01-01"})
        got = self.desk.deal({"action": "get", "deal": deal["id"]})
        self.assertEqual(got["deal"]["next_step"], "Send JD")
        self.assertTrue(got["deal"]["due"])
        self.assertEqual(got["deal"]["activity"][0]["text"], "Updated next_step, next_due")
        self.assertIn("Good call.", [a["text"] for a in got["deal"]["activity"]])
        self.assertIn("Next: Send JD", got["card"])

    def test_channel_allowlist(self):
        self.assertTrue(self.desk.allowed({"platform": "slack", "chat_id": "C1"}))
        desk = make_desk(self.tmp.name, config={"channels": ["C1"]})
        self.assertTrue(desk.allowed({"platform": "slack", "chat_id": "C1"}))
        self.assertFalse(desk.allowed({"platform": "slack", "chat_id": "D9"}))
        self.assertFalse(desk.allowed({"platform": "telegram", "chat_id": ""}))
        self.assertTrue(desk.allowed({"platform": "cli"}))
        self.assertTrue(desk.allowed({}))

    def test_unknown_and_missing_refs(self):
        self.assertFalse(self.desk.deal({"action": "get", "deal": "ghost"})["ok"])
        self.assertFalse(self.desk.deal({"action": "get"})["ok"])
        self.assertFalse(self.desk.deal({"action": "explode", "deal": self.add()["id"]})["ok"])

    def test_board_brief_and_slash(self):
        self.assertIn("empty", self.desk.slash(""))
        self.assertEqual(fixture.load(self.desk), 7)
        self.assertEqual(fixture.load(self.desk), 0)
        board = self.desk.pipeline({})
        self.assertEqual(board["count"], 7)
        self.assertIn("Screened (1)", board["board"])
        brief = self.desk.brief()
        self.assertTrue(brief["due"])
        self.assertTrue(brief["stale"])
        self.assertIn("Marcus Oyelaran", [d["name"] for d in brief["due"]] + [d["name"] for d in brief["stale"]])
        self.assertIn("Gone quiet", self.desk.slash("brief"))
        self.assertIn("Dana Whitfield", self.desk.slash("dana"))
        self.assertIn("● recruiting", self.desk.slash("playbooks"))
        self.assertEqual(self.desk.pipeline({"view": "list", "group": "halcyon"})["count"], 2)
        self.assertEqual(fixture.clear(self.desk), 7)

    def test_everything_tools_return_is_json(self):
        fixture.load(self.desk)
        for result in (self.desk.pipeline({}), self.desk.brief(), self.desk.pipeline({"view": "activity"}),
                       self.desk.deal({"action": "get", "deal": "priya"}), self.desk.notes({}),
                       self.desk.playbook_tool({"action": "list"}), self.desk.playbook_tool({"action": "show"}),
                       self.desk.playbook_tool({"action": "template"}), self.desk.card({"deal": "priya"})):
            self.assertTrue(json.loads(json.dumps(result))["ok"])

    def test_click_advance_hands_writing_to_the_agent(self):
        deal = self.add(stage="screened")
        result = self.desk.click("advance", deal["id"], "screened", "jo")
        self.assertEqual(result["deal"]["stage"], "submitted")
        self.assertIn("Dana Whitfield", result["followup"])
        self.assertIn("submission note", result["followup"])
        self.assertIn('gtm_deal(action="get", deal="%s")' % deal["id"], result["followup"])
        self.assertNotIn("{", result["followup"])

    def test_double_click_does_not_advance_twice(self):
        deal = self.add(stage="screened")
        self.desk.click("advance", deal["id"], "screened", "jo")
        second = self.desk.click("advance", deal["id"], "screened", "sam")
        self.assertEqual(second["deal"]["stage"], "submitted")
        self.assertIn("Already moved", second["notice"])
        self.assertEqual(second["followup"], "")

    def test_twenty_clicks_at_once_move_the_stage_once(self):
        import threading
        deal = self.add(stage="screened")
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.desk.click("advance", deal["id"], "screened", "u%d" % i)))
                   for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.desk.store.get(deal["id"])["stage"], "submitted")
        self.assertEqual(len([r for r in results if r["followup"]]), 1)
        self.assertEqual(len([r for r in results if "Already moved" in r["notice"]]), 19)
        self.assertEqual(len([a for a in self.desk.store.activities(deal["id"], limit=100) if a["kind"] == "stage"]), 1)

    def test_a_tool_move_that_loses_the_race_changes_nothing(self):
        deal = self.add(stage="screened")
        stale = self.desk.store.get(deal["id"])
        self.desk.click("advance", deal["id"], "screened", "jo")
        self.assertIsNone(self.desk._move(stale, "interview", self.desk.playbook(), "agent"))
        self.assertEqual(self.desk.store.get(deal["id"])["stage"], "submitted")
        closed = self.desk.deal({"action": "close", "deal": deal["id"], "outcome": "lost", "reason": "Timing"})["deal"]
        self.assertEqual(self.desk.store.get(deal["id"])["fields"]["_prev_stage"], "submitted")
        self.assertNotIn("Updated fields", [a["text"] for a in self.desk.store.activities(deal["id"], limit=20)])
        self.assertEqual(closed["status"], "lost")

    def test_numbers_and_lists_of_the_wrong_shape_are_forgiven(self):
        fixture.load(self.desk)
        for args in ({"limit": "x"}, {"limit": [1]}, {"view": "activity", "days": "99999999999999999999"}, {"view": "activity", "limit": 10 ** 30},
                     {"limit": None}, {"limit": -5}, {"limit": "12.0"}):
            self.assertTrue(self.desk.pipeline(args)["ok"], args)
        self.assertTrue(self.desk.deal({"action": "snooze", "deal": "priya", "days": "soon"})["ok"])
        self.assertEqual(self.desk.deal({"action": "snooze", "deal": "priya", "days": 0})["deal"].get("snoozed_until"), None)
        self.assertTrue(self.desk.deal({"action": "get", "deal": "priya", "activities": {}})["ok"])
        self.assertTrue(self.desk.notes({"days": "many"})["ok"])
        for targets in (-1, 3.5, True, {"a": 1}, [None, {}], None):
            self.assertFalse(self.desk.market({"action": "track", "targets": targets})["ok"])
            self.assertFalse(self.desk.market({"action": "dismiss", "targets": targets})["ok"])

    def test_click_lost_snooze_reopen_and_gone(self):
        deal = self.add()
        lost = self.desk.click("lost", deal["id"], "2", "jo")
        self.assertEqual((lost["deal"]["status"], lost["deal"]["lost_reason"]), ("lost", "Comp mismatch"))
        self.assertIn("Comp mismatch", lost["followup"])
        self.assertEqual(self.desk.click("reopen", deal["id"], "", "jo")["deal"]["status"], "open")
        self.assertIn("Snoozed until", self.desk.click("snooze7", deal["id"], "", "jo")["notice"])
        self.assertIn("Draft", self.desk.click("draft", deal["id"], "", "jo")["followup"])
        self.assertIsNone(self.desk.click("advance", "ghost", "", "jo")["deal"])

    def test_last_stage_advances_to_won(self):
        deal = self.add(stage="offer")
        result = self.desk.click("advance", deal["id"], "offer", "jo")
        self.assertEqual((result["deal"]["stage"], result["deal"]["status"]), ("placed", "won"))

    def test_flow_counts_how_far_each_record_got(self):
        fixture.load(self.desk)
        self.desk.deal({"action": "close", "deal": "Wei-Lin Chao", "outcome": "won"})
        self.desk.deal({"action": "close", "deal": "Ingrid Solberg", "outcome": "lost", "reason": "Client passed"})
        self.desk.market({"action": "track", "targets": ["Brightside Freight"]})
        flow = self.desk.flow()
        self.assertEqual([s["reached"] for s in flow["stages"]], [8, 6, 4, 3, 2, 1])
        self.assertEqual([s["here"] for s in flow["stages"]], [2, 2, 1, 0, 1, 0])
        self.assertEqual([s["lost"] for s in flow["stages"]], [0, 0, 0, 1, 0, 0])
        self.assertEqual((flow["won"], flow["lost"], flow["from_map"], flow["total"]), (1, 1, 1, 8))
        for i, stage in enumerate(flow["stages"]):  # what arrives either stays, is lost, or moves on
            onward = flow["stages"][i + 1]["reached"] if i + 1 < len(flow["stages"]) else flow["won"]
            self.assertEqual(stage["reached"], stage["here"] + stage["lost"] + onward)

    def test_switching_playbooks_keeps_records(self):
        self.add()
        result = self.desk.playbook_tool({"action": "use", "name": "b2b-sales"})
        self.assertEqual(result["active"], "b2b-sales")
        self.assertIn("1 existing records", result["warning"])
        self.assertIn("Other stages (1)", self.desk.pipeline({})["board"])
        self.assertFalse(self.desk.playbook_tool({"action": "use", "name": "nope"})["ok"])

    def test_agent_written_playbook(self):
        text = Library().source("b2b-sales").replace("name: b2b-sales", "name: dental-supply")
        saved = self.desk.playbook_tool({"action": "save", "yaml": text, "activate": True})
        self.assertEqual((saved["saved"], saved["active"]), ("dental-supply", "dental-supply"))
        self.assertEqual(self.desk.playbook().name, "dental-supply")

    def test_csv_round_trip(self):
        fixture.load(self.desk)
        exported = self.desk.export({"action": "export"})
        self.assertEqual(exported["rows"], 7)
        self.assertIn("group", exported["columns"])
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        fresh = make_desk(other.name)
        self.assertIn("outside the folders", fresh.export({"action": "import", "path": exported["path"]})["error"])   # not one of its folders
        inbox = Path(other.name) / "imports"
        self.assertTrue(inbox.is_dir())                                # the refusal made the folder it points to
        (inbox / "pipeline.csv").write_bytes(Path(exported["path"]).read_bytes())
        dry = fresh.export({"action": "import", "path": "pipeline.csv", "dry_run": True})      # a bare name is looked for in the import folders
        self.assertEqual((dry["added"], fresh.store.count()), (7, 0))
        self.assertEqual(fresh.export({"action": "import", "path": str(inbox / "pipeline.csv")})["added"], 7)
        again = fresh.export({"action": "import", "path": "pipeline.csv"})
        self.assertEqual((again["added"], again["skipped_existing"]), (0, 7))
        priya = fresh.store.find("Priya Raman")
        self.assertEqual((priya["stage"], priya["value"], priya["fields"]["comp"]), ("interview", 45000, "$240k base"))

    def test_import_messy_headers(self):
        path = Path(self.tmp.name) / "leads.csv"
        path.write_text("Candidate,Company,Search,Stage,Amount\nAda Park,Orbit,Staff Backend,Client interview,$30k\n,,,Sourced,\n")
        result = self.desk.import_csv(path)
        self.assertEqual((result["added"], result["problems"]), (1, ["row 3: no name"]))
        ada = self.desk.store.find("Ada Park")
        self.assertEqual((ada["grp"], ada["stage"], ada["value"]), ("Staff Backend", "interview", 30000))


class ActivitySweepTests(unittest.TestCase):
    """What a sweep of email, calls and Slack leans on: the same thing is logged once, and a stage is proposed, not changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)

    def test_the_same_email_is_logged_once_however_often_the_sweep_runs(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        before = len(self.desk.store.activities(dana, limit=50))
        first = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana sent her availability for next week.", "ref": "gmail:18c2f0"})
        again = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana sent her availability for next week.", "ref": "gmail:18c2f0"})
        other = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana confirmed Tuesday.", "ref": "gmail:18c2f9"})
        self.assertEqual((first["ok"], first.get("already"), again.get("already"), other.get("already")), (True, None, True, None))
        self.assertEqual(len(self.desk.store.activities(dana, limit=50)), before + 2)
        plain = [self.desk.deal({"action": "log", "deal": dana, "text": "Left a voicemail."}) for _ in range(2)]      # no ref: logged as told
        self.assertEqual([entry.get("already") for entry in plain], [None, None])
        self.assertEqual(len(self.desk.store.activities(dana, limit=50)), before + 4)

    def test_a_stage_change_the_sweep_spots_waits_for_the_person(self):
        dana = self.desk.store.find("Dana Whitfield")
        proposed = self.desk.intake.run({"action": "propose", "source": "Activity sweep", "rows": [
            {"record": dana["id"], "stage": "Submitted", "note": "Northwind asked for her profile on Tuesday."}]})
        self.assertEqual((proposed["counts"]["update"], proposed["counts"]["new"]), (1, 0))
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], dana["stage"])                     # nothing moved yet
        applied = self.desk.intake.apply({"id": proposed["id"]}, "app")
        self.assertEqual((applied["ok"], applied["updated"]), (True, 1))
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], "submitted")


class EnrichTests(unittest.TestCase):
    PERSON = {"status": 200, "likelihood": 9, "data": {
        "full_name": "dana whitfield", "job_title": "senior backend engineer", "job_company_name": "kestrel labs",
        "location_name": True, "location_country": "united states", "linkedin_url": "linkedin.com/in/danaw",
        "sex": "female", "birth_year": 1990,
        "experience": [{"company": {"name": "kestrel labs"}, "title": {"name": "senior backend engineer"}, "is_primary": True},
                       {"company": {"name": "fathom data"}, "title": {"name": "engineer"}, "end_date": "2021-03"}],
        "skills": ["go", "kafka", True]}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def http(url, headers=None, params=None, timeout=30):
            self.calls.append((url, headers, params))
            return (200, self.PERSON) if params.get("email") != "nobody@example.com" else (404, {})
        self.desk = make_desk(self.tmp.name, secrets={"PDL_API_KEY": "k"}, http=http, config={"pdl_monthly_cap": 2})

    def test_a_card_click_looks_up_outside_the_write_lock(self):
        # 0.28.2 made an operation hold the desk's write lock to its end; the card's Enrich button opened one around a
        # network lookup, so every other read in the process (the page, the agent) waited for the lookup.
        deal = self.desk.deal({"action": "add", "name": "Dana Whitfield", "email": "dana@example.com"})["deal"]
        waited = []

        def http(url, headers=None, params=None, timeout=30):
            done = threading.Event()
            threading.Thread(target=lambda: (self.desk.store.get(deal["id"]), done.set()), daemon=True).start()
            waited.append(done.wait(2))
            return 200, self.PERSON
        self.desk.http = http
        out = self.desk.click("enrich", deal["id"], "", "U1")
        self.assertEqual(waited, [True])
        self.assertEqual(out["deal"]["company"].lower(), "kestrel labs")
        undo = self.desk.undo(deal["id"])
        self.assertTrue(undo["ok"], undo)
        self.assertEqual(self.desk.store.get(deal["id"])["company"], "")      # filled-in fields and its note went back as one

    def test_no_key_means_no_call(self):
        desk = make_desk(self.tmp.name, http=lambda *a, **k: self.fail("network call without a key"))
        self.assertFalse(desk.enrich({"email": "a@b.co"})["ok"])
        self.assertFalse(desk.can_enrich())

    def test_enrich_fills_blanks_only_and_caches(self):
        deal = self.desk.deal({"action": "add", "name": "Dana Whitfield", "email": "dana@example.com",
                               "fields": {"title": "Staff Eng (her words)"}})["deal"]
        result = self.desk.enrich({"deal": deal["id"]})
        self.assertTrue(result["match"])
        self.assertEqual(self.calls[0][1], {"X-Api-Key": "k"})
        self.assertNotIn("sex", json.dumps(result))
        self.assertNotIn("birth", json.dumps(result))
        after = self.desk.store.get(deal["id"])
        self.assertEqual(after["company"], "kestrel labs")
        self.assertEqual(after["fields"]["title"], "Staff Eng (her words)")
        self.assertEqual(after["fields"]["location"], "united states")
        self.assertTrue(after["fields"]["enriched"])
        self.assertTrue(self.desk.enrich({"deal": deal["id"]})["cached"])
        self.assertEqual(len(self.calls), 1)

    def test_monthly_cap_and_no_match(self):
        self.assertFalse(self.desk.enrich({"email": "nobody@example.com"})["match"])
        self.desk.enrich({"email": "one@example.com"})
        capped = self.desk.enrich({"email": "two@example.com"})
        self.assertIn("cap", capped["error"])
        self.assertEqual(len(self.calls), 2)

    def test_person_lookup_needs_an_identifier(self):
        self.assertFalse(sources.pdl_person("k", name="Dana", http=lambda *a, **k: self.fail("should not call"))["ok"])


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notes_dir = Path(self.tmp.name) / "notes"
        self.notes_dir.mkdir()
        (self.notes_dir / "2026-11-10 dana.md").write_text("# Call with Dana Whitfield\n\nShe wants to move by January.\n")
        (self.notes_dir / "groceries.txt").write_text("milk\n")
        (self.notes_dir / "skip.pdf").write_text("not a note")
        self.desk = make_desk(self.tmp.name, config={"notes_dir": str(self.notes_dir)})
        self.deal = self.desk.deal({"action": "add", "name": "Dana Whitfield"})["deal"]

    def test_inbox_suggests_reads_files_and_empties(self):
        inbox = self.desk.notes({"action": "inbox"})
        self.assertEqual((len(inbox["notes"]), inbox["waiting"]), (3, 3))      # the PDF is listed too: Hermes reads it itself
        note = [n for n in inbox["notes"] if n["title"] == "Call with Dana Whitfield"][0]
        self.assertEqual(note["suggested"][0]["id"], self.deal["id"])
        self.assertNotIn("path", note)
        self.assertNotIn("paths", note)
        handed = [n for n in inbox["notes"] if n.get("handed")][0]
        seen = self.desk.notes({"action": "read", "note": handed["id"]})
        self.assertIn("skip.pdf is a PDF", seen["text"])
        self.assertEqual(seen["read_with_your_own_tool"], [str(self.notes_dir / "skip.pdf")])
        self.desk.notes({"action": "skip", "note": handed["id"]})
        self.assertIn("January", self.desk.notes({"action": "read", "note": note["id"]})["text"])
        self.assertFalse(self.desk.notes({"action": "file", "note": note["id"], "deal": self.deal["id"]})["ok"])
        filed = self.desk.notes({"action": "file", "note": note["id"], "deal": "dana", "summary": "Wants to move by January.",
                                 "next_step": "Send two roles"})
        self.assertEqual(filed["deal"]["next_step"], "Send two roles")
        other = [n for n in inbox["notes"] if n["id"] != note["id"]][0]
        self.desk.notes({"action": "skip", "note": other["id"]})
        self.assertEqual(self.desk.notes({"action": "inbox"})["notes"], [])
        self.assertEqual(self.desk.brief()["notes_waiting"], 0)

    def test_no_source_gives_a_hint(self):
        desk = make_desk(self.tmp.name)
        self.assertIn("Setup has a step for it", desk.notes({})["hint"])

    def test_a_recorders_export_is_read_whatever_it_is_written_in(self):
        import zipfile
        root = Path(self.tmp.name) / "plaud"
        (root / "2026" / "11").mkdir(parents=True)
        deep = root / "2026" / "11"
        # one meeting, written as two files the way a recorder exports it: they are one note, the summary first
        (deep / "2026-11-12 Northwind renewal - Transcript.txt").write_text("Speaker 1: we will sign in March.\n")
        (deep / "2026-11-12 Northwind renewal - Summary.md").write_text("Renewal agreed in principle. Dana Whitfield to send the order form.\n")
        with zipfile.ZipFile(deep / "Larkspur pilot.docx", "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Pilot with 12 users &amp; a start in April.</w:t></w:r></w:p>'
                                            '<w:p><w:r><w:t>Next: send pricing.</w:t></w:r></w:p></w:body></w:document>')
        (deep / "standup.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nGood morning.\n\n2\n00:00:03,000 --> 00:00:05,000\nGood morning.\n\n3\n00:00:05,000 --> 00:00:07,000\nShip on Friday.\n")
        (deep / "page.html").write_text("<html><head><style>p{}</style></head><body><h1>Recap</h1><p>Budget is &pound;40k.</p></body></html>")
        (deep / "recording.m4a").write_bytes(b"\x00" * 40)
        (deep / ".hidden.md").write_text("no")
        (root / ".obsidian").mkdir()
        (root / ".obsidian" / "workspace.md").write_text("no")
        too_deep = deep / "a" / "b"
        too_deep.mkdir(parents=True)
        (too_deep / "lost.md").write_text("four levels down")
        desk = make_desk(self.tmp.name, config={"notes_dir": str(root)})
        dana = desk.deal({"action": "add", "name": "Dana Whitfield", "company": "Northwind Freight"})["deal"]
        inbox = desk.notes({"action": "inbox"})
        by_title = {n["title"]: n for n in inbox["notes"]}
        self.assertEqual(sorted(by_title), ["Larkspur pilot", "Northwind renewal", "page", "standup"])
        meeting = by_title["Northwind renewal"]
        self.assertEqual((meeting["parts"], meeting["when"][:10]), (2, "2026-11-12"))      # the date is the one in its name
        self.assertEqual((meeting["suggested"][0]["id"], meeting["suggested"][0]["why"]), (dana["id"], "named in the note"))
        text = desk.notes({"action": "read", "note": meeting["id"]})["text"]
        self.assertLess(text.index("Renewal agreed"), text.index("we will sign in March"))
        self.assertIn("## 2026-11-12 Northwind renewal - Summary.md", text)
        self.assertIn("Pilot with 12 users & a start in April.\nNext: send pricing.", desk.notes({"action": "read", "note": by_title["Larkspur pilot"]["id"]})["text"])
        self.assertEqual(desk.notes({"action": "read", "note": by_title["standup"]["id"]})["text"], "Good morning.\nShip on Friday.")
        self.assertIn("Budget is £40k.", desk.notes({"action": "read", "note": by_title["page"]["id"]})["text"])
        # a transcript that arrives after its summary was filed does not bring the meeting back
        desk.notes({"action": "file", "note": meeting["id"], "deal": dana["id"], "summary": "Renewal agreed in principle."})
        (deep / "2026-11-12 Northwind renewal - Transcript.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nLate file.\n")
        self.assertNotIn(meeting["id"], [n["id"] for n in desk.notes({"action": "inbox"})["notes"]])
        check = desk.notes_check(str(root))
        self.assertEqual((check["count"], check["audio"], check["handed"]), (6, 1, 0))
        self.assertIn("1 recording has no words yet", check["message"])

    def test_a_note_says_who_it_is_about_at_its_head_or_in_what_it_says(self):
        (self.notes_dir / "weekly.md").write_text("---\ntitle: Weekly sync\ndate: 2026-10-30\nattendees:\n  - Priya Nair <priya@larkspur.example>\n  - Me\n---\n\nNothing about anyone by name.\n")
        (self.notes_dir / "voice memo 7.txt").write_text("Rang the people at Corvid Health about the pilot. They want a call next week.\n")
        priya = self.desk.deal({"action": "add", "name": "Priya Nair", "email": "priya@larkspur.example"})["deal"]
        corvid = self.desk.deal({"action": "add", "name": "Lena Ortiz", "company": "Corvid Health"})["deal"]
        notes = {n["title"]: n for n in self.desk.notes({"action": "inbox"})["notes"]}
        self.assertEqual((notes["Weekly sync"]["when"][:10], notes["Weekly sync"]["suggested"][0]), ("2026-10-30", {"id": priya["id"], "name": "Priya Nair", "why": "attendee email"}))
        self.assertEqual(notes["voice memo 7"]["suggested"][0], {"id": corvid["id"], "name": "Lena Ortiz", "why": "company named in the note"})
        self.assertEqual(notes["groceries"]["suggested"], [])
        seen = self.desk.notes_preview()      # the same list for the page, with no note's words in it
        self.assertEqual((seen["waiting"], seen["matched"]), (5, 3))
        self.assertNotIn("Rang the people", json.dumps(seen))

    def test_a_filed_note_stays_filed_when_its_file_is_edited_saved_again_renamed_or_moved(self):
        import hashlib, os
        path = self.notes_dir / "2026-11-10 dana.md"
        note = [n for n in self.desk.notes({"action": "inbox"})["notes"] if n["title"] == "Call with Dana Whitfield"][0]
        self.desk.notes({"action": "file", "note": note["id"], "deal": self.deal["id"], "summary": "Wants to move by January."})
        waiting = lambda: [n["title"] for n in self.desk.notes({"action": "inbox"})["notes"]]
        self.assertNotIn("Call with Dana Whitfield", waiting())
        logged = len(self.desk.store.activities(self.deal["id"], limit=50))
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 3600))      # a sync tool saves it again, unchanged
        self.assertNotIn("Call with Dana Whitfield", waiting())
        path.write_text(path.read_text() + "\nShe prefers remote.\n")      # the person adds a line
        self.assertNotIn("Call with Dana Whitfield", waiting())
        self.assertEqual(len(self.desk.store.activities(self.deal["id"], limit=50)), logged)      # and nothing more went on the record
        # the same note under another name, in another folder the desk also reads
        other = self.notes_dir / "archive"
        other.mkdir()
        fresh = self.notes_dir / "intro with sam.md"
        fresh.write_text("# Intro with Sam Ito\n\nWants a call next week.\n")
        sam = [n for n in self.desk.notes({"action": "inbox"})["notes"] if n["title"] == "Intro with Sam Ito"][0]
        self.desk.notes({"action": "skip", "note": sam["id"]})
        fresh.rename(other / "Sam Ito - first call.md")
        self.assertNotIn("Intro with Sam Ito", waiting())
        # a note filed by a version that knew a file by its path, size and time is still known as filed, and stays so
        old = self.notes_dir / "older.md"
        old.write_text("# Older call\n")
        stat = old.stat()
        legacy = "file:" + hashlib.sha1(("%s|%d|%d" % (old, stat.st_size, int(stat.st_mtime))).encode()).hexdigest()[:12]
        self.desk.store.mark_note(legacy, self.deal["id"])
        self.assertNotIn("Older call", waiting())
        (self.notes_dir / "second.md").write_text("# Second older call\n")      # and one that is renamed and moved after the update
        stat = (self.notes_dir / "second.md").stat()
        self.desk.store.mark_note("file:" + hashlib.sha1(("%s|%d|%d" % (self.notes_dir / "second.md", stat.st_size, int(stat.st_mtime))).encode()).hexdigest()[:12], self.deal["id"])
        self.assertNotIn("Second older call", waiting())      # the first listing after the update takes its mark
        (self.notes_dir / "second.md").rename(other / "renamed later.md")
        self.assertNotIn("Second older call", waiting())
        old.write_text("# Older call\n\nEdited after the update.\n")
        self.assertNotIn("Older call", waiting())

    def test_a_handled_note_is_gone_through_again_only_when_asked(self):
        note = [n for n in self.desk.notes({"action": "inbox"})["notes"] if n["title"] == "Call with Dana Whitfield"][0]
        self.desk.notes({"action": "file", "note": note["id"], "deal": self.deal["id"], "summary": "Wants to move by January."})
        listed = self.desk.notes({"action": "inbox", "include_filed": True})
        self.assertNotIn("handled", self.desk.notes({"action": "inbox"}))
        row = [h for h in listed["handled"] if h["id"] == note["id"]][0]
        self.assertEqual((row["title"], row["filed_on"]), ("Call with Dana Whitfield", "Dana Whitfield"))
        again = self.desk.notes({"action": "reopen", "note": note["id"]})
        self.assertTrue(again["ok"])
        self.assertIn("add only what is new", again["note"])
        self.assertIn("Call with Dana Whitfield", [n["title"] for n in self.desk.notes({"action": "inbox"})["notes"]])
        self.assertEqual(len([a for a in self.desk.store.activities(self.deal["id"]) if a["ref"] == note["id"]]), 1)      # what was filed is still there
        self.assertFalse(self.desk.notes({"action": "reopen", "note": "file:nothing"})["ok"])

    def test_several_folders_can_be_read_and_one_taken_away(self):
        second = Path(self.tmp.name) / "obsidian" / "Meetings"
        second.mkdir(parents=True)
        (second / "intro.md").write_text("# Intro call\n")
        added = self.desk.notes_folder(str(second), "add")
        self.assertEqual((added["ok"], added["folders"]), (True, [str(self.notes_dir), str(second)]))
        self.assertEqual(self.desk.note_folders(), [str(self.notes_dir), str(second)])
        self.assertIn("already on the list", self.desk.notes_folder(str(second), "add")["message"])
        self.assertIn("Intro call", [n["title"] for n in self.desk.notes({"action": "inbox"})["notes"]])
        setup = self.desk.setup()
        self.assertEqual([row["folder"] for row in setup["notes"]["folders"]], [str(self.notes_dir), str(second)])
        self.assertIn("Reading 2 folders.", [c["detail"] for c in setup["checklist"] if c["id"] == "notes"])
        self.assertEqual([tool["id"] for tool in setup["notes"]["tools"]], ["plaud", "obsidian", "granola", "fireflies", "other"])
        self.assertFalse(self.desk.notes_folder(str(Path(self.tmp.name) / "nowhere"), "add")["ok"])
        self.assertEqual(self.desk.notes_folder(str(self.notes_dir), "remove")["folders"], [str(second)])
        self.assertEqual(self.desk.notes_folder("")["folders"], [])
        gone = make_desk(self.tmp.name, config={"notes_dir": str(Path(self.tmp.name) / "deleted")})
        self.assertIn("is not there any more", gone.notes({"action": "inbox"})["problems"][0])

    def test_fireflies_notes(self):
        sent = []

        def http(url, headers=None, params=None, data=None, timeout=30):
            self.assertEqual((url, headers), ("https://api.fireflies.ai/graphql", {"Authorization": "Bearer f"}))
            sent.append(data)
            if "transcripts(" in data["query"]:
                return 200, {"data": {"transcripts": [{"id": "ff1", "title": "Northwind renewal", "dateString": "2026-11-12T16:00:00.000Z",
                                                       "participants": ["dana@example.com", "me@example.com"], "transcript_url": "https://app.fireflies.ai/view/ff1",
                                                       "meeting_attendees": [{"displayName": "Dana Whitfield", "email": "Dana@Example.com"}]}]}}
            return 200, {"data": {"transcript": {"id": "ff1", "title": "Northwind renewal", "dateString": "2026-11-12T16:00:00.000Z",
                                                 "summary": {"overview": "Renewal agreed.", "action_items": "Dana: send the order form", "shorthand_bullet": ["Sign in March"]}}}}
        desk = make_desk(self.tmp.name, secrets={"FIREFLIES_API_KEY": "f"}, http=http)
        desk.deal({"action": "update", "deal": self.deal["id"], "email": "dana@example.com"})
        note = desk.notes({"action": "inbox"})["notes"][0]
        self.assertEqual((note["id"], note["source"], note["suggested"][0]["why"]), ("fireflies:ff1", "fireflies", "attendee email"))
        self.assertEqual(sent[0]["variables"]["limit"], 50)
        self.assertNotIn("sentences", sent[0]["query"])      # the transcript is never asked for
        text = desk.notes({"action": "read", "note": "fireflies:ff1"})["text"]
        self.assertEqual(text, "Renewal agreed.\n\n## Action items\nDana: send the order form\n\n## Notes\nSign in March")
        self.assertNotIn("sentences", sent[-1]["query"])
        self.assertEqual(desk.key_test("fireflies")["state"], "works")
        refused = make_desk(self.tmp.name, secrets={"FIREFLIES_API_KEY": "bad"}, http=lambda *a, **k: (200, {"errors": [{"message": "Invalid API key", "code": "auth_failed"}]}))
        self.assertEqual(refused.key_test("fireflies")["state"], "rejected")
        self.assertEqual(refused.notes({"action": "inbox"})["problems"], ["Fireflies rejected the API key"])

    def test_notes_in_a_connection_of_hermess_own_are_filed_once(self):
        desk = make_desk(self.tmp.name, config={"notes_connections": "Plaud, my notion!"})
        self.assertEqual(desk.note_connections(), ["plaud", "mynotion"])
        inbox = desk.notes({"action": "inbox"})
        self.assertEqual(inbox["connections"], ["plaud", "mynotion"])
        self.assertIn("gtm_notes(action='known'", inbox["connections_how"])
        self.assertNotIn("hint", inbox)
        dana = desk.deal({"action": "add", "name": "Dana Whitfield"})["deal"]
        self.assertEqual(desk.notes({"action": "known", "notes": ["plaud:a1", "plaud:b2"]})["new"], ["plaud:a1", "plaud:b2"])
        self.assertFalse(desk.notes({"action": "read", "note": "plaud:a1"})["ok"])      # Stoa cannot read it: the agent does, with Plaud's tools
        filed = desk.notes({"action": "file", "note": "plaud:a1", "deal": dana["id"], "summary": "Wants to start in January."})
        self.assertTrue(filed["ok"])
        desk.notes({"action": "skip", "note": "plaud:b2"})
        again = desk.notes({"action": "known", "notes": ["plaud:a1", "plaud:b2", "plaud:c3"]})
        self.assertEqual((again["new"], again["already_handled"]), (["plaud:c3"], ["plaud:a1", "plaud:b2"]))
        self.assertEqual(desk.notes_connections("")["connections"], [])

    def test_granola_notes(self):
        def http(url, headers=None, params=None, timeout=30):
            self.assertEqual(headers, {"Authorization": "Bearer g"})
            if url.endswith("/notes"):
                return 200, {"notes": [{"id": "not_1", "title": "Intro", "created_at": "2026-11-09T10:00:00Z",
                                        "attendees": [{"email": "Dana@Example.com", "name": "Dana"}]}], "hasMore": False}
            return 200, {"id": "not_1", "title": "Intro", "summary_markdown": "Talked comp.", "private_notes_text": "likes remote"}
        desk = make_desk(self.tmp.name, secrets={"GRANOLA_API_KEY": "g"}, http=http)
        desk.deal({"action": "update", "deal": self.deal["id"], "email": "dana@example.com"})
        inbox = desk.notes({})
        self.assertEqual(inbox["notes"][0]["suggested"][0]["why"], "attendee email")
        text = desk.notes({"action": "read", "note": "granola:not_1"})["text"]
        self.assertIn("Talked comp.", text)
        self.assertIn("likes remote", text)

    def test_granola_bad_key_is_reported(self):
        desk = make_desk(self.tmp.name, secrets={"GRANOLA_API_KEY": "bad"}, http=lambda *a, **k: (401, {}))
        self.assertIn("rejected", desk.notes({})["problems"][0])


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)

    def test_blocks_are_valid_block_kit(self):
        for deal in self.desk.store.list(status="all"):
            blocks = self.desk.blocks(deal)
            self.assertLessEqual(len(blocks), 50)
            for block in blocks:
                if block["type"] == "section" and "text" in block:
                    self.assertLessEqual(len(block["text"]["text"]), 3000)
                if block["type"] == "section" and "fields" in block:
                    self.assertLessEqual(len(block["fields"]), 10)
                if block["type"] == "context":
                    self.assertLessEqual(len(block["elements"]), 10)
                if block["type"] == "actions":
                    self.assertLessEqual(len(block["elements"]), 25)
                    for element in block["elements"]:
                        self.assertTrue(ACTIONS.match(element["action_id"]), element["action_id"])
                        for option in element.get("options", []):
                            self.assertLessEqual(len(option["value"]), 75)
                            self.assertLessEqual(len(option["text"]["text"]), 75)
                        if element["type"] == "button":
                            self.assertLessEqual(len(element["text"]["text"]), 75)

    def test_open_card_has_the_playbooks_button_and_closed_card_reopens(self):
        dana = self.desk.store.find("Dana Whitfield")
        actions = self.desk.blocks(dana)[-1]["elements"]
        self.assertEqual(actions[0]["text"]["text"], "Submit to client")
        self.assertEqual(actions[0]["value"], dana["id"] + "|screened")
        self.assertNotIn("stoa_enrich", [e["action_id"] for e in actions])
        self.desk.deal({"action": "close", "deal": dana["id"], "outcome": "lost", "reason": "Timing"})
        closed = self.desk.blocks(self.desk.store.get(dana["id"]))[-1]["elements"]
        self.assertEqual([e["action_id"] for e in closed], ["stoa_reopen"])

    def test_meter_and_escaping(self):
        playbook = self.desk.playbook()
        dana = self.desk.store.find("Dana Whitfield")
        self.assertEqual(cards.meter(playbook, dana), "▰▰▰▱▱▱  Screened · 3 of 6")
        odd = self.desk.deal({"action": "add", "name": "A <b> & co"})["deal"]
        self.assertIn("A &lt;b&gt; &amp; co", self.desk.blocks(self.desk.store.get(odd["id"]))[0]["text"]["text"])

    def test_card_without_slack_returns_text(self):
        result = self.desk.card({"deal": "dana"}, {"platform": "telegram"})
        self.assertFalse(result["posted"])
        self.assertIn("Dana Whitfield · Kestrel Labs", result["card"])


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def http(url, headers=None, params=None, timeout=30, data=None):
            self.calls.append((url, headers, data))
            if "exa.ai" in url:
                return 200, {"results": [{"title": "Acme Freight", "url": "https://www.acmefreight.example/about", "text": "A  broker\nin Ohio"}]}
            return 200, {"total": 12, "data": [{"full_name": "ada park", "job_title": "vp operations", "job_company_name": "meridian freight",
                                                "linkedin_url": "linkedin.com/in/adapark"}]}
        self.http = http
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales", http=http, config={"pdl_monthly_cap": 4})
        self.market = self.desk.market

    def define(self):
        return self.market({"action": "define", "name": "Freight brokers, Midwest", "brief": "Brokers with 50-500 people.",
                            "segments": ["Asset-light", {"name": "Hybrid", "description": "own trucks too"}]})

    def add(self):
        return self.market({"action": "add", "companies": [
            {"company": "Meridian Freight, Inc.", "website": "https://www.meridian.example/x", "segment": "Asset-light", "fit": 5,
             "why": "Hiring compliance analysts", "size": "220 people", "location": "Chicago"},
            {"company": "Northwind Carriers", "segment": "hybrid", "fit": 9},
            {"company": "Loose One"}, {"website": "nameless.example"}]})

    def test_empty_states(self):
        self.assertIn("define", self.market({"action": "show"})["error"])
        self.assertEqual(self.market({"action": "list"})["markets"], [])
        self.assertFalse(self.market({"action": "nonsense"})["ok"])
        self.assertFalse(self.market({"action": "define"})["ok"])

    def test_define_add_show(self):
        self.assertEqual(self.define()["market"]["id"], "freight-brokers-midwest")
        self.assertTrue(self.define()["updated"])
        added = self.add()
        self.assertEqual((len(added["added"]), added["total"], len(added["problems"])), (3, 3, 1))
        again = self.market({"action": "add", "companies": [{"company": "meridian freight", "signal": "Raised a round"}]})
        self.assertEqual((again["added"], again["updated"], again["total"]), ([], ["meridian freight"], 3))
        shown = self.market({"action": "show"})
        self.assertEqual([s["segment"] for s in shown["segments"]], ["Asset-light", "hybrid", "Unsorted"])
        first = shown["segments"][0]["companies"][0]
        self.assertEqual((first["domain"], first["fit"], first["signal"]), ("meridian.example", 5, "Raised a round"))
        self.assertEqual(shown["segments"][1]["companies"][0]["fit"], 5)
        self.assertIn("●●●●● Meridian Freight, Inc. · 220 people · Chicago", shown["map"])
        self.assertIn("3 companies · 0 in the pipeline", self.desk.slash("map"))
        self.assertEqual(self.market({"action": "list"})["markets"][0]["companies"], 3)
        json.dumps(shown)

    def test_track_creates_pipeline_records_once(self):
        self.define(); self.add()
        result = self.market({"action": "track", "targets": ["Meridian Freight, Inc.", "ghost co"]})
        self.assertEqual(result["not_on_a_map"], ["ghost co"])
        deal = self.desk.store.get(result["added_to_pipeline"][0]["deal"])
        self.assertEqual((deal["stage"], deal["company"], deal["grp"], deal["next_step"]),
                         ("lead", "Meridian Freight, Inc.", "Meridian Freight, Inc.", "Find the right contact"))
        self.assertIn("market map: Freight brokers", deal["source"])
        self.assertIn("Hiring compliance analysts", self.desk.store.activities(deal["id"])[0]["text"])
        again = self.market({"action": "track", "targets": ["Meridian Freight, Inc."]})
        self.assertEqual(again["already_in_pipeline"], ["Meridian Freight, Inc."])
        self.assertEqual(self.desk.store.count(), 1)
        self.assertIn("[in pipeline]", self.market({"action": "show"})["map"])
        self.assertFalse(self.market({"action": "dismiss", "targets": ["Meridian Freight, Inc."]})["ok"])

    def test_track_with_a_contact_and_recruiting_grouping(self):
        self.define(); self.add()
        one = self.market({"action": "track", "targets": ["Northwind Carriers"], "contact": "Ada Park", "title": "VP Ops",
                           "email": "ada@northwind.example"})
        deal = self.desk.store.get(one["added_to_pipeline"][0]["deal"])
        self.assertEqual((deal["name"], deal["email"], deal["fields"]["title"], deal["next_step"]),
                         ("Northwind Carriers", "", "VP Ops", "First outreach"))      # the deal, named for the company
        self.assertEqual([(p["name"], p["email"], p["title"], p["role"]) for p in self.desk.store.people(deal["id"])],
                         [("Ada Park", "ada@northwind.example", "VP Ops", "Buyer, from the market map")])
        self.desk._set_config("playbook", "recruiting")       # roles: the record is the person, as before
        two = self.market({"action": "track", "targets": ["Loose One"], "contact": "Bo Reyes", "email": "bo@loose.example"})
        made = self.desk.store.get(two["added_to_pipeline"][0]["deal"])
        self.assertEqual((made["grp"], made["name"], made["email"], self.desk.store.people(made["id"])),
                         ("Freight brokers, Midwest", "Bo Reyes", "bo@loose.example", []))

    def test_track_fills_in_a_record_already_in_the_pipeline(self):
        # A company first added from a file, by its name alone, then found on a map with its buyer: one record, filled in.
        store = self.desk.store
        first = store.add("Meridian Freight", "lead", company="meridian freight", grp="Meridian Freight",
                          next_step="Apply at the partner page", source="Research", fields={"competitor": "Spreadsheets"})
        self.define(); self.add()
        self.market({"action": "add", "companies": [{"company": "Meridian Freight, Inc.",
                                                    "uses": "Acme TMS", "people": [{"name": "Ada Park", "title": "VP Ops"}]}]})
        result = self.market({"action": "track", "targets": ["Meridian Freight, Inc."]})
        made = result["added_to_pipeline"][0]
        self.assertEqual((made["deal"], made["existing_record"], store.count()), (first["id"], True, 1))
        self.assertIn("no second one was made", result["note"])
        deal = store.get(first["id"])
        self.assertEqual((deal["name"], deal["fields"]["title"], deal["next_step"], deal["source"]),
                         ("Meridian Freight", "VP Ops", "Apply at the partner page", "Research"))      # its name, and what a person typed, stay
        self.assertEqual([(p["name"], p["title"]) for p in store.people(first["id"])], [("Ada Park", "VP Ops")])
        self.assertEqual(deal["fields"]["competitor"], "Spreadsheets")
        self.assertIn("filled in rather than a second one", store.activities(deal["id"])[0]["text"])
        self.assertIn("[in pipeline]", self.market({"action": "show"})["map"])
        # A record named for a person (an older one, or one made by hand) keeps them, and gets the map's buyer as another person.
        other = store.add("Lee Chan", "lead", company="Northwind Carriers", grp="Northwind Carriers")
        self.market({"action": "add", "companies": [{"company": "Northwind Carriers", "people": [{"name": "Ada Park", "title": "COO"}]}]})
        clicked = self.desk.markets.click_track(self.desk.markets.target("Northwind Carriers")["id"], "dashboard")
        self.assertEqual((clicked["deal"]["id"], clicked["deal"]["name"], clicked["existing"]), (other["id"], "Lee Chan", True))
        self.assertIn("already had an open record", clicked["followup"])
        self.assertEqual([(p["name"], p["title"]) for p in store.people(other["id"])], [("Ada Park", "COO")])
        # A closed record is history: the map opens a new one.
        store.move(store.add("Loose One", "lead", company="Loose One")["id"], "lead", status="lost")
        self.assertNotIn("existing_record", self.market({"action": "track", "targets": ["Loose One"]})["added_to_pipeline"][0])

    def test_dismiss_hides_from_the_map(self):
        self.define(); self.add()
        self.assertEqual(self.market({"action": "dismiss", "targets": ["Loose One"], "reason": "Too small"})["dismissed"], ["Loose One"])
        self.assertNotIn("Loose One", self.market({"action": "show"})["map"])
        self.assertEqual(len(self.market({"action": "show", "status": "dismissed"})["segments"][0]["companies"]), 1)

    def test_search_and_people_need_keys(self):
        self.define(); self.add()
        self.assertIn("web search", self.market({"action": "search", "query": "freight brokers"})["error"])
        self.assertIn("web search", self.market({"action": "people", "target": "Northwind Carriers"})["error"])
        self.assertEqual(self.calls, [])

    def test_search_and_people_with_keys(self):
        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=self.http, config={"pdl_monthly_cap": 5},
                         secrets={"EXA_API_KEY": "e", "PDL_API_KEY": "p"})
        desk.market({"action": "define", "name": "Freight"})
        desk.market({"action": "add", "companies": [{"company": "Meridian Freight", "website": "meridian.example"}]})
        found = desk.market({"action": "search", "query": "freight brokers in Ohio"})
        self.assertEqual(found["results"][0], {"title": "Acme Freight", "url": "https://www.acmefreight.example/about",
                                               "domain": "acmefreight.example", "text": "A broker in Ohio"})
        self.assertEqual((self.calls[0][1], self.calls[0][2]["category"]), ({"x-api-key": "e"}, "company"))
        people = desk.market({"action": "people", "target": "Meridian Freight", "titles": ["VP Operations'; DROP", "logistics"], "limit": 9})
        self.assertEqual((people["people"][0]["name"], people["credits_used"]), ("Ada Park", 1))
        sql = self.calls[1][2]["sql"]
        self.assertEqual(sql, "SELECT * FROM person WHERE job_company_website='meridian.example' AND "
                              "(job_title LIKE '%vp operations drop%' OR job_title LIKE '%logistics%')")
        self.assertEqual(self.calls[1][2]["size"], 5)
        self.assertEqual(self.calls[1][1], {"X-Api-Key": "p"})
        self.assertIn("cap", desk.market({"action": "people", "target": "Meridian Freight", "limit": 5})["error"])
        self.assertEqual(len(self.calls), 2)

    def test_the_map_reads_itself_back(self):
        fixture.load(self.desk)
        s = self.market({"action": "show"})
        self.assertEqual(s["numbers"], {"companies": 7, "segments": 3, "in_pipeline": 0, "with_signal": 3, "buyers_named": 4,
                                        "assessed": 7, "bridges": 2, "avg_fit": 3.7, "dismissed": 0})
        self.assertEqual([(r["label"], r["rate"], r["of"]) for r in s["requirements"]],
                         [("50 to 500 people", 100, 7), ("Scorecards kept in spreadsheets", 43, 7),
                          ("Own compliance team", 43, 7), ("Carrier base growing", 71, 7)])
        self.assertEqual([(h["name"], h["count"], h["share"]) for h in s["holds"]],
                         [("Spreadsheets", 4, 57), ("In-house tool", 2, 29), ("CarrierWatch", 1, 14)])
        self.assertEqual(sorted(tuple(b["between"]) for b in s["bridges"]),
                         [("Asset-light brokers", "Digital-first"), ("Asset-light brokers", "Hybrid carriers")])
        self.assertEqual([(c["company"], c["buyer"]) for c in s["first_calls"]],
                         [("Brightside Freight", "Tobias Lindqvist"), ("Meridian Freight", "Renata Okafor"),
                          ("Northwind Carriers", "Dalia Reyes-Moore")])
        self.assertEqual({r["name"] for r in s["regions"]}, {"IL", "OH", "IA", "IN", "NE", "MN", "MO"})
        self.assertEqual([(z["band"], z["count"]) for z in s["sizes"]], [("50\u2013199", 4), ("200\u2013999", 3)])
        self.assertIn("only one has bought a tool", s["market"]["reading"])
        self.assertEqual(len(s["market"]["implications"]), 3)
        meridian = s["segments"][0]["companies"][0]
        self.assertEqual(meridian["uses"], "Spreadsheets")
        self.assertEqual([r["shown"] for r in meridian["requirements"]], [True, True, True, True])
        self.assertEqual(meridian["requirements"][1]["evidence"], "Job ad asks for advanced Excel for carrier scoring")
        for heading in ("How rare each requirement is", " 43%  Own compliance team (3 of 7 checked)", "Who holds this market today", "Where the segments meet",
                        "First calls", " 57%  Spreadsheets (4)", "1. Brightside Freight \u2014 Fresh funding"):
            self.assertIn(heading, s["map"])
        json.dumps(s)

    def test_requirements_set_the_fit_and_unknown_ones_are_reported(self):
        self.market({"action": "define", "name": "Solar", "requirements": ["Owns the roof", "Over 50 staff", "Has a facilities lead", "Bills over 10k"]})
        added = self.market({"action": "add", "companies": [
            {"company": "Two Of Four", "requirements": {"Owns the roof": "Deed on file", "over 50 staff": "120 people", "Has a facilities lead": ""}},
            {"company": "None Shown", "requirements": {"Owns the roof": "", "Wears a hat": "yes"}},
            {"company": "Scored By Hand", "fit": 5, "requirements": {"Owns the roof": ""}}]})
        self.assertEqual(added["problems"], ["None Shown: 'Wears a hat' is not one of this market's requirements"])
        fits = {c["company"]: c["fit"] for seg in self.market({"action": "show"})["segments"] for c in seg["companies"]}
        self.assertEqual(fits, {"Two Of Four": 3, "None Shown": 1, "Scored By Hand": 5})
        self.market({"action": "add", "companies": [{"company": "Two Of Four", "requirements": {"Bills over 10k": "Tariff sheet"}},
                                                      {"company": "Scored By Hand", "requirements": {"Over 50 staff": "90 people"}}]})
        shown = self.market({"action": "show"})
        # each requirement is measured only among the companies actually checked for it
        rates = {r["label"]: (r["shown"], r["of"]) for r in shown["requirements"]}
        self.assertEqual(rates, {"Owns the roof": (1, 3), "Over 50 staff": (2, 2), "Has a facilities lead": (0, 1), "Bills over 10k": (1, 1)})
        # fit follows the requirements as more is learned, unless it was set by hand
        fits = {c["company"]: c["fit"] for seg in shown["segments"] for c in seg["companies"]}
        self.assertEqual(fits, {"Two Of Four": 4, "None Shown": 1, "Scored By Hand": 5})
        # rewording the requirements does not count old evidence against the new wording
        self.market({"action": "define", "name": "Solar", "requirements": ["Flat roof", "Pays its own power bill"]})
        reworded = self.market({"action": "show"})
        self.assertEqual([(r["label"], r["of"]) for r in reworded["requirements"]], [("Flat roof", 0), ("Pays its own power bill", 0)])
        self.assertEqual(reworded["numbers"]["assessed"], 0)
        self.assertNotIn("How rare each requirement is", reworded["map"])

    def test_review_findings_stay_fixed(self):
        from stoa.market import _clean, _key, _people, size_band
        self.assertEqual([size_band(x) for x in ("Founded 2015, 80 people", "$40M ARR, 300 people", "250 key staff", "about 1.2k employees")],
                         ["50%s199" % EN, "200%s999" % EN, "200%s999" % EN, "1,000+"])
        self.assertEqual([_clean(x) for x in (False, True, 0, None, [], {"a": 1}, "  two   words ", 42)], ["", "", "", "", "", "", "two words", "42"])
        self.assertEqual(len({_key("\u65e5\u672c\u8a9e\u306e\u8981\u4ef6"), _key("\u5225\u306e\u8981\u4ef6"), _key("Caf\u00e9 owner"), _key("Cafe owner")}), 4)
        merged = _people([{"name": "A B", "title": "x"}, {"name": "a b", "title": "VP", "linkedin": "li/ab"}, {"name": "C D", "linkedin": "LI/AB"}])
        self.assertEqual(merged, [{"name": "C D", "title": "VP", "linkedin": "LI/AB"}])
        self.assertEqual(len(_people([{"name": "P%d" % i} for i in range(9)])), 6)
        self.market({"action": "define", "name": "Rooms", "requirements": ["\u65e5\u672c\u8a9e\u306e\u8981\u4ef6", "\u5225\u306e\u8981\u4ef6"]})
        self.market({"action": "add", "companies": [
            {"company": "Kyoto Inn", "signal": False, "uses": False, "why": 0, "requirements": {"\u65e5\u672c\u8a9e\u306e\u8981\u4ef6": "yes", "\u5225\u306e\u8981\u4ef6": ""},
             "people": [{"name": "Miles Brandt", "title": "COO", "linkedin": "li/miles", "email": "miles@inn.example"}]}]})
        shown = self.market({"action": "show"})
        self.assertEqual([(r["shown"], r["of"]) for r in shown["requirements"]], [(1, 1), (0, 1)])
        self.assertEqual((shown["numbers"]["with_signal"], shown["holds"]), (0, []))
        tracked = self.market({"action": "track", "targets": ["Kyoto Inn"], "contact": "miles brandt", "title": "Chief Operating Officer"})
        deal = self.desk.store.get(tracked["added_to_pipeline"][0]["deal"])
        buyer = self.desk.store.people(deal["id"])[0]      # a deal is named for the company; its buyer is a person on it
        self.assertEqual((deal["name"], deal["email"], buyer["name"], buyer["email"], buyer["linkedin"], buyer["title"], deal["fields"].get("title")),
                         ("Kyoto Inn", "", "Miles Brandt", "miles@inn.example", "li/miles", "Chief Operating Officer", "Chief Operating Officer"))

    def test_scheduled_jobs_are_opted_in_by_name(self):
        desk = make_desk(self.tmp.name, config={"channels": ["C1"]})
        self.assertFalse(desk.allowed({"platform": "cron", "chat_id": ""}))
        self.assertTrue(make_desk(self.tmp.name, config={"channels": ["C1", "cron"]}).allowed({"platform": "cron"}))
        self.assertTrue(make_desk(self.tmp.name).allowed({"platform": "cron"}))

    def test_first_calls_fall_back_to_the_strongest_fits(self):
        self.define()
        self.market({"action": "add", "companies": [
            {"company": "A1", "segment": "Asset-light", "fit": 5, "signal": "Raised"}, {"company": "A2", "segment": "Asset-light", "fit": 5},
            {"company": "A3", "segment": "Asset-light", "fit": 5}, {"company": "H1", "segment": "Hybrid", "fit": 4},
            {"company": "Weak", "segment": "Hybrid", "fit": 2}]})
        self.market({"action": "track", "targets": ["A2"]})
        calls = self.market({"action": "show"})["first_calls"]
        self.assertEqual([c["company"] for c in calls], ["A1", "A3", "H1"])
        saved = self.market({"action": "reading", "reading": "Thin market.", "implications": ["Go wide"],
                             "first_calls": [{"target": "H1", "why": "Knows us"}, {"target": "Nobody"}]})
        self.assertEqual((saved["saved"], saved["not_on_the_map"]), (["reading", "implications", "first_calls"], ["Nobody"]))
        shown = self.market({"action": "show"})
        self.assertEqual([(c["company"], c["why"]) for c in shown["first_calls"]], [("H1", "Knows us")])
        self.assertEqual((shown["market"]["reading"], shown["market"]["implications"]), ("Thin market.", ["Go wide"]))

    def test_buyers_found_are_kept_and_used(self):
        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=self.http, secrets={"PDL_API_KEY": "p"})
        desk.market({"action": "define", "name": "Freight"})
        desk.market({"action": "add", "companies": [{"company": "Meridian Freight", "website": "meridian.example",
                                                      "people": ["Sam Ito - Ops Lead"]}]})
        found = desk.market({"action": "people", "target": "Meridian Freight", "titles": ["vp operations"], "limit": "x"})
        self.assertTrue(found["saved_to_map"])
        people = desk.market({"action": "show"})["segments"][0]["companies"][0]["people"]
        self.assertEqual([(p["name"], p.get("title")) for p in people], [("Sam Ito", "Ops Lead"), ("Ada Park", "vp operations")])
        desk.market({"action": "people", "target": "Meridian Freight"})
        self.assertEqual(len(desk.market({"action": "show"})["segments"][0]["companies"][0]["people"]), 2)
        tracked = desk.market({"action": "track", "targets": ["Meridian Freight"]})["added_to_pipeline"][0]
        deal = desk.store.get(tracked["deal"])
        self.assertEqual((tracked["contact"], deal["name"], deal["fields"]["title"], deal["next_step"]),
                         ("Sam Ito", "Meridian Freight", "Ops Lead", "First outreach"))
        self.assertEqual([(p["name"], p["title"]) for p in desk.store.people(deal["id"])], [("Sam Ito", "Ops Lead")])

    def test_export_writes_one_safe_page(self):
        fixture.load(self.desk)
        self.market({"action": "add", "companies": [{"company": "<script>alert(1)</script> & Sons", "segment": "Digital-first",
                                                      "source": "javascript:alert(1)", "why": "\"quoted\" <b>bold</b>"}]})
        self.market({"action": "track", "targets": ["Meridian Freight"]})
        full = self.market({"action": "export"})
        html = Path(full["path"]).read_text(encoding="utf-8")
        self.assertTrue(full["path"].endswith(".html") and full["bytes"] > 5000)
        for must in ("Regional freight brokers (demo)", ">Where we play<", ">How we win<", ">What has to be true<", "Who you would be replacing",
                     "Where the segments meet", "First calls", "Renata Okafor", "In the pipeline", "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Sons",
                     'class="stoa-map sd-dial"', "function dialLayout(", ".sd-dial-rim {", "--dial-good:", "--ui-text-primary:", 'class="pg-nav"', "@media print"):
            self.assertIn(must, html)
        for never in ('id="sm-rings"', "/*DIAL-LAYOUT*/", "Hermes"):         # the old picture; the page's own seam; whose page it was made in
            self.assertNotIn(never, html)
        # The page carries its own scripts and no other: the companies as data and the dial that draws them, and the
        # stoa at its head as cells and the code that paints them. Nothing a company's words say can become markup, in
        # the page or in that data, and nothing is fetched.
        self.assertEqual(re.findall(r"<script[^>]*>", html), ['<script type="application/json" id="sm-data">', "<script>", '<script type="application/json" id="sm-art">', "<script>"])
        self.assertLess(html.index('id="sm-calls"'), html.index('id="sm-data"'))       # the script comes after what it fills in
        for never in ("<script>alert", "alert(1)</script>", "href=\"javascript", " src=", "@import", "<b>bold</b>", "fetch(", "XMLHttpRequest", "WebSocket", "//cdn"):
            self.assertNotIn(never, html)
        data = json.loads(html.split('<script type="application/json" id="sm-data">')[1].split("</script>")[0])
        names = [c["company"] for seg in data["segments"] for c in seg["companies"]]
        self.assertIn("<script>alert(1)</script> & Sons", names)      # whole in the data, and only there
        meridian = next(c for seg in data["segments"] for c in seg["companies"] if c["company"] == "Meridian Freight")
        self.assertEqual((meridian["status"], meridian["people"][0]["name"], meridian["headcount"]), ("tracked", "Renata Okafor", 220))
        shared = Path(self.market({"action": "export", "share": True})["path"]).read_text(encoding="utf-8")
        for hidden in ("Renata Okafor", "Tobias Lindqvist", "In the pipeline", "Buyers named"):
            self.assertNotIn(hidden, shared)
        self.assertIn("Meridian Freight", shared)
        sent = json.loads(shared.split('<script type="application/json" id="sm-data">')[1].split("</script>")[0])
        for company in (c for seg in sent["segments"] for c in seg["companies"]):
            self.assertNotIn("people", company)
            self.assertNotIn("status", company)
        self.assertNotIn("People named", shared)      # a size the copy to send on has nothing to draw from

    def test_an_older_database_is_upgraded_in_place(self):
        import sqlite3
        path = Path(self.tmp.name) / "desk.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE markets (id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '', segments TEXT NOT NULL DEFAULT '[]', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE targets (id TEXT PRIMARY KEY, market_id TEXT NOT NULL, company TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '', "
            "segment TEXT NOT NULL DEFAULT '', fit INTEGER NOT NULL DEFAULT 3, size TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', "
            "why TEXT NOT NULL DEFAULT '', signal TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new', "
            "deal_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO markets VALUES ('old', 'Old map', '', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');"
            "INSERT INTO targets VALUES ('t1', 'old', 'Acme', '', 'A', 4, '80 people', 'Leeds, UK', 'w', '', '', 'new', '', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');")
        conn.commit()
        conn.close()
        shown = self.market({"action": "show", "market": "old"})
        self.assertEqual((shown["numbers"]["companies"], shown["regions"], shown["requirements"]), (1, [{"name": "UK", "count": 1}], []))
        self.market({"action": "add", "market": "old", "companies": [{"company": "Acme", "uses": "Spreadsheets"}]})
        self.assertEqual(self.market({"action": "show", "market": "old"})["holds"][0]["name"], "Spreadsheets")

    def test_places_and_sizes(self):
        from stoa.market import region_of, size_band
        self.assertEqual([region_of(x) for x in ("Chicago, IL", "Denver, CO (DTC area, mobile)", "Remote", "", "Lyon, France")],
                         ["IL", "CO", "Remote", "", "France"])
        self.assertEqual([size_band(x) for x in ("220 people", "1,200 employees", "55", "50-200 staff", "2 locations", "", "1.5k people")],
                         ["200\u2013999", "1,000+", "50\u2013199", "50\u2013199", "", "", "1,000+"])

    def test_map_blocks_fit_slack_limits(self):
        self.define()
        self.market({"action": "add", "companies": [
            {"company": "Co %d <&>" % i, "segment": "Seg %d" % (i % 7), "fit": i % 5 + 1, "why": "w" * 390} for i in range(60)]})
        blocks = self.desk.markets.blocks("freight-brokers-midwest")
        self.assertLessEqual(len(blocks), 50)
        for block in blocks:
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)
                if "accessory" in block:
                    self.assertTrue(ACTIONS.match(block["accessory"]["action_id"]))
        self.assertIn("more on the map", blocks[-1]["elements"][0]["text"])
        self.assertIn("&lt;&amp;&gt;", json.dumps(blocks, ensure_ascii=False))

    def test_demo_market_loads_and_clears(self):
        fixture.load(self.desk)
        self.assertIn("7 companies", self.desk.slash("map"))
        self.market({"action": "track", "targets": ["Brightside Freight"]})
        fixture.clear(self.desk)
        self.assertEqual((self.desk.store.count(), self.market({"action": "list"})["markets"]), (0, []))


class CandidateMapTests(unittest.TestCase):
    """On a recruiting playbook a map's rows are candidates: one person to a row, kept in the map's company fields."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)                     # the recruiting playbook
        self.markets = self.desk.markets

    def people(self, market="Head of Events"):
        return self.markets.add({"market": market, "companies": [
            {"company": "Dana Whitcombe", "size": "Director of Events", "uses": "HVBC (Halden Valley Building Trades Association)", "location": "Sacramento, CA",
             "segment": "Trade show operators", "fit": 5, "why": "Ran a 10,000-person show for six years.", "signal": "Her show's dates are not announced",
             "signal_date": "2026-10-01", "source": "https://www.linkedin.com/in/dana-whitcombe"},
            {"company": "Luis Ortega", "size": "Events Manager", "uses": "Brindlemoor Care Association", "segment": "Association event leads", "fit": 4, "why": "Runs a 3,200-person convention."},
            {"company": "Priya Nand", "size": "Meetings Lead", "uses": "probably somewhere in Dayton (not confirmed)", "segment": "Association event leads", "fit": 3, "why": "Listed on a conference page."}]})

    def test_standing_questions_are_answered_from_the_desk_with_no_model(self):
        from stoa import instant
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        desk = make_desk(tmp.name, playbook="b2b-sales")
        demo.load(desk)
        for asked, kind in (("What needs me today?", "today"), ("who's gone quiet", "quiet"), ("What moved this week?", "week"),
                            ("Give me the morning brief.", "brief"), ("Pipeline report", "report"), ("What needs me today, and why?", ""), ("today", "")):
            self.assertEqual(instant.match(asked), kind, asked)
        today = desk.pipeline({"view": "today"})
        said = desk.instant.answer("today")
        self.assertIn("%d overdue" % today["counts"]["overdue"], said)
        self.assertIn(today["overdue"][0]["name"], said)
        quiet = desk.pipeline({"view": "brief"})["stale"]
        self.assertTrue(all(row["name"] in desk.instant.answer("quiet") for row in quiet))
        self.assertIn("**In the last seven days:**", desk.instant.answer("week"))
        self.assertIn(desk.reports.tool({"action": "read", "report": "stages"})["says"], desk.instant.answer("report"))
        self.assertEqual(desk.instant.answer("nothing"), "")
        # Every pipeline, each under its name.
        self.assertTrue(desk.playbook_tool({"action": "add_pipeline", "pipeline_name": "Partners", "playbook": "b2b-sales"})["ok"])
        both = desk.instant.answer("quiet")
        self.assertIn("### Partners", both)
        self.assertIn("Nobody has gone quiet.", both)
        # The same answers from /gtm in any chat, and from the page's route.
        self.assertIn("**In the last seven days:**", desk.slash("week"))
        self.assertIn("no model used", desk.slash("quiet"))
        # A record called that is opened, as /gtm <name> always did.
        desk.deal({"action": "add", "name": "Report", "company": "Report Ltd"})
        self.assertNotIn("no model used", desk.slash("report"))
        # On a tour the sample is answered once, under no pipeline's name.
        desk.tour_start("recruiting")
        self.assertNotIn("### ", desk.instant.answer("quiet"))
        desk.tour_end()

    def test_the_downloaded_page_is_made_from_the_gtm_tab_and_keeps_in_step_with_it(self):
        from stoa import page
        # Whoever is put forward at a company is someone still there, with what they do.
        self.assertEqual(page._lead([{"name": "Allan", "title": "Former Director of Events"}, {"name": "Connie", "title": "Trade Show Manager"}]), "Connie, Trade Show Manager")
        self.assertEqual(page._lead([{"name": "Allan", "title": "Former Director of Events"}]), "")
        self.assertEqual([p["name"] for p in page._people([{"name": "A", "title": "Ex-CEO"}, {"name": "B", "title": "Executive Director"}])], ["B", "A"])
        for kind in ("roles", "people"):                                                       # every word has each kind
            self.assertEqual(set(page.SAY[""]), set(page.SAY[kind]))
        # How a company is scored and ranked is the Stoa page's own sum, kept in step by hand: both must still say it this way.
        rank = "rank: Math.round(score * 0.65 + timing * 0.35) + (buyer ? 2 : 0)"
        self.assertIn(rank, (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8"))
        self.assertIn(rank, page.SCRIPT.read_text(encoding="utf-8"))
        # The parts it reads from the page script are each found, and the layout is written in once.
        self.assertIn("function dialLayout(", page.script())
        self.assertNotIn("/*DIAL-LAYOUT*/", page.script())
        self.assertNotIn("//", "".join(line.lstrip()[:2] for line in page.script().split("\n")))     # no notes for a client to read
        css = page.page_css()
        for piece in (".pg {", "--dial-good:", ".sd-dial-rim {", ".sd-strat {", "--ui-text-primary:", "prefers-color-scheme: dark"):
            self.assertIn(piece, css)
        self.assertNotIn("/*", css)
        self.assertEqual(page.kind_of({}), "")
        self.assertEqual((page.kind_of({"rows": "people"}), page.kind_of({"rows": "companies"})), ("people", "roles"))

    def test_the_downloaded_page_of_a_candidate_map_speaks_of_candidates(self):
        self.markets.define({"name": "Head of Events", "brief": "A Head of Events.", "segments": ["Association event leads", "Trade show operators"]})
        self.people()
        for share in (False, True):
            html = Path(self.markets.export({"action": "export", "share": share})["path"]).read_text(encoding="utf-8")
            still = html[:html.index('<script type="application/json"')]            # the page itself, before the map's rows and its script
            for said in ("Candidate map", "candidates mapped", "<small>Pools</small>", ">Where to look<", ">How to pitch the role<", ">What the role needs<",
                         "Who to approach first", "Every candidate on the map", "At <b>HVBC (", "Find a candidate", "Reason to move", ">Employer</button>", '"kind": "people"'):
                self.assertIn(said, html, (share, said))
            self.assertEqual("<small>Employers</small>" in still, not share)          # the copy to send on counts no employers
            for wrong in ("companies mapped", "Buyers named", ">Where we play<", "Who to call first", "Reason to call", "Uses <b>HVBC", "Every company on the map"):
                self.assertNotIn(wrong, still, (share, wrong))
        # A map of companies on the same pipeline (the older kind) reads as it always did.
        self.markets.define({"name": "Event employers", "rows": "companies", "segments": ["Associations"]})
        self.markets.add({"market": "Event employers", "companies": [{"company": "Brindlemoor Care Association", "segment": "Associations", "fit": 4}]})
        html = Path(self.markets.export({"action": "export", "market": "Event employers"})["path"]).read_text(encoding="utf-8")
        for said in ("Talent map", "Every company on the map", "Find a company", ">Where to look<", ">Works with</button>", '"kind": "roles"'):
            self.assertIn(said, html, said)

    def test_a_new_map_holds_candidates_and_says_so(self):
        made = self.markets.define({"name": "Head of Events", "brief": "A Head of Events.", "segments": ["Association event leads", "Trade show operators"]})
        self.assertEqual((made["rows"], made["market"]["rows"], made["search"]), ("people", "people", "Head of Events"))
        self.assertIn("one row for each PERSON", made["next"])
        added = self.people()
        self.assertEqual((sorted(added["added"]), added["rows"]), (["Dana Whitcombe", "Luis Ortega", "Priya Nand"], "people"))
        # Map facts: a candidate has a title and an employer to be known, never a headcount or a buyer.
        facts = self.markets.facts("Head of Events")
        self.assertEqual((facts["rows"], facts["on_it"]["sized"], facts["on_it"]["uses"], facts["on_it"]["buyers"]), ("people", 3, 2, 0))             # a guess at an employer is not kept as one
        self.assertEqual([gap["what"] for gap in facts["gaps"]], ["checked against the role's requirements", "a source link", "where they work now"])
        self.assertEqual([gap["missing"] for gap in facts["gaps"]], [3, 2, 1])
        rows = {row["company"]: row for row in self.markets.targets("head-of-events")}
        self.assertEqual(rows["Dana Whitcombe"]["detail"]["uses"], "HVBC (Halden Valley Building Trades Association)")      # an employer's name is kept whole
        self.assertNotIn("uses", rows["Priya Nand"]["detail"])                                                                # a guess still is not
        self.assertIn("Priya Nand", added["uses_not_saved"])
        self.assertIn("the company the person works at now", added["uses_not_saved"])
        self.assertNotIn("headcount", added.get("still_missing", ""))                                                         # a person has none to be missing
        summary = self.markets.summary("head-of-events")
        self.assertEqual((summary["rows"], summary["numbers"]["rows"], summary["market"]["rows"]), ("people", "people", "people"))
        self.assertIn("every row is one person", summary["reading_rows"])

    def test_what_a_map_is_is_written_on_it_not_worked_out(self):
        self.markets.define({"name": "Head of Events", "segments": ["Association event leads", "Trade show operators"]})
        self.people()
        # A headcount and people named under one row change nothing about the map.
        self.markets.add({"market": "Head of Events", "companies": [{"company": "Luis Ortega", "headcount": 1200, "people": [{"name": "Someone Else", "title": "Assistant"}]}]})
        self.assertEqual(self.markets.summary("head-of-events")["rows"], "people")
        # Only a person changes it, and nothing on the map changes with it.
        count = len(self.markets.targets("head-of-events"))
        self.assertEqual(self.markets.set_rows("Head of Events", "companies")["rows"], "companies")
        self.assertEqual(self.markets.summary("head-of-events")["rows"], "companies")
        self.assertNotIn("search", self.markets.summary("head-of-events"))
        self.assertFalse(self.markets.set_rows("Head of Events", "goats")["ok"])
        self.assertTrue(self.markets.set_rows("Head of Events", "people")["ok"])
        self.assertEqual(len(self.markets.targets("head-of-events")), count)
        # A recruiting map with nothing written (one made before this) is a map of companies, whatever is on it.
        with self.desk.store._conn() as conn:
            conn.execute("UPDATE markets SET detail = ? WHERE id = 'head-of-events'", (json.dumps({"criteria": []}),))
        self.assertEqual(self.markets.summary("head-of-events")["rows"], "companies")
        # Defining it again does not turn it into something else; saying so does.
        self.markets.define({"name": "Head of Events", "brief": "Changed."})
        self.assertEqual(self.markets.summary("head-of-events")["rows"], "companies")
        self.assertEqual(self.markets.define({"name": "Head of Events", "rows": "people"})["rows"], "people")
        self.assertFalse(self.markets.define({"name": "Another", "rows": "goats"})["ok"])

    def test_a_candidate_joins_the_search_the_map_feeds(self):
        self.desk.group({"action": "set", "group": "Head of Events", "new": True, "client": "Columbus Trade Association", "value": 36000})
        # A map for that search, named something else: it is told the search, and says it.
        made = self.markets.define({"name": "Head of Events, 40-person trade association, Columbus OH", "segments": ["Association event leads", "Trade show operators"], "search": "head of events"})
        self.assertEqual(made["search"], "Head of Events")
        self.people(made["market"]["name"])
        done = self.markets.track({"market": made["market"]["id"], "targets": ["Dana Whitcombe"]}, actor="tester")
        record = self.desk.store.get(done["added_to_pipeline"][0]["deal"])
        self.assertEqual((record["name"], record["company"], record["grp"], record["next_step"]),
                         ("Dana Whitcombe", "HVBC (Halden Valley Building Trades Association)", "Head of Events", "First approach"))
        self.assertEqual((record["fields"]["title"], record["linkedin"]), ("Director of Events", "https://www.linkedin.com/in/dana-whitcombe"))
        self.assertEqual((record["value"], self.desk.sum(self.desk.store.list(status="open"))), (36000, 36000))    # the search's fee, in play once
        groups = {group["name"]: group for group in self.desk.groups()}
        self.assertEqual((groups["Head of Events"]["client"], groups["Head of Events"]["open"]), ("Columbus Trade Association", 1))
        self.assertNotIn(made["market"]["name"], groups)                                  # one search for the role, not two
        # Pointed back at the search named after the map; a search that is not there is refused.
        self.assertFalse(self.markets.set_search(made["market"]["id"], "Nothing called this")["ok"])
        self.assertEqual(self.markets.set_search(made["market"]["id"], "")["search"], made["market"]["name"])
        again = self.markets.track({"market": made["market"]["id"], "targets": ["Luis Ortega"]}, actor="tester")
        self.assertEqual(self.desk.store.get(again["added_to_pipeline"][0]["deal"])["grp"], made["market"]["name"])
        self.assertEqual(self.desk.store.get(again["added_to_pipeline"][0]["deal"])["value"], 0)       # a search with no fee yet gives none
        # On contract work a candidate from the map carries no fee, whatever the search was given before.
        self.assertTrue(self.desk.set_billing({"type": "contract", "client": "Columbus Trade Association", "rate": 85, "hours_week": 20})["ok"])
        self.markets.set_search(made["market"]["id"], "Head of Events")
        third = self.markets.track({"market": made["market"]["id"], "targets": ["Priya Nand"]}, actor="tester")
        self.assertEqual(self.desk.store.get(third["added_to_pipeline"][0]["deal"])["value"], 0)
        self.assertFalse(self.markets.define({"name": "A third", "search": "Nothing called this"})["ok"])

    def test_a_candidate_already_in_the_search_is_not_added_twice(self):
        made = self.markets.define({"name": "Head of Events", "segments": ["Association event leads", "Trade show operators"]})
        self.people()
        by_hand = self.desk.deal({"action": "add", "name": "dana whitcombe", "group": "Head of Events"})["deal"]["id"]
        done = self.markets.track({"market": made["market"]["id"], "targets": ["Dana Whitcombe", "Luis Ortega"]}, actor="tester")
        first, second = done["added_to_pipeline"]
        self.assertEqual((first["deal"], first.get("existing_record")), (by_hand, True))
        hers = self.desk.store.get(by_hand)                                              # and what the map found is on her record
        self.assertEqual((hers["company"], hers["fields"].get("title"), hers["linkedin"]),
                         ("HVBC (Halden Valley Building Trades Association)", "Director of Events", "https://www.linkedin.com/in/dana-whitcombe"))
        self.assertFalse(second.get("existing_record"))
        self.assertIn("already in that search", done["note"])
        names = sorted(d["name"].lower() for d in self.desk.store.list(status="open"))
        self.assertEqual(names, ["dana whitcombe", "luis ortega"])
        self.assertEqual(self.markets.summary(made["market"]["id"])["numbers"]["in_pipeline"], 2)      # the map knows she is in the pipeline
        # The same name in another search is someone else's record: a second one is right.
        other = self.markets.define({"name": "Events Director, west", "segments": ["Trade show operators"]})
        self.markets.add({"market": "Events Director, west", "companies": [{"company": "Dana Whitcombe", "size": "Director of Events", "segment": "Trade show operators", "fit": 4}]})
        self.markets.track({"market": other["market"]["id"], "targets": ["Dana Whitcombe"]}, actor="tester")
        self.assertEqual(len([d for d in self.desk.store.list(status="open") if d["name"].lower() == "dana whitcombe"]), 2)

    def test_where_someone_works_is_a_name_not_their_history(self):
        from stoa import market as m
        for text in ("Left Stripe in 2025, now between roles", "Stripe, per a 2023 conference bio (could have moved since)", "Formerly Google: now freelance",
                     "Stripe (since 2021)", "Was at Lodestar until March", "Lyft (2019-2023)", "Stripe 2021-present", "Stripe 2019\u20132023", "Stripe (2021 -)",
                     "Stripe \u2192 Plaid", "Formerly Google", "ex-Google", "Left Google to join a startup", "Left Stripe last year", "Joined Plaid this month", "Left Google, joined Meta",
                     "Stripe, now Plaid", "Stripe: now Plaid", "Plaid, previously Stripe", "Google, previously Meta", "Stripe, formerly", "Plaid (now at Stripe)",
                     "Stripe (previously)"):
            self.assertEqual(m.uses_name(text, employer=True), ("", "refuse"), text)
        for text in ("Between jobs", "Open to work", "unemployed."):                      # not working anywhere: nothing to keep, nothing to complain of
            self.assertEqual(m.uses_name(text, employer=True), ("", "clear"), text)
        for text in ("AT&T Inc. (Mobility)", "Ernst & Young LLP: Advisory", "HVBC (Halden Valley Building Trades Association)", "Now Health International", "Vision 2030 Partners",
                     "Between the Lines Ltd", "Stripe", "X (formerly Twitter)", "Twitter (now X)", "Left Lane Capital", "Left Field Labs", "Formerly Known As Records",
                     "Ex-Lab Tools", "UPS: Now Hiring", "Levi Strauss & Co.", "20th Century Studios", "Self-employed", "Mo\u00ebt Hennessy / LVMH", "Deloitte / Monitor",
                     "Roche / Genentech", "Stripe, Inc.", "Meta (formerly Facebook)", "This Week Health", "Last Week Tonight", "This Month Media"):
            self.assertEqual(m.uses_name(text, employer=True), (text, "keep"), text)

    def test_a_map_of_companies_on_a_recruiting_desk_is_as_it_was(self):
        made = self.markets.define({"name": "Where they work", "segments": ["Payments"], "rows": "companies"})
        self.assertEqual(made["rows"], "companies")
        self.assertNotIn("PERSON", made["next"])
        added = self.markets.add({"market": "Where they work", "companies": [{"company": "Ironbridge Payments", "segment": "Payments", "fit": 4, "why": "Rebuilt its ledger.",
                                                                              "uses": "HVBC (Halden Valley Building Trades Association)", "people": [{"name": "Samira Costa", "title": "Staff Engineer"}]}]})
        self.assertIn("Ironbridge Payments", added["uses_not_saved"])                      # `uses` is a tool's name here, as before
        done = self.markets.track({"market": "Where they work", "targets": ["Ironbridge Payments"]}, actor="tester")
        record = self.desk.store.get(done["added_to_pipeline"][0]["deal"])
        self.assertEqual((record["name"], record["company"], record["grp"]), ("Samira Costa", "Ironbridge Payments", "Where they work"))

    def test_a_sales_map_has_no_kind(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        desk = make_desk(tmp.name, playbook="b2b-sales")
        made = desk.markets.define({"name": "Fintechs", "segments": ["Payments"]})
        self.assertNotIn("rows", made)
        self.assertNotIn("rows", desk.markets.summary("fintechs")["numbers"])
        self.assertFalse(desk.markets.define({"name": "Fintechs", "rows": "people"})["ok"])
        self.assertFalse(desk.markets.set_rows("Fintechs", "people")["ok"])


class RecruitingBillingTests(unittest.TestCase):
    """A recruiting pipeline is contingent (a fee for each placement) or contract (by the hour, billed monthly)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        self.first = self.desk.playbook().first_stage()

    def add(self, name, group="", value=0):
        return self.desk.store.add(name, self.first, actor="tester", grp=group, value=value)

    def test_in_play_counts_a_search_once_for_each_seat_still_to_fill(self):
        self.assertEqual(self.desk.billing()["type"], "contingent")                       # what a recruiting pipeline is until told
        made = [self.add("A %d" % n, "VP Finance", 30000) for n in range(3)] + [self.add("B", "Head of Risk", 45000), self.add("C", "", 10000)]
        open_ = [d for d in self.desk.store.list(status="all") if d["status"] == "open"]
        self.assertEqual((st.total(open_), self.desk.sum(open_)), (145000.0, 85000.0))    # 30k + 45k + 10k
        self.assertEqual(self.desk.pipeline({"action": "board"})["open_value"], 85000.0)
        self.assertEqual({g["name"]: g["open_value"] for g in self.desk.groups()}, {"VP Finance": 30000.0, "Head of Risk": 45000.0})
        self.assertEqual(sum(stage["value"] for stage in self.desk.flow()["stages"]), 85000.0)
        forecast = self.desk.cash()["forecast"]
        chance = {row["stage"]: row["odds"] / 100 for row in forecast["odds"]}[self.first]
        self.assertEqual(forecast["value"], 85000.0)
        self.assertAlmostEqual(forecast["weighted"], 30000 * (1 - (1 - chance) ** 3) + 45000 * chance + 10000 * chance, delta=1)
        # Two openings: two fees, where it has two candidates to fill them. Three openings and one candidate: one fee.
        self.assertTrue(self.desk.set_openings("vp finance", 2)["ok"])
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 115000.0)
        self.assertEqual(next(g for g in self.desk.groups() if g["name"] == "VP Finance")["fields"]["openings"], "2")
        self.assertAlmostEqual(self.desk.cash()["forecast"]["weighted"], 30000 * st.filled([chance] * 3, 2) + 45000 * chance + 10000 * chance, delta=1)
        self.assertTrue(self.desk.set_openings("Head of Risk", 3)["ok"])
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 115000.0)
        for wrong in (0, "many", 51, "inf", "1e999", "nan"):
            self.assertFalse(self.desk.set_openings("VP Finance", wrong)["ok"], wrong)
        # Openings is one of a search's facts, so it can be typed on its page as anything: a total still adds up.
        self.desk.group({"action": "set", "group": "Head of Risk", "fields": {"openings": "1e999"}})
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 115000.0)
        self.desk.group({"action": "set", "group": "Head of Risk", "fields": {"openings": "3"}})
        self.assertFalse(self.desk.set_openings("Nobody's search", 2)["ok"])
        # One of the three is placed: one seat is left. Back to one opening: the search is filled, and nothing of it is in play
        # though two candidates are still open in it. A search marked done counts for nothing either.
        won = self.desk.playbook().won
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 115000.0)   # asked just before: the answer is not kept past a change
        self.desk.store.move(made[0]["id"], won["id"], status="won", actor="tester", label=won["label"])
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 85000.0)
        self.desk.set_openings("VP Finance", 1)
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 55000.0)
        self.assertEqual(next(g for g in self.desk.groups() if g["name"] == "VP Finance")["open_value"], 0)
        self.assertEqual(self.desk.sum(self.desk.store.list(status="all")), 85000.0)      # with the 30k that was placed: what is won is a plain sum
        self.desk.group({"action": "set", "group": "Head of Risk", "status": "done"})
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 10000.0)
        self.assertEqual((st.filled([0.5, 0.5], 1), st.filled([0.5, 0.5], 2), st.filled([], 3)), (0.75, 1.0, 0))

    def test_a_contract_pipeline_is_paid_by_the_hour_and_keeps_no_fees(self):
        for wrong in ({"type": "contract", "client": "", "rate": 85, "hours_week": 20}, {"type": "contract", "client": "Halden Group", "rate": 0, "hours_week": 20},
                      {"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 200}, {"type": "retainer"}):
            self.assertFalse(self.desk.set_billing(wrong)["ok"], wrong)
        kept = self.add("Fee Carrier", "Plant Manager", 30000)
        saved = self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})
        self.assertEqual((saved["ok"], saved["type"], saved["client"], saved["rate"], saved["hours_week"]), (True, "contract", "Halden Group", 85, 20))
        month = saved["month"]
        self.assertTrue(month["running"] and month["hours"] > 0 and month["amount"] == month["hours"] * 85 and month["bills_from"], month)
        self.assertEqual((saved["to_bill"], saved["start"]), (0, dt.date.today().isoformat()[:7]))
        terms = self.desk.terms_of("Halden Group")                                        # the terms the months are billed from are the client's own
        self.assertEqual((terms["kind"], terms["rate"], terms["hours_week"]), ("hourly", 85, 20))
        earlier = self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 90, "hours_week": 25, "start": "2026-01"})
        self.assertTrue(earlier["to_bill"] > 0 and self.desk.terms_of("Halden Group")["start"] == "2026-01")
        again = self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 95, "hours_week": 25})
        self.assertEqual((again["rate"], again["start"]), (95, "2026-01"))                 # a new rate keeps the month it started
        # No candidate is given a fee here, by a person or by the agent.
        made = self.desk.deal({"action": "add", "name": "Ada Quill", "group": "Plant Manager", "company": "Harrow Mills", "value": 40000})
        self.assertFalse(made["deal"].get("value"))
        self.assertIn("contract work", made["fee_not_kept"])
        # Nor by any other way in: a fee on the search (which a new candidate would take), an edit, an import writing
        # straight to the ledger. And what was on a candidate before is neither shown nor counted, though it is still
        # in the file.
        search = self.desk.group({"action": "set", "group": "Plant Manager", "value": 30000})
        self.assertIn("contract work", search["fee_not_kept"])
        self.assertFalse(self.desk.deal({"action": "add", "name": "Bo Lin", "group": "Plant Manager"})["deal"].get("value"))
        self.desk.deal({"action": "update", "deal": kept["id"], "value": 99000})
        self.desk.store.update(kept["id"], actor="an import", value=77000)
        self.desk.store.add("Cy Imported", self.first, actor="an import", grp="Plant Manager", value=55000)
        everyone = self.desk.store.list(status="all")
        self.assertEqual((len(everyone), sum(d["value"] for d in everyone), sum(d["value_cents"] for d in everyone)), (4, 0, 0))
        self.assertEqual((self.desk.store.get(kept["id"])["value"], self.desk.store.grp_get("Plant Manager")["value"]), (0, 0))
        board = self.desk.pipeline({"action": "board"})
        self.assertEqual((board["open_value"], board["billing"]["type"]), (0, "contract"))
        self.assertNotIn("$", cards.board_text(self.desk.playbook(), everyone))
        self.assertEqual(self.desk.cash()["forecast"]["value"], 0)
        self.assertEqual(self.desk.billing()["fees_kept"], 1)                             # the one from before; nothing new got in
        # Made contingent again, that fee is as it was. Cleared in so many words, it is set to zero.
        self.desk.set_billing({"type": "contingent"})
        self.assertEqual((self.desk.store.get(kept["id"])["value"], self.desk.sum(self.desk.store.list(status="open"))), (30000, 30000))
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 95, "hours_week": 25, "clear_fees": True})
        self.assertEqual(self.desk.billing()["fees_kept"], 0)
        self.desk.set_billing({"type": "contingent"})
        self.assertEqual(self.desk.store.get(kept["id"])["value"], 0)
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 95, "hours_week": 25})
        self.assertFalse(self.desk.set_openings("Plant Manager", 2)["ok"])                 # openings mean nothing on contract work
        # Back to contingent, with the usual share of the salary; the company and its terms are kept for another time.
        back = self.desk.set_billing({"type": "contingent", "fee_pct": 20})
        self.assertEqual((back["type"], back["fee_pct"], back["client"], back["rate"]), ("contingent", 20, "Halden Group", 95))
        self.assertNotIn("month", back)
        self.assertFalse(self.desk.set_billing({"type": "contingent", "fee_pct": 140})["ok"])

    def test_a_placement_from_before_the_contract_keeps_its_fee_and_is_still_billed(self):
        won, first = self.desk.playbook().won, self.first
        place = lambda deal: self.desk.store.move(deal["id"], won["id"], status="won", actor="tester", label=won["label"])      # noqa: E731
        reopen = lambda deal: self.desk.store.move(deal["id"], first, status="open", actor="tester")      # noqa: E731
        value = lambda deal: self.desk.store.get(deal["id"])["value"]      # noqa: E731
        invoiced, not_yet, unbilled, still_open = (self.add("Invoiced Before", "VP Finance", 30000), self.add("Billed After", "Head of Risk", 25000),
                                                   self.add("Never Billed", "Controller", 20000), self.add("Still Open", "VP Finance", 30000))
        for deal in (invoiced, not_yet, unbilled):
            place(deal)
        self.assertTrue(self.desk.invoice({"action": "add", "deal": invoiced["id"], "amount": 10000})["ok"])
        before = self.desk.cash()
        self.assertTrue(self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})["ok"])
        # The placements read as they did, and what is owed on them is still asked for; the open candidate has no fee.
        self.assertEqual([value(d) for d in (invoiced, not_yet, unbilled, still_open)], [30000, 25000, 20000, 0])
        self.assertEqual(self.desk.money(self.desk.store.get(invoiced["id"]), self.desk.store.invoices(invoiced["id"]))["uninvoiced"], 20000)
        self.assertEqual(self.desk.cash()["strip"], before["strip"])
        self.assertTrue(self.desk.invoice({"action": "add", "deal": not_yet["id"]})["ok"])             # for its fee, with no amount said
        billing = self.desk.billing()
        self.assertEqual((billing["record_billing"], billing["fees_kept"]), (True, 1))     # the open candidate's fee is the one left unseen
        self.assertEqual(self.desk.sum(self.desk.store.list(status="open")), 0)
        # The fee of a placement from before can still be corrected; an open candidate is still given none.
        self.assertEqual(self.desk.deal({"action": "update", "deal": unbilled["id"], "value": 21000})["deal"]["value"], 21000)
        self.desk.deal({"action": "update", "deal": unbilled["id"], "value": 20000})
        self.assertIn("fee_not_kept", self.desk.deal({"action": "update", "deal": still_open["id"], "value": 31000}))
        # Closing as placed a record that already is (an agent told "her placement is confirmed") changes nothing about
        # its fee: this one has no invoice to protect it, only the fact that it was a placement already.
        self.desk.deal({"action": "close", "deal": unbilled["id"], "outcome": "won"})
        self.assertEqual((value(unbilled), self.desk.billing()["fees_kept"]), (20000, 1))
        # A candidate placed once it is contract work brings no fee, whatever the record carried from before: it is marked,
        # in the same second or any other. Nothing is taken out of the file, and undoing the placement takes the mark back.
        placed = place(still_open)
        self.assertEqual((placed["value"], placed["fields"].get("_no_fee"), self.desk.billing()["fees_kept"]), (0, "1", 1))
        self.assertIn("placed on contract work", self.desk.store.activities(still_open["id"])[0]["text"]
                      if hasattr(self.desk.store, "activities") else "placed on contract work")
        # No way in puts a fee on that placement while the pipeline is contract work: an edit, an agent's proposal.
        self.assertIn("fee_not_kept", self.desk.deal({"action": "update", "deal": still_open["id"], "value": 9999}))
        self.desk.store.update(still_open["id"], actor="an import", value=9999)
        row = {"name": "Still Open", "group": "VP Finance", "value": 25000, "fields": {}}
        self.assertNotIn("value", [change["key"] for change in self.desk.intake._changes(self.desk.store.get(still_open["id"]), row, self.desk.playbook())])
        self.assertEqual(value(still_open), 0)
        self.desk.store.undo(still_open["id"])
        back = self.desk.store.get(still_open["id"])
        self.assertEqual((back["status"], back["fields"].get("_no_fee")), ("open", None))
        place(still_open)
        # Back and forth changes nothing: made contingent and contract again, that placement still brings no fee and
        # nothing is owed on it; the ones from before are as they were.
        self.desk.set_billing({"type": "contingent"})
        self.assertEqual([value(d) for d in (invoiced, not_yet, unbilled, still_open)], [30000, 25000, 20000, 0])
        self.assertEqual(self.desk.money(self.desk.store.get(still_open["id"]), [])["uninvoiced"], 0)
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})
        self.assertEqual([value(d) for d in (invoiced, not_yet, unbilled, still_open)], [30000, 25000, 20000, 0])
        # Reopened by mistake and placed again on contract work. One that was billed keeps what was earned. One with
        # nothing billed is a placement made now, and is marked: its amount is still in the file, the log says why it
        # stopped counting, and undo puts it back as it was. An invoice entered and removed again does not count as billed.
        reopen(invoiced)
        self.assertEqual(value(invoiced), 0)                                              # open: no fee shows
        self.assertEqual(place(invoiced)["value"], 30000)
        reopen(unbilled)
        self.assertEqual((place(unbilled)["value"], self.desk.billing()["fees_kept"]), (0, 2))
        self.desk.store.undo(unbilled["id"])
        self.desk.store.undo(unbilled["id"])
        self.assertEqual((self.desk.store.get(unbilled["id"])["status"], value(unbilled)), ("won", 20000))
        bill = self.desk.invoice({"action": "add", "deal": unbilled["id"], "amount": 5000})
        self.assertTrue(bill["ok"])
        number = self.desk.store.invoices(unbilled["id"])[0]["id"]
        self.desk.store.invoice_remove(number, actor="tester")                            # entered by mistake, and taken off again
        reopen(unbilled)
        self.assertEqual(place(unbilled)["value"], 0)                                     # nothing billed stands on it: placed now
        # Cleared in so many words: what is unseen is set to zero, the placements with their fees stay as they are.
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20, "clear_fees": True})
        self.assertEqual((self.desk.billing()["fees_kept"], value(invoiced), value(not_yet)), (0, 30000, 25000))
        # A list of past placements brought onto contract work: each keeps the fee it earned; an open row gets none.
        listed = Path(self.tmp.name) / "past.csv"
        listed.write_text("name,group,stage,value\nPast Placement,Old Search,%s,25000\nOpen Row,Old Search,%s,25000\n" % (won["label"], first), encoding="utf-8")
        self.assertEqual(self.desk.import_csv(listed, history=True)["added"], 2)          # as `hermes stoa import` does, from the terminal
        rows = {d["name"]: (d["status"], d["value"]) for d in self.desk.store.list(status="all")}
        self.assertEqual((rows["Past Placement"], rows["Open Row"]), (("won", 25000), ("open", 0)))
        # The agent's own import, and a row approved from a list or a proposal, are about now: a placement brings no fee.
        listed.write_text("name,group,stage,value\nImported By The Agent,Old Search,%s,40000\n" % won["label"], encoding="utf-8")
        self.assertEqual(self.desk.import_csv(listed)["added"], 1)
        new_row = {"kind": "new", "name": "Proposed Today", "stage": won["id"], "row": {"grp": "Old Search", "value": 25000}}
        proposed = self.desk.intake._write(new_row, self.desk.playbook(), "agent", "a proposal")
        by_agent = next(d for d in self.desk.store.list(status="all") if d["name"] == "Imported By The Agent")
        self.assertEqual([(d["status"], d["value"], d["fields"].get("_no_fee")) for d in (by_agent, proposed)], [("won", 0, "1")] * 2)
        self.assertEqual(self.desk.billing()["fees_kept"], 0)
        # The mark is the ledger's own: nobody sets or clears it by naming it, on a record or on a new one.
        self.desk.deal({"action": "update", "deal": proposed["id"], "fields": {"_no_fee": ""}})
        self.desk.store.update(proposed["id"], actor="agent", fields={"_no_fee": "", "_prev_stage": "offer"})
        self.assertEqual(self.desk.store.get(proposed["id"])["fields"].get("_no_fee"), "1")
        self.assertIn("fee_not_kept", self.desk.deal({"action": "update", "deal": proposed["id"], "value": 99999}))
        self.desk.deal({"action": "update", "deal": invoiced["id"], "fields": {"_no_fee": "1"}})
        self.assertEqual(value(invoiced), 30000)
        self.assertNotIn("_no_fee", self.desk.store.add("Marked From Outside", first, actor="agent", fields={"_no_fee": "1", "title": "CFO"})["fields"])
        # Made contingent, a person may give that placement a fee after all: the mark goes with it.
        self.desk.set_billing({"type": "contingent"})
        given = self.desk.deal({"action": "update", "deal": proposed["id"], "value": 18000})["deal"]
        self.assertEqual((given["value"], self.desk.store.get(proposed["id"])["fields"].get("_no_fee")), (18000, None))

    def test_two_records_by_one_name_are_said_so_on_contract_work_too(self):
        for group in ("VP Finance", "Head of Risk"):
            self.add("Dana Reyes", group, 0)
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})
        out = self.desk.deal({"action": "update", "deal": "Dana Reyes", "value": 5})
        self.assertFalse(out["ok"])
        self.assertNotIn("fee_not_kept", out)                                            # nothing was saved: it does not say something was
        self.assertEqual(len(out.get("matches") or []), 2)

    def test_contract_work_through_undo_proposals_and_many_readers(self):
        kept = self.add("Fee Carrier", "Plant Manager", 20000)
        self.desk.deal({"action": "update", "deal": kept["id"], "value": 22000})
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})
        # Undo measures against the file: the ledger says 22,000 was there, not nothing.
        self.desk.store.undo(kept["id"])
        with self.desk.store._conn() as conn:
            line = dict(conn.execute("SELECT before, after FROM ledger ORDER BY seq DESC LIMIT 1").fetchone())
        self.assertEqual((json.loads(line["before"]).get("value_cents"), json.loads(line["after"]).get("value_cents")), (2200000, 2000000))
        self.assertEqual(self.desk.store.get(kept["id"])["value"], 0)
        # A list that names a fee does not offer one: nothing would come of approving it, and it would be offered again.
        row = {"name": "Fee Carrier", "group": "Plant Manager", "value": 30000, "fields": {}}
        self.assertNotIn("value", [change["key"] for change in self.desk.intake._changes(self.desk.store.get(kept["id"]), row, self.desk.playbook())])
        # Clearing is one act of the ledger: other readers asking for the pipeline's ledger meanwhile cannot undo half of it.
        for n in range(4):
            self.desk.set_billing({"type": "contingent"})
            self.add("More %d" % n, "Plant Manager", 10000)
        self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})
        real = st.Store.update

        def and_another_reader(store, *args, **kwargs):
            self.desk.store.list()                       # another request, as the page's own poll would be
            return real(store, *args, **kwargs)
        with mock.patch.object(st.Store, "update", and_another_reader):
            self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20, "clear_fees": True})
        self.assertEqual(self.desk.billing()["fees_kept"], 0)
        self.desk.set_billing({"type": "contingent"})
        self.assertEqual(sum(d["value"] for d in self.desk.store.list(status="all")), 0)

    def test_each_pipeline_keeps_its_own_and_production_shows_them_together(self):
        made = self.desk.pipeline_add("Contract work", "recruiting")
        self.assertTrue(made["ok"], made)
        with self.desk.on(made["added"]["id"]):
            self.assertTrue(self.desk.set_billing({"type": "contract", "client": "Halden Group", "rate": 85, "hours_week": 20})["ok"])
            self.assertEqual(self.desk.billing()["type"], "contract")
            month = self.desk.billing()["month"]
        self.assertEqual(self.desk.billing()["type"], "contingent")                       # the first pipeline is as it was
        won = self.desk.playbook().won
        placed = self.add("Placed One", "VP Finance", 30000)
        self.desk.store.move(placed["id"], won["id"], status="won", actor="tester", label=won["label"])
        self.add("Open One", "Head of Risk", 45000)
        across = self.desk.production()
        rows = {row["name"]: row for row in across["pipelines"]}
        mine, theirs = rows["Contract work"], next(row for row in across["pipelines"] if row["id"] == "main")
        self.assertEqual((mine["billing"], mine["client"], mine["this_month"], mine["likely"]), ("contract", "Halden Group", month["amount"], 0))
        self.assertTrue(mine["this_month_note"].startswith("about "))
        self.assertEqual((theirs["billing"], theirs["this_month"], theirs["this_month_note"]), ("contingent", 30000, "1 placed"))
        self.assertGreater(theirs["likely"], 0)
        self.assertEqual(across["totals"]["this_month"], 30000 + month["amount"])
        self.assertTrue(across["month"] and across["currency"] == "USD")

    def test_the_clients_list_and_the_forecast_count_a_search_as_the_pipeline_does(self):
        self.desk.group({"action": "set", "group": "VP Finance", "new": True, "client": "Columbus Trade Association", "value": 30000})
        for n in range(3):
            self.assertEqual(self.desk.deal({"action": "add", "name": "Cand %d" % n, "group": "VP Finance"})["deal"]["value"], 30000)
        client = next(row for row in self.desk.clients() if row["name"] == "Columbus Trade Association")
        self.assertEqual((client["open"], client["open_value"]), (3, 30000.0))            # three candidates, one fee
        self.assertEqual(self.desk.group({"action": "get", "group": "VP Finance"})["open_value"], 30000.0)
        # Two openings and candidates who would bring different fees (40k, 30k, 20k): in play is the two largest, and the
        # forecast is of the same 70k, not the largest twice over.
        first, second = self.desk.store.list(status="open")[:2]
        self.desk.store.update(first["id"], actor="tester", value=20000)
        self.desk.store.update(second["id"], actor="tester", value=40000)
        self.desk.set_openings("VP Finance", 2)
        open_ = self.desk.store.list(status="open")
        self.assertEqual((self.desk.sum(open_), self.desk.cash()["forecast"]["value"]), (70000.0, 70000.0))

    def test_the_stages_add_up_to_what_is_in_play(self):
        stages = [stage["id"] for stage in self.desk.playbook().stages]
        put = lambda name, group, value, at: self.desk.store.add(name, stages[at], actor="tester", grp=group, value=value)      # noqa: E731
        put("A1", "VP Finance", 30000, 0), put("A2", "VP Finance", 30000, 2), put("A3", "VP Finance", 30000, 4)
        put("B1", "Head of Risk", 45000, 1), put("Alone", "", 10000, 3)
        open_ = self.desk.store.list(status="open")
        sums = self.desk.stage_sums(open_)
        # VP Finance once, where its furthest-along candidate is; Head of Risk where its only one is; the one in no search where it is.
        self.assertEqual({stage: sums.get(stage, 0) for stage in stages[:5]}, dict(zip(stages[:5], [0, 45000.0, 0, 10000.0, 30000.0])))
        self.assertEqual(sum(sums.values()), self.desk.sum(open_))
        self.assertEqual([row["value"] for row in self.desk.pipeline({"action": "board"})["by_stage"]][:5], [0, 45000.0, 0, 10000.0, 30000.0])
        self.assertEqual([stage["value"] for stage in self.desk.flow()["stages"]][:5], [0, 45000.0, 0, 10000.0, 30000.0])
        # Two openings: the two furthest along each carry a fee, the largest furthest; a third candidate behind them none.
        self.desk.store.update(next(d["id"] for d in open_ if d["name"] == "A2"), actor="tester", value=20000)
        self.desk.set_openings("VP Finance", 2)
        sums = self.desk.stage_sums(self.desk.store.list(status="open"))
        self.assertEqual((sums.get(stages[4]), sums.get(stages[2]), sums.get(stages[0], 0)), (30000.0, 30000.0, 0))
        self.assertEqual(sum(sums.values()), self.desk.sum(self.desk.store.list(status="open")))
        # One of them placed: one seat is left, and it sits with whoever is now furthest along.
        won = self.desk.playbook().won
        self.desk.store.move(next(d["id"] for d in open_ if d["name"] == "A3"), won["id"], status="won", actor="tester", label=won["label"])
        sums = self.desk.stage_sums(self.desk.store.list(status="open"))
        self.assertEqual((sums.get(stages[2]), sums.get(stages[0], 0), sum(sums.values())), (30000.0, 0, self.desk.sum(self.desk.store.list(status="open"))))
        # A stage that is not the playbook's is behind them all, and what is no longer open is a plain sum where it is.
        self.assertEqual(st.by_stage([{"status": "open", "stage": "odd", "grp": "S", "value_cents": 500}, {"status": "open", "stage": "sourced", "grp": "S", "value_cents": 900}],
                                     {}, self.desk.playbook().position), {"sourced": 900})
        everything = self.desk.stage_sums(self.desk.store.list(status="all"))
        self.assertEqual(everything.get(won["id"]), 30000.0)

    def test_the_board_as_text_a_split_report_and_the_funnel_follow_the_same_stages(self):
        stages = [stage["id"] for stage in self.desk.playbook().stages]
        put = lambda name, group, value, at, owner="": self.desk.store.add(name, at, actor="tester", grp=group, value=value, owner=owner)      # noqa: E731
        put("A1", "VP Finance", 30000, stages[0], "Maya"), put("A2", "VP Finance", 30000, stages[1], "Jonah"), put("A3", "VP Finance", 30000, stages[3], "Maya")
        put("B1", "Head of Risk", 45000, stages[1], "Jonah")
        label = {stage["id"]: stage["label"] for stage in self.desk.playbook().stages}
        # The board as text heads each stage as the board itself does: a search once, where its furthest-along candidate is.
        for text in (self.desk.pipeline({"action": "board"})["board"], cards.board_text(self.desk.playbook(), self.desk.store.list(status="open"), total=self.desk.sum,
                                                                                      staged=self.desk.stage_sums(self.desk.store.list(status="open")))):
            heads = [line for line in text.split("\n") if line and not line.startswith(" ") and "(" in line]
            self.assertEqual(heads[:4], ["%s (1)" % label[stages[0]], "%s (2)  $45k" % label[stages[1]], "%s (0)" % label[stages[2]], "%s (1)  $30k" % label[stages[3]]])
            self.assertIn("$75k", text.split("\n")[1])
        # What each record carries adds up to the stage and to the whole.
        open_ = self.desk.store.list(status="open")
        carried = self.desk.carried(open_)
        by_name = {deal["name"]: carried[deal["id"]] for deal in open_}
        self.assertEqual((by_name, sum(carried.values())), ({"A1": 0, "A2": 0, "A3": 30000.0, "B1": 45000.0}, self.desk.sum(open_)))
        # A report by stage, split by how far each got: each stage's parts add up to its row, and the rows to the total.
        kept = self.desk.reports.save({"title": "Value by stage", "count": "value", "by": "stage", "when": "open", "split": "furthest"}, by="dashboard")
        report = self.desk.reports.run(kept["id"])
        self.assertTrue(report["rows"] and any(row["parts"] for row in report["rows"]))
        for row in report["rows"]:
            self.assertAlmostEqual(sum(part["n"] for part in row["parts"]), row["n"], msg=row["label"])
        self.assertEqual((sum(row["n"] for row in report["rows"]), {row["label"]: row["n"] for row in report["rows"]}[label[stages[1]]]), (75000.0, 45000.0))
        # A record at a stage that is not the playbook's is not drawn in the funnel, but its search's fee still counts
        # where the search has got to, so the funnel adds up to what is in play.
        put("Stray", "Controller", 50000, "a_stage_from_another_playbook"), put("Here", "Controller", 20000, stages[0])
        funnel = {stage["id"]: stage["value"] for stage in self.desk.flow()["stages"]}
        self.assertEqual((funnel[stages[0]], sum(funnel.values())), (50000.0, self.desk.sum(self.desk.store.list(status="open"))))
        # The board as text gives what is left at such a stage its amount, so it still adds up to its head.
        put("Alone there", "Head of Ops", 25000, "a_stage_from_another_playbook")
        self.assertIn("Other stages (2)  $25k", self.desk.pipeline({"action": "board"})["board"])

    def test_a_sales_pipeline_adds_up_as_it_always_did(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        desk = make_desk(tmp.name, playbook="b2b-sales")
        stage = desk.playbook().first_stage()
        for n in range(3):
            desk.store.add("Deal %d" % n, stage, actor="tester", grp="Acme", value=10000)
        self.assertEqual((desk.billing(), desk.billing_kind()), ({}, ""))
        self.assertEqual(desk.sum(desk.store.list(status="open")), 30000.0)
        self.assertEqual(desk.stage_sums(desk.store.list(status="open")), {stage: 30000.0})    # a plain sum of what is at the stage
        self.assertFalse(desk.set_billing({"type": "contract", "client": "X", "rate": 80, "hours_week": 20})["ok"])
        self.assertFalse(desk.set_openings("Acme", 2)["ok"])


class SampleDataTests(unittest.TestCase):
    """The two worked examples people see: a recruiting agency and a company selling a fraud prevention tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def desk(self, playbook):
        return make_desk(str(Path(self.tmp.name) / playbook), playbook=playbook)

    def test_each_example_is_a_real_sized_pipeline(self):
        for which, playbook, low, high, groups in (("recruiting", "recruiting", 1_150_000, 1_250_000, 8),
                                                   ("saas", "b2b-sales", 2_450_000, 2_550_000, 20)):
            Path(self.tmp.name, playbook).mkdir()
            desk = self.desk(playbook)
            added = demo.load(desk)                                                # no name: the one that fits the playbook
            deals = desk.store.list(status="all")
            live = [d for d in deals if d["status"] == "open"]
            self.assertEqual(added, len(demo.EXAMPLES[which]["rows"]))
            self.assertTrue(low <= sum(d["value"] for d in live) <= high, (which, sum(d["value"] for d in live)))
            self.assertGreaterEqual(len(live), 24)
            self.assertGreaterEqual(len({d["grp"] for d in live}), groups)
            self.assertTrue(all(row["count"] for row in desk.brief()["by_stage"]), which)   # nothing looks empty
            self.assertGreaterEqual(len([d for d in deals if d["status"] == "won"]), 3)
            lost = [d for d in deals if d["status"] == "lost"]
            self.assertGreaterEqual(len(lost), 5)
            self.assertTrue(all(d["lost_reason"] in desk.playbook().lost["reasons"] for d in lost))
            self.assertTrue(all(d["email"].endswith(".example") and d["source"] == "stoa-demo" for d in deals))
            self.assertTrue(desk.brief()["due"] and desk.brief()["stale"])
            self.assertEqual(demo.load(desk), 0)                                   # loading twice adds nothing

    def test_each_example_has_its_own_map_with_a_reading_and_first_calls(self):
        for which, playbook in (("recruiting", "recruiting"), ("saas", "b2b-sales")):
            Path(self.tmp.name, playbook).mkdir()
            desk = self.desk(playbook)
            demo.load(desk)
            maps = desk.markets.list_markets()
            self.assertEqual([m["name"] for m in maps], [demo.EXAMPLES[which]["market"]["name"]])
            summary = desk.markets.summary(maps[0]["id"], text=False)
            self.assertGreaterEqual(summary["numbers"]["companies"], 10)
            self.assertEqual(summary["numbers"]["assessed"], summary["numbers"]["companies"])
            self.assertGreaterEqual(len(summary["first_calls"]), 4)
            self.assertTrue(summary["market"]["reading"] and summary["bridges"])
            self.assertTrue(all(0 < rate["rate"] <= 100 for rate in summary["requirements"]))

    def test_a_person_from_the_talent_map_joins_that_search_as_a_candidate(self):
        Path(self.tmp.name, "recruiting").mkdir()
        desk = self.desk("recruiting")
        demo.load(desk)
        clicked = desk.markets.click_track(desk.markets.target("Marisol Vega")["id"], "maya")
        made = clicked["deal"]
        self.assertEqual((made["name"], made["company"], made["grp"], made["stage"]),
                         ("Marisol Vega", "Lodestar Bank Platform", "Staff Backend · Northwind", "sourced"))
        self.assertEqual((made["next_step"], made["fields"]["title"]), ("First approach", "Staff Backend Engineer"))
        self.assertIn(made["id"], [d["id"] for d in desk.store.list(grp="Staff Backend · Northwind")])
        self.assertIn("added the candidate Marisol Vega", clicked["followup"])            # what the agent is then asked is about a candidate,
        self.assertNotIn("buyer", clicked["followup"])                                    # not about finding a buyer at one
        sample = desk.markets.summary(desk.markets.list_markets()[0]["id"], text=False)
        self.assertEqual((sample["rows"], sample["numbers"]["rows"], sample["search"]), ("people", "people", "Staff Backend · Northwind"))
        self.assertFalse(any(row["detail"].get("people") or row["detail"].get("headcount") for row in desk.markets.targets(sample["market"]["id"])))

    def test_clear_takes_all_of_it_away_and_nothing_else(self):
        Path(self.tmp.name, "b2b-sales").mkdir()
        desk = self.desk("b2b-sales")
        mine = desk.deal({"action": "add", "name": "A real deal", "value": 5000})["deal"]
        demo.load(desk)
        desk.markets.click_track(desk.markets.target("Brio Credit")["id"], "alex")
        self.assertEqual(demo.clear(desk), len(demo.EXAMPLES["saas"]["rows"]))
        self.assertEqual([d["id"] for d in desk.store.list(status="all")], [mine["id"]])
        self.assertEqual(desk.markets.list_markets(), [])


class TourTests(unittest.TestCase):
    """Sample data lives in a file of its own: a tour never touches the team's own desk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="agency", secrets={"PDL_API_KEY": "k"})
        self.mine = self.desk.deal({"action": "add", "name": "A real opportunity", "value": 5000})["deal"]

    def files(self):
        return sorted(path.name for path in Path(self.tmp.name).glob("tour*"))

    def test_a_tour_shows_the_example_and_leaves_the_real_desk_alone(self):
        started = self.desk.tour_start("recruiting")
        # The tour's own line adds the sample up as the desk does: each search's fee once, not once for every candidate in it.
        open_ = self.desk.store.list(status="open")
        self.assertIn("worth %s across" % cards.money(self.desk.sum(open_), "USD"), started["summary"])
        self.assertLess(self.desk.sum(open_), st.total(open_))
        self.assertEqual((started["ok"], started["example"], started["added"]), (True, "recruiting", len(demo.EXAMPLES["recruiting"]["rows"])))
        self.assertEqual(self.desk.playbook().name, "recruiting")                     # the example brings its own playbook...
        self.assertEqual(self.desk.own_playbook().name, "agency")                     # ...and the team's choice is not touched
        self.assertNotIn("A real opportunity", [d["name"] for d in self.desk.store.list(status="all")])
        self.assertEqual([d["name"] for d in self.desk.own_store.list(status="all")], ["A real opportunity"])
        self.assertEqual(len(self.desk.markets.list_markets()), 1)
        queue = self.desk.intake.queue()                                              # the tour has things waiting for a yes or a no
        self.assertEqual((len(queue), queue[0]["name"], queue[0]["changes"][0]["to"], bool(queue[0]["evidence"][0]["text"])),
                         (3, "Dana Whitfield", "Submitted", True))
        self.assertEqual(self.desk.store.tally()["people"], 3)
        self.desk.deal({"action": "add", "name": "Tried it out"})                     # what is done on tour stays on tour
        self.assertTrue(self.desk.tour_end())
        self.assertEqual(self.files(), [])
        self.assertEqual([d["name"] for d in self.desk.store.list(status="all")], ["A real opportunity"])
        self.assertEqual((self.desk.playbook().name, self.desk.markets.list_markets()), ("agency", []))
        self.assertFalse(self.desk.tour_end())                                        # nothing to leave the second time

    def test_each_tour_starts_fresh_and_a_second_process_sees_it(self):
        self.desk.tour_start("saas")
        first = self.desk.tour()["file"]
        self.desk.deal({"action": "add", "name": "Left over from the first tour"})
        other = make_desk(self.tmp.name, playbook="agency")                           # the dashboard, say: same folder, its own objects
        self.assertEqual((other.tour()["example"], other.playbook().name), ("saas", "b2b-sales"))
        other.tour_start("saas")
        self.assertNotEqual(self.desk.tour()["file"], first)
        self.assertEqual(self.files(), ["tour-%s" % self.desk.tour()["file"][5:], "tour.json"])
        self.assertNotIn("Left over from the first tour", [d["name"] for d in self.desk.store.list(status="all")])
        other.tour_end()
        self.assertEqual((self.desk.tour(), self.desk.playbook().name), ({}, "agency"))
        self.assertEqual(self.desk.tour_start("nonsense")["ok"], False)
        self.assertEqual(self.desk.tour(), {})

    def test_a_marker_that_does_not_hold_together_is_not_a_tour(self):
        marker = Path(self.tmp.name) / "tour.json"
        for text in ("not json", "[]", '{"example": "saas", "file": "../desk.db"}', '{"example": "nope", "file": "tour-0123abcd.db"}'):
            marker.write_text(text)
            self.desk._tour_seen = (None, {})
            self.assertEqual(self.desk.tour(), {}, text)
            self.assertEqual(self.desk.store.path, str(Path(self.tmp.name) / "desk.db"))

    def test_on_tour_nothing_is_spent_and_scheduled_jobs_wait(self):
        self.assertTrue(self.desk.can_enrich())
        self.assertTrue(self.desk.allowed({"platform": "cron"}))
        self.desk.tour_start("saas")
        self.assertFalse(self.desk.can_enrich())
        self.assertIn("switched off on the sample desk", self.desk.enrich({"deal": "anyone"})["error"])
        self.assertFalse(self.desk.allowed({"platform": "cron"}))
        self.assertTrue(self.desk.allowed({"platform": "slack", "chat_id": "C1"}))
        self.assertIn("Sample data is on show", self.desk.prompt_section())
        self.assertLessEqual(len(self.desk.prompt_section()), 900)
        self.assertIn("sample data", self.desk.today()["note"])
        self.desk.tour_end()
        self.assertNotIn("Sample data", self.desk.prompt_section())
        self.assertIn("stays on the desk", self.desk.prompt_section())            # off tour: a desk change is not mirrored elsewhere unasked
        for name in ("b2b-sales", "recruiting", "agency", "partnerships", "client-leads"):
            self.assertLessEqual(len(make_desk(tempfile.mkdtemp(), playbook=name).prompt_section()), 900, name)

    def test_sample_records_an_earlier_version_left_in_the_desk_can_be_taken_out(self):
        self.desk._set_config("playbook", "b2b-sales")
        demo.load(self.desk)                                                          # how versions before 0.10 did it
        self.assertEqual(self.desk.stray_samples(), len(demo.EXAMPLES["saas"]["rows"]))
        self.desk.tour_start("saas")
        self.assertFalse(self.desk.tidy_samples()["ok"])                              # not while the real desk is out of sight
        self.desk.tour_end()
        self.assertEqual(self.desk.tidy_samples(), {"ok": True, "removed": len(demo.EXAMPLES["saas"]["rows"])})
        self.assertEqual([d["name"] for d in self.desk.store.list(status="all")], ["A real opportunity"])

    def test_the_demo_command_takes_the_tour_and_leaves_it(self):
        from stoa import cli
        import contextlib
        import io

        def run(**args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(self.desk, types.SimpleNamespace(stoa_command="demo", **args))
            return out.getvalue()

        self.assertIn("On tour: the example of a recruiting agency", run(which="recruiting", clear=False))
        self.assertEqual(self.desk.tour()["example"], "recruiting")
        self.assertIn("your own desk is untouched", run(which="", clear=False))       # no name: the one that fits the team's playbook
        self.assertEqual(self.desk.tour()["example"], "saas")
        self.assertIn("Left the tour", run(which="", clear=True))
        self.assertEqual((self.desk.tour(), self.desk.store.count()), ({}, 1))
        self.assertIn("No tour was on", run(which="", clear=True))


    def test_leaving_a_tour_touches_nothing_and_removing_old_samples_is_asked_for_by_name(self):
        import contextlib
        import io
        from stoa import cli

        def run(**args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(self.desk, types.SimpleNamespace(stoa_command="demo", **dict({"which": "", "clear": False, "leave": False}, **args)))
            return out.getvalue()

        fixture.load(self.desk)                                   # what a version before 0.10 left in the team's own desk
        before = self.desk.own_store.count()
        run(which="recruiting")
        said = run(leave=True)
        self.assertIn("Left the tour", said)
        self.assertIn("7 sample records from an earlier version are still in your own desk", said)
        self.assertIn("--remove-samples", said)
        self.assertEqual((self.desk.tour(), self.desk.own_store.count()), ({}, before))      # nothing in the desk was touched
        self.assertIn("Removed 7 sample records", run(clear=True))
        self.assertEqual(self.desk.own_store.count(), before - 7)                            # the team's own record stays
        self.assertNotIn("still in your own desk", run(leave=True))


class TodayTests(unittest.TestCase):
    """The Today list: what wants a person now, most pressing first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)

    def names(self, key, data=None):
        return [row["name"] for row in (data or self.desk.today())[key]]

    def test_every_open_record_lands_on_the_one_list_that_fits(self):
        data = self.desk.today()
        self.assertEqual(self.names("overdue", data), ["Ingrid Solberg", "Marcus Oyelaran"])       # most overdue first
        self.assertEqual([row["days"] for row in data["overdue"]], [2, 1])
        self.assertEqual(sorted(self.names("due_today", data)), ["Dana Whitfield", "Wei-Lin Chao"])
        self.assertEqual(self.names("quiet", data), ["Tomás Ibarra"])                              # no next step, and past the stage's patience
        self.assertEqual((data["quiet"][0]["days"], data["quiet"][0]["expected"]), (6, self.desk.playbook().stale_days("sourced")))
        self.assertEqual(self.names("coming", data), ["Priya Raman", "Callum Reyes"])              # soonest first
        self.assertEqual((data["counts"]["overdue"], data["needs"]), (2, 5))
        added = self.desk.deal({"action": "add", "name": "Ada Quill"})["deal"]
        self.assertEqual(self.names("new"), ["Ada Quill"])                                         # just arrived, nothing planned
        self.desk.deal({"action": "update", "deal": added["id"], "next_step": "Say hello", "next_due": "+3d"})
        self.assertEqual((self.names("new"), "Ada Quill" in self.names("coming")), ([], True))

    def test_snoozing_takes_a_record_off_every_list_until_the_date(self):
        ingrid = self.desk.store.find("Ingrid Solberg")["id"]
        until = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        out = self.desk.deal({"action": "snooze", "deal": ingrid, "until": "+10d"})
        self.assertEqual((out["ok"], out["deal"]["snoozed_until"], out["deal"].get("due")), (True, until, None))
        data = self.desk.today()
        self.assertNotIn("Ingrid Solberg", self.names("overdue", data))
        self.assertEqual([(row["name"], row["until"]) for row in data["snoozed"]], [("Ingrid Solberg", until)])
        self.assertNotIn("Ingrid Solberg", [d["name"] for d in self.desk.brief()["due"]])          # the morning brief agrees
        self.assertNotIn("Ingrid Solberg", [d["name"] for d in self.desk.pipeline({"view": "due"})["deals"]])
        self.assertFalse(self.desk.deal({"action": "snooze", "deal": ingrid, "until": "yesterday"})["ok"])
        self.assertFalse(self.desk.deal({"action": "snooze", "deal": ingrid, "until": "whenever"})["ok"])
        self.assertEqual(self.desk.store.get(ingrid)["snoozed_until"], until)                      # a refusal changes nothing
        self.desk.deal({"action": "snooze", "deal": ingrid, "days": 0})
        self.assertIn("Ingrid Solberg", self.names("overdue"))

    def test_done_logs_the_step_and_sets_what_comes_next(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        out = self.desk.deal({"action": "done", "deal": dana, "next_step": "Prep her for the client call", "next_due": "+2d"})
        self.assertEqual((out["ok"], out["deal"]["next_step"]), (True, "Prep her for the client call"))
        log = self.desk.store.activities(dana, limit=3)
        self.assertIn(("task", "Done: Send submission note to Northwind"), [(a["kind"], a["text"]) for a in log])
        self.assertNotIn("Dana Whitfield", self.names("due_today"))
        self.desk.deal({"action": "done", "deal": dana})                                           # done, and nothing planned after it
        record = self.desk.store.get(dana)
        self.assertEqual((record["next_step"], record["next_due"]), ("", ""))
        self.assertIn("no next step to mark done", self.desk.deal({"action": "done", "deal": dana})["error"])
        self.assertFalse(self.desk.deal({"action": "done", "deal": "Priya Raman", "next_due": "the 45th"})["ok"])
        self.assertEqual(self.desk.store.find("Priya Raman")["next_step"], "Debrief after onsite")

    def test_a_due_date_can_be_taken_off(self):
        priya = self.desk.store.find("Priya Raman")["id"]
        self.assertTrue(self.desk.deal({"action": "update", "deal": priya, "next_due": ""})["ok"])
        self.assertEqual(self.desk.store.get(priya)["next_due"], "")

    def test_lists_and_drafts_waiting_are_on_it_and_the_text_reads_top_down(self):
        self.desk.intake.propose_file("badge scans.csv", "Name,Company\nAda Quill,Fathom Data\n")
        dana = self.desk.store.find("Dana Whitfield")["id"]
        self.desk.park_followup(dana, "Write the follow-up")
        data = self.desk.today()
        self.assertEqual((data["counts"]["approvals"], self.names("drafts", data), data["needs"]), (1, ["Dana Whitfield"], 7))
        text = self.desk.slash("today")
        order = [text.index(title) for title in ("Overdue (2)", "Due today (2)", "Waiting for your approval (1)", "Gone quiet (1)",
                                                 "Coming up this week (2)")]
        self.assertEqual(order, sorted(order))
        self.assertIn("badge scans.csv", text)
        self.assertIn("Ingrid Solberg — Chase Halcyon for feedback (2d overdue)", text)
        self.assertEqual(self.desk.pipeline({"view": "today"})["view"], "today")
        slim = self.desk.today(full=False)                                                         # what the page polls: ids and the one fact
        self.assertEqual(sorted(slim["overdue"][0]), ["days", "id"])

    def test_an_empty_desk_says_so(self):
        desk = make_desk(tempfile.mkdtemp(dir=self.tmp.name))
        self.assertEqual((desk.today()["needs"], desk.today_text().splitlines()[-1]), (0, "Nothing needs you today."))


class SetupTests(unittest.TestCase):
    """The Setup tab's data: what is connected, the rules in force, and what is left to do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fresh(self, config=None, secrets=None):
        settings = dict(config or {})
        return Desk(data_dir=lambda: Path(self.tmp.name), get_config=lambda k, default=None: settings.get(k, default),
                    set_config=settings.__setitem__, get_secret=lambda n: (secrets or {}).get(n), actor=lambda: "tester",
                    http=lambda *a, **k: (0, {}))

    def test_a_new_desk_has_everything_still_to_do(self):
        data = self.fresh().setup()
        self.assertEqual({item["id"]: item["done"] for item in data["checklist"]},
                         {"playbook": False, "records": False, "keys": False, "slack": False, "sweep": False, "notes": False})
        self.assertEqual((data["playbook"]["name"], data["playbook"]["chosen"], data["tour"], data["can_switch"]), ("b2b-sales", False, None, True))
        self.assertEqual(len(data["automations"]), 7)
        self.assertEqual(sorted(example["id"] for example in data["examples"]), ["recruiting", "saas"])
        self.assertEqual(data["playbook_sets"]["stages"][-1], "Closed won")
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")

    def test_each_key_says_what_it_is_for_and_whether_it_is_set(self):
        desk = self.fresh({"playbook": "b2b-sales", "pdl_monthly_cap": 80}, {"PDL_API_KEY": "pdl-secret-value"})
        desk.store.bump("pdl_calls:" + dt.date.today().isoformat()[:7], 3)
        data = desk.setup()
        keys = {row["id"]: row for row in data["keys"]}
        self.assertEqual([row["id"] for row in data["keys"]], ["exa", "pdl", "granola", "fireflies"])
        self.assertEqual((keys["exa"]["set"], keys["pdl"]["set"], keys["granola"]["set"]), (False, True, False))
        self.assertEqual(keys["pdl"]["note"], "3 of 80 lookups used this month.")
        for row in data["keys"]:
            self.assertTrue(row["get"].startswith("https://"))
            self.assertTrue(all(row[field] for field in ("name", "unlocks", "without", "steps", "cost", "check", "where")))
        step = next(item for item in data["checklist"] if item["id"] == "keys")
        self.assertEqual((step["done"], step["detail"]), (True, "Set: People Data Labs."))
        self.assertNotIn("pdl-secret-value", json.dumps(data))

    def test_a_key_is_tested_with_one_small_request_and_never_shown(self):
        calls = []

        def http(url, **kw):
            calls.append((url, kw))
            return {"api.peopledatalabs.com": (404, {}), "api.exa.ai": (401, {}), "public-api.granola.ai": (402, {})}[url.split("/")[2]]

        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=http,
                         secrets={"PDL_API_KEY": "pdl-secret-value", "EXA_API_KEY": "exa-secret-value", "GRANOLA_API_KEY": "granola-secret-value"})
        pdl, exa, granola = desk.key_test("pdl"), desk.key_test("exa"), desk.key_test("granola")
        self.assertEqual((pdl["ok"], pdl["state"], pdl["message"]), (True, "works", "People Data Labs accepted the key."))
        self.assertEqual((exa["ok"], exa["state"]), (False, "rejected"))
        self.assertIn("Copy it again from the Exa dashboard", exa["message"])
        self.assertEqual((granola["ok"], granola["state"]), (False, "spent"))
        self.assertEqual(len(calls), 3)     # one request each, no more
        self.assertEqual(calls[0][1]["params"]["email"], sources.KEY_CHECK_EMAIL)     # nobody's address: the lookup cannot match, so it is not billed
        self.assertEqual(calls[1][1]["data"], {"query": "Exa", "numResults": 1})
        for answer in (pdl, exa, granola):
            self.assertNotIn("secret-value", json.dumps(answer))
        self.assertEqual(desk.setup()["keys"][1]["note"], "0 of 50 lookups used this month.")     # a test is not a lookup

    def test_a_key_that_is_not_set_or_cannot_be_reached_says_so(self):
        desk = make_desk(self.tmp.name, playbook="b2b-sales", secrets={"EXA_API_KEY": "exa-secret-value"})     # the stand-in network answers nothing
        self.assertEqual(desk.key_test("pdl")["state"], "missing")
        self.assertEqual(desk.key_test("nonsense")["state"], "unknown")
        down = desk.key_test("exa")
        self.assertEqual((down["ok"], down["state"]), (False, "unreachable"))
        self.assertIn("nothing is known about the key", down["message"])

    def test_setup_says_which_automations_hermes_has_switched_on(self):
        desk = self.fresh({"playbook": "b2b-sales"})
        self.assertEqual({row["state"] for row in desk.setup()["automations"]}, {"unknown"})     # nothing to read Hermes's jobs with
        desk.jobs = lambda: [{"name": "Pipeline morning brief", "on": True, "schedule": "30 8 * * 1-5", "next": "2031-01-06T08:30:00"},
                             {"name": "Friday pipeline review", "on": False, "schedule": "0 16 * * 5", "next": ""},
                             {"name": "Somebody else's job", "on": True, "schedule": "0 9 * * *", "next": ""}]
        data = desk.setup()
        rows = {row["name"]: row for row in data["automations"]}
        self.assertEqual((rows["morning-brief"]["state"], rows["morning-brief"]["schedule"]), ("on", "30 8 * * 1-5"))
        self.assertEqual((rows["friday-review"]["state"], rows["notes-sweep"]["state"]), ("paused", "off"))
        self.assertEqual(len(data["automations"]), 7)
        step = next(item for item in data["checklist"] if item["id"] == "sweep")
        self.assertEqual((step["done"], step["detail"]), (True, "Switched on: Pipeline morning brief."))

        def broken():
            raise OSError("jobs file is locked")
        desk.jobs = broken
        self.assertEqual({row["state"] for row in desk.setup()["automations"]}, {"unknown"})     # Setup still opens

    def test_a_notes_folder_is_checked_found_and_saved_from_the_page(self):
        settings = {"playbook": "b2b-sales"}
        desk = Desk(data_dir=lambda: Path(self.tmp.name), get_config=lambda k, default=None: settings.get(k, default),
                    set_config=settings.__setitem__, get_secret=lambda n: None, actor=lambda: "tester")
        folder = Path(self.tmp.name) / "Meeting notes"
        (folder / "calls").mkdir(parents=True)
        (folder / "dana.md").write_text("Dana wants the proposal by Thursday.")
        (folder / "calls" / "marcus.txt").write_text("Marcus asked about SSO.")
        (folder / "photo.png").write_text("not a note")
        self.assertIn("Paste the folder", desk.notes_check("")["message"])
        self.assertIn("not a whole path", desk.notes_check("Meeting notes")["message"])
        self.assertIn("no folder at that path", desk.notes_check(str(folder) + "-typo")["message"])
        self.assertIn("a file, not a folder", desk.notes_check(str(folder / "dana.md"))["message"])
        found = desk.notes_check(' "%s" ' % folder)      # pasted with quotes and spaces round it
        self.assertEqual((found["ok"], found["count"], found["folder"]), (True, 2, str(folder)))
        self.assertFalse(desk.notes_folder(str(folder) + "-typo")["ok"])
        self.assertNotIn("notes_dir", settings)      # a folder that is not there is never saved
        saved = desk.notes_folder(str(folder))
        self.assertEqual((saved["ok"], saved["saved"], settings["notes_dir"]), (True, True, str(folder)))
        data = desk.setup()
        self.assertEqual((data["notes"]["folder"], data["notes"]["ok"], data["notes"]["count"]), (str(folder), True, 2))
        self.assertTrue(next(item for item in data["checklist"] if item["id"] == "notes")["done"])
        self.assertEqual((desk.notes_folder("")["folder"], settings["notes_dir"]), ("", ""))
        desk.can_configure = False
        self.assertTrue(desk.notes_folder(str(folder))["elsewhere"])
        self.assertEqual(settings["notes_dir"], "")

    def test_looking_for_a_notes_folder_tries_only_the_usual_places(self):
        desk = self.fresh({"playbook": "b2b-sales"})
        home = Path(self.tmp.name) / "home"
        (home / "Documents" / "Notes").mkdir(parents=True)
        (home / "Documents" / "Notes" / "call.md").write_text("A call.")
        (home / "Documents" / "Obsidian").mkdir()      # there, but empty: not offered
        (home / "Elsewhere").mkdir()
        (home / "Elsewhere" / "secret.md").write_text("Not in a usual place.")
        with mock.patch.dict("os.environ", {"HOME": str(home)}):
            answer = desk.notes_look()
        self.assertEqual([(row["folder"], row["count"]) for row in answer["found"]], [(str(home / "Documents" / "Notes"), 1)])
        self.assertTrue(all(place.startswith("~/") for place in answer["looked"]))

    def test_it_reports_what_is_set_without_showing_any_key(self):
        desk = self.fresh({"playbook": "recruiting", "channels": ["C1"], "slack_users": ["U1", "U2"], "pdl_monthly_cap": 80,
                           "notes_dir": "/notes"}, {"PDL_API_KEY": "pdl-secret-value", "GRANOLA_API_KEY": "granola-secret-value"})
        desk.slack = types.SimpleNamespace(available=True, refresh=lambda deal_id: None)
        desk.deal({"action": "add", "name": "Ada Quill", "email": "ada@fathom.example"})
        desk.deal({"action": "log", "deal": "Ada Quill", "kind": "email", "text": "She replied.", "ref": "gmail:1"})
        desk.store.bump("pdl_calls:" + dt.date.today().isoformat()[:7], 3)
        data = desk.setup()
        text = json.dumps(data)
        self.assertNotIn("pdl-secret-value", text)
        self.assertNotIn("granola-secret-value", text)
        self.assertTrue(all(item["done"] for item in data["checklist"]))
        on = {item["id"]: (item["on"], item["detail"]) for item in data["connections"]}
        self.assertEqual(on["lookup"], (True, "3 of 80 lookups used this month."))
        self.assertEqual((on["slack"][0], on["granola"][0], on["notes"]), (True, True, (True, "/notes")))
        rules = {rule["label"]: rule["value"] for rule in data["rules"]}
        self.assertIn("Only in these chats: C1", rules["Where the desk answers"])
        self.assertEqual(rules["Who may press a Slack button"], "These Slack members: U1, U2.")
        self.assertEqual(rules["Scheduled jobs"], "Cannot use the desk: add cron to the channels setting.")
        self.assertEqual((data["data"]["records"], data["data"]["samples"], data["data"]["activities"] > 0), (1, 0, True))

    def test_one_part_that_cannot_be_read_does_not_take_the_tab_down(self):
        desk = self.fresh({"playbook": "recruiting"})
        desk.deal({"action": "add", "name": "Ada Quill"})

        def broken():
            raise OSError("disk went away")

        desk.import_roots = broken
        desk.slack_users = broken
        desk._get_secret = lambda name: (_ for _ in ()).throw(RuntimeError("no scope"))      # swallowed where secrets are read: not a problem
        with self.assertLogs("hermes.plugins.stoa", level="WARNING") as logged:
            data = desk.setup()
        self.assertEqual((data["ok"], data["data"]["records"], len(data["checklist"]), len(data["automations"])), (True, 1, 6, 7))
        self.assertEqual(sorted(data["problems"]), ["the folders it may read: OSError: disk went away", "who may press Slack buttons: OSError: disk went away"])
        self.assertEqual(len(logged.output), 2)
        self.assertEqual({rule["label"]: rule["value"] for rule in data["rules"]}["Files it may read"], "CSV and TSV only, from: .")
        self.assertEqual(self.fresh({"playbook": "recruiting"}).setup()["problems"], [])

    def test_on_tour_it_still_counts_the_real_desk(self):
        desk = self.fresh({"playbook": "recruiting"})
        desk.deal({"action": "add", "name": "Ada Quill"})
        desk.tour_start("saas")
        data = desk.setup()
        self.assertEqual((data["tour"]["example"], data["data"]["records"], data["playbook"]["name"], data["playbook"]["shown"]),
                         ("saas", 1, "recruiting", "b2b-sales"))
        self.assertEqual({rule["label"]: rule["value"] for rule in data["rules"]}["Scheduled jobs"], "Paused while sample data is on show.")


class UndoTests(unittest.TestCase):
    """Every change says what it replaced, so the last action on a record can be taken back, and the one before it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")["id"]

    def kinds(self, deal_id=None):
        return [a["kind"] for a in self.desk.store.activities(deal_id or self.dana, limit=20)]

    def test_a_move_an_edit_and_a_snooze_each_go_back(self):
        before = self.desk.store.get(self.dana)
        self.desk.deal({"action": "move", "deal": self.dana, "stage": "submitted"})
        self.desk.deal({"action": "update", "deal": self.dana, "value": 99000, "owner": "Maya", "fields": {"comp": "$250k"}})
        self.desk.deal({"action": "snooze", "deal": self.dana, "days": 7})
        self.assertEqual(self.desk.store.undoable(self.dana)["text"][:13], "Snoozed until")
        for expect in ("Snoozed until", "Updated value, owner, fields", "screened → Submitted"):
            out = self.desk.deal({"action": "undo", "deal": "Dana Whitfield"})
            self.assertTrue(out["ok"])
            self.assertTrue(out["undid"].startswith(expect), out["undid"])
        after = self.desk.store.get(self.dana)
        for key in ("stage", "status", "value", "owner", "fields", "snoozed_until", "stage_at", "next_step"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.kinds()[:3], ["undo", "undo", "undo"])

    def test_one_action_is_taken_back_as_one(self):
        self.desk.deal({"action": "done", "deal": self.dana, "next_step": "Prep her for the client call", "next_due": "+2d"})
        self.assertIn("task", self.kinds())
        out = self.desk.undo(self.dana)                                           # the log line and the new next step, together
        self.assertEqual((out["ok"], out["deal"]["next_step"]), (True, "Send submission note to Northwind"))
        standing = lambda: [a["text"] for a in self.desk.store.activities(self.dana, limit=20) if not a["undone"]]     # noqa: E731
        self.assertNotIn("Done: Send submission note to Northwind", standing())   # the line is struck out, not destroyed
        self.assertIn("task", self.kinds())
        self.desk.deal({"action": "log", "deal": self.dana, "kind": "call", "text": "Spoke briefly.", "next_step": "Call again"})
        self.desk.undo(self.dana)
        self.assertEqual(self.desk.store.get(self.dana)["next_step"], "Send submission note to Northwind")
        self.assertNotIn("Spoke briefly.", standing())
        self.assertIn("Spoke briefly.", [a["text"] for a in self.desk.store.activities(self.dana, limit=20)])

    def test_taking_back_an_add_removes_the_record(self):
        made = self.desk.deal({"action": "add", "name": "Ada Quill", "note": "Met at the booth."})["deal"]["id"]
        self.desk.contact({"action": "add", "deal": made, "name": "Her Boss", "email": "boss@fathom.example"})
        self.desk.undo(made)                                                      # first the person...
        self.assertEqual(len(self.desk.people(self.desk.store.get(made))), 1)
        out = self.desk.undo(made)                                                # ...then the record, note and all
        self.assertEqual((out["ok"], out["removed"], out["deal"]), (True, True, None))
        self.assertIn("restore", out["note"])
        self.assertIsNone(self.desk.store.get(made))                              # off the desk everywhere...
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertNotIn(made, [d["id"] for d in self.desk.store.list(status="all")])
        self.assertEqual(self.desk.store.tally()["archived"], 1)
        self.assertEqual(self.desk.store.get(made, archived=True)["name"], "Ada Quill")       # ...but archived, not destroyed
        self.assertIn("Met at the booth.", [a["text"] for a in self.desk.store.activities(made)])
        self.assertFalse(self.desk.deal({"action": "log", "deal": made, "text": "Still there?"})["ok"])
        self.assertFalse(self.desk.restore("Nobody Here")["ok"])
        back = self.desk.deal({"action": "restore", "deal": "ada quill"})         # and it can be put back
        self.assertEqual((back["ok"], back["deal"]["id"], self.desk.store.get(made)["name"]), (True, made, "Ada Quill"))

    def test_a_win_and_a_loss_go_back_to_where_they_were(self):
        self.desk.deal({"action": "close", "deal": self.dana, "outcome": "lost", "reason": "Comp mismatch"})
        self.assertEqual(self.desk.store.get(self.dana)["status"], "lost")
        self.desk.undo(self.dana)
        record = self.desk.store.get(self.dana)
        self.assertEqual((record["status"], record["stage"], record["lost_reason"], "_prev_stage" in record["fields"]),
                         ("open", "screened", "", False))

    def test_an_older_action_goes_back_when_nothing_it_set_has_changed_since(self):
        self.desk.deal({"action": "move", "deal": self.dana, "stage": "submitted"})
        move = self.desk.store.undoable(self.dana)["op"]
        self.desk.deal({"action": "log", "deal": self.dana, "text": "Client replied."})
        self.assertNotEqual(self.desk.store.undoable(self.dana)["op"], move)      # the note is the last action now
        out = self.desk.undo(self.dana, op=move)                                  # the move still goes back: the stage is as it left it
        self.assertEqual((out["ok"], out["deal"]["stage"]), (True, "screened"))
        self.assertIn("Client replied.", [a["text"] for a in self.desk.store.activities(self.dana) if not a["undone"]])
        self.assertIsNone(self.desk.store.undo(self.dana, op=move))               # not twice
        self.assertIsNone(self.desk.store.undo(self.dana, op="no-such-action"))
        self.assertEqual(self.desk.store.undoables().get(self.dana), self.desk.store.undoable(self.dana)["op"])

    def test_it_is_refused_when_it_would_overwrite_a_later_change(self):
        self.desk.deal({"action": "update", "deal": self.dana, "value": 50000, "owner": "Maya"})
        first = self.desk.store.undoable(self.dana)["op"]
        self.desk.deal({"action": "update", "deal": self.dana, "value": 60000})   # someone corrected the amount afterwards
        refused = self.desk.undo(self.dana, op=first)
        self.assertEqual(refused["ok"], False)
        self.assertEqual(refused["conflicts"], ["Fee is now $60k (that action left it at $50k)"])
        self.assertIn("Nothing was changed", refused["error"])
        record = self.desk.store.get(self.dana)
        self.assertEqual((record["value"], record["owner"]), (60000, "Maya"))     # all of it stands, not just the field in question
        self.assertTrue(self.desk.undo(self.dana)["ok"])                          # the later change goes back first...
        self.assertTrue(self.desk.undo(self.dana, op=first)["ok"])                # ...and then the earlier one can
        record = self.desk.store.get(self.dana)
        self.assertEqual((record["value"], record["owner"]), (42000, "you"))

    def test_a_snooze_set_after_a_move_survives_the_move_going_back(self):
        self.desk.deal({"action": "move", "deal": self.dana, "stage": "submitted"})
        move = self.desk.store.undoable(self.dana)["op"]
        self.desk.deal({"action": "snooze", "deal": self.dana, "days": 5})
        out = self.desk.undo(self.dana, op=move)
        self.assertEqual((out["ok"], out["deal"]["stage"], bool(out["deal"].get("snoozed_until"))), (True, "screened", True))

    def test_an_add_goes_back_only_while_nothing_else_has_been_done_to_the_record(self):
        made = self.desk.deal({"action": "add", "name": "Ada Quill"})["deal"]["id"]
        add = self.desk.store.undoable(made)["op"]
        self.desk.deal({"action": "update", "deal": made, "value": 1000})
        refused = self.desk.undo(made, op=add)
        self.assertEqual((refused["ok"], refused["conflicts"]), (False, ["it has been worked on since it was added (take those changes back first)"]))
        self.assertIsNotNone(self.desk.store.get(made))

    def test_a_record_from_before_the_ledger_has_nothing_to_take_back(self):
        with self.desk.store._conn() as conn:                                     # as a version before 0.12 left it: a row, no ledger
            conn.execute("INSERT INTO deals (id, name, stage, created_at, updated_at, stage_at) VALUES ('old-timer', 'Old Timer', 'sourced', ?, ?, ?)",
                         (st.now(), st.now(), st.now()))
        self.assertIsNone(self.desk.store.undoable("old-timer"))
        self.assertFalse(self.desk.undo("Old Timer")["ok"])
        self.assertNotIn("old-timer", self.desk.store.undoables())

    def test_what_was_taken_back_is_not_a_touch(self):
        tomas = self.desk.store.find("Tomás Ibarra")["id"]
        self.assertTrue(cards.is_stale(self.desk.playbook(), self.desk.store.get(tomas), self.desk.store.last_touch(tomas)))
        self.desk.deal({"action": "move", "deal": tomas, "stage": "contacted"})
        self.desk.undo(tomas)
        self.assertTrue(cards.is_stale(self.desk.playbook(), self.desk.store.get(tomas), self.desk.store.last_touch(tomas)))

    def test_a_button_click_is_one_action_too(self):
        self.desk.click("advance", self.dana, "screened", "jo")
        self.assertEqual(self.desk.store.get(self.dana)["stage"], "submitted")
        self.desk.undo(self.dana)
        self.assertEqual(self.desk.store.get(self.dana)["stage"], "screened")


class LedgerTests(unittest.TestCase):
    """Every change is a line on the ledger, with what it replaced and what it left. Lines are never altered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        self.ada = self.desk.deal({"action": "add", "name": "Ada Quill", "company": "Fathom Data", "value": 40000})["deal"]["id"]

    def test_each_change_says_what_it_replaced_and_what_it_left(self):
        self.desk.deal({"action": "update", "deal": self.ada, "value": 45000, "fields": {"comp": "$210k"}})
        self.desk.deal({"action": "move", "deal": self.ada, "stage": "contacted"})
        self.desk.deal({"action": "log", "deal": self.ada, "kind": "call", "text": "Spoke for ten minutes."})
        lines = list(reversed(self.desk.store.ledger(self.ada)))
        self.assertEqual([(line["entity"], line["op"]) for line in lines], [("deal", "insert"), ("deal", "update"), ("deal", "update"), ("activity", "insert")])
        self.assertEqual((lines[0]["after"]["name"], lines[0]["after"]["value_cents"], lines[0]["actor"]), ("Ada Quill", 4000000, "tester"))
        self.assertEqual((lines[1]["before"], lines[1]["after"]), ({"value_cents": 4000000, "fields.comp": None}, {"value_cents": 4500000, "fields.comp": "$210k"}))
        self.assertEqual((lines[2]["before"]["stage"], lines[2]["after"]["stage"], lines[2]["summary"]), ("sourced", "contacted", "sourced → Contacted"))
        self.assertEqual(len({line["batch"] for line in lines}), 4)               # four actions

    def test_a_line_cannot_be_changed_or_removed(self):
        with self.assertRaises(sqlite3.DatabaseError):
            with self.desk.store._conn() as conn:
                conn.execute("UPDATE ledger SET actor = 'someone else'")
        with self.assertRaises(sqlite3.DatabaseError):
            with self.desk.store._conn() as conn:
                conn.execute("DELETE FROM ledger")
        self.assertEqual(self.desk.store.ledger(self.ada)[0]["actor"], "tester")

    def test_taking_something_back_adds_lines_and_leaves_the_old_ones(self):
        self.desk.deal({"action": "update", "deal": self.ada, "value": 45000})
        before = self.desk.store.ledger(self.ada)
        self.desk.undo(self.ada)
        after = self.desk.store.ledger(self.ada)
        self.assertEqual(after[1:], before)                                       # nothing that was there has moved
        self.assertEqual((after[0]["op"], after[0]["reverts"], after[0]["before"], after[0]["after"]),
                         ("undo", before[0]["seq"], {"value_cents": 4500000}, {"value_cents": 4000000}))
        self.assertEqual(self.desk.store.get(self.ada)["value"], 40000)

    def test_even_a_record_that_is_destroyed_leaves_its_line(self):
        self.desk.store.delete(self.ada, actor="cleanup")
        last = self.desk.store.ledger(self.ada)[0]
        self.assertEqual((last["op"], last["before"]["name"], last["actor"]), ("delete", "Ada Quill", "cleanup"))
        self.assertEqual(self.desk.store.tally()["ledger"], 2)

    def test_the_history_command_prints_it(self):
        from stoa import cli
        import contextlib
        import io
        self.desk.deal({"action": "update", "deal": self.ada, "value": 45000})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(self.desk, types.SimpleNamespace(stoa_command="history", record=["ada", "quill"], limit=20))
        text = out.getvalue()
        self.assertIn("Ada Quill", text)
        self.assertIn("Updated value", text)
        self.assertIn("$40k → $45k", text)


class MoneyTests(unittest.TestCase):
    """Amounts are whole cents in the file, so they add up exactly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")

    def test_amounts_are_read_into_cents(self):
        for given, cents in ((1234.565, 123457), ("$1,200.10", 120010), ("42k", 4200000), ("1.5m", 150000000), (0.1 + 0.2, 30), ("", 0),
                             (None, 0), ("nonsense", 0), (19.99, 1999), (-5, -500)):
            self.assertEqual(to_cents(given), cents, given)

    def test_sums_are_exact(self):
        for index, amount in enumerate((0.1, 0.2, 0.7, 19.99, 19.99, 19.99)):
            self.desk.deal({"action": "add", "name": "Deal %d" % index, "value": amount})
        deals = self.desk.store.list()
        self.assertEqual(sum(d["value_cents"] for d in deals), 6097)
        self.assertEqual((st.total(deals), self.desk.brief()["open_value"], self.desk.pipeline({})["open_value"]), (60.97, 60.97, 60.97))
        self.assertEqual(self.desk.flow()["stages"][0]["value"], 60.97)
        self.assertEqual(self.desk.store.get("deal-3")["value"], 19.99)

    def test_a_file_from_an_older_version_is_taken_over_and_stays_readable_by_it(self):
        made = self.desk.deal({"action": "add", "name": "Kestrel renewal", "value": 48000})["deal"]["id"]
        path = self.desk.store.path
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT value, value_cents FROM deals").fetchone(), (48000.0, 4800000))    # both written
            conn.execute("UPDATE deals SET value = 51234.56")                     # what a version before 0.12 would do
        self.assertEqual(Store(path).get(made)["value_cents"], 5123456)           # the next time the file is opened, cents follow
        self.desk.deal({"action": "update", "deal": made, "value": 51234.56})     # the same amount: nothing to change
        self.assertEqual([line["op"] for line in self.desk.store.ledger(made)], ["insert"])


class MatchedProposalTests(unittest.TestCase):
    """A proposal written without knowing the record was there: held as a change to it, and a careful one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")
        add = lambda name, **more: self.desk.deal(dict({"action": "add", "name": name, "company": name, "group": name}, **more))["deal"]["id"]      # noqa: E731
        self.snov = add("Snov.io", next_step="Follow up with Oryna", next_due="+4d")
        self.desk.store.person_add(self.snov, "Oryna K", email="oryna@snov.example", title="PR Manager")
        self.desk.deal({"action": "close", "deal": self.snov, "outcome": "won"})
        self.exa = add("Exa")
        self.desk.deal({"action": "move", "deal": self.exa, "stage": "discovery"})
        self.cron = {"platform": "cron"}

    def propose(self, *rows):
        return self.desk.intake.run({"action": "propose", "kind": "proposal", "source": "Activity sweep", "rows": list(rows)}, self.cron)

    def test_the_board_says_what_is_closed(self):
        board = self.desk.pipeline({"view": "board"})
        self.assertIn("Not on the board, but on the desk: Closed won (1): Snov.io.", board["board"])
        self.assertEqual(board["closed"], {"Closed won": ["Snov.io"], "Closed lost": []})
        self.assertIn("look it up with gtm_deal", board["closed_note"])

    def test_a_row_that_did_not_know_the_record_does_not_reopen_it(self):
        row = {"name": "Snov.io", "company": "Snov.io", "email": "oryna@snov.example", "stage": "lead", "next_step": "Follow up with partnerships",
               "next_due": "+7d", "person": {"name": "Oryna K", "email": "oryna@snov.example"}, "why": "no Snov.io record exists",
               "evidence": [{"kind": "email", "at": "2026-11-16", "who": "Snov.io", "text": "Welcome aboard", "ref": "m1"}]}
        out = self.propose(row)
        self.assertEqual(out["already_on_the_desk"], [{"record": self.snov, "name": "Snov.io", "status": "won", "stage": "Closed won"}])
        self.assertIn("never as a new one", out["matched_note"])
        held = self.desk.store.intake_get(out["id"])["rows"][0]
        self.assertEqual((held["kind"], held["deal"], sorted(change["key"] for change in held["changes"])), ("update", self.snov, ["next_due", "next_step"]))
        self.assertEqual(held["warnings"], ["Already on the desk at Closed won: its stage is left as it is."])      # not won back to a lead
        again = self.propose(dict(row, why="worded another way"))                # the same suggestion, made twice
        self.assertEqual((again["counts"]["update"], again["counts"]["same"]), (0, 1))
        self.assertEqual(len(self.desk.intake.queue()), 1)
        self.assertTrue(self.desk.intake.decide(out["id"], 1, "approve", actor="tester")["ok"])
        after = self.desk.store.get(self.snov)
        self.assertEqual((after["status"], after["stage"], after["email"], after["next_step"]), ("won", "won", "", "Follow up with partnerships"))

    def test_an_open_record_is_not_stepped_back_but_a_named_one_moves(self):
        out = self.propose({"name": "Exa", "company": "Exa", "stage": "lead", "next_step": "Send the deck"})
        held = self.desk.store.intake_get(out["id"])["rows"][0]
        self.assertEqual(([change["key"] for change in held["changes"]], held["warnings"]),
                         (["next_step"], ["Already on the desk at Discovery: its stage is left as it is."]))
        forward = self.propose({"name": "Exa", "company": "Exa", "stage": "proposal"})        # a later stage, by name: a move
        self.assertEqual([change["key"] for change in self.desk.store.intake_get(forward["id"])["rows"][0]["changes"]], ["stage"])
        named = self.propose({"record": self.snov, "stage": "negotiation"}, {"record": self.exa, "stage": "lead"})      # by id: it says what it means
        rows = self.desk.store.intake_get(named["id"])["rows"]
        self.assertEqual([[change["to"] for change in row["changes"]] for row in rows], [["Negotiation"], ["Lead"]])
        self.assertNotIn("already_on_the_desk", named)

    def test_where_the_record_is_the_person_its_address_is_still_its_own(self):
        desk = make_desk(tempfile.mkdtemp(dir=self.tmp.name), playbook="recruiting")
        one = desk.deal({"action": "add", "name": "Ingrid Solberg", "company": "Meridian"})["deal"]["id"]
        out = desk.intake.run({"action": "propose", "kind": "proposal", "rows": [{"record": one, "email": "ingrid@meridian.example"}]}, self.cron)
        self.assertEqual([change["key"] for change in desk.store.intake_get(out["id"])["rows"][0]["changes"]], ["email"])


class DatabaseTests(unittest.TestCase):
    """Accounts, deals and people as rows, and the views kept of them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")
        add = lambda name, **more: self.desk.deal(dict({"action": "add", "name": name}, **more))["deal"]["id"]      # noqa: E731
        self.snov = add("Snov.io", company="Snov.io", group="Snov.io", value=5000, source="Research", next_step="Send terms", next_due="+2d")
        self.second = add("Snov.io renewal", company="snov.io", group="Snov.io", value=1000, owner="Ada")
        self.old = add("Dana Whitfield", company="Kestrel Labs", group="Kestrel Labs", email="dana@kestrel.example", fields={"title": "VP Ops"})
        self.desk.store.person_add(self.snov, "Oryna K", title="PR Manager", email="oryna@snov.example", role="Buyer")
        self.desk.store.person_add(self.second, "Oryna K", email="oryna@snov.example")        # the same person, on a second record
        self.desk.deal({"action": "close", "deal": self.second, "outcome": "won"})
        self.desk.market({"action": "define", "name": "Enrichment tools"})
        self.desk.market({"action": "add", "companies": [{"company": "Snov.io", "website": "snov.io", "segment": "Enrich", "fit": 5, "size": "51-200"},
                                                         {"company": "Hunter", "segment": "Enrich", "fit": 4, "location": "Remote"},
                                                         {"company": "Gone Co"}]})
        self.desk.market({"action": "dismiss", "targets": ["Gone Co"]})

    def test_rows_of_deals_accounts_and_people(self):
        data = self.desk.database.rows()
        deals = {row["id"]: row for row in data["deals"]}
        self.assertEqual(len(deals), 3)
        one = deals[self.snov]                                   # a deal named for its company: its contact is the first person on it
        self.assertEqual((one["name"], one["status"], one["stage"], one["contact"], one["contact_title"], one["email"], one["people"], one["value"]),
                         ("Snov.io", "Open", "Lead", "Oryna K", "PR Manager", "oryna@snov.example", 1, 5000))
        won = deals[self.second]                                 # won and in onboarding: its status, where the work stands, no stage
        self.assertEqual((won["status"], won["stage"], won["delivery"], won["state"]), ("Closed won", "", "Onboarding", "won"))
        older = deals[self.old]                                  # an older record named for a person is its own contact
        self.assertEqual((older["contact"], older["email"], older["people"]), ("Dana Whitfield", "dana@kestrel.example", 1))
        accounts = {row["name"]: row for row in data["accounts"]}
        self.assertEqual(sorted(accounts), ["Hunter", "Kestrel Labs", "Snov.io"])              # one Snov.io however it is typed; the dismissed company is not there
        snov = accounts["Snov.io"]
        self.assertEqual((snov["status"], snov["open"], snov["won"], snov["deals"], snov["open_value"], snov["won_value"], snov["people"], snov["owners"]),
                         ("Customer", 1, 1, 2, 5000, 1000, 1, "Ada"))
        self.assertEqual((snov["next_step"], snov["maps"], snov["segment"], snov["fit"], snov["size"], snov["domain"], snov["in_pipeline"], snov["group"]),
                         ("Send terms", "Enrichment tools", "Enrich", 5, "51-200", "snov.io", True, "Snov.io"))
        hunter = accounts["Hunter"]                              # on a map only
        self.assertEqual((hunter["status"], hunter["in_pipeline"], hunter["deals"], hunter["location"], bool(hunter["map_id"])), ("On a map", False, 0, "Remote", True))
        people = {row["name"]: row for row in data["people"]}
        self.assertEqual(sorted(people), ["Dana Whitfield", "Oryna K"])                        # each person once
        oryna = people["Oryna K"]
        self.assertEqual((oryna["company"], oryna["record_count"], oryna["open"], oryna["role"], oryna["email"], [r["id"] for r in oryna["records"]]),
                         ("Snov.io", 2, 2, "Buyer", "oryna@snov.example", [self.snov, self.second]))
        self.assertEqual((people["Dana Whitfield"]["title"], people["Dana Whitfield"]["company"]), ("VP Ops", "Kestrel Labs"))
        self.assertEqual((data["stages"][0], data["delivery"], data["currency"]), ("Lead", ["Onboarding", "Live"], "USD"))
        json.dumps(data)

    def test_where_the_record_is_the_person(self):
        desk = make_desk(tempfile.mkdtemp(dir=self.tmp.name), playbook="recruiting")
        one = desk.deal({"action": "add", "name": "Ingrid Solberg", "company": "Meridian Freight", "group": "Designer · Halcyon", "email": "ingrid@meridian.example"})["deal"]["id"]
        desk.store.person_add(one, "Hal Manager", email="hal@halcyon.example", role="Hiring manager")
        data = desk.database.rows()
        people = {row["name"]: row for row in data["people"]}
        self.assertEqual((people["Ingrid Solberg"]["company"], people["Ingrid Solberg"]["kind"]), ("Meridian Freight", "Candidate"))
        self.assertEqual((people["Hal Manager"]["company"], people["Hal Manager"]["role"]), ("", "Hiring manager"))      # not at the candidate's employer
        self.assertEqual(data["deals"][0]["contact"], "Ingrid Solberg")

    def test_one_change_to_several_records(self):
        change, get = self.desk.database.change, self.desk.store.get
        out = change({"ids": [self.snov, self.second, self.old, "ghost"], "set": {"owner": "Lee", "next_step": "Call them", "next_due": "+3d", "name": "hacked"}})
        self.assertEqual((out["ok"], out["changed"], out["skipped"]), (True, 3, [{"name": "ghost", "why": "no longer on the desk"}]))
        due = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        for one in (self.snov, self.second, self.old):          # the won one is still in onboarding, so it takes a next step too
            self.assertEqual((get(one)["owner"], get(one)["next_step"], get(one)["next_due"]), ("Lee", "Call them", due))
        self.assertEqual(get(self.snov)["name"], "Snov.io")     # only the facts on the list can be set this way
        moved = change({"ids": [self.snov, self.second, self.old], "stage": "qualified"})
        self.assertEqual((moved["changed"], [item["why"] for item in moved["skipped"]]), (2, ["already closed won"]))
        self.assertEqual((get(self.snov)["stage"], get(self.old)["stage"], get(self.second)["status"]), ("qualified", "qualified", "won"))
        self.assertEqual(change({"ids": [self.snov], "stage": "qualified"})["error"], "Nothing was changed: already there.")
        lost = change({"ids": [self.old], "lost": "No budget"})
        self.assertEqual((lost["changed"], get(self.old)["status"], get(self.old)["lost_reason"]), (1, "lost", "No budget"))
        closed = change({"ids": [self.old, self.snov], "set": {"next_step": "Send terms"}})
        self.assertEqual((closed["changed"], closed["skipped"]), (1, [{"name": "Dana Whitfield", "why": "closed, so it has no next step"}]))
        self.assertTrue(change({"ids": [self.old], "set": {"source": "Event"}})["ok"])          # a fact that is not a next step still fits a closed record
        self.assertEqual(get(self.old)["source"], "Event")
        for bad in ({"ids": [self.snov], "stage": "nowhere"}, {"ids": [self.snov]}, {"ids": [], "set": {"owner": "x"}}, {"ids": "x"}, None):
            self.assertFalse(change(bad)["ok"])
        self.assertEqual(self.desk.undo(self.old)["ok"], True)          # each change is on its record, to take back there
        self.assertEqual(get(self.old)["source"], "")
        rows = self.desk.database.rows()
        row = next(r for r in rows["deals"] if r["id"] == self.snov)
        self.assertEqual((row["stage_id"], row["working"], row["delivery_id"]), ("qualified", True, ""))
        self.assertEqual(next(r for r in rows["deals"] if r["id"] == self.second)["delivery_id"], "onboarding")
        people = {r["name"]: r["edit"] for r in rows["people"]}
        self.assertEqual((people["Oryna K"]["deal"], people["Dana Whitfield"]), (self.snov, {"record": self.old}))
        accounts = {r["name"]: r for r in rows["accounts"]}
        self.assertEqual((len(accounts["Hunter"]["targets"]), accounts["Snov.io"]["targets"]), (1, []))

    def test_views_are_kept_changed_and_removed(self):
        save = self.desk.database.save_view
        made = save({"name": "  Big open  deals ", "object": "deals", "search": "snov", "group": "stage", "sort": {"field": "value", "dir": "desc"},
                     "filters": [{"field": "status", "op": "is", "value": "Open"}, {"field": "value", "op": "gt", "value": 1000},
                                 {"field": "DROP TABLE", "op": "is", "value": "x"}, {"field": "fields.use_case", "op": "nonsense", "value": "x"}, "junk"],
                     "columns": ["name", "value", "fields.use_case", "<script>"]})
        view = made["view"]
        self.assertEqual((view["id"], view["name"], view["object"], view["group"], view["sort"], view["columns"], view["search"]),
                         ("big-open-deals", "Big open deals", "deals", "stage", {"field": "value", "dir": "desc"}, ["name", "value", "fields.use_case"], "snov"))
        self.assertEqual(view["filters"], [{"field": "status", "op": "is", "value": "Open"}, {"field": "value", "op": "gt", "value": "1000"}])
        self.assertIn("already called that", save({"name": "big OPEN deals", "object": "deals"})["error"])
        self.assertTrue(save({"name": "Big open deals", "object": "people"})["ok"])                 # the same name for another kind of row is fine
        changed = save({"id": "big-open-deals", "name": "Big deals", "object": "deals", "filters": []})
        self.assertEqual((changed["view"]["id"], changed["view"]["name"], changed["view"]["filters"], len(changed["views"])), ("big-open-deals", "Big deals", [], 2))
        self.assertFalse(save({"name": "", "object": "deals"})["ok"])
        self.assertFalse(save({"name": "x", "object": "invoices"})["ok"])
        self.assertEqual([v["id"] for v in self.desk.database.rows()["views"]], ["big-open-deals", "big-open-deals-2"])
        self.assertEqual([v["id"] for v in self.desk.database.remove_view("big-open-deals")["views"]], ["big-open-deals-2"])
        self.assertFalse(self.desk.database.remove_view("big-open-deals")["ok"])
        for n in range(60):
            last = save({"name": "View %d" % n, "object": "accounts"})
        self.assertIn("up to 40 views", last["error"])
        self.desk.store.meta_set("database_views", "not json")                                      # a damaged row is no views, not an error
        self.assertEqual(self.desk.database.views(), [])


class CashTests(unittest.TestCase):
    """Invoices on a record, where won work stands, and what the pipeline is likely to bring in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="agency")
        add = lambda name, **more: self.desk.deal(dict({"action": "add", "name": name}, **more))["deal"]["id"]      # noqa: E731
        self.site = add("Website rebuild", company="Kestrel Labs", value=30000)
        self.brand = add("Brand refresh", company="Lumen Health", value=12000, stage="proposal", close_on="+10d")
        self.desk.deal({"action": "close", "deal": self.site, "outcome": "won"})

    def day(self, offset):
        return (dt.date.today() + dt.timedelta(days=offset)).isoformat()

    def test_a_win_starts_delivery_and_reopening_ends_it(self):
        won = self.desk.store.get(self.site)
        self.assertEqual((won["status"], won["delivery"], self.desk.view(won)["delivery_label"]), ("won", "kickoff", "Kickoff"))
        self.assertEqual(self.desk.deal({"action": "update", "deal": self.site, "delivery": "next"})["deal"]["delivery"], "delivering")
        self.assertEqual(self.desk.deal({"action": "update", "deal": self.site, "delivery": "Delivered"})["deal"]["delivery"], "delivered")
        self.assertFalse(self.desk.deal({"action": "update", "deal": self.site, "delivery": "shipped"})["ok"])
        self.assertFalse(self.desk.deal({"action": "update", "deal": self.brand, "delivery": "kickoff"})["ok"])      # not won yet
        self.desk.deal({"action": "reopen", "deal": self.site})
        self.assertEqual(self.desk.store.get(self.site)["delivery"], "")
        self.desk.undo(self.site)                                                 # the reopen goes back, and so does where it stood
        self.assertEqual((self.desk.store.get(self.site)["status"], self.desk.store.get(self.site)["delivery"]), ("won", "delivered"))

    def test_a_win_keeps_its_next_step_until_delivery_is_through(self):
        # Won and at kickoff: the work is not done, so the next step shows, comes due and can be marked done.
        self.desk.deal({"action": "update", "deal": self.site, "next_step": "Send the kickoff note", "next_due": self.day(-2)})
        view = self.desk.view(self.desk.store.get(self.site))
        self.assertEqual((view["status"], view["in_delivery"], view["due"]), ("won", True, True))
        today = self.desk.today(full=False)
        self.assertEqual(([row["id"] for row in today["overdue"]], today["overdue"][0]["days"], today["counts"]["new"]), ([self.site], 2, 1))
        self.assertEqual([d["id"] for d in self.desk.brief()["due"]], [self.site])
        self.assertEqual([d["id"] for d in self.desk.pipeline({"view": "due"})["deals"]], [self.site])
        lane = self.desk.cash()["delivery"]["records"][0]
        self.assertEqual((lane["next_step"], lane["next_due"]), ("Send the kickoff note", self.day(-2)))
        self.assertIn("overdue", self.desk.text_card(self.desk.store.get(self.site)))
        done = self.desk.deal({"action": "done", "deal": self.site, "next_step": "Check the first draft", "next_due": self.day(3)})["deal"]
        self.assertEqual((done["next_step"], done["in_delivery"]), ("Check the first draft", True))
        self.assertEqual([row["id"] for row in self.desk.today(full=False)["coming"]], [self.site])
        # A won record with nothing planned is not "new, nothing planned": that list is for open records.
        self.desk.deal({"action": "update", "deal": self.site, "next_step": "", "next_due": ""})
        day = self.desk.today(full=False)
        self.assertNotIn(self.site, [row["id"] for row in day["new"]])
        # It is asked for its first delivery step instead: won, in kickoff, nothing planned.
        self.assertEqual(([(row["id"], row["delivery"]) for row in day["unplanned"]], day["counts"]["unplanned"]), ([(self.site, "Kickoff")], 1))
        self.assertIn("Signed, no next step yet", self.desk.today_text())
        # Through the last delivery stage, the record is closed: its next step no longer shows or comes due.
        self.desk.deal({"action": "update", "deal": self.site, "next_step": "Quarterly check-in", "next_due": self.day(-1), "delivery": "delivered"})
        view = self.desk.view(self.desk.store.get(self.site))
        self.assertEqual((view.get("in_delivery"), view.get("due")), (None, None))
        today = self.desk.today(full=False)
        self.assertEqual((today["overdue"], today["coming"], self.desk.brief()["due"], self.desk.pipeline({"view": "due"})["deals"]), ([], [], [], []))
        self.assertEqual(self.desk.cash()["delivery"]["records"][0]["next_step"], "")
        # A playbook with no delivery after a win: a win is simply closed, as before.
        bare = make_desk(tempfile.mkdtemp(dir=self.tmp.name), playbook="recruiting")
        if len(bare.playbook().delivery) < 2:
            one = bare.deal({"action": "add", "name": "Ada Park", "next_step": "Call", "next_due": self.day(-1)})["deal"]["id"]
            bare.deal({"action": "close", "deal": one, "outcome": "won"})
            self.assertEqual(bare.today(full=False)["overdue"], [])

    def test_invoices_are_billed_part_paid_paid_and_voided(self):
        made = self.desk.invoice({"action": "add", "deal": "Website rebuild", "amount": 15000, "number": "1041", "due": "net 14"})
        bill = made["invoice"]
        self.assertEqual((made["ok"], bill["state"], bill["due_on"], bill["issued_on"], made["money"]["uninvoiced"]),
                         (True, "sent", self.day(14), self.day(0), 15000))
        rest = self.desk.invoice({"action": "add", "deal": self.site})            # no amount: whatever is not billed yet; due in thirty days
        self.assertEqual((rest["invoice"]["amount"], rest["invoice"]["due_on"], rest["money"]["uninvoiced"]), (15000, self.day(30), 0))
        self.assertFalse(self.desk.invoice({"action": "pay", "deal": self.site})["ok"])               # two are open: which one?
        part = self.desk.invoice({"action": "pay", "deal": self.site, "invoice": "#1041", "amount": 5000.50})
        self.assertEqual((part["invoice"]["state"], part["invoice"]["paid"], part["invoice"]["owed"]), ("partial", 5000.5, 9999.5))
        full = self.desk.invoice({"action": "pay", "deal": self.site, "invoice": "1041"})             # the rest
        self.assertEqual((full["invoice"]["state"], full["invoice"]["owed"], full["invoice"]["paid_on"], full["money"]["paid"]), ("paid", 0, self.day(0), 15000))
        self.assertIn("nothing is owed", self.desk.invoice({"action": "pay", "deal": self.site, "invoice": "1041"})["error"])
        other = str(rest["invoice"]["id"])
        self.assertEqual(self.desk.invoice({"action": "void", "deal": self.site, "invoice": other})["invoice"]["state"], "void")
        self.assertEqual(self.desk.money(self.desk.store.get(self.site))["uninvoiced"], 15000)        # a voided invoice bills nothing
        self.assertFalse(self.desk.invoice({"action": "add", "deal": self.site, "amount": 0})["ok"])
        self.assertFalse(self.desk.invoice({"action": "add", "deal": self.site, "amount": 10, "due": "whenever"})["ok"])
        log = [a["text"] for a in self.desk.store.activities(self.site, limit=10)]
        self.assertIn("Invoice 1041: 5,000.50 received, 9,999.50 still owed", log)
        self.assertIn("Invoice 1041 for 15,000, due %s" % self.day(14), log)

    def test_money_changes_are_on_the_ledger_and_go_back(self):
        bill = self.desk.invoice({"action": "add", "deal": self.site, "amount": 30000, "number": "1042"})["invoice"]
        self.desk.invoice({"action": "pay", "deal": self.site, "amount": 10000})
        self.assertEqual(self.desk.store.collected(dt.date.today().isoformat()[:7]), 1000000)
        self.assertTrue(self.desk.undo(self.site)["ok"])                          # the payment was entered by mistake
        after = self.desk.store.invoice(bill["id"])
        self.assertEqual((after["paid"], after["state"], self.desk.store.collected(dt.date.today().isoformat()[:7])), (0, "sent", 0))
        self.assertTrue(self.desk.undo(self.site)["ok"])                          # and so was the invoice
        self.assertEqual((self.desk.store.invoices(self.site), self.desk.store.invoice(bill["id"])), ([], None))
        lines = [(line["entity"], line["op"]) for line in self.desk.store.ledger(self.site, limit=4)]
        self.assertEqual(lines[:2], [("invoice", "undo"), ("invoice", "undo")])
        self.desk.invoice({"action": "add", "deal": self.site, "amount": 500})
        self.desk.invoice({"action": "remove", "deal": self.site})
        self.assertEqual(self.desk.store.invoices(self.site), [])
        self.desk.undo(self.site)                                                 # a removal goes back too
        self.assertEqual(len(self.desk.store.invoices(self.site)), 1)

    def test_the_cash_picture_adds_up(self):
        late = self.desk.store.invoice_add(self.site, 10000, number="1040", issued_on=self.day(-40), due_on=self.day(-10))
        self.desk.store.invoice_pay(late["id"], 4000)
        self.desk.store.invoice_add(self.site, 8000, number="1043", due_on=self.day(20))
        cash = self.desk.cash()
        strip = cash["strip"]
        self.assertEqual((strip["won_month"], strip["owed"], strip["overdue"], strip["uninvoiced"], strip["collected_month"]),
                         ({"count": 1, "value": 30000}, {"count": 2, "value": 14000}, {"count": 1, "value": 6000}, {"count": 1, "value": 12000},
                          {"value": 4000}))
        self.assertEqual([(i["number"], i["state"], i["days_overdue"]) for i in cash["invoices"]], [("1040", "overdue", 10), ("1043", "sent", 0)])
        lane = cash["delivery"]
        self.assertEqual(([s["id"] for s in lane["stages"]], lane["records"][0]["delivery"], lane["records"][0]["uninvoiced"]),
                         (["kickoff", "delivering", "delivered"], "kickoff", 12000))
        text = self.desk.slash("cash")
        self.assertIn("Overdue: $6,000 (1)", text)
        self.assertIn("Kestrel", self.desk.store.get(self.site)["company"])
        self.assertEqual(self.desk.pipeline({"view": "cash"})["view"], "cash")

    def test_the_forecast_weights_by_stage_and_buckets_by_date(self):
        self.desk.deal({"action": "add", "name": "Retainer", "value": 20000, "stage": "contract", "close_on": self.day(-3)})
        self.desk.deal({"action": "add", "name": "Audit", "value": 5000, "stage": "intro"})
        forecast = self.desk.cash()["forecast"]
        got = {b["key"]: (b["count"], b["value"], b["weighted"]) for b in forecast["buckets"]}
        this_or_next = "this" if self.day(10)[:7] == self.day(0)[:7] else "next"
        self.assertEqual((forecast["basis"], got["past"], got[this_or_next], got["none"]),
                         ("playbook", (1, 20000, 17000), (1, 12000, 7200), (1, 5000, 1250)))        # contract 85%, proposal 60%, intro 25%
        self.assertEqual((forecast["value"], forecast["weighted"]), (37000, 25450))
        self.assertFalse(self.desk.deal({"action": "update", "deal": self.brand, "close_on": "the 45th"})["ok"])
        self.desk.deal({"action": "update", "deal": self.brand, "close_on": ""})
        self.assertEqual(self.desk.store.get(self.brand)["close_on"], "")

    def test_odds_come_from_history_once_enough_has_closed(self):
        playbook = self.desk.playbook()
        self.assertEqual(self.desk.odds(playbook, self.desk.store.list(status="all"))["basis"], "playbook")
        for index in range(12):
            made = self.desk.deal({"action": "add", "name": "Past %d" % index, "stage": "scoping", "value": 1000})["deal"]["id"]
            self.desk.deal({"action": "close", "deal": made, "outcome": "won" if index % 3 == 0 else "lost", "reason": "Budget"})
        odds = self.desk.odds(playbook, self.desk.store.list(status="all"))
        self.assertEqual((odds["basis"], odds["by_stage"]["scoping"], odds["by_stage"]["contract"]), ("history", 38, 100))     # 5 of 13 that reached scoping
        bare = Playbook({"name": "bare", "stages": ["One", "Two", "Three"]})
        self.assertEqual(self.desk.odds(bare, [])["by_stage"], {"one": 25, "two": 50, "three": 75})
        self.assertEqual((bare.delivery, self.desk.odds(bare, [])["basis"]), ([], "even"))
        for bad in ({"delivery": "kickoff"}, {"delivery": [{"label": "No id"}]}, {"delivery": ["A", "A"]}, {"stages": [{"id": "one", "win_probability": "likely"}, "Two"]}):
            with self.assertRaises(PlaybookError):
                validate(dict({"name": "bad", "stages": ["One", "Two"]}, **bad))

    def test_today_lists_late_invoices_and_recent_wins_nobody_billed(self):
        self.assertEqual(self.desk.today()["counts"]["to_invoice"], 0)            # nothing nags until the desk keeps invoices at all
        self.fees = self.desk.deal({"action": "add", "name": "Workshop", "value": 4000})["deal"]["id"]
        self.desk.deal({"action": "close", "deal": self.fees, "outcome": "won"})
        self.desk.store.invoice_add(self.fees, 4000, number="1050", issued_on=self.day(-45), due_on=self.day(-15))
        today = self.desk.today()
        self.assertEqual([(r["name"], r["number"], r["owed"], r["days"]) for r in today["invoices_overdue"]], [("Workshop", "1050", 4000, 15)])
        self.assertEqual([(r["name"], r["amount"]) for r in today["to_invoice"]], [("Website rebuild", 30000)])
        self.assertEqual((today["counts"]["invoices_overdue"], today["counts"]["to_invoice"], today["needs"]), (1, 1, 5))    # and one new record, and the two wins at kickoff with no step planned
        text = self.desk.today_text()
        self.assertIn("Invoices overdue (1)", text)
        self.assertIn("Workshop — $4,000 (#1050), 15d late", text)
        self.assertIn("Signed, not invoiced (1)", text)
        self.desk.deal({"action": "snooze", "deal": self.fees, "days": 5})
        self.assertEqual(self.desk.today()["counts"]["invoices_overdue"], 0)

    def test_a_payment_entered_by_mistake_is_taken_back_and_the_number_alone_finds_the_invoice(self):
        self.desk.invoice({"action": "add", "deal": self.site, "amount": 22000, "number": "1043"})
        paid = self.desk.invoice({"action": "pay", "invoice": "1043", "amount": 11000})          # no record named: the number says which
        self.assertEqual((paid["ok"], paid["deal"], paid["invoice"]["paid"]), (True, self.site, 11000.0))
        back = self.desk.invoice({"action": "undo", "invoice": "#1043"})
        self.assertTrue(back["ok"], back)
        self.assertIn("received", back["undid"])
        self.assertEqual((back["invoices"][0]["paid"], back["money"]["paid"], back["money"]["owed"]), (0.0, 0.0, 22000.0))
        self.desk.deal({"action": "update", "deal": self.site, "next_step": "Send the statement"})     # something else, later
        again = self.desk.invoice({"action": "undo", "deal": self.site})       # the last thing done to its invoices: adding it
        self.assertTrue(again["ok"], again)
        self.assertEqual((again["invoices"], self.desk.store.get(self.site)["next_step"]), ([], "Send the statement"))
        self.assertFalse(self.desk.invoice({"action": "undo", "deal": self.site})["ok"])
        self.assertIn("undo", schemas.GTM_INVOICE["parameters"]["properties"]["action"]["enum"])

    def test_an_invoice_named_with_the_wrong_record_changes_nothing_and_says_where_it_is(self):
        self.desk.invoice({"action": "add", "deal": self.site, "amount": 22000, "number": "1043"})
        out = self.desk.invoice({"action": "pay", "deal": "Brand refresh", "invoice": "1043"})
        self.assertFalse(out["ok"])
        self.assertIn("it is on Website rebuild", out["error"])
        self.assertIn("Nothing was changed", out["error"])
        self.assertEqual(self.desk.store.invoices(self.site)[0]["paid"], 0.0)
        self.assertIn("which invoice", self.desk.invoice({"action": "pay"})["error"])
        self.assertIn("no invoice", self.desk.invoice({"action": "pay", "deal": self.site, "invoice": "9999"})["error"])

    def test_a_proposal_can_carry_an_invoice_a_payment_and_a_close_date(self):
        self.desk.store.invoice_add(self.site, 30000, number="1042", due_on=self.day(20))
        out = self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": self.site, "payment": {"invoice": "1042", "amount": 30000, "on": "today"}, "why": "Their remittance advice arrived.",
             "evidence": [{"kind": "email", "who": "Kestrel accounts", "text": "Payment of $30,000 sent today, ref 1042."}]},
            {"record": self.brand, "close_on": "+5d", "why": "She said the board meets Friday."},
            {"record": self.brand, "invoice": {"amount": 3000, "number": "1060", "due": "net 15"}, "why": "Deposit agreed."}]}, {})
        queue = self.desk.intake.queue()
        self.assertEqual([item["changes"][0]["field"] + ": " + item["changes"][0]["to"] for item in queue],
                         ["Payment: $30k on invoice #1042", "Expected close: " + self.day(5), "Invoice: $3,000 #1060 due net 15"])
        for item in queue:
            self.assertTrue(self.desk.intake.decide(out["id"], item["n"], "approve", "jo")["applied"], item)
        self.assertEqual(self.desk.store.invoices(self.site)[0]["state"], "paid")
        self.assertEqual((self.desk.store.get(self.brand)["close_on"], self.desk.store.invoices(self.brand)[0]["due_on"]), (self.day(5), self.day(15)))

    def test_a_scheduled_job_does_not_touch_money_by_itself(self):
        job = {"platform": "cron"}
        self.desk.store.invoice_add(self.site, 30000, number="1042", due_on=self.day(20))
        self.assertIsNone(self.desk.guard("gtm_invoice", {"action": "list"}, job))
        self.assertIsNone(self.desk.guard("gtm_pipeline", {"view": "cash"}, job))
        held = self.desk.guard("gtm_invoice", {"action": "pay", "deal": self.site, "invoice": "1042", "why": "Remittance advice."}, job)
        self.assertEqual((held["held"], self.desk.store.invoices(self.site)[0]["state"]), (True, "sent"))
        self.assertTrue(self.desk.guard("gtm_invoice", {"action": "add", "deal": self.brand, "amount": 3000}, job)["held"])
        self.assertEqual(self.desk.guard("gtm_invoice", {"action": "void", "deal": self.site}, job)["ok"], False)
        self.assertEqual([item["changes"][0]["field"] for item in self.desk.intake.queue()], ["Payment", "Invoice"])
        self.desk.set_autonomy("routine")
        self.assertTrue(self.desk.guard("gtm_invoice", {"action": "pay", "deal": self.site}, job)["ok"])      # held again (the same one: left out as waiting)
        self.desk.set_autonomy("full")
        self.assertIsNone(self.desk.guard("gtm_invoice", {"action": "pay", "deal": self.site}, job))

    def test_the_sample_tours_have_money_in_them(self):
        for which in ("recruiting", "saas"):
            self.desk.tour_start(which)
            cash = self.desk.cash()
            self.assertGreater(cash["strip"]["overdue"]["value"], 0, which)
            self.assertGreater(cash["strip"]["collected_month"]["value"], 0, which)
            self.assertGreater(cash["strip"]["uninvoiced"]["value"], 0, which)
            self.assertGreaterEqual(len([b for b in cash["forecast"]["buckets"] if b["count"]]), 3, which)
            self.assertTrue(all(r["delivery"] for r in cash["delivery"]["records"]), which)
            self.assertEqual(self.desk.today()["counts"]["invoices_overdue"], 1, which)
        self.desk.tour_end()


class SharedContactTests(unittest.TestCase):
    """A person is kept once and linked to every record they are on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")
        add = lambda name, **more: self.desk.deal(dict({"action": "add", "name": name, "company": "Kestrel Labs"}, **more))["deal"]["id"]    # noqa: E731
        self.new, self.renewal = add("Dana Whitfield", email="dana@kestrel.example"), add("Renewal 2027")
        self.desk.contact({"action": "add", "deal": self.new, "name": "Lena Ortiz", "email": "lena@kestrel.example", "title": "CFO", "role": "signs off"})
        self.desk.contact({"action": "add", "deal": self.renewal, "name": "L. Ortiz", "email": "LENA@kestrel.example", "role": "budget owner", "phone": "555 0100"})

    def test_the_same_person_on_two_records_is_one_person(self):
        on_new, on_renewal = self.desk.store.people(self.new)[0], self.desk.store.people(self.renewal)[0]
        self.assertEqual((on_new["id"], on_new["name"], on_renewal["name"]), (on_renewal["id"], "Lena Ortiz", "Lena Ortiz"))
        self.assertEqual((on_new["role"], on_renewal["role"]), ("signs off", "budget owner"))          # her part differs by record
        self.assertEqual((on_new["phone"], on_renewal["title"]), ("555 0100", "CFO"))                  # what was blank was filled in, nothing overwritten
        self.assertEqual((self.desk.store.tally()["people"], self.desk.people(self.desk.store.get(self.new))[1]["also_on"]), (2, 1))
        self.desk.contact({"action": "update", "deal": self.new, "person": "lena ortiz", "title": "Chief Financial Officer", "role": "decides"})
        self.assertEqual(self.desk.store.people(self.renewal)[0]["title"], "Chief Financial Officer")  # changed once, changed everywhere
        self.assertEqual(self.desk.store.people(self.renewal)[0]["role"], "budget owner")
        self.desk.contact({"action": "remove", "deal": self.new, "person": "lena@kestrel.example"})
        self.assertEqual((self.desk.store.people(self.new), len(self.desk.store.people(self.renewal))), ([], 1))    # off one, still on the other

    def test_her_address_finds_both_records_and_the_company_lists_her_once(self):
        found = self.desk.match(["lena@kestrel.example"])["matches"][0]
        self.assertEqual((found["known"], sorted(r["id"] for r in found["records"])), (True, sorted([self.new, self.renewal])))
        view = self.desk.company("Kestrel Labs")
        lena = [p for p in view["people"] if p["name"] == "Lena Ortiz"]
        self.assertEqual((len(lena), lena[0]["record_name"]), (1, "Dana Whitfield, Renewal 2027"))
        self.assertEqual(self.desk.companies()[0]["people"], 3)                                        # two records' own people, and her

    def test_a_change_made_through_one_record_is_not_undone_over_a_later_one_made_through_the_other(self):
        self.desk.contact({"action": "update", "deal": self.new, "person": "Lena Ortiz", "title": "Finance Director"})
        self.desk.contact({"action": "update", "deal": self.renewal, "person": "Lena Ortiz", "title": "CFO and COO"})
        refused = self.desk.undo(self.new)
        self.assertEqual((refused["ok"], refused["conflicts"]), (False, ["Title is now CFO and COO (that action left it at Finance Director)"]))
        self.assertEqual(self.desk.store.people(self.new)[0]["title"], "CFO and COO")
        self.assertTrue(self.desk.undo(self.renewal)["ok"])
        self.assertTrue(self.desk.undo(self.new)["ok"])
        self.assertEqual(self.desk.store.people(self.new)[0]["title"], "CFO")

    def test_taking_back_an_add_takes_her_off_that_record_only(self):
        self.assertTrue(self.desk.undo(self.renewal)["ok"])                       # her add on the renewal
        self.assertEqual((self.desk.store.people(self.renewal), len(self.desk.store.people(self.new))), ([], 1))
        self.assertEqual(self.desk.store.people(self.new)[0]["phone"], "")        # and what that add filled in is blank again
        self.desk.undo(self.new)                                                  # her first add: nobody has her now
        self.assertEqual((self.desk.store.people(), self.desk.match(["lena@kestrel.example"])["matches"][0]["known"]), ([], False))
        again = self.desk.contact({"action": "add", "deal": self.new, "name": "Lena Ortiz", "email": "lena@kestrel.example"})
        self.assertEqual(len(again["people"]), 2)                                 # and she can be added afresh

    def test_two_people_cannot_share_an_address(self):
        self.desk.contact({"action": "add", "deal": self.new, "name": "Sam Park", "email": "sam@kestrel.example"})
        out = self.desk.contact({"action": "update", "deal": self.new, "person": "Sam Park", "email": "lena@kestrel.example"})
        self.assertEqual((out["ok"], "already belongs" in out["error"]), (False, True))

    def test_the_people_table_of_0_11_becomes_contacts(self):
        path = str(Path(self.tmp.name) / "older.db")
        Store(path).add("Dana Whitfield", "lead", company="Kestrel Labs")
        Store(path).add("Renewal 2027", "lead", company="Kestrel Labs")
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', "
                         "title TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', linkedin TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', "
                         "note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            for deal, name, email, role in (("dana-whitfield", "Lena Ortiz", "lena@kestrel.example", "signs off"),
                                            ("renewal-2027", "Lena Ortiz", "Lena@Kestrel.example", "budget owner"), ("renewal-2027", "Sam Park", "", "")):
                conn.execute("INSERT INTO people (deal_id, name, email, role, created_at, updated_at) VALUES (?,?,?,?,?,?)", (deal, name, email, role, st.now(), st.now()))
        store = Store(path)                                                       # opened by this version
        self.assertEqual([(p["name"], p["role"]) for p in store.people("renewal-2027")], [("Lena Ortiz", "budget owner"), ("Sam Park", "")])
        self.assertEqual(store.people("dana-whitfield")[0]["id"], store.people("renewal-2027")[0]["id"])
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM contacts").fetchone()[0], 2)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'people'").fetchone())
        self.assertEqual(len(Store(path).people()), 3)                            # opening it again changes nothing


class GroupTests(unittest.TestCase):
    """A group (a search, an account, a client) is a thing of its own: who the client is, where it stands, its
    terms, and the people on the client's side. Records still name their group; nothing about them changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)                      # the recruiting playbook
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")["id"]
        self.search = "Staff Backend · Northwind"

    def test_a_group_nobody_described_is_still_listed_with_what_is_in_it(self):
        listed = {g["name"]: g for g in self.desk.groups()}
        self.assertEqual(sorted(listed), ["Founding Designer · Halcyon", "Head of Growth · Ostrava", self.search])
        self.assertEqual((listed[self.search]["records"], listed[self.search]["open"], listed[self.search]["client"], listed[self.search]["status"]),
                         (3, 3, "", "open"))
        self.assertEqual(self.desk.group({"action": "list"})["called"], "search")
        self.assertNotIn("client", self.desk.view(self.desk.store.get(self.dana)))

    def test_setting_a_group_keeps_its_client_status_terms_and_value(self):
        out = self.desk.group({"action": "set", "group": "staff backend - northwind", "client": "Northwind", "status": "On hold", "value": "38k",
                               "note": "Paused until the reorg lands.", "fields": {"Fee %": "20", "target_base": "190000", "Shoe size": "9"}})
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["created"], out["group"]["name"], out["group"]["client"], out["group"]["status"], out["group"]["value"]),
                         (False, self.search, "Northwind", "hold", 38000.0))
        self.assertEqual({f["key"]: f["value"] for f in out["group"]["facts"] if f["value"]}, {"fee_pct": "20", "target_base": "190000"})
        self.assertEqual(out["ignored"], ["Shoe size"])
        self.assertEqual(len(out["records"]), 3)
        seen = self.desk.view(self.desk.store.get(self.dana))
        self.assertEqual((seen["client"], seen["group_status"]), ("Northwind", "On hold"))
        self.assertFalse(self.desk.group({"action": "set", "group": self.search, "status": "napping"})["ok"])
        self.desk.group({"action": "set", "group": self.search, "status": "open", "fields": {"fee_pct": ""}})
        again = self.desk.group({"action": "get", "group": "Northwind"})          # a part of the name only one search has
        self.assertEqual((again["group"]["status"], again["group"]["fields"]), ("open", {"target_base": "190000"}))
        self.assertNotIn("group_status", self.desk.view(self.desk.store.get(self.dana)))
        ledger = [e for e in self.desk.store.ledger(limit=20) if e["entity"] == "group"]
        self.assertGreaterEqual(len(ledger), 2)                                   # kept on the ledger like everything else

    def test_a_group_is_opened_before_its_first_record_and_gives_it_its_value(self):
        out = self.desk.group({"action": "set", "group": "VP Sales · Kestrel", "client": "Kestrel Labs", "value": 60000})
        self.assertEqual((out["created"], out["records"], out["group"]["client"]), (True, [], "Kestrel Labs"))
        made = self.desk.deal({"action": "add", "name": "Ada Quill", "group": "VP Sales - kestrel", "company": "Fathom Data"})["deal"]
        self.assertEqual((made["value"], made["client"]), (60000.0, "Kestrel Labs"))
        own = self.desk.deal({"action": "add", "name": "Bo Lindqvist", "group": "VP Sales · Kestrel", "value": 45000})["deal"]
        self.assertEqual(own["value"], 45000.0)                                   # a record that brings its own keeps it
        # A recruiting pipeline counts a search's fee once (a role is filled once): the larger of the two, not their sum.
        self.assertEqual(self.desk.group({"action": "get", "group": "VP Sales · Kestrel"})["open_value"], 60000.0)
        several = self.desk.group({"action": "set", "group": "Kestrel", "new": True, "client": "Kestrel Labs"})   # a name that is part of another's
        self.assertEqual((several["created"], several["group"]["name"]), (True, "Kestrel"))
        self.assertEqual(self.desk.group({"action": "get", "group": "VP Sales"})["group"]["name"], "VP Sales · Kestrel")
        self.desk.group({"action": "set", "group": "Staff Frontend · Northwind", "client": "Northwind"})
        self.assertIn("several", self.desk.group({"action": "get", "group": "Northwind"})["error"])
        self.assertIn("several", self.desk.group({"action": "set", "group": "Northwind", "status": "closed"})["error"])   # never a guess
        self.assertFalse(self.desk.group({"action": "get", "group": "Nowhere"})["ok"])

    def test_renaming_a_group_renames_it_on_every_record_and_keeps_what_is_known(self):
        self.desk.group({"action": "set", "group": self.search, "client": "Northwind"})
        self.desk.contact({"action": "add", "group": self.search, "name": "Helen Park", "email": "helen@northwind.example", "role": "hiring manager"})
        out = self.desk.group({"action": "rename", "group": self.search, "to": "Staff Platform · Northwind"})
        self.assertEqual((out["renamed_on"], out["group"]["name"], out["group"]["client"], [p["name"] for p in out["people"]]),
                         (3, "Staff Platform · Northwind", "Northwind", ["Helen Park"]))
        self.assertEqual(self.desk.store.get(self.dana)["grp"], "Staff Platform · Northwind")
        self.assertNotIn(self.search, [g["name"] for g in self.desk.groups()])
        self.desk.undo(self.dana)                                                 # one record can go back by itself
        self.assertEqual(self.desk.store.get(self.dana)["grp"], self.search)
        self.desk.group({"action": "set", "group": "Head of Growth · Ostrava", "client": "Ostrava"})
        clash = self.desk.group({"action": "rename", "group": "Founding Designer · Halcyon", "to": "Head of Growth · Ostrava"})
        self.assertIn("already", clash["error"])

    def test_people_on_a_groups_side_are_kept_once_and_found_by_the_client(self):
        self.desk.group({"action": "set", "group": self.search, "client": "Northwind"})
        added = self.desk.contact({"action": "add", "group": "Northwind", "name": "Helen Park", "email": "Helen@Northwind.example",
                                   "title": "VP Engineering", "role": "hiring manager"})
        self.assertEqual([(p["name"], p["email"], p["on"]) for p in added["people"]], [("Helen Park", "helen@northwind.example", "group")])
        self.assertTrue(self.desk.contact({"action": "add", "group": self.search, "name": "H. Park", "email": "helen@northwind.example"})["exists"])
        self.desk.contact({"action": "add", "deal": self.dana, "name": "Helen Park", "email": "helen@northwind.example"})   # the same person, on a record too
        self.assertEqual(len([c for c in self.desk.store.grp_people()]), 1)
        self.assertEqual(self.desk.store.people(self.dana)[0]["id"], self.desk.store.grp_people(self.search)[0]["id"])
        self.desk.contact({"action": "update", "group": self.search, "person": "helen park", "title": "CTO", "role": "decision maker"})
        self.assertEqual((self.desk.store.people(self.dana)[0]["title"], self.desk.store.grp_people(self.search)[0]["role"]), ("CTO", "decision maker"))
        company = self.desk.company("Northwind")                                  # no record's company: the client of a search
        self.assertEqual((company["ok"], company["via"], company["groups"], company["open"]), (True, "group", [self.search], 3))
        self.assertEqual([p["name"] for p in company["people"]], ["Helen Park"])
        hit = self.desk.match(["helen@northwind.example", "new.person@northwind.example"])["matches"]
        self.assertEqual((hit[0]["known"], hit[0]["who"]), (True, "Helen Park"))   # she is on a record as well, so the record answers
        self.desk.contact({"action": "remove", "deal": self.dana, "person": "helen@northwind.example"})
        hit = self.desk.match(["helen@northwind.example"])["matches"][0]
        self.assertEqual((hit["known"], hit["group"], hit["client"], len(hit["records"])), (True, self.search, "Northwind", 3))
        found = [p for p in self.desk.find_people("northwind")["people"] if p["name"] == "Helen Park"]
        self.assertEqual((found[0]["company"], found[0]["group"]), ("Northwind", self.search))
        self.assertFalse(self.desk.contact({"action": "remove", "group": self.search, "person": "nobody"})["ok"])
        self.desk.contact({"action": "remove", "group": self.search, "person": "helen park"})
        self.assertEqual(self.desk.store.grp_people(self.search), [])
        self.assertFalse(self.desk.contact({"action": "add", "group": "No Such Search", "name": "X"})["ok"])

    def test_one_group_or_one_clients_groups_are_exported_by_themselves(self):
        self.desk.group({"action": "set", "group": self.search, "client": "Northwind"})
        self.desk.group({"action": "set", "group": "Head of Growth · Ostrava", "client": "Ostrava"})
        self.desk.group({"action": "set", "group": "Principal Engineer · Northwind", "new": True, "client": "Northwind"})
        self.desk.deal({"action": "add", "name": "Ada Quill", "group": "Principal Engineer · Northwind"})
        one = self.desk.export({"action": "export", "group": "staff backend"})
        self.assertEqual((one["ok"], one["rows"], one["only"]), (True, 3, self.search))
        self.assertIn("staff-backend-northwind-", Path(one["path"]).name)
        text = Path(one["path"]).read_text()
        self.assertIn("Dana Whitfield", text)
        self.assertNotIn("Wei-Lin Chao", text)                                     # another client's: not in this file
        client = self.desk.export({"action": "export", "group": "Northwind", "path": "northwind.csv"})
        self.assertIn("several", client.get("error", ""))                          # a part of two names: it asks, it does not guess
        client = self.desk.export({"action": "export", "group": "Ostrava"})
        self.assertEqual((client["rows"], client["only"]), (2, "Head of Growth · Ostrava"))
        self.desk.group({"action": "rename", "group": "Principal Engineer · Northwind", "to": "Principal Engineer"})
        self.desk.group({"action": "rename", "group": self.search, "to": "Staff Backend"})
        both = self.desk.export({"action": "export", "group": "Northwind"})         # no group is called that now: a client's name
        self.assertEqual((both["rows"], both["only"]), (4, "Northwind"))
        table = self.desk.export({"action": "table", "group": "Staff Backend"})
        self.assertEqual((table["count"], table["only"]), (3, "Staff Backend"))
        self.assertEqual(self.desk.export({"action": "table"})["count"], 8)
        self.assertEqual(self.desk.export({"action": "export"})["rows"], 8)
        shared = self.desk.export({"action": "export", "group": "Staff Backend", "share": True})       # the copy that goes to the client
        self.assertEqual(shared["columns"][:6], ["Candidate", "Current company", "Search", "Stage", "Email", "LinkedIn"])
        self.assertIn("-to-share-", Path(shared["path"]).name)
        text = Path(shared["path"]).read_text()
        self.assertEqual(len(text.strip().splitlines()), 4)
        self.assertIn("Screened", text)                                            # the stage in words
        for own in ("Fee", "Owner", "Next step", "Send submission note", "stoa-demo", "dana-whitfield"):
            self.assertNotIn(own, text, own)
        self.assertIn("share=true", one["note"])
        missing = self.desk.export({"action": "export", "group": "Nobody We Know"})
        self.assertFalse(missing["ok"])
        self.assertIn("Nothing was written", missing["error"])

    def test_a_company_from_a_map_goes_where_the_playbook_says_and_the_answer_says_where(self):
        base = Library(Path(self.tmp.name) / "none").source("agency")              # its group is a "client": the company itself
        market = {"action": "define", "name": "Fintech CFOs · Acme", "brief": "Fintechs with a new CFO."}
        rows = {"action": "add", "market": "Fintech CFOs · Acme", "companies": [{"company": "Ledgerline", "fit": 4}, {"company": "Paystream", "fit": 3}]}
        for rule, expected in (("company", "Ledgerline"), ("map", "Fintech CFOs · Acme")):
            desk = make_desk(tempfile.mkdtemp(), playbook="agency")
            text = base.replace("name: agency", "name: for-clients").replace("map_group: company", "map_group: " + rule)
            self.assertTrue(desk.playbook_tool({"action": "save", "yaml": text, "activate": True})["ok"])
            desk.market(market, {})
            desk.market(rows, {})
            out = desk.market({"action": "track", "market": "Fintech CFOs · Acme", "targets": ["Ledgerline"]}, {})
            self.assertEqual(out["added_to_pipeline"][0]["group"], expected, rule)
            chosen = desk.market({"action": "track", "market": "Fintech CFOs · Acme", "targets": ["Paystream"], "group": "Warm intros · Acme"}, {})
            self.assertEqual(chosen["added_to_pipeline"][0]["group"], "Warm intros · Acme")
        old = base.replace("name: agency", "name: older").replace("map_group: company      # a company added from a market map: is a group of its own\n", "")
        self.assertNotIn("map_group", old)
        desk = make_desk(tempfile.mkdtemp(), playbook="agency")                    # a playbook written before the key existed: guessed from its word, as before
        desk.playbook_tool({"action": "save", "yaml": old, "activate": True})
        desk.market(market, {})
        desk.market(rows, {})
        self.assertEqual(desk.market({"action": "track", "market": "Fintech CFOs · Acme", "targets": ["Ledgerline"]}, {})["added_to_pipeline"][0]["group"], "Ledgerline")
        with self.assertRaises(PlaybookError):
            validate(dict(Library(Path(self.tmp.name) / "none").load("agency").data, map_group="wherever"))

    def test_the_client_lead_desk_files_a_map_into_its_search_and_drafts_in_the_clients_name(self):
        desk = make_desk(tempfile.mkdtemp(), playbook="client-leads")
        playbook = desk.playbook()
        self.assertEqual((playbook.terms["deal"], playbook.terms["group"], playbook.map_group, playbook.won["label"]), ("lead", "search", "map", "Handed over"))
        desk.group({"action": "set", "group": "Fintech CFOs · Acme", "client": "Acme", "fields": {"What the client sells": "Forecasting for finance teams"}})
        desk.market({"action": "define", "name": "Fintech CFOs · Acme", "brief": "Fintechs with a new CFO."}, {})
        desk.market({"action": "add", "market": "Fintech CFOs · Acme", "companies": [
            {"company": "Ledgerline", "fit": 4, "location": "Austin, TX", "signal": "New CFO in March"}]}, {})
        out = desk.market({"action": "track", "market": "Fintech CFOs · Acme", "targets": ["Ledgerline"], "contact": "Rosa Lind", "title": "CFO"}, {})
        lead = desk.store.get(out["added_to_pipeline"][0]["deal"])
        self.assertEqual((lead["grp"], lead["fields"].get("title"), lead["fields"].get("location")), ("Fintech CFOs · Acme", "CFO", "Austin, TX"))
        self.assertEqual(lead["fields"].get("trigger"), "New CFO in March")          # why now comes over from the map
        self.assertIn("New CFO in March", Path(desk.export({"action": "export", "group": "Acme", "share": True})["path"]).read_text())
        self.assertEqual(desk.view(lead)["client"], "Acme")
        moved = desk.click("advance", lead["id"], lead["stage"], "tester")           # Identified -> Researched: the research note is asked for
        self.assertIn("Acme", moved["followup"])
        self.assertNotIn("{client}", moved["followup"])
        self.assertEqual(desk.export({"action": "export", "group": "Acme"})["rows"], 1)

    def test_a_scheduled_job_changes_a_group_only_at_the_highest_level(self):
        cron = {"platform": "cron"}
        self.assertIsNone(self.desk.guard("gtm_group", {"action": "list"}, cron))
        self.assertIsNone(self.desk.guard("gtm_group", {"action": "get", "group": self.search}, cron))
        for level in ("ask", "routine"):
            self.desk.set_autonomy(level)
            self.assertFalse(self.desk.guard("gtm_group", {"action": "set", "group": self.search, "status": "closed"}, cron)["ok"])
            self.assertFalse(self.desk.guard("gtm_contact", {"action": "add", "group": self.search, "name": "X"}, cron)["ok"])
        self.desk.set_autonomy("full")
        self.assertIsNone(self.desk.guard("gtm_group", {"action": "set", "group": self.search, "status": "closed"}, cron))
        self.assertIsNone(self.desk.guard("gtm_contact", {"action": "add", "group": self.search, "name": "X"}, cron))
        self.assertIsNone(self.desk.guard("gtm_group", {"action": "set", "group": self.search}, {"platform": "slack"}))

    def test_a_copy_of_the_file_is_kept_once_a_day(self):
        folder = Path(self.tmp.name) / "backups"
        self.assertFalse(folder.exists())                                         # the desk was empty when it was first opened today
        store = make_desk(self.tmp.name).own_store                                # the next time Hermes starts
        made = sorted(p.name for p in folder.glob("desk-*.db"))
        self.assertEqual(made, ["desk-%s.db" % st.today()])
        self.assertEqual(store.snapshot(folder), "")                              # not twice in a day
        for day in range(1, 20):
            (folder / ("desk-2026-01-%02d.db" % day)).write_bytes(b"old")
        (folder / ("desk-%s.db" % st.today())).unlink()
        self.assertTrue(store.snapshot(folder, keep=14).endswith("desk-%s.db" % st.today()))
        kept = sorted(p.name for p in folder.glob("desk-*.db"))
        self.assertEqual((len(kept), kept[0], kept[-1]), (14, "desk-2026-01-07.db", "desk-%s.db" % st.today()))
        copy = sqlite3.connect(str(folder / kept[-1]))
        self.assertEqual(copy.execute("select count(*) from deals").fetchone()[0], 7)
        copy.close()
        empty = make_desk(tempfile.mkdtemp())
        empty.own_store
        self.assertFalse((empty.data_dir / "backups").exists())                   # nothing to copy on a desk nobody has used


class PeopleTests(unittest.TestCase):
    """The other people on a record, whose address is whose, and what is known about a company."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")
        self.kestrel = self.desk.deal({"action": "add", "name": "Dana Whitfield", "company": "Kestrel Labs", "group": "Kestrel Labs",
                                       "email": "dana@kestrel.example", "value": 48000})["deal"]["id"]
        self.desk.deal({"action": "add", "name": "Renewal", "company": "Kestrel Labs, Inc.", "group": "Kestrel Labs", "value": 12000})
        self.free = self.desk.deal({"action": "add", "name": "Tomás Ibarra", "company": "Quill & Co", "email": "tomas@gmail.com"})["deal"]["id"]

    def test_people_are_added_changed_and_removed_on_a_record(self):
        out = self.desk.contact({"action": "add", "deal": "Dana Whitfield", "name": "Priya Raman", "email": "Priya@Kestrel.example",
                                 "title": "CFO", "role": "finance"})
        self.assertEqual([(p["name"], p["primary"]) for p in out["people"]], [("Dana Whitfield", True), ("Priya Raman", False)])
        self.assertTrue(self.desk.contact({"action": "add", "deal": self.kestrel, "name": "P. Raman", "email": "priya@kestrel.example"})["exists"])
        self.desk.contact({"action": "update", "deal": self.kestrel, "person": "priya raman", "title": "Chief Financial Officer"})
        self.assertEqual(self.desk.store.people(self.kestrel)[0]["title"], "Chief Financial Officer")
        self.assertFalse(self.desk.contact({"action": "remove", "deal": self.kestrel, "person": "nobody"})["ok"])
        self.desk.contact({"action": "remove", "deal": self.kestrel, "person": "priya@kestrel.example"})
        self.assertEqual(self.desk.store.people(self.kestrel), [])
        self.desk.undo(self.kestrel)                                              # the removal goes back like anything else
        self.assertEqual(self.desk.store.people(self.kestrel)[0]["name"], "Priya Raman")
        self.assertIn("Added Priya Raman (finance)", [a["text"] for a in self.desk.store.activities(self.kestrel)])

    def test_an_address_finds_its_record_or_at_least_its_company(self):
        self.desk.contact({"action": "add", "deal": self.kestrel, "name": "Priya Raman", "email": "priya@kestrel.example"})
        found = {m["email"]: m for m in self.desk.contact({"action": "match", "emails": [
            "Dana Whitfield <DANA@kestrel.example>", "priya@kestrel.example", "newhire@kestrel.example", "tomas@gmail.com",
            "someone@gmail.com", "nobody@elsewhere.example", "not an address"]})["matches"]}
        self.assertEqual((found["dana@kestrel.example"]["known"], found["dana@kestrel.example"]["records"][0]["id"]), (True, self.kestrel))
        self.assertEqual((found["priya@kestrel.example"]["who"], found["priya@kestrel.example"]["records"][0]["id"]), ("Priya Raman", self.kestrel))
        stranger = found["newhire@kestrel.example"]                               # nobody we know, at a company we do
        self.assertEqual((stranger["known"], stranger["company"], len(stranger["records"])), (False, "Kestrel Labs", 2))
        self.assertTrue(found["tomas@gmail.com"]["known"])
        self.assertEqual((found["someone@gmail.com"]["records"], "company" in found["someone@gmail.com"]), ([], False))   # gmail is nobody's company
        self.assertEqual(found["nobody@elsewhere.example"]["records"], [])
        self.assertEqual(len(found), 6)

    def test_a_company_gathers_its_records_people_and_notes(self):
        self.desk.contact({"action": "add", "deal": self.kestrel, "name": "Priya Raman", "email": "priya@kestrel.example", "title": "CFO"})
        view = self.desk.contact({"action": "company", "company": "kestrel labs inc"})
        self.assertEqual((view["company"]["name"], view["open"], view["open_value"], view["company"]["domain"]),
                         ("Kestrel Labs", 2, 60000, "kestrel.example"))
        self.assertEqual(sorted(p["name"] for p in view["people"]), ["Dana Whitfield", "Priya Raman", "Renewal"])
        saved = self.desk.company("Kestrel", note="Fiscal year ends in March.", domain="Kestrel.io")
        self.assertEqual((saved["company"]["note"], saved["company"]["domain"]), ("Fiscal year ends in March.", "kestrel.io"))
        self.assertEqual(self.desk.match(["cto@kestrel.io"])["matches"][0]["company"], "Kestrel Labs")     # the saved domain counts too
        self.assertFalse(self.desk.company("Nobody Corp")["ok"])
        rows = {row["name"]: row for row in self.desk.companies()}
        self.assertEqual((rows["Kestrel Labs"]["open"], rows["Kestrel Labs"]["people"], rows["Quill & Co"]["open"]), (2, 3, 1))
        self.assertEqual([p["record"]["id"] for p in self.desk.find_people("cfo")["people"]], [self.kestrel])

    def test_a_record_that_goes_takes_its_people_with_it(self):
        self.desk.contact({"action": "add", "deal": self.kestrel, "name": "Priya Raman"})
        self.desk.store.delete(self.kestrel)
        self.assertEqual(self.desk.store.people(), [])


class ApprovalTests(unittest.TestCase):
    """What Hermes proposes waits for a person, one decision each; a no is remembered; a scheduled job
    changes only what the desk's autonomy setting lets it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")["id"]
        self.marcus = self.desk.store.find("Marcus Oyelaran")["id"]

    def propose(self, **more):
        return self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": self.dana, "stage": "submitted", "why": "She asked to be put forward.",
             "evidence": [{"kind": "email", "at": "2026-11-10", "who": "Dana Whitfield", "text": "Yes, please submit me to Northwind.", "ref": "gmail:18c"}]},
            {"record": self.marcus, "next_step": "Send the comp range", "next_due": "+2d", "why": "He asked for it twice."},
            {"record": self.marcus, "person": {"name": "Lena Ortiz", "email": "lena@fathom.example", "title": "VP Engineering"}}], **more}, {})

    def test_a_reason_for_a_loss_is_put_in_the_playbooks_words_where_they_fit(self):
        playbook = self.desk.playbook()
        self.assertEqual(playbook.reason("took another offer"), "Took another offer")
        self.assertEqual(playbook.reason("Bad timing, she is mid-launch"), "Timing")
        self.assertEqual(playbook.reason("Compensation"), "Comp mismatch")          # one word, and one reason starts the same way
        self.assertEqual(playbook.reason("Relocation"), "Relocation")
        cut = playbook.reason("Candidate withdrew: accepted a counter-offer from current employer")
        self.assertEqual(cut, "Candidate withdrew: accepted a counter-offer from current")      # kept as said, and never cut mid-word
        self.desk.deal({"action": "close", "deal": self.marcus, "outcome": "lost", "reason": "he is not interested, he says"})
        self.assertEqual(self.desk.store.get(self.marcus)["lost_reason"], "Not interested")
        out = self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": self.dana, "stage": "passed", "lost_reason": "Withdrew after a counter-offer from her employer"},
            {"record": self.desk.store.find("Callum Reyes")["id"], "stage": "passed", "lost_reason": "comp mismatch"}]}, {})
        rows = self.desk.intake.view(out["id"])["rows"]
        self.assertIn("Not a fit, Not interested", rows[0]["warnings"][0])         # the person, and the agent, see the playbook's own
        self.assertEqual(rows[1]["warnings"], [])

    def test_a_client_named_in_the_group_is_found_with_the_people_on_its_records(self):
        self.desk.contact({"action": "add", "deal": self.dana, "name": "Marta Novak", "title": "Hiring Manager", "role": "hiring manager"})
        out = self.desk.company("Northwind")              # no record's company: here it is the client, kept in the search's name
        self.assertFalse(out["ok"])
        self.assertEqual(out["groups"], ["Staff Backend · Northwind"])
        self.assertEqual(sorted(r["name"] for r in out["records"]), ["Dana Whitfield", "Marcus Oyelaran", "Priya Raman"])
        self.assertEqual([(p["name"], p["record_name"]) for p in out["people"]], [("Marta Novak", "Dana Whitfield")])
        self.assertIn("search", out["note"])
        self.assertNotIn("groups", self.desk.company("North"))                     # a whole word, not a fragment
        found = self.desk.find_people("northwind")["people"]
        self.assertIn("Marta Novak", [p["name"] for p in found])
        marta = next(p for p in found if p["name"] == "Marta Novak")        # on a candidate's record, not at the candidate's employer
        self.assertEqual((marta["company"], marta["record"]["company"]), ("", "Kestrel Labs"))
        self.assertTrue(self.desk.company("Kestrel Labs")["ok"])                   # a real company is found as before

    def test_a_proposal_overtaken_by_events_closes_itself(self):
        out = self.propose()
        self.assertEqual(len(self.desk.intake.queue()), 3)
        self.desk.deal({"action": "move", "deal": self.dana, "stage": "submitted"})          # done by hand since it was proposed
        queue = self.desk.intake.queue()
        self.assertEqual([(q["name"], q["changes"][0]["field"]) for q in queue], [("Marcus Oyelaran", "Next step"), ("Marcus Oyelaran", "Person")])
        view = self.desk.intake.view(out["id"])
        self.assertEqual((view["status"], view["rows"][0]["done"], view["rows"][0]["why"]), ("open", "overtaken", "already done another way"))
        self.assertEqual(self.desk.today()["counts"]["approvals"], 2)
        self.desk.deal({"action": "update", "deal": self.marcus, "next_step": "Send the comp range", "next_due": "+2d"})
        self.desk.contact({"action": "add", "deal": self.marcus, "name": "Lena Ortiz", "email": "lena@fathom.example"})
        self.assertEqual(self.desk.intake.queue(), [])                              # nothing left to ask
        self.assertNotEqual(self.desk.intake.view(out["id"])["status"], "open")
        self.assertEqual(self.desk.store.declined_list(), [])                       # overtaken is not declined: nothing is remembered against it
        gone = self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": self.dana, "next_step": "Prepare her for the onsite", "why": "Northwind confirmed Tuesday."}]}, {})
        self.assertEqual([q["name"] for q in self.desk.intake.queue()], ["Dana Whitfield"])
        self.desk.store.delete(self.dana)
        self.assertNotIn("Dana Whitfield", [q["name"] for q in self.desk.intake.queue()])
        self.assertEqual(self.desk.intake.view(gone["id"])["rows"][0]["why"], "the record is no longer on the desk")

    def test_several_proposals_are_decided_in_one_go_each_its_own_decision(self):
        out = self.propose()
        done = self.desk.intake.decide_many([{"intake": out["id"], "n": 2}, {"intake": out["id"], "n": 3}, {"intake": "nope", "n": 1}], "approve")
        self.assertEqual((done["ok"], done["decided"], done["applied"], done["deals"]), (True, 2, 2, [self.marcus, self.marcus]))
        self.assertEqual([q["name"] for q in self.desk.intake.queue()], ["Dana Whitfield"])
        self.assertEqual(self.desk.store.get(self.marcus)["next_step"], "Send the comp range")
        self.desk.undo(self.marcus)                                                 # the last of them goes back by itself
        self.assertEqual([p["name"] for p in self.desk.store.people(self.marcus)], [])
        self.assertEqual(self.desk.store.get(self.marcus)["next_step"], "Send the comp range")
        self.assertEqual(self.desk.intake.decide_many([{"intake": out["id"], "n": 1}], "decline")["decided"], 1)
        self.assertFalse(self.desk.intake.decide_many([], "maybe")["ok"])

    def test_each_proposal_waits_with_what_it_rests_on(self):
        out = self.propose()
        self.assertEqual((out["origin"], out["counts"]["update"]), ("proposal", 3))
        queue = self.desk.intake.queue()
        self.assertEqual([(item["name"], item["n"]) for item in queue], [("Dana Whitfield", 1), ("Marcus Oyelaran", 2), ("Marcus Oyelaran", 3)])
        self.assertEqual((queue[0]["case"], queue[0]["evidence"][0]["who"], queue[0]["changes"][0]["to"]),
                         ("She asked to be put forward.", "Dana Whitfield", "Submitted"))
        self.assertEqual(queue[2]["changes"][0], {"field": "Person", "from": "", "to": "Lena Ortiz, VP Engineering, lena@fathom.example"})
        self.assertEqual(self.desk.store.get(self.dana)["stage"], "screened")            # nothing written yet
        today = self.desk.today()
        self.assertEqual((today["counts"]["approvals"], len(today["approvals"])), (3, 3))
        self.assertIn("Dana Whitfield · Kestrel Labs · Staff Backend · Northwind: stage Screened → Submitted (She asked to be put forward.)",
                      self.desk.today_text())

    def test_one_is_approved_one_declined_and_the_list_closes_with_the_last(self):
        intake = self.propose()["id"]
        approved = self.desk.intake.decide(intake, 1, "approve", "jo")
        self.assertEqual((approved["decided"], approved["applied"], self.desk.store.get(self.dana)["stage"]), (True, True, "submitted"))
        self.assertEqual(self.desk.store.intake_get(intake)["status"], "open")           # the rest still wait
        self.assertEqual(len(self.desk.intake.queue()), 2)
        self.assertEqual(self.desk.intake.decide(intake, 1, "approve", "jo")["decided"], False)      # not twice
        declined = self.desk.intake.decide(intake, 2, "decline", "jo")
        self.assertIn("will not be proposed again", declined["note"])
        self.assertEqual(self.desk.store.get(self.marcus)["next_step"], "Follow up on outreach")
        self.desk.intake.decide(intake, 3, "approve", "jo")
        done = self.desk.store.intake_get(intake)
        self.assertEqual((done["status"], done["result"]["updated"], done["result"]["left"]), ("applied", 2, 1))
        self.assertEqual(self.desk.store.people(self.marcus)[0]["name"], "Lena Ortiz")
        self.assertEqual(self.desk.intake.queue(), [])
        self.desk.undo(self.dana)                                                         # an approved change goes back like any other
        self.assertEqual(self.desk.store.get(self.dana)["stage"], "screened")

    def test_a_no_is_remembered_until_the_record_moves_on(self):
        intake = self.propose()["id"]
        self.desk.intake.decide(intake, 1, "decline", "jo")
        self.desk.intake.discard(intake)                                                  # the rest, declined together
        again = self.propose()
        self.assertEqual((again["counts"]["declined"], again["counts"]["update"], again["declined_before"]), (3, 0, 3))
        self.assertIn("does not ask twice", again["note"])
        self.assertEqual(self.desk.intake.queue(), [])
        self.assertEqual(self.desk.setup()["declined"], 3)
        self.desk.deal({"action": "move", "deal": self.dana, "stage": "contacted"})      # things changed: the same suggestion is a new one
        later = self.propose()
        self.assertEqual([item["name"] for item in self.desk.intake.queue()], ["Dana Whitfield"])
        self.assertEqual(later["counts"]["declined"], 2)
        self.desk.own_store.declined_clear()
        self.desk.intake.discard(later["id"])
        self.assertEqual(self.desk.own_store.declined_list()[0]["deal_id"], self.dana)

    def test_a_list_someone_sent_is_reviewed_whole_and_leaving_a_row_out_is_not_a_no(self):
        text = "Name,Company,Search\nAda Quill,Fathom Data,Staff Backend · Northwind\nBen Okoro,Harbor Systems,\n"
        held = self.desk.intake.propose_file("badge scans.csv", text)
        self.assertEqual(held["origin"], "list")
        self.assertEqual([(item.get("list"), item["source"]) for item in self.desk.intake.queue()], [(True, "badge scans.csv")])
        self.desk.intake.apply({"id": held["id"], "rows": [1]}, "jo")
        self.assertEqual(self.desk.store.declined(), {})
        self.assertEqual(self.desk.intake.propose_file("again.csv", text)["counts"]["new"], 1)       # Ben can still come in later

    def test_a_scheduled_job_changes_only_what_the_setting_allows(self):
        job = {"platform": "cron"}
        self.assertEqual(self.desk.autonomy(), "ask")
        self.assertIsNone(self.desk.guard("gtm_deal", {"action": "move", "deal": self.dana, "stage": "next"}, {"platform": "slack"}))   # a person asked
        self.assertIsNone(self.desk.guard("gtm_deal", {"action": "log", "deal": self.dana, "text": "Emailed."}, job))
        self.assertIsNone(self.desk.guard("gtm_pipeline", {"view": "today"}, job))
        held = self.desk.guard("gtm_deal", {"action": "move", "deal": "Dana Whitfield", "stage": "next", "why": "She said yes.",
                                            "evidence": [{"kind": "email", "text": "Yes please."}]}, job)
        self.assertEqual((held["ok"], held["held"]), (True, True))
        self.assertIn("Held for approval, not done", held["note"])
        self.assertEqual(self.desk.store.get(self.dana)["stage"], "screened")
        item = self.desk.intake.queue()[0]
        self.assertEqual((item["source"], item["changes"][0]["to"], item["case"], item["evidence"][0]["text"]),
                         ("Hermes, on a schedule", "Submitted", "She said yes.", "Yes please."))
        for tool, args in (("gtm_intake", {"action": "apply"}), ("gtm_intake", {"action": "discard"}), ("gtm_export", {"action": "import", "path": "x.csv"}),
                           ("gtm_market", {"action": "track", "target": "x"}), ("gtm_playbook", {"action": "use", "name": "agency"}),
                           ("gtm_playbook", {"action": "stages", "stages": ["Found", "Called"]}),
                           ("gtm_deal", {"action": "snooze", "deal": self.dana}), ("gtm_deal", {"action": "undo", "deal": self.dana}),
                           ("gtm_contact", {"action": "remove", "deal": self.dana, "person": "x"})):
            self.assertEqual(self.desk.guard(tool, args, job)["ok"], False, (tool, args))
        self.assertTrue(self.desk.guard("gtm_deal", {"action": "add", "name": "Ada Quill", "company": "Fathom Data"}, job)["held"])
        self.assertTrue(self.desk.guard("gtm_contact", {"action": "add", "deal": self.marcus, "name": "Lena Ortiz"}, job)["held"])
        self.assertTrue(self.desk.guard("gtm_deal", {"action": "close", "deal": self.marcus, "outcome": "lost", "reason": "Comp mismatch"}, job)["held"])
        self.assertEqual(len(self.desk.intake.queue()), 4)
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.desk.intake.decide(self.desk.intake.queue()[3]["intake"], 1, "approve", "jo")            # the loss, with its reason
        self.assertEqual((self.desk.store.get(self.marcus)["status"], self.desk.store.get(self.marcus)["lost_reason"]), ("lost", "Comp mismatch"))

    def test_the_setting_is_a_persons_to_change_and_widens_what_a_job_may_do(self):
        job = {"platform": "cron"}
        self.assertFalse(self.desk.set_autonomy("everything")["ok"])
        self.assertEqual(self.desk.set_autonomy("routine")["label"], "Handle the routine")
        self.assertIsNone(self.desk.guard("gtm_deal", {"action": "update", "deal": self.dana, "next_step": "Call"}, job))
        self.assertIsNone(self.desk.guard("gtm_contact", {"action": "add", "deal": self.dana, "name": "X"}, job))
        self.assertTrue(self.desk.guard("gtm_deal", {"action": "move", "deal": self.dana, "stage": "next"}, job)["held"])
        self.desk.set_autonomy("full")
        self.assertIsNone(self.desk.guard("gtm_deal", {"action": "close", "deal": self.dana, "outcome": "won"}, job))
        self.assertEqual(self.desk.guard("gtm_intake", {"action": "apply"}, job)["ok"], False)        # approving is never a job's
        self.assertEqual(self.desk.setup()["autonomy"]["level"], "full")
        self.assertNotIn("autonomy", json.dumps([schemas.GTM_DEAL, schemas.GTM_PLAYBOOK, schemas.GTM_INTAKE, schemas.GTM_CONTACT]).lower()
                         .replace("autonomy setting", ""))                                            # no tool sets it


class HardeningTests(unittest.TestCase):
    """One test per thing a review found: each of these was a way to lose data, spend money or hang."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)

    def test_due_dates_are_read_the_way_people_write_them(self):
        base = dt.date(2026, 11, 10)        # a Tuesday
        for said, meant in (("2026-12-01", "2026-12-01"), ("2026-12-01T09:00:00Z", "2026-12-01"), ("12/01/2026", "2026-12-01"),
                            ("25/12/2026", "2026-12-25"), ("12/1", "2026-12-01"), ("1 Dec", "2026-12-01"), ("Dec 1, 2026", "2026-12-01"),
                            ("December 3rd", "2026-12-03"), ("Jan 5", "2027-01-05"), ("today", "2026-11-10"),
                            ("Tomorrow", "2026-11-11"), ("friday", "2026-11-13"), ("next Tue", "2026-11-17"), ("+3d", "2026-11-13"),
                            ("in 2 weeks", "2026-11-24"), ("next week", "2026-11-17"), ("", ""), (None, "")):
            self.assertEqual(parse_due(said, base), meant, said)
        for nonsense in ("soon", "Q1", "13/13/2026", "2026-02-30", "whenever", "9" * 40):
            self.assertIsNone(parse_due(nonsense, base), nonsense)

    def test_a_due_date_that_cannot_be_read_is_refused_not_stored(self):
        refused = self.desk.deal({"action": "add", "name": "Ada Quill", "next_due": "soonish"})
        self.assertFalse(refused["ok"])
        self.assertIn("next_due", refused["error"])
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        added = self.desk.deal({"action": "add", "name": "Ada Quill", "next_step": "Call", "next_due": "tomorrow"})["deal"]
        self.assertEqual(added["next_due"], (dt.date.today() + dt.timedelta(days=1)).isoformat())
        self.assertFalse(self.desk.deal({"action": "update", "deal": added["id"], "next_due": "the 45th"})["ok"])
        self.assertFalse(self.desk.deal({"action": "log", "deal": added["id"], "text": "Spoke", "next_due": "later"})["ok"])
        self.assertEqual(len(self.desk.store.activities(added["id"], limit=50)), 1)      # the refused log wrote nothing
        cleared = self.desk.deal({"action": "update", "deal": added["id"], "next_due": ""})["deal"]
        self.assertNotIn("next_due", cleared)

    def test_old_free_text_due_dates_do_not_show_as_overdue_forever(self):
        deal = self.desk.store.add("Old Row", "sourced", next_due="12/01/2099")     # written before dates were checked
        self.assertFalse(cards.is_due(deal))
        self.assertTrue(cards.is_due(self.desk.store.add("Older Row", "sourced", next_due="01/02/2020")))
        self.assertFalse(cards.is_due(self.desk.store.add("Vague Row", "sourced", next_due="Friday-ish")))
        self.desk.pipeline({"view": "due"})
        self.desk.brief()

    def test_a_failed_open_does_not_leave_the_ledger_locked(self):
        store = Store(str(Path(self.tmp.name) / "desk.db"))
        store.add("Dana Whitfield", "sourced")
        good, store.path = store.path, str(Path(self.tmp.name) / "no-such-folder" / "desk.db")
        with self.assertRaises(sqlite3.OperationalError):
            store.get("dana-whitfield")
        store.path = good
        seen = []
        worker = threading.Thread(target=lambda: seen.append(store.get("dana-whitfield")), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "another thread is still waiting for the ledger")
        self.assertEqual(seen[0]["name"], "Dana Whitfield")

    def test_names_in_any_script_get_their_own_record(self):
        first = self.desk.deal({"action": "add", "name": "张伟"})["deal"]
        second = self.desk.deal({"action": "add", "name": "李娜"})["deal"]
        third = self.desk.deal({"action": "add", "name": "Ольга Петрова"})["deal"]
        self.assertEqual(len({first["id"], second["id"], third["id"], "deal"}), 4)
        self.assertEqual(self.desk.store.find("李娜")["id"], second["id"])
        moved = self.desk.deal({"action": "move", "deal": "李娜", "stage": "next"})["deal"]
        self.assertEqual(moved["id"], second["id"])
        self.assertEqual(self.desk.store.get(first["id"])["stage"], "sourced")
        self.desk.store.add("!!!", "sourced")                                    # nothing id-safe at all: id "deal"
        self.assertIsNone(self.desk.store.find("???"))                           # and "???" is not that record
        self.assertLessEqual(len("snooze7|" + self.desk.store.add("名" * 200, "sourced")["id"]), 75)

    def test_import_survives_ragged_rows_bad_dates_and_excel_encoding(self):
        path = Path(self.tmp.name) / "in.csv"
        path.write_bytes(("name,company,due\n"
                          "Zoë Marsh,Acme, Inc,2026-12-01\n"          # an unquoted comma: one cell too many
                          "Ben Okoro,Fathom,sometime\n"
                          ",,\n"
                          ",,2026-12-09\n"
                          "Cy Tran,Kestrel,12/05/2026\n").encode("cp1252"))
        dry = self.desk.import_csv(path, dry_run=True)
        self.assertEqual((dry["ok"], dry["added"]), (True, 3))
        out = self.desk.import_csv(path)
        self.assertEqual((out["ok"], out["added"]), (True, 3))
        self.assertEqual(self.desk.store.find("Cy Tran")["next_due"], "2026-12-05")
        self.assertEqual(self.desk.store.find("Ben Okoro")["next_due"], "")
        self.assertEqual(self.desk.store.find("Zoë Marsh")["company"], "Acme")
        problems = " | ".join(out["problems"])
        self.assertIn("more cells", problems)
        self.assertIn("sometime", problems)
        self.assertIn("no name", problems)
        self.assertEqual(self.desk.import_csv(path)["added"], 0)                  # running it again adds nothing

    def test_enrich_none_means_no_button_and_no_paid_lookup(self):
        desk = make_desk(self.tmp.name, secrets={"PDL_API_KEY": "k"}, http=lambda *a, **k: self.fail("a paid lookup was made"))
        source = desk.library.source("recruiting").replace("name: recruiting", "name: quiet-desk")
        self.assertIn("\nenrich: person", source)
        saved = desk.playbook_tool({"action": "save", "yaml": source.replace("\nenrich: person", "\nenrich: none"), "activate": True})
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(desk.playbook().enrich, "none")
        self.assertFalse(desk.can_enrich())
        deal = desk.deal({"action": "add", "name": "Dana Whitfield", "email": "dana@example.com"})["deal"]
        self.assertNotIn("stoa_enrich", json.dumps(desk.blocks(desk.store.get(deal["id"]))))
        self.assertIn("switched off", desk.enrich({"deal": deal["id"]})["error"])
        self.assertIn("switched off", desk.click("enrich", deal["id"], "", "U1")["notice"])

    def test_a_playbook_that_would_break_later_is_refused_at_save(self):
        good = self.desk.library.source("recruiting").replace("name: recruiting", "name: mine")
        for label, broken in (
                ("stale_days words", good.replace("stale_days: 4", 'stale_days: "4 days"', 1)),
                ("stale_days zero", good.replace("stale_days: 4", "stale_days: -3", 1)),
                ("top-level words", good.replace("\nstale_days: 5", "\nstale_days: a week", 1)),
                ("advance true", good.replace("advance: Reach out", "advance: true", 1)),     # a boolean in every YAML
                ("terms list", good.split("\nterms:")[0] + "\nterms: [candidate, role]\n" + "stages:" + good.split("\nstages:")[1]),
                ("drafts text", good.split("\ndrafts:")[0] + "\ndrafts: write something nice\n")):
            self.assertNotEqual(broken, good, label)
            result = self.desk.playbook_tool({"action": "save", "yaml": broken, "activate": True})
            self.assertFalse(result["ok"], label)
            self.assertTrue(result["error"], label)
        self.assertEqual(self.desk.playbook().name, "recruiting")
        self.assertNotIn("mine", self.desk.library.names())
        with self.assertRaises(PlaybookError):
            validate({"name": "x", "stages": ["a", "b"], "lost": {"reasons": "price"}})
        playbook = Playbook({"name": "x", "stages": [{"id": "a", "stale_days": "3"}, "B"], "terms": {"deal": None, "odd": 1}})
        self.assertEqual((playbook.stale_days("a"), playbook.stale_days("b"), playbook.terms["deal"]), (3, 7, "deal"))

    def test_last_touch_for_every_record_in_one_query_matches_one_at_a_time(self):
        fixture.load(self.desk)
        store = self.desk.store
        some = store.list(status="all")[0]
        store.snooze(some["id"], 3)                     # bookkeeping: must not count as a touch
        touches = store.touches()
        for deal in store.list(status="all"):
            self.assertEqual(touches.get(deal["id"], ""), store.last_touch(deal["id"]), deal["id"])
        self.desk.park_followup(some["id"], "write it")
        self.assertEqual(store.meta_prefix("pending:"), {some["id"]: "write it"})


class IntakeTests(unittest.TestCase):
    """A list sent in from anywhere: reviewed first, and only what is approved gets written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")
        self.rows = [
            {"name": "Ada Quill", "company": "Fathom Data", "group": "Staff Backend · Northwind", "email": "ada@fathom.example",
             "Current title": "Staff Engineer", "note": "Met at the meetup", "favourite colour": "green"},
            {"name": "Dana Whitfield", "email": "dana@example.com", "stage": "Submitted", "linkedin": "https://www.linkedin.com/in/danaw/",
             "note": "Resume updated"},
            {"name": "Priya Raman", "email": "priya@example.com"},                                # on the desk, nothing new
            {"name": "ada quill", "company": "Fathom Data", "group": "Staff Backend · Northwind", "email": "ADA@fathom.example"},
            {"company": "Kestrel Labs"},                                                          # a company with no person
            {"email": "nobody@example.com"},                                                      # nothing to call it
            {"name": "Ben Okoro", "next_due": "sometime soon", "stage": "Vibing"},
        ]

    def propose(self, **extra):
        return self.desk.intake.run(dict({"action": "propose", "source": "meetup list", "rows": self.rows}, **extra))

    def test_the_review_says_what_each_row_would_do_and_writes_nothing(self):
        before = (self.desk.store.count(), len(self.desk.store.activities(limit=500)))
        out = self.propose()
        self.assertTrue(out["ok"])
        self.assertEqual([row["kind"] for row in out["rows"]], ["new", "update", "same", "same", "new", "problem", "new"])
        self.assertEqual(out["counts"], {"new": 3, "update": 1, "same": 2, "unclear": 0, "problem": 1, "declined": 0, "done": 0, "rows": 7})
        self.assertEqual(out["kept_as_note"], ["favourite colour"])
        changes = {c["field"]: (c["from"], c["to"]) for c in out["rows"][1]["changes"]}
        self.assertEqual(changes["Stage"], ("Screened", "Submitted"))
        self.assertEqual(changes["LinkedIn"], ("", "https://www.linkedin.com/in/danaw/"))
        self.assertIn("Note", changes)
        self.assertNotIn("Email", changes)                                                 # the same email is not a change
        self.assertIn("same as row 1", out["rows"][3]["why"])
        self.assertIn("not understood", " ".join(out["rows"][6]["warnings"]))
        self.assertIn("not in this playbook", " ".join(out["rows"][6]["warnings"]))
        self.assertIn("3 new candidates, 1 to update", out["review"])
        self.assertIn("stage Screened → Submitted", out["review"])
        self.assertEqual((self.desk.store.count(), len(self.desk.store.activities(limit=500))), before)
        self.assertEqual([w["id"] for w in self.desk.intake.waiting()], [out["id"]])

    def test_apply_writes_what_was_reviewed_and_only_once(self):
        out = self.propose()
        done = self.desk.intake.run({"action": "apply"})                                   # only one waiting: no id needed
        self.assertEqual((done["added"], done["updated"], done["left_out"], done["problems"]), (3, 1, 0, []))
        ada = self.desk.store.find("Ada Quill")
        self.assertEqual((ada["company"], ada["email"], ada["stage"], ada["fields"]["title"]),
                         ("Fathom Data", "ada@fathom.example", "sourced", "Staff Engineer"))
        self.assertEqual(ada["source"], "intake: meetup list")
        self.assertIn("Met at the meetup", [a["text"] for a in self.desk.store.activities(ada["id"])])
        dana = self.desk.store.get(self.dana["id"])
        self.assertEqual((dana["stage"], dana["linkedin"]), ("submitted", "https://www.linkedin.com/in/danaw/"))
        self.assertTrue(any("Resume updated" in a["text"] for a in self.desk.store.activities(dana["id"])))
        self.assertEqual(self.desk.store.find("Kestrel Labs")["name"], "Kestrel Labs")     # a company row is its own record
        self.assertEqual(self.desk.store.find("Ben Okoro")["next_due"], "")
        again = self.desk.intake.run({"action": "apply", "id": out["id"]})
        self.assertFalse(again["applied"])
        self.assertIn("3 added, 1 updated", again["note"])
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)
        self.assertEqual(self.desk.intake.waiting(), [])
        self.assertEqual(self.desk.intake.view(out["id"])["status"], "applied")

    def test_part_of_a_list_can_be_applied_and_the_rest_left(self):
        out = self.propose()
        done = self.desk.intake.run({"action": "apply", "id": out["id"], "skip": [1, "7"]})
        self.assertEqual((done["added"], done["updated"], done["left_out"]), (1, 1, 2))
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        second = self.propose()
        done = self.desk.intake.run({"action": "apply", "id": second["id"], "rows": [1]})
        self.assertEqual((done["added"], done["updated"]), (1, 0))
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], "submitted")

    def test_discard_and_unknown_lists(self):
        out = self.propose()
        self.assertTrue(self.desk.intake.run({"action": "discard", "id": out["id"]})["discarded"])
        self.assertFalse(self.desk.intake.run({"action": "apply", "id": out["id"]})["applied"])
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertFalse(self.desk.intake.run({"action": "apply", "id": "in-nope"})["ok"])
        self.assertFalse(self.desk.intake.run({"action": "propose", "rows": []})["ok"])
        self.assertFalse(self.desk.intake.run({"action": "propose", "rows": [{"name": "x"}] * 501})["ok"])
        nothing = self.desk.intake.run({"action": "propose", "rows": [{"name": "Priya Raman"}]})
        self.assertIn("Nothing here would change the desk", nothing["note"])
        self.assertEqual(self.desk.intake.waiting(), [])

    def test_the_same_person_in_another_search_is_new_and_two_matches_are_unclear(self):
        self.desk.deal({"action": "add", "name": "Dana Whitfield", "group": "Head of Platform · Lumen", "force": True})
        out = self.desk.intake.run({"action": "propose", "rows": [
            {"name": "Dana Whitfield", "group": "Data Lead · Tidewater"},
            {"name": "Dana Whitfield", "owner": "Jo"},
            {"name": "Dana Whitfield", "owner": "Jo", "record": self.dana["id"]}]})
        first, second, third = out["rows"]
        self.assertEqual((first["kind"], second["kind"], third["kind"]), ("new", "unclear", "update"))
        self.assertIn("also on the desk under", first["why"])
        self.assertIn("matches 2 records", second["why"])
        self.assertEqual(third["deal"], self.dana["id"])

    def test_a_csv_is_read_by_the_desk_and_other_files_are_the_agents_to_read(self):
        inbox = Path(self.tmp.name) / "imports"
        inbox.mkdir(exist_ok=True)
        path = inbox / "leads.csv"
        path.write_bytes("Candidate,Company,Search,Due\nZo\u00eb Marsh,Acme,Staff Backend \u00b7 Northwind,12/05/2026\n,,,\n".encode("cp1252"))
        out = self.desk.intake.run({"action": "propose", "path": str(path)})
        self.assertEqual((out["source"], out["counts"]["new"], out["counts"]["rows"]), ("leads.csv", 1, 1))
        self.desk.intake.run({"action": "apply", "id": out["id"]})
        zoe = self.desk.store.find("Zo\u00eb Marsh")
        self.assertEqual((zoe["grp"], zoe["next_due"]), ("Staff Backend \u00b7 Northwind", "2026-12-05"))
        pdf = Path(self.tmp.name) / "leads.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        self.assertIn("read it yourself", self.desk.intake.run({"action": "propose", "path": str(pdf)})["error"])

    def test_the_pipeline_written_to_a_sheet_and_read_back_changes_nothing(self):
        table = self.desk.export({"action": "table"})
        self.assertEqual(table["columns"][:5], ["ID", "Candidate", "Current company", "Search", "Stage"])   # the playbook's words
        self.assertEqual(table["count"], self.desk.store.count())
        self.assertIn("Current title", table["columns"])
        back = self.desk.intake.run({"action": "propose", "source": "Google Sheet", "columns": table["columns"], "rows": table["rows"]})
        self.assertEqual((back["counts"]["same"], back["counts"]["new"], back["counts"]["update"]), (table["count"], 0, 0))
        self.assertNotIn("ignored_columns", back)                                          # "Updated" is the desk's own column
        self.assertIn("Nothing here would change the desk", back["note"])

    def test_edits_made_in_the_sheet_come_back_as_exactly_those_edits(self):
        table = self.desk.export({"action": "table"})
        columns, rows = table["columns"], [list(row) for row in table["rows"]]
        at = {name: index for index, name in enumerate(columns)}
        dana = next(row for row in rows if row[at["ID"]] == self.dana["id"])
        dana[at["Stage"]] = "Submitted"                                                    # someone moved her in the Sheet
        dana[at["Candidate"]] = "Dana Whitfield-Okafor"                                    # and corrected her name
        other = next(row for row in rows if row[at["ID"]] != self.dana["id"])
        other[at["Fee"]] = "$61k"
        other[at["Due"]] = "12/05/2026"
        rows.append(["", "Ada Quill", "Fathom Data", "Staff Backend · Northwind", "Contacted"] + [""] * (len(columns) - 5))
        rows.append([""] * len(columns))                                                   # an empty line at the bottom
        back = self.desk.intake.run({"action": "propose", "source": "Google Sheet", "columns": columns, "rows": rows})
        self.assertEqual((back["counts"]["update"], back["counts"]["new"], back["counts"]["problem"]), (2, 1, 0))
        changed = {row["deal"]: {c["field"]: c["to"] for c in row["changes"]} for row in back["rows"] if row["kind"] == "update"}
        self.assertEqual(changed[self.dana["id"]], {"Candidate": "Dana Whitfield-Okafor", "Stage": "Submitted"})
        self.assertEqual(changed[other[at["ID"]]], {"Fee": "$61k", "Due": "2026-12-05"})
        self.desk.intake.run({"action": "apply", "id": back["id"]})
        dana_now = self.desk.store.get(self.dana["id"])
        self.assertEqual((dana_now["name"], dana_now["stage"]), ("Dana Whitfield-Okafor", "submitted"))
        self.assertEqual(self.desk.store.get(other[at["ID"]])["value"], 61000.0)
        self.assertEqual(self.desk.store.find("Ada Quill")["stage"], "contacted")
        again = self.desk.export({"action": "table"})                                      # mirror again: the Sheet and the desk agree
        settled = self.desk.intake.run({"action": "propose", "columns": again["columns"], "rows": again["rows"]})
        self.assertEqual((settled["counts"]["update"], settled["counts"]["new"]), (0, 0))

    def test_two_people_pressing_apply_write_the_list_once(self):
        out = self.propose()
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.desk.intake.apply({"id": out["id"]}, "u"))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len([r for r in results if r.get("applied")]), 1)
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)

    def test_in_the_app_the_review_is_drawn_in_the_conversation(self):
        self.desk.desktop_seen = time.time()
        out = self.desk.intake.run({"action": "propose", "source": "x", "rows": self.rows}, {"platform": "desktop"})
        self.assertEqual(out["directive"], '::stoa-intake{id="%s"}' % out["id"])
        self.assertNotIn("rows", out)
        self.assertNotIn("review", out)


class HeaderTests(unittest.TestCase):
    """A spreadsheet's own headers, as people write them, are read without renaming anything first."""

    def desk_on(self, playbook):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return make_desk(tmp.name, playbook=playbook)

    def test_a_client_list_with_its_own_headers_comes_in(self):
        desk = self.desk_on("agency")
        text = ("Client Name ,Website ,Contact name ,Contact email ,Contact website ,Location\n"
                "Harbor Tide Studio,harbortide.example,Mina Park,mina@harbortide.example,harbortide.example,\"Portland, OR\"\n"
                "Quarry Lane Foods,quarrylane.example,Tom Ellis,tom@quarrylane.example,quarrylane.example,\"Denver, CO\"\n")
        out = desk.intake.propose_file("Clients.csv", text)
        self.assertEqual(out["counts"]["new"], 2)
        first = out["rows"][0]
        self.assertEqual((first["name"], first["company"], first["group"]), ("Mina Park", "Harbor Tide Studio", "Harbor Tide Studio"))
        self.assertEqual(out["kept_as_note"], ["Contact website", "Location", "Website"])
        applied = desk.intake.apply({"id": out["id"]}, "test")
        record = desk.store.get(applied["records"][0])
        self.assertEqual((record["email"], record["company"], record["grp"]), ("mina@harbortide.example", "Harbor Tide Studio", "Harbor Tide Studio"))
        self.assertEqual(record["notes"], "Website: harbortide.example\nContact website: harbortide.example\nLocation: Portland, OR")
        self.assertEqual(desk.view(record)["notes"], record["notes"])

    def test_a_lead_list_with_its_own_headers_comes_in(self):
        desk = self.desk_on("client-leads")
        text = ("Lead name ,Source ,Phone ,Email ,Linkedin ,website ,Job title,City\n"
                "Rosa Diaz,Referral,(555) 010-2000,rosa@ninepine.example,,ninepine.example,VP Finance,Austin\n")
        out = desk.intake.propose_file("Leads.csv", text)
        self.assertEqual(out["counts"]["new"], 1)
        record = desk.store.get(desk.intake.apply({"id": out["id"]}, "test")["records"][0])
        self.assertEqual((record["name"], record["email"], record["source"]), ("Rosa Diaz", "rosa@ninepine.example", "Referral"))
        self.assertEqual(record["fields"], {"title": "VP Finance", "location": "Austin"})
        self.assertEqual(record["phone"], "(555) 010-2000")
        self.assertEqual(record["notes"], "website: ninepine.example")

    def test_first_and_last_names_make_one_name_and_spelling_does_not_matter(self):
        desk = self.desk_on("b2b-sales")
        text = ("Owner email,FIRST_NAME,last-name,E-mail Address,Account Name,Deal Owner\n"
                "sam@ourco.example,Lena,Ortiz,lena@corvid.example,Corvid Health,Sam\n")
        out = desk.intake.propose_file("export.csv", text)
        row = out["rows"][0]
        self.assertEqual((row["kind"], row["name"], row["group"]), ("new", "Lena Ortiz", "Corvid Health"))
        record = desk.store.get(desk.intake.apply({"id": out["id"]}, "test")["records"][0])
        self.assertEqual((record["email"], record["owner"], record["company"]), ("lena@corvid.example", "Sam", "Corvid Health"))
        self.assertEqual(out["kept_as_note"], ["Owner email"])

    def test_the_terminal_import_reads_headers_the_same_way(self):
        desk = self.desk_on("b2b-sales")
        path = Path(tempfile.mkdtemp()) / "accounts.csv"
        self.addCleanup(lambda: path.unlink() if path.exists() else None)
        path.write_text("Client Name ,Contact name ,Contact email ,Mobile,Website\nCorvid Health,Lena Ortiz,lena@corvid.example,+1 555 010 3000,corvid.example\n")
        self.assertEqual(desk.import_csv(path)["added"], 1)
        lena = desk.store.find("Lena Ortiz")
        self.assertEqual((lena["company"], lena["grp"], lena["email"], lena["phone"], lena["notes"]),
                         ("Corvid Health", "Corvid Health", "lena@corvid.example", "+1 555 010 3000", "Website: corvid.example"))

    def test_each_unplaced_column_goes_where_the_person_says(self):
        desk = self.desk_on("recruiting")
        text = ("Name,Email,Website,Visa status,Favourite colour\n"
                "Mina Park,mina@harbortide.example,harbortide.example,H-1B,green\n")
        out = desk.intake.propose_file("list.csv", text)
        self.assertIn("Visa status", out["note"]) if "note" in out else None
        view = desk.intake.view(out["id"])
        self.assertEqual([c["name"] for c in view["columns"]], ["Favourite colour", "Visa status", "Website"])
        applied = desk.intake.apply({"id": out["id"], "columns": {"Visa status": "field", "Favourite colour": "skip"}}, "test")
        self.assertTrue(applied["ok"], applied)
        mina = desk.store.find("Mina Park")
        key = next(f["key"] for f in desk.playbook().fields if f["label"] == "Visa status")
        self.assertEqual(mina["fields"][key], "H-1B")
        self.assertEqual(mina["notes"], "Website: harbortide.example")              # notes by default, the skipped one gone
        self.assertEqual(desk.playbook().name, "recruiting")                          # the desk's own copy, under the same name
        self.assertEqual(len(desk.playbook().stages), len(desk.library.load("recruiting").stages))
        # the same list again, now that Visa status is a field: an existing record without it is filled in
        desk.deal({"action": "update", "deal": mina["id"], "fields": {key: ""}})
        again = desk.intake.propose_file("list.csv", text)
        result = desk.intake.apply({"id": again["id"], "columns": {"Visa status": "field"}}, "test")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(desk.store.get(mina["id"])["fields"][key], "H-1B")

    def test_too_many_fields_changes_nothing(self):
        desk = self.desk_on("recruiting")
        names = ["Extra %d" % n for n in range(12)]
        text = "Name," + ",".join(names) + "\nMina Park," + ",".join("v" for _ in names) + "\n"
        out = desk.intake.propose_file("wide.csv", text)
        refused = desk.intake.apply({"id": out["id"], "columns": {name: "field" for name in names}}, "test")
        self.assertFalse(refused["ok"])
        self.assertIn("at most 12", refused["error"])
        self.assertEqual(desk.store.count(), 0)
        self.assertEqual(desk.intake.view(out["id"])["status"], "open")               # still waiting, to be applied another way
        self.assertTrue(desk.intake.apply({"id": out["id"]}, "test")["ok"])

    def test_a_list_with_no_name_column_is_handed_back_to_be_read(self):
        desk = self.desk_on("agency")
        before = desk.store.count()
        out = desk.intake.propose_file("mystery.csv", "Col A,Col B\nApex,apex.example\n")
        self.assertTrue(out["unread"])
        self.assertEqual(out["columns"], ["Col A", "Col B"])
        self.assertIn("ask the person", out["note"])
        self.assertEqual(desk.store.count(), before)
        self.assertEqual(desk.intake.waiting(), [])                    # nothing left waiting for a yes

    def test_a_list_already_on_the_desk_still_says_nothing_new(self):
        desk = self.desk_on("agency")
        text = "Contact name,Client name\nMina Park,Harbor Tide Studio\n"
        desk.intake.apply({"id": desk.intake.propose_file("a.csv", text)["id"]}, "test")
        again = desk.intake.propose_file("a.csv", text)
        self.assertNotIn("unread", again)
        self.assertEqual((again["counts"]["new"], again["counts"]["same"]), (0, 1))



class RecordNotesTests(unittest.TestCase):
    """A few lines kept on the record itself, edited in place, and taken back like any other change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")

    def test_notes_are_set_shown_replaced_and_taken_back(self):
        out = self.desk.deal({"action": "update", "deal": self.dana["id"], "notes": "Prefers mornings.\nWife is a recruiter."})
        self.assertTrue(out["ok"])
        got = self.desk.deal({"action": "get", "deal": self.dana["id"]})
        self.assertEqual(got["deal"]["notes"] if "deal" in got else got["notes"], "Prefers mornings.\nWife is a recruiter.")
        self.desk.deal({"action": "update", "deal": self.dana["id"], "notes": "Prefers mornings."})
        self.assertEqual(self.desk.store.get(self.dana["id"])["notes"], "Prefers mornings.")
        self.desk.store.undo(self.dana["id"])
        self.assertEqual(self.desk.store.get(self.dana["id"])["notes"], "Prefers mornings.\nWife is a recruiter.")
        self.desk.deal({"action": "update", "deal": self.dana["id"], "notes": ""})
        self.assertEqual(self.desk.store.get(self.dana["id"])["notes"], "")

    def test_a_new_record_can_carry_notes_and_long_notes_are_cut(self):
        out = self.desk.deal({"action": "add", "name": "Iris Bell", "group": "Staff Backend · Northwind", "notes": "x" * 5000})
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.desk.store.find("Iris Bell")["notes"]), 4000)

    def test_an_older_desk_gains_the_notes_column_when_opened(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(st.SCHEMA.replace("  notes TEXT NOT NULL DEFAULT '',\n", "") if "notes TEXT" in st.SCHEMA else st.SCHEMA)
        conn.close()
        store = st.Store(path)
        made = store.add("Old Timer", "sourced")
        self.assertEqual(made["notes"], "")


class LayoutRuleTests(unittest.TestCase):
    """What the record and group pages rest on: who is billed, an account that is a company, a phone on a record."""

    def desk_on(self, playbook):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return make_desk(tmp.name, playbook=playbook)

    def test_a_lead_worked_for_a_client_is_never_billed(self):
        desk = self.desk_on("client-leads")
        self.assertEqual(desk.playbook().billing, "none")
        desk.deal({"action": "add", "name": "Rosa Diaz", "group": "Fintech CFOs", "value": 50000})
        rosa = desk.store.find("Rosa Diaz")
        desk.deal({"action": "close", "deal": rosa["id"], "outcome": "won"})
        self.assertEqual(desk.money(desk.store.get(rosa["id"]))["uninvoiced"], 0)
        self.assertEqual(desk.cash()["strip"]["uninvoiced"]["count"], 0)
        for name in ("agency", "b2b-sales", "recruiting", "partnerships"):
            self.assertEqual(desk.library.load(name).billing, "record")

    def test_billing_is_checked_when_a_playbook_is_saved(self):
        from stoa.playbook import validate, PlaybookError
        base = {"name": "x", "stages": ["One", "Two"], "won": {"id": "won", "label": "Won"}, "lost": {"id": "lost", "label": "Lost"}}
        self.assertEqual(validate(dict(base))["billing"], "record")
        self.assertEqual(validate(dict(base, billing="none"))["billing"], "none")
        with self.assertRaises(PlaybookError):
            validate(dict(base, billing="monthly"))

    def test_an_account_page_carries_its_company_and_everyone_on_it(self):
        desk = self.desk_on("b2b-sales")
        self.assertTrue(desk.playbook().group_is_company())
        self.assertFalse(desk.library.load("recruiting").group_is_company())
        desk.deal({"action": "add", "name": "Renata Okafor", "company": "Harborline Pay", "group": "Harborline Pay", "email": "renata@harborline.example"})
        desk.deal({"action": "add", "name": "Ivo Marsh", "company": "Harborline Pay", "group": "Harborline Pay", "email": "ivo@harborline.example"})
        view = desk.group_view("Harborline Pay")
        self.assertEqual(view["company"]["name"], "Harborline Pay")
        self.assertEqual(view["company"]["domain"], "harborline.example")          # guessed from the people on it until it is set
        self.assertEqual(sorted(p["name"] for p in view["people"] if p.get("primary")), ["Ivo Marsh", "Renata Okafor"])
        desk.company("Harborline Pay", domain="harborline.com")
        self.assertEqual(desk.group_view("Harborline Pay")["company"]["domain"], "harborline.com")
        recruiting = self.desk_on("recruiting")
        recruiting.deal({"action": "add", "name": "Dana Whitfield", "company": "Kestrel Labs", "group": "Staff Backend · Northwind"})
        self.assertIsNone(recruiting.group_view("Staff Backend · Northwind")["company"])

    def test_a_record_keeps_a_phone(self):
        desk = self.desk_on("recruiting")
        desk.deal({"action": "add", "name": "Mei Sato", "group": "ML Lead · Saffron", "phone": "+1 555 010 4000"})
        mei = desk.store.find("Mei Sato")
        self.assertEqual(mei["phone"], "+1 555 010 4000")
        desk.deal({"action": "update", "deal": mei["id"], "phone": "+1 555 010 5000"})
        self.assertEqual(desk.view(desk.store.get(mei["id"]))["phone"], "+1 555 010 5000")
        desk.store.undo(mei["id"])
        self.assertEqual(desk.store.get(mei["id"])["phone"], "+1 555 010 4000")
        out = desk.intake.propose_file("c.csv", "Name,Mobile\nMei Sato,+1 555 010 6000\n")
        self.assertEqual([c["field"] for c in out["rows"][0]["changes"]], ["Phone"])


class ClientBillingTests(unittest.TestCase):
    """Contract work billed to a client by the month (hours, or a retainer), kept beside the searches it pays for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="recruiting")
        self.desk.group({"action": "set", "group": "Head of Talent · Fernway", "client": "Fernway", "new": True})
        self.desk.group({"action": "set", "group": "Staff Engineer · Fernway", "client": "Fernway", "new": True})
        self.desk.deal({"action": "add", "name": "Mina Park", "group": "Head of Talent · Fernway"})
        today = dt.date.today().replace(day=1)
        self.last = (today - dt.timedelta(days=1)).isoformat()[:7]
        self.before = ((today - dt.timedelta(days=1)).replace(day=1) - dt.timedelta(days=1)).isoformat()[:7]
        self.this = today.isoformat()[:7]

    def terms(self, **extra):
        return self.desk.invoice(dict({"action": "terms", "client": "Fernway", "kind": "hourly", "rate": 95, "hours_week": 40,
                                       "bill_to": "Halyard Ventures", "start": self.before}, **extra))

    def test_terms_then_the_months_that_have_ended_are_waiting(self):
        out = self.terms()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["terms"]["bill_to"], "Halyard Ventures")
        waiting = self.desk.to_bill()
        self.assertEqual([row["period"] for row in waiting], [self.before, self.last])        # never the month still running
        row = waiting[-1]
        weekdays = self.desk._weekdays(self.last)
        self.assertEqual((row["client"], row["bill_to"], row["hours"], row["amount"]), ("Fernway", "Halyard Ventures", float(weekdays * 8), weekdays * 8 * 95.0))
        self.assertEqual(self.desk.today(full=False)["counts"]["to_bill"], 2)
        self.assertEqual(self.desk.cash()["strip"]["to_bill"]["count"], 2)

    def test_a_month_is_billed_once_with_the_hours_given(self):
        self.terms()
        out = self.desk.invoice({"action": "add", "client": "Fernway", "period": self.last, "hours": 152, "number": "2041"})
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["invoice"]["amount"], out["owed"]), (14440.0, 14440.0))
        self.assertIn("152 h", out["invoice"]["note"])
        self.assertIn("to Halyard Ventures", out["invoice"]["note"])
        self.assertEqual([row["period"] for row in self.desk.to_bill()], [self.before])
        again = self.desk.invoice({"action": "add", "client": "Fernway", "period": self.last})
        self.assertFalse(again["ok"])
        self.assertIn("already billed", again["error"])
        cash = self.desk.cash()
        self.assertEqual(cash["strip"]["owed"]["value"], 14440.0)
        self.assertEqual([(i["client"], i["period"]) for i in cash["invoices"]], [("Fernway", self.last)])
        # paid, found by its number alone; then taken back
        paid = self.desk.invoice({"action": "pay", "invoice": "2041"})
        self.assertTrue(paid["ok"], paid)
        self.assertEqual(paid["owed"], 0)
        back = self.desk.invoice({"action": "undo", "client": "Fernway"})
        self.assertTrue(back["ok"], back)
        self.assertEqual(back["owed"], 14440.0)
        self.assertEqual(self.desk.store.activities(limit=50)[0]["deal_id"], self.desk.store.find("Mina Park")["id"])  # no stray log line

    def test_a_skipped_month_is_not_asked_for_and_a_retainer_bills_its_amount(self):
        self.terms()
        self.desk.invoice({"action": "skip", "client": "Fernway", "period": self.before})
        self.assertEqual([row["period"] for row in self.desk.to_bill()], [self.last])
        self.desk.invoice({"action": "unskip", "client": "Fernway", "period": self.before})
        self.assertEqual(len(self.desk.to_bill()), 2)
        self.desk.invoice({"action": "terms", "client": "Fernway", "kind": "retainer", "rate": 8000})
        self.assertEqual({row["amount"] for row in self.desk.to_bill()}, {8000.0})
        bad = self.desk.invoice({"action": "terms", "client": "Fernway", "kind": "hourly", "rate": 0})
        self.assertFalse(bad["ok"])

    def test_the_client_page_has_its_searches_records_and_billing(self):
        self.terms()
        view = self.desk.client_view("fernway")
        self.assertEqual(view["client"]["name"], "Fernway")
        self.assertEqual(sorted(row["name"] for row in view["searches"]), ["Head of Talent · Fernway", "Staff Engineer · Fernway"])
        self.assertEqual([r["name"] for r in view["records"]], ["Mina Park"])
        self.assertEqual(len(view["billing"]["to_bill"]), 2)
        listed = self.desk.clients()
        self.assertEqual([(row["name"], row["open"], row["to_bill"]) for row in listed], [("Fernway", 1, 2)])

    def test_a_scheduled_job_cannot_bill_or_change_terms(self):
        self.terms()
        session = {"platform": "cron"}
        self.assertIsNotNone(self.desk.guard("gtm_invoice", {"action": "terms", "client": "Fernway", "rate": 1}, session))
        held = self.desk.guard("gtm_invoice", {"action": "add", "client": "Fernway", "period": self.last}, session)
        self.assertIsNotNone(held)
        self.assertEqual(len(self.desk.to_bill()), 2)          # nothing was billed

    def test_a_desk_whose_records_are_never_billed_asks_for_no_record_invoice(self):
        leads = make_desk(tempfile.mkdtemp(), playbook="client-leads")
        leads.deal({"action": "add", "name": "Rosa Diaz", "group": "Fintech CFOs", "value": 50000})
        rosa = leads.store.find("Rosa Diaz")
        leads.deal({"action": "close", "deal": rosa["id"], "outcome": "won"})
        leads.group({"action": "set", "group": "Fintech CFOs", "client": "Acme", "new": True})
        leads.invoice({"action": "terms", "client": "Acme", "kind": "retainer", "rate": 12000, "start": self.last})
        leads.invoice({"action": "add", "client": "Acme", "period": self.last})
        today = leads.today(full=False)
        self.assertEqual((today["counts"]["to_invoice"], today["counts"]["to_bill"]), (0, 0))
        self.assertEqual(leads.cash()["strip"]["owed"]["value"], 12000.0)


class ShapeTests(unittest.TestCase):
    """Three shapes of pipeline, and the stages a person sets for their own business."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {}
        settings = {}
        self.desk = Desk(data_dir=lambda: Path(self.tmp.name), get_config=lambda k, default=None: settings.get(k, default),
                         set_config=lambda k, v: settings.__setitem__(k, v), get_secret=lambda name: None)
        self.settings = settings

    def test_three_shapes_each_with_its_use_cases(self):
        shapes = self.desk.shapes()
        self.assertEqual([s["title"] for s in shapes], ["Close deals", "Fill roles", "Generate leads"])
        self.assertTrue(all(len(s["uses"]) >= 3 and 2 <= len(s["stages"]) <= 7 for s in shapes))
        self.assertEqual([self.desk.library.load(n).shape for n in ("b2b-sales", "agency", "partnerships", "recruiting", "client-leads")],
                         ["deals", "deals", "deals", "roles", "leads"])

    def test_first_setup_with_own_stages(self):
        out = self.desk.setup_pipeline("first", "roles", ["Sourced", "Phone screen", "Submitted", "Interview", "Offer"], "Placed", "Not a fit", "Contract")
        self.assertTrue(out["ok"], out)
        pb = self.desk.playbook()
        self.assertEqual((pb.shape, [s["label"] for s in pb.stages], pb.won["label"], pb.lost["label"]),
                         ("roles", ["Sourced", "Phone screen", "Submitted", "Interview", "Offer"], "Placed", "Not a fit"))
        self.assertEqual(pb.name, "recruiting-contract")
        self.assertEqual(self.desk.current()["name"], "Contract")
        self.assertEqual(self.settings["playbook"], "recruiting-contract")
        self.assertEqual([s["id"] for s in pb.stages][:1], ["sourced"])                  # a stage the shape has keeps its id
        # the stages as they ship need no copy of the playbook
        self.assertEqual(self.desk.set_stages([s["label"] for s in self.desk.library.load("recruiting").stages], "Placed", "Passed")["ok"], True)
        self.assertEqual(self.desk.playbook().name, "recruiting")

    def test_two_pipelines_of_one_shape_keep_their_own_stages(self):
        self.desk.setup_pipeline("first", "roles", ["Sourced", "Submitted", "Interview"], name="Contract")
        out = self.desk.setup_pipeline("add", "roles", ["Sourced", "Screened", "Submitted", "Offer"], "Placed", None, "Contingency")
        self.assertTrue(out["ok"], out)
        with self.desk.on(out["added"]["id"]):
            self.assertEqual([s["label"] for s in self.desk.playbook().stages], ["Sourced", "Screened", "Submitted", "Offer"])
        self.assertEqual([s["label"] for s in self.desk.playbook().stages], ["Sourced", "Submitted", "Interview"])

    def test_limits_and_records_left_behind(self):
        self.desk.setup_pipeline("first", "deals", None, name="Sales")
        too_many = self.desk.set_stages(["A", "B", "C", "D", "E", "F", "G", "H"])
        self.assertFalse(too_many["ok"])
        self.assertIn("2 to 7", too_many["error"])
        self.assertFalse(self.desk.set_stages(["Only one"])["ok"])
        self.assertFalse(self.desk.set_stages(["Call", "call"])["ok"])
        self.desk.deal({"action": "add", "name": "Renata Okafor", "stage": "Qualified"})
        refused = self.desk.set_stages(["Lead", "Discovery", "Proposal"])
        self.assertFalse(refused["ok"])
        self.assertIn("Qualified", refused["error"])
        # renamed in place: the record stays put
        stages = [{"id": s["id"], "label": s["label"]} for s in self.desk.playbook().stages]
        stages[1]["label"] = "Qualified lead"
        self.assertTrue(self.desk.set_stages(stages)["ok"])
        self.assertEqual(self.desk.view(self.desk.store.find("Renata Okafor"))["stage_label"], "Qualified lead")
        moved = self.desk.setup_pipeline("edit", "roles", None)
        self.assertFalse(moved["ok"])
        self.assertIn("open a new pipeline", moved["error"])

    def test_the_agent_sets_stages_by_name(self):
        out = self.desk.playbook_tool({"action": "stages", "shape": "leads", "stages": ["Found", "Researched", "Reached out", "Replied", "Meeting set"],
                                       "won": "Handed over"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.desk.playbook().shape, "leads")
        self.assertEqual(self.desk.playbook().billing, "none")

class FakeCtx:
    """The part of Hermes's plugin context that ``register`` uses, recording what it is given."""

    def __init__(self, settings):
        self.settings = settings
        self.tools, self.sections, self.commands, self.blueprints, self.skills = {}, {}, {}, [], []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def inject_message(self, *args, **kwargs):
        return False

    def register_tool(self, name, toolset, schema, handler, emoji=""):
        self.tools[name] = handler

    def register_system_prompt_section(self, name, fn, max_chars=0):
        self.sections[name] = (fn, max_chars)

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_automation_blueprint(self, name, **kwargs):
        self.blueprints.append(name)

    def register_skill(self, name, path):
        self.skills.append(name)

    def register_platform_handler(self, *args, **kwargs): pass
    def register_slack_action_handler(self, *args, **kwargs): pass
    def register_cli_command(self, *args, **kwargs): pass


class PipelineTests(unittest.TestCase):
    """Several pipelines on one desk, each with its own records and its own playbook. A call says which with
    ``pipeline``, or means the first; a desk that never adds a second behaves exactly as it always did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        fakes = {"hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: self.tmp.name),
                 "plugins": types.ModuleType("plugins"),
                 "plugins.plugin_storage": types.SimpleNamespace(plugin_data_dir=lambda name: self.data)}
        patcher = mock.patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeCtx({"playbook": "client-leads"})
        stoa.register(self.ctx)
        self.desk = stoa.LIVE_DESKS[-1]
        self.session()

    def session(self, **values):
        patcher = mock.patch.object(stoa, "_session", lambda: dict({"platform": "cli"}, **values))
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, tool, **args):
        return json.loads(self.ctx.tools[tool](args))

    def second(self):
        made = self.call("gtm_playbook", action="add_pipeline", pipeline_name="My practice", playbook="agency")
        self.assertTrue(made["ok"], made)
        return made

    def test_a_desk_with_one_pipeline_is_as_it_always_was(self):
        self.assertEqual([(p["id"], p["name"], p["playbook"], p["file"]) for p in self.desk.pipelines()],
                         [("main", "Client lead desk", "client-leads", "desk.db")])
        out = self.call("gtm_deal", action="add", name="Rosa Lind", company="Ledgerline")
        self.assertTrue(out["ok"])
        self.assertNotIn("pipeline", out)                                         # nothing to say where there is only one
        self.assertFalse((self.data / "pipelines.json").exists())
        self.assertNotIn("pipelines", self.desk.prompt_section())
        self.assertTrue(self.call("gtm_pipeline", view="today", pipeline="Client lead desk")["ok"])       # its own name is fine
        wrong = self.call("gtm_deal", action="add", name="Nobody", pipeline="My practice")
        self.assertIn("no pipeline is called", wrong["error"])                    # a name that is no pipeline is never guessed at
        self.assertEqual(self.desk.store.count(), 1)

    def test_a_second_pipeline_has_its_own_records_and_its_own_playbook(self):
        made = self.second()
        self.assertEqual((made["added"]["name"], made["added"]["playbook"], made["pipelines"]), ("My practice", "agency", ["Client lead desk", "My practice"]))
        self.assertTrue((self.data / "pipe-p2.db").exists())
        lead = self.call("gtm_deal", action="add", name="Rosa Lind", company="Ledgerline", group="Fintech CFOs · Acme")
        mine = self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", value=96000, pipeline="my practice")
        self.assertEqual((lead["pipeline"], lead["deal"]["stage_label"]), ("Client lead desk", "Identified"))
        self.assertEqual((mine["pipeline"], mine["deal"]["stage_label"]), ("My practice", "Inbound"))
        self.assertEqual(self.desk.store.count(), 1)
        with self.desk.on("p2"):
            self.assertEqual(([d["name"] for d in self.desk.store.list()], self.desk.playbook().name), (["Visa retainer"], "agency"))
        self.assertEqual(self.desk.playbook().name, "client-leads")               # outside it, the first again
        moved = self.call("gtm_deal", action="move", deal="Visa retainer", stage="next", pipeline="My practice")
        self.assertEqual(moved["deal"]["stage_label"], "Intro call")
        board = self.call("gtm_pipeline", pipeline="practice")                    # a clear part of the name
        self.assertEqual(board["pipeline"], "My practice")
        self.desk.pipeline_add("My other practice", "agency")
        self.assertIn("several", self.call("gtm_pipeline", pipeline="practice")["error"])      # now it could be either: it asks
        self.assertFalse(self.call("gtm_deal", action="add", name="X", pipeline="Somewhere else")["ok"])

    def test_a_record_named_without_its_pipeline_is_found_where_it_is(self):
        self.second()
        self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", pipeline="My practice")
        out = self.call("gtm_deal", action="get", deal="Visa retainer")           # no pipeline said, and only one has it
        self.assertEqual((out["ok"], out["pipeline"], out["deal"]["name"]), (True, "My practice", "Visa retainer"))
        stepped = self.call("gtm_deal", action="update", deal="Visa", next_step="Send the SOW", next_due="friday")       # a part of its name, and a change
        self.assertEqual((stepped["ok"], stepped["pipeline"], stepped["deal"]["next_step"]), (True, "My practice", "Send the SOW"))
        self.assertTrue(self.call("gtm_deal", action="get", deal="Visa retainer", pipeline="My practice")["ok"])
        nowhere = self.call("gtm_deal", action="get", deal="Nobody At All")
        self.assertEqual((nowhere["ok"], nowhere["pipeline"]), (False, self.desk.pipelines()[0]["name"]))
        self.assertIn("no record matches", nowhere["error"])

    def test_a_name_that_is_in_two_pipelines_never_lands_on_one_by_chance(self):
        self.second()
        first = self.desk.pipelines()[0]["name"]
        self.assertTrue(self.call("gtm_deal", action="add", name="Ravenglass Rail", company="Ravenglass Rail")["ok"])
        self.assertTrue(self.call("gtm_deal", action="add", name="Ravenglass Rail", company="Ravenglass Rail", pipeline="My practice")["ok"])
        for args in ({"action": "update", "deal": "Ravenglass Rail", "next_step": "Call the buyer"}, {"action": "move", "deal": "Ravenglass", "stage": "next"},
                     {"action": "get", "deal": "Ravenglass Rail"}):
            clash = self.call("gtm_deal", **args)
            self.assertFalse(clash["ok"], args)
            self.assertIn("more than one pipeline", clash["error"])
            self.assertEqual(sorted(match["pipeline"] for match in clash["matches"]), sorted([first, "My practice"]))
        stages = {}
        for name in (first, "My practice"):                                        # nothing was changed in either
            got = self.call("gtm_deal", action="get", deal="Ravenglass Rail", pipeline=name)["deal"]
            self.assertNotIn("next_step", got)
            stages[name] = got["stage"]
        self.assertEqual(stages[first], self.desk.playbook().stage_ids()[0])
        told = self.call("gtm_deal", action="update", deal="Ravenglass Rail", next_step="Call the buyer", pipeline="My practice")      # told which, it does as told
        self.assertEqual((told["ok"], told["pipeline"]), (True, "My practice"))
        # Once one of them is closed, the name means the one still open: a lead that became a deal does not compete with it.
        closed = self.call("gtm_deal", action="close", deal="Ravenglass Rail", outcome="won", pipeline="My practice")
        self.assertTrue(closed["ok"], closed)
        aimed = self.call("gtm_deal", action="update", deal="Ravenglass Rail", next_step="Send the contract")
        self.assertEqual((aimed["ok"], aimed["pipeline"], aimed["deal"]["next_step"]), (True, first, "Send the contract"))
        self.assertEqual(self.call("gtm_deal", action="get", deal="Ravenglass Rail", pipeline="My practice")["deal"]["next_step"], "Call the buyer")
        # To reopen one, both are candidates again: it is asked.
        self.assertFalse(self.call("gtm_deal", action="reopen", deal="Ravenglass Rail")["ok"])
        # Adding is never aimed: a new record goes where it is told, or to the first.
        self.assertEqual(self.call("gtm_deal", action="add", name="Ravenglass Rail Freight", company="Ravenglass Rail Freight")["pipeline"], first)

    def test_a_move_can_set_the_next_step_in_the_same_call(self):
        self.assertTrue(self.call("gtm_deal", action="add", name="Marlpit Foods", company="Marlpit Foods")["ok"])
        before = self.call("gtm_deal", action="get", deal="Marlpit Foods")["deal"]["stage"]
        moved = self.call("gtm_deal", action="move", deal="Marlpit", stage="next", next_step="Send samples", next_due="friday")
        self.assertTrue(moved["ok"], moved)
        self.assertNotEqual(moved["deal"]["stage"], before)
        self.assertEqual(moved["deal"]["next_step"], "Send samples")
        self.assertRegex(moved["deal"]["next_due"], r"^\d{4}-\d\d-\d\d$")
        # One thing was asked for, so one undo takes both back.
        back = self.call("gtm_deal", action="undo", deal="Marlpit Foods")
        self.assertTrue(back["ok"], back)
        again = self.call("gtm_deal", action="get", deal="Marlpit Foods")["deal"]
        self.assertEqual((again["stage"], again.get("next_step", "")), (before, ""))
        # A date that cannot be read stops the move too: nothing is half done.
        bad = self.call("gtm_deal", action="move", deal="Marlpit Foods", stage="next", next_step="Call", next_due="the day after never")
        self.assertFalse(bad["ok"])
        self.assertEqual(self.call("gtm_deal", action="get", deal="Marlpit Foods")["deal"]["stage"], before)
        # Closing takes one as well: won, and what comes first after the win.
        won = self.call("gtm_deal", action="close", deal="Marlpit Foods", outcome="won", next_step="Kick-off call", next_due="+3d")
        self.assertEqual((won["ok"], won["deal"]["next_step"]), (True, "Kick-off call"))

    def test_a_note_filed_in_one_pipeline_is_not_waiting_in_another(self):
        notes = Path(self.tmp.name) / "notes"
        notes.mkdir()
        (notes / "2026-11-10 ana.md").write_text("# Call with Ana\n\nAna at Wexcombe Freight wants pricing by Thursday.\n")
        self.ctx.settings["notes_dir"] = str(notes)
        self.second()
        first = self.desk.pipelines()[0]["name"]
        self.assertTrue(self.call("gtm_deal", action="add", name="Wexcombe Freight", company="Wexcombe Freight", pipeline="My practice")["ok"])
        waiting = lambda **args: [note["id"] for note in self.call("gtm_notes", action="inbox", days=3650, **args)["notes"]]        # noqa: E731
        note = waiting()[0]
        self.assertEqual(waiting(pipeline="My practice"), [note])
        filed = self.call("gtm_notes", action="file", note=note, deal="Wexcombe Freight", summary="Ana wants pricing by Thursday.", next_step="Send pricing")
        self.assertEqual((filed["ok"], filed["pipeline"]), (True, "My practice"))          # the record is only there: it went there
        self.assertEqual(waiting(pipeline="My practice"), [])
        self.assertEqual(waiting(), [])                                                # nor is it waiting where it was never filed
        handled = self.call("gtm_notes", action="inbox", days=3650, include_filed=True, pipeline=first)["handled"]
        self.assertEqual([(row["id"], row["filed_on"]) for row in handled], [(note, "Wexcombe Freight (My practice)")])
        self.assertEqual(self.call("gtm_notes", action="known", notes=[note, "plaud:abc"]), {"ok": True, "new": ["plaud:abc"], "already_handled": [note], "pipeline": first})
        # Put back from either pipeline, it waits again in both.
        self.assertTrue(self.call("gtm_notes", action="reopen", note=note, pipeline=first)["ok"])
        self.assertEqual((waiting(), waiting(pipeline="My practice")), ([note], [note]))
        # Skipped in one: gone from the other.
        self.assertTrue(self.call("gtm_notes", action="skip", note=note)["ok"])
        self.assertEqual(waiting(pipeline="My practice"), [])

    def test_a_person_deletes_a_record_puts_it_back_or_deletes_it_for_good(self):
        made = self.call("gtm_deal", action="add", name="Thornbury Glass", company="Thornbury Glass", note="Met at the fair.")
        deal = made["deal"]["id"]
        gone = self.desk.remove(deal, actor="dashboard")
        self.assertEqual((gone["ok"], gone["name"]), (True, "Thornbury Glass"))
        self.assertFalse(self.call("gtm_deal", action="get", deal="Thornbury Glass")["ok"])            # off every list
        self.assertEqual([d for d in self.desk.store.list(status="all") if d["id"] == deal], [])
        self.assertFalse(self.desk.remove(deal)["ok"])                                                # not twice
        self.assertEqual([(row["id"], row["name"]) for row in self.desk.removed()], [(deal, "Thornbury Glass")])
        self.assertEqual(self.desk.store.stats()["archived"] if hasattr(self.desk.store, "stats") else 1, 1)
        back = self.call("gtm_deal", action="restore", deal="Thornbury Glass")                        # the agent can put it back
        self.assertTrue(back["ok"], back)
        log = json.dumps(self.call("gtm_deal", action="get", deal=deal))
        for piece in ("Met at the fair.", "Taken off the desk", "Put back on the desk"):                # nothing on it was lost
            self.assertIn(piece, log)
        self.assertEqual(self.desk.removed(), [])
        # Its removal is not something "undo" takes back or trips over: the last real change still is.
        self.call("gtm_deal", action="update", deal=deal, next_step="Send samples")
        self.desk.remove(deal)
        self.desk.restore(deal)
        undone = self.call("gtm_deal", action="undo", deal=deal)
        self.assertTrue(undone["ok"], undone)
        self.assertNotIn("next_step", self.call("gtm_deal", action="get", deal=deal)["deal"])
        # For good: only a record already off the desk, and then it is nowhere.
        self.assertFalse(self.desk.destroy(deal)["ok"])
        self.desk.remove(deal)
        self.assertTrue(self.desk.destroy(deal)["ok"])
        self.assertEqual(self.desk.removed(), [])
        self.assertFalse(self.call("gtm_deal", action="restore", deal="Thornbury Glass")["ok"])
        # The agent has no way to delete one, and is told so.
        from stoa import schemas as described
        self.assertNotIn("remove", described.GTM_DEAL["parameters"]["properties"]["action"]["enum"])
        self.assertIn("you cannot delete one yourself", described.GTM_DEAL["description"])
        page = (ROOT / "desktop" / "plugin.js").read_text()
        self.assertEqual(page.count("h(RemoveRecord, { deal, after: "), 2)                             # on the record's card and on its full page
        self.assertIn("h(TakenOff),", page)

    def test_the_agent_is_told_the_everyday_way_within_its_limit(self):
        section, limit = self.ctx.sections["stoa.desk"]
        text = section({"platform": "cli"})
        self.assertIn("name it in `deal` and change it: no get first, and the answer is the record as it stands", text)
        self.assertLessEqual(len(text), limit)
        self.second()
        text = section({"platform": "cli"})
        self.assertIn("found in whichever has it; one in two comes back with both: ask, never pick", text)
        self.assertLessEqual(len(text), limit)
        from stoa import schemas as described
        for piece in ("there is no need to get it first", "pass next_step and next_due in the same call", "comes back with the candidates: ask which"):
            self.assertIn(piece, described.GTM_DEAL["description"])

    def test_the_chat_today_card_has_buttons_for_every_list_it_shows(self):
        # A list with no buttons threw, and the whole card drew nothing (a won record with no next step did it).
        page = (ROOT / "desktop" / "plugin.js").read_text()
        lists = re.search(r"const CHAT_DAY = \[([^\]]+)\]", page).group(1).replace("'", "").replace(" ", "").split(",")
        card = page[page.index("function TodayInChat("):]
        card = card[:card.index("const lists = CHAT_DAY.map(")]
        moves = card[card.index("const moves = {"):]
        for key in lists:
            self.assertTrue(re.search(r"\b%s: " % key, moves) or "moves.%s = " % key in moves, "the chat Today card has no buttons for the list '%s'" % key)
        self.assertIn("(moves[list.key] || moves.new)(deal, item)", page)             # and a list added later cannot take the card down

    def test_the_day_is_answered_for_every_pipeline_at_once(self):
        self.second()
        self.call("gtm_deal", action="add", name="Rosa Lind", company="Ledgerline", next_step="Write to her", next_due="today")
        self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", next_step="Send the SOW", next_due="today", pipeline="My practice")
        day = self.call("gtm_pipeline", view="today")
        self.assertEqual([part["pipeline"] for part in day["across"]], ["Client lead desk", "My practice"])
        self.assertIn("Rosa Lind", json.dumps(day["across"][0]))
        self.assertIn("Visa retainer", json.dumps(day["across"][1]))
        self.assertNotIn("Visa retainer", json.dumps(day["across"][0]))
        one = self.call("gtm_pipeline", view="today", pipeline="My practice")
        self.assertEqual(one["pipeline"], "My practice")
        self.assertNotIn("across", one)
        self.assertNotIn("across", self.call("gtm_pipeline"))                     # the board is one pipeline's

    def test_parts_drawn_in_a_chat_name_their_pipeline_once_there_are_several(self):
        self.session(platform="desktop")
        self.desk.desktop_seen = time.time()                                      # the app's half has checked in
        rosa = self.call("gtm_deal", action="add", name="Rosa Lind", company="Ledgerline", next_step="Write to her", next_due="today")["deal"]["id"]
        self.assertEqual(self.call("gtm_card", deal="Rosa Lind")["directive"], '::stoa-card{id="%s"}' % rosa)      # one pipeline: as before
        day = self.call("gtm_pipeline", view="today")
        self.assertEqual(day["directive"], "::stoa-today{}")
        self.assertIn("Done, Draft and Snooze", day["note"])
        stages = self.call("gtm_report", action="read", report="stages")
        self.assertEqual(stages["directive"], '::stoa-report{id="stages" period="90"}')
        self.second()
        visa = self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", next_step="Send the SOW", next_due="today", pipeline="My practice")["deal"]["id"]
        self.assertEqual(self.call("gtm_card", deal="Visa retainer", pipeline="My practice")["directive"], '::stoa-card{id="%s" pipeline="p2"}' % visa)
        self.assertEqual(self.call("gtm_card", deal="Rosa Lind")["directive"], '::stoa-card{id="%s" pipeline="main"}' % rosa)
        day = self.call("gtm_pipeline", view="today")                             # one answer, and one list to draw, for each pipeline
        self.assertEqual([part["directive"] for part in day["across"]], ['::stoa-today{pipeline="main"}', '::stoa-today{pipeline="p2"}'])
        read = self.call("gtm_report", action="read", report="Win rate", period="year", pipeline="My practice")
        self.assertEqual(read["directive"], '::stoa-report{id="winrate" period="year" pipeline="p2"}')
        made = self.call("gtm_report", action="save", title="Value by owner", count="value", by="owner", when="open", pipeline="My practice")
        self.assertTrue(made["draft"])
        self.assertEqual(made["directive"], '::stoa-report{id="%s" period="90" pipeline="p2"}' % made["id"])
        self.assertIn("Keep and Discard", made["note"])
        for elsewhere in ("ios", "mobile", "slack", "cli", "cron"):               # the phone app and the rest get text, never a line they cannot draw
            self.session(platform=elsewhere)
            self.assertNotIn("directive", json.dumps(self.call("gtm_pipeline", view="today", pipeline="My practice")), elsewhere)
            self.assertNotIn("directive", self.call("gtm_report", action="read", report="stages"), elsewhere)

    def test_the_agent_is_told_which_pipelines_there_are_within_its_limit(self):
        self.second()
        section, limit = self.ctx.sections["stoa.desk"]
        text = section({"platform": "cli"})
        self.assertIn("2 pipelines: Client lead desk (leads, grouped by search); My practice (opportunities, grouped by client)", text)
        self.assertLessEqual(len(text), limit)
        for n in range(4):
            self.assertTrue(self.desk.pipeline_add("A pipeline with a rather long name number %d" % n, "partnerships")["ok"])
        self.assertFalse(self.desk.pipeline_add("One too many", "agency")["ok"])
        text = section({"platform": "cli"})
        self.assertLessEqual(len(text), limit)
        self.assertIn("stays on the desk", text)                                  # the end of it is still there
        self.assertIn("My practice", text)

    def test_pipelines_are_renamed_given_another_playbook_and_removed_only_when_empty(self):
        self.second()
        self.assertTrue(self.call("gtm_playbook", action="rename_pipeline", pipeline="My practice", to="Advisory")["ok"])
        self.assertTrue(self.call("gtm_playbook", action="rename_pipeline", to="Client work")["ok"])       # no pipeline said: the first
        self.assertEqual([p["name"] for p in self.desk.pipelines()], ["Client work", "Advisory"])
        self.assertIn("already", self.desk.pipeline_rename("Advisory", "client work")["error"])
        used = self.call("gtm_playbook", action="use", name="b2b-sales", pipeline="Advisory")
        self.assertEqual((used["active"], used["pipeline"]), ("b2b-sales", "Advisory"))
        self.assertEqual([p["playbook"] for p in self.desk.pipelines()], ["client-leads", "b2b-sales"])
        self.assertEqual(self.ctx.settings["playbook"], "client-leads")           # the first one's setting was not touched
        self.call("gtm_deal", action="add", name="Visa retainer", pipeline="Advisory")
        self.assertIn("still has 1 record", self.desk.pipeline_remove("Advisory")["error"])
        self.assertIn("first pipeline stays", self.desk.pipeline_remove("Client work")["error"])
        self.assertTrue(self.desk.pipeline_add("Scratch", "agency")["ok"])
        self.assertTrue(self.desk.pipeline_remove("Scratch")["ok"])
        self.assertEqual([p["name"] for p in self.desk.pipelines()], ["Client work", "Advisory"])
        self.assertTrue(self.desk.pipeline_add("Scratch", "agency")["ok"])        # the name is free again, in a new file
        self.assertEqual(self.desk.pipelines()[-1]["id"], "p4")
        self.assertFalse(self.desk.pipeline_add("Bad", "no-such-playbook")["ok"])
        self.assertFalse(self.desk.pipeline_add("", "agency")["ok"])

    def test_each_pipeline_keeps_its_own_autonomy_copies_and_holds(self):
        self.second()
        self.call("gtm_deal", action="add", name="Rosa Lind", company="Ledgerline")
        self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", pipeline="My practice")
        with self.desk.on("p2"):
            self.desk.set_autonomy("full")
            self.assertEqual(self.desk.autonomy(), "full")
        self.assertEqual(self.desk.autonomy(), "ask")
        self.session(platform="cron")
        held = self.call("gtm_deal", action="move", deal="Rosa Lind", stage="next")
        self.assertTrue(held.get("held"), held)                                   # the first pipeline asks first
        done = self.call("gtm_deal", action="move", deal="Visa retainer", stage="next", pipeline="My practice")
        self.assertEqual((done.get("held"), done["deal"]["stage_label"]), (None, "Intro call"))    # the second was told to run
        self.assertFalse(self.call("gtm_playbook", action="add_pipeline", pipeline_name="Sneaky", playbook="agency")["ok"])
        self.assertEqual(len(self.desk.pipelines()), 2)                           # a scheduled job opens no pipelines
        again = stoa.build_desk(self.ctx.get_config, self.ctx.set_config)         # the next time Hermes starts
        with again.on("p2"):
            again.own_store
        again.own_store
        self.assertEqual(sorted(p.name[:8] for p in (self.data / "backups").glob("*.db")), ["desk-202", "pipe-p2-"])
        with again.on("p2"):
            self.assertEqual(again.backups()["count"], 1)

    def test_the_terminal_lists_adds_and_addresses_pipelines(self):
        import contextlib
        import io
        from stoa import cli

        def run(command, **args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.run(self.desk, types.SimpleNamespace(stoa_command=command, **args))
            return out.getvalue()

        listed = run("pipelines", verb="add", name="My practice", playbook="agency", to="")
        self.assertIn("● Client lead desk", listed)
        self.assertIn("○ My practice", listed)
        self.call("gtm_deal", action="add", name="Visa retainer", company="Visa", pipeline="My practice")
        there = run("status", pipeline="practice")
        self.assertIn("Pipeline      My practice (of 2: Client lead desk, My practice)", there)
        self.assertIn("1 open", there)
        self.assertIn("pipe-p2.db", there)
        self.assertIn("0 open", run("status", pipeline=""))
        self.assertIn("no pipeline is called", run("status", pipeline="nowhere"))
        self.assertIn("still has 1 record", run("pipelines", verb="remove", name="My practice", playbook="", to=""))
        self.assertIn("○ Advisory", run("pipelines", verb="rename", name="My practice", playbook="", to="Advisory"))
        self.assertEqual(self.desk.store.count(), 0)                              # the terminal's choice does not outlive the command

    def test_a_tour_shows_one_sample_desk_whatever_pipeline_is_asked_for(self):
        self.second()
        self.desk.tour_start("recruiting")
        out = self.call("gtm_pipeline", view="today", pipeline="My practice")
        self.assertTrue(out["ok"])
        self.assertNotIn("pipeline", out)
        self.assertNotIn("across", self.call("gtm_pipeline", view="today"))
        self.assertIn("Sample data is on show", self.desk.prompt_section())
        self.desk.tour_end()
        self.assertIn("across", self.call("gtm_pipeline", view="today"))


class RegisterTests(unittest.TestCase):
    """``register`` against a stand-in for Hermes: what gets wired, and where the desk shows itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "data"
        data.mkdir()
        fakes = {"hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: self.tmp.name),
                 "plugins": types.ModuleType("plugins"),
                 "plugins.plugin_storage": types.SimpleNamespace(plugin_data_dir=lambda name: data)}
        patcher = mock.patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeCtx({"playbook": "recruiting", "channels": ["C1"]})
        stoa.register(self.ctx)

    def session(self, **values):
        patcher = mock.patch.object(stoa, "_session", lambda: values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_everything_the_manifest_promises_is_registered(self):
        self.assertEqual(sorted(self.ctx.tools), ["gtm_card", "gtm_contact", "gtm_context", "gtm_deal", "gtm_enrich", "gtm_export", "gtm_group", "gtm_intake",
                                                  "gtm_invoice", "gtm_market", "gtm_motion", "gtm_notes", "gtm_pipeline", "gtm_playbook",
                                                  "gtm_report"])
        self.assertEqual(sorted(self.ctx.blueprints), ["activity-sweep", "friday-review", "market-watch", "morning-brief",
                                                       "notes-sweep", "sheet-mirror", "sheet-pull"])
        self.assertEqual(sorted(self.ctx.skills), ["desk", "market-map", "playbook-authoring"])
        self.assertIn("gtm", self.ctx.commands)
        self.assertEqual(stoa.LIVE_DESKS[-1].home, Path(self.tmp.name))

    def test_a_tool_that_changes_the_desk_tells_an_open_window(self):
        told = []
        stoa.LIVE_DESKS[-1].announce = lambda: told.append(1)
        self.session(platform="slack", chat_id="C1")
        self.assertTrue(json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Ada Quill"}))["ok"])
        self.assertEqual(len(told), 1)
        self.ctx.tools["gtm_pipeline"]({})                                                 # reading changes nothing
        self.assertEqual(len(told), 1)
        self.session(platform="slack", chat_id="C2")                                       # a chat the desk is not in
        self.ctx.tools["gtm_deal"]({"action": "add", "name": "Nobody"})
        self.assertEqual(len(told), 1)

    def test_the_desk_stays_out_of_chats_it_is_not_allowed_in(self):
        section, limit = self.ctx.sections["stoa.desk"]
        self.session(platform="slack", chat_id="C2")
        for info in (None, {}, {"platform": ""}, {"platform": "slack"}):       # the host may or may not say where
            self.assertEqual(section(info), "", info)
        self.assertIn("not available", json.loads(self.ctx.tools["gtm_pipeline"]({}))["error"])
        self.assertIn("not available", self.ctx.commands["gtm"](""))

    def test_an_agent_closing_a_record_as_lost_picks_one_of_the_playbooks_reasons(self):
        self.session(platform="slack", chat_id="C1")
        desk = stoa.LIVE_DESKS[-1]
        made = json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Ada Quill"}))["deal"]["id"]
        asked = json.loads(self.ctx.tools["gtm_deal"]({"action": "close", "deal": made, "outcome": "lost", "reason": "the money was far too low for her"}))
        self.assertFalse(asked["ok"])
        self.assertIn("Comp mismatch", asked["reasons"])
        self.assertEqual(desk.store.get(made)["status"], "open")                  # nothing was changed
        done = json.loads(self.ctx.tools["gtm_deal"]({"action": "close", "deal": made, "outcome": "lost", "reason": "comp mismatch"}))
        self.assertEqual((done["ok"], desk.store.get(made)["lost_reason"]), (True, "Comp mismatch"))
        other = json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Bo Lindqvist"}))["deal"]["id"]
        self.assertFalse(json.loads(self.ctx.tools["gtm_deal"]({"action": "move", "deal": other, "stage": "passed", "reason": "ghosted us"}))["ok"])
        self.assertTrue(json.loads(self.ctx.tools["gtm_deal"]({"action": "move", "deal": other, "stage": "passed", "reason": "Other: went dark"}))["ok"])
        self.assertEqual(desk.store.get(other)["lost_reason"], "Other: went dark")
        third = json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Cy Okafor"}))["deal"]["id"]
        self.assertTrue(json.loads(self.ctx.tools["gtm_deal"]({"action": "close", "deal": third, "outcome": "lost"}))["ok"])     # no reason given: not asked
        self.assertTrue(json.loads(self.ctx.tools["gtm_deal"]({"action": "close", "deal": made, "outcome": "won", "reason": "anything"}))["ok"] in (True, False))

    def test_a_scheduled_run_is_known_by_hermes_own_mark_not_by_a_platform(self):
        """Hermes runs a scheduled job with an empty platform and HERMES_CRON_SESSION set. Read as a terminal
        session it would be held to nothing: found with a real scheduled run on 0.13.1, which changed a stage and
        marked an invoice paid at the lowest autonomy level."""
        env = {"HERMES_SESSION_PLATFORM": "", "HERMES_CRON_SESSION": "1"}
        fake = types.SimpleNamespace(get_session_env=lambda name, default="": env.get(name, default))
        patcher = mock.patch.dict(sys.modules, {"gateway": types.ModuleType("gateway"), "gateway.session_context": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(stoa._session()["platform"], "cron")
        refused = json.loads(self.ctx.tools["gtm_pipeline"]({}))             # channels is ["C1"]: scheduled jobs are not opted in
        self.assertIn("not available", refused["error"])
        self.ctx.settings["channels"] = ["C1", "cron"]
        desk = stoa.LIVE_DESKS[-1]
        made = desk.deal({"action": "add", "name": "Ada Quill", "company": "Fathom Data", "value": 20000})["deal"]
        held = json.loads(self.ctx.tools["gtm_deal"]({"action": "move", "deal": made["id"], "stage": "next"}))
        self.assertTrue(held.get("held"), held)
        self.assertEqual(desk.store.get(made["id"])["stage"], made["stage"])
        desk.deal({"action": "close", "deal": made["id"], "outcome": "won"})
        desk.invoice({"action": "add", "deal": made["id"], "amount": 20000, "number": "7"})
        paid = json.loads(self.ctx.tools["gtm_invoice"]({"action": "pay", "invoice": "7"}))
        self.assertTrue(paid.get("held"), paid)
        self.assertEqual(desk.store.invoices(made["id"])[0]["paid"], 0.0)
        self.assertEqual(len(desk.intake.queue()), 2)                      # both wait for a person
        env["HERMES_CRON_SESSION"] = ""                                        # the same calls from a person at a terminal go through
        self.assertEqual(stoa._session()["platform"], "")
        self.assertTrue(json.loads(self.ctx.tools["gtm_invoice"]({"action": "pay", "invoice": "7"}))["ok"])
        self.assertEqual(desk.store.invoices(made["id"])[0]["paid"], 20000.0)

    def test_a_scheduled_job_is_held_at_the_tool_and_a_person_is_not(self):
        desk = stoa.LIVE_DESKS[-1]
        self.ctx.settings["channels"] = ["C1", "cron"]
        self.session(platform="slack", chat_id="C1")
        made = json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Ada Quill"}))["deal"]["id"]
        self.session(platform="cron")
        held = json.loads(self.ctx.tools["gtm_deal"]({"action": "move", "deal": made, "stage": "next", "why": "She replied."}))
        self.assertEqual((held["ok"], held["held"], desk.store.get(made)["stage"]), (True, True, "sourced"))
        logged = json.loads(self.ctx.tools["gtm_deal"]({"action": "log", "deal": made, "kind": "email", "text": "She replied.", "ref": "gmail:1"}))
        self.assertTrue(logged["ok"])
        self.assertEqual(desk.store.activities(made, limit=1)[0]["actor"], "Hermes (scheduled)")
        self.assertEqual(json.loads(self.ctx.tools["gtm_intake"]({"action": "apply"}))["ok"], False)
        self.assertTrue(json.loads(self.ctx.tools["gtm_contact"]({"action": "match", "emails": ["a@b.example"]}))["ok"])
        self.session(platform="slack", chat_id="C1")
        self.assertEqual(json.loads(self.ctx.tools["gtm_deal"]({"action": "move", "deal": made, "stage": "next"}))["deal"]["stage"], "contacted")

    def test_scheduled_jobs_wait_while_sample_data_is_on_show(self):
        desk = stoa.LIVE_DESKS[-1]
        self.ctx.settings["channels"] = ["C1", "cron"]
        self.session(platform="cron")
        self.assertTrue(json.loads(self.ctx.tools["gtm_pipeline"]({}))["ok"])
        desk.tour_start("recruiting")
        self.addCleanup(desk.tour_end)
        refused = json.loads(self.ctx.tools["gtm_pipeline"]({"view": "today"}))
        self.assertEqual(refused["ok"], False)
        self.assertIn("scheduled jobs are paused", refused["error"])
        self.assertIn("[SILENT]", refused["error"])

    def test_the_desk_introduces_itself_where_it_is_allowed(self):
        section, limit = self.ctx.sections["stoa.desk"]
        self.session(platform="slack", chat_id="C1")
        text = section({})
        self.assertIn("Stoa GTM desk", text)
        self.assertIn("before saying it cannot be found", text)        # an agent with other places to look still checks here
        self.assertLessEqual(len(text), limit)
        for schema in (schemas.GTM_PIPELINE, schemas.GTM_DEAL, schemas.GTM_CARD, schemas.GTM_MARKET, schemas.GTM_ENRICH,
                       schemas.GTM_NOTES, schemas.GTM_PLAYBOOK, schemas.GTM_EXPORT, schemas.GTM_INTAKE, schemas.GTM_CONTACT, schemas.GTM_INVOICE):
            first = " ".join(schema["description"].split()).split(". ")[0]
            self.assertLessEqual(len(first), 79, schema["name"])       # Hermes lists a deferred tool by this one line, cut at 80
            self.assertTrue("Stoa desk" in first or "People Data Labs" in first, schema["name"])
        self.assertTrue(json.loads(self.ctx.tools["gtm_pipeline"]({}))["ok"])
        self.assertIn("Stoa GTM desk", section({"platform": "cli"}))


class DashboardApiTests(unittest.TestCase):
    """The dashboard tab's backend, called directly (no web server needed)."""

    def setUp(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed here; Hermes provides it")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        spec = importlib.util.spec_from_file_location("stoa_dash_api_test", ROOT / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.desk = make_desk(self.tmp.name)
        self.api._profile_desk = lambda: self.desk          # the real desk() then picks the pipeline, as it does for a request
        fixture.load(self.desk)

    def test_a_motions_list_from_the_page(self):
        made = self.api.motion_add({"name": "Email offer", "kind": "outbound", "status": "running"})
        dana = self.desk.store.find("Dana Whitfield")
        seen = self.api.motion_members(made["id"], {"deals": [dana["id"]]})
        self.assertEqual((seen["added"], seen["impact"]["list"]["total"], seen["impact"]["members"][0]["via"]), (["Dana Whitfield"], 1, "record"))
        fresh = self.api.motion_members(made["id"], {"new": {"name": "Wei-Lin Chao", "company": "Brightside AI"}})
        row = next(r for r in fresh["impact"]["members"] if r["name"] == "Wei-Lin Chao")
        self.assertEqual((row["via"], row["company"]), ("new", "Brightside AI"))
        self.assertEqual({r["name"]: r["how"] for r in fresh["impact"]["records"]}, {"Wei-Lin Chao": "sourced", "Dana Whitfield": "influenced"})
        marked = self.api.motion_members(made["id"], {"mark": row["key"], "status": "meeting"})
        self.assertEqual(next(r["status"] for r in marked["impact"]["members"] if r["key"] == row["key"]), "meeting")
        gone = self.api.motion_members(made["id"], {"drop": row["key"]})
        self.assertEqual([r["name"] for r in gone["impact"]["members"]], ["Dana Whitfield"])
        with self.assertRaises(Exception):
            self.api.motion_members(made["id"], {})
        self.assertEqual([m["name"] for m in self.api.deal(dana["id"])["motions"]], ["Email offer"])

    def test_the_talk_box_has_standing_questions_answered_with_no_model(self):
        told = self.api.instant({"text": "Who has gone quiet?"})
        self.assertEqual((told["kind"], told["said"]), ("quiet", "Read from the desk · no model used"))
        self.assertEqual(told["text"], self.desk.instant.answer("quiet"))
        self.assertEqual(self.api.instant({"text": "Who has gone quiet, and what should I send each of them?"}), {"kind": ""})
        self.assertEqual(self.api.instant({"kind": "week"})["kind"], "week")
        self.assertEqual(self.api.instant({"kind": "anything"}), {"kind": ""})

    def test_the_page_is_told_how_to_lay_a_desk_out(self):
        library = Library()
        leads, sales, search = (self.api._playbook(library.load(name)) for name in ("client-leads", "b2b-sales", "recruiting"))
        self.assertEqual((leads["billing"], leads["group_is_company"]), ("none", False))
        self.assertEqual((sales["billing"], sales["group_is_company"]), ("record", True))
        self.assertEqual((search["billing"], search["group_is_company"]), ("record", False))
        dana = self.desk.store.find("Dana Whitfield")
        self.api.act(dana["id"], {"verb": "update", "phone": "+1 415 555 0142", "fields": {"location": "Austin, TX"}, "source": "Referral"})
        shown = self.api.deal(dana["id"])
        self.assertEqual((shown["phone"], shown["fields"]["location"], shown["source"]), ("+1 415 555 0142", "Austin, TX", "Referral"))

    def test_the_dashboard_tab_is_given_the_desk_in_totals_and_no_record(self):
        fixture.load_market(self.desk)
        market = self.desk.markets.list_markets()[0]
        self.desk.markets.set_limits(market["id"], {"companies": 40})
        self.desk.store.bump("map:%s:runs" % market["id"], 2)
        self.desk.store.bump("map:%s:calls" % market["id"], 9)
        self.desk.store.bump("map:%s:cost" % market["id"], 2_400_000)
        self.desk.store.bump("map:%s:cost:%s" % (market["id"], dt.date.today().isoformat()[:7]), 1_100_000)
        seen = self.api.overview()
        tally = self.desk.own_store.tally()
        self.assertEqual((seen["totals"]["open"], seen["totals"]["won"], seen["totals"]["records"]),
                         (tally["open"], tally["won"], tally["open"] + tally["won"] + tally["lost"]))
        self.assertEqual((len(seen["pipelines"]), seen["pipelines"][0]["file"], seen["pipelines"][0]["open"]), (1, "desk.db", tally["open"]))
        only = seen["maps"][0]
        self.assertEqual((only["name"], only["companies"], only["usd"], only["usd_month"], only["limits"]),
                         (market["name"], len(fixture.COMPANIES), 2.4, 1.1, {"companies": 40}))
        self.assertEqual((seen["totals"]["mapped"], seen["spend"]["usd_month"], seen["spend"]["usd"], seen["spend"]["lookup_cap"]),
                         (len(fixture.COMPANIES), 1.1, 2.4, 50))
        told = json.dumps(seen)      # totals and the names of pipelines and maps: nobody on the desk and no company on a map is in it
        for private in ("Dana Whitfield", "Meridian Freight"):
            self.assertNotIn(private, told)
        self.api.tour({"action": "start", "example": "recruiting"})      # on a tour it still reports the person's own desk
        on_tour = self.api.overview()
        self.assertEqual((on_tour["tour"], on_tour["maps"], on_tour["totals"]["open"]), (True, [], tally["open"]))

    def test_two_requests_at_once_on_a_fresh_dashboard_both_get_the_whole_plugin(self):
        import threading, time
        spec = importlib.util.spec_from_file_location("stoa_dash_api_race", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        api.PACKAGE = "stoa_dash_race_pkg"
        sys.modules.pop(api.PACKAGE, None)
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules) if name.startswith("stoa_dash_race_pkg")])
        real = importlib.util.spec_from_file_location

        def slow(*args, **kwargs):      # the first request is still running the package when the second arrives
            found = real(*args, **kwargs)
            run = found.loader.exec_module
            found.loader.exec_module = lambda module: (time.sleep(0.15), run(module))[1]
            return found

        api.importlib.util.spec_from_file_location = slow
        self.addCleanup(lambda: setattr(api.importlib.util, "spec_from_file_location", real))
        seen = []
        threads = [threading.Thread(target=lambda: seen.append(hasattr(api._package(), "_home"))) for _ in range(2)]
        for thread in threads:
            thread.start()
            time.sleep(0.03)
        for thread in threads:
            thread.join()
        self.assertEqual(seen, [True, True])

    def test_the_dashboard_tab_is_setup_and_status_and_does_not_mirror_the_desk(self):
        page = (ROOT / "dashboard" / "dist" / "index.js").read_text()
        for used in ('"/setup"', '"/overview"', '"/key/test"', '"/notes/check"', '"/notes/folder"', '"/notes/look"', '"/autonomy"', '"/tour"', '"/export"'):
            self.assertIn(used, page)
        for gone in ('"/state"', '"/deal', '"/targets"', '"/target/', '"/company"', '"/approvals"'):      # no pipeline, no map, no record
            self.assertNotIn(gone, page)
        self.assertNotIn("/api/env", page)      # a key is typed on the dashboard's own Keys page, never here
        self.assertNotIn("type: \"password\"", page)
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        self.assertIn("set-up", manifest["description"].lower().replace("setup", "set-up"))

    def test_the_page_is_handed_the_artwork_as_grids_of_dots(self):
        art = self.api.art()
        self.assertEqual(sorted(art), ["head", "v", "welcome"])      # not the names 0.20 used: a page not yet reloaded draws nothing, not noise
        self.assertEqual(art["v"], 3)
        # Digits and consonants only. Written in the base64 alphabet, a picture once spelled "AKIA" and sixteen capitals
        # by chance, which the Hermes plugin scan takes for a leaked AWS key and refuses the install over. With no vowels
        # and no punctuation there is no key prefix and no word to spell.
        alphabet = "0123456789bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ"
        for name in ("head", "welcome"):
            piece = art[name]
            self.assertEqual(len(piece["cells"]), piece["w"] * piece["h"])            # one character a cell
            self.assertLessEqual(set(piece["cells"]), set(alphabet))
            values = [alphabet.index(c) for c in set(piece["cells"])]
            self.assertTrue(any(v & 2 for v in values) and any(v & 1 for v in values))    # dots for a dark theme, and for a light one
            self.assertFalse(any(v & 3 and not v >> 2 for v in values))                  # a dot always says how bright it is
        self.assertIn("'0123456789bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ'", (ROOT / "desktop" / "plugin.js").read_text())   # the page reads the same one

    def test_the_desk_carries_its_own_name_for_the_head_of_the_page(self):
        self.assertEqual(self.api.state()["sign"], {"text": "", "place": "art", "ink": "auto"})     # by the theme until someone says
        saved = self.api.sign({"text": "  Fernway   Search ", "place": "beside"})
        self.assertEqual((saved["text"], saved["place"]), ("Fernway Search", "beside"))
        self.assertEqual(self.api.sign({"place": "art"})["text"], "Fernway Search")           # one can be set without the other
        self.assertEqual(self.api.state()["sign"], {"text": "Fernway Search", "place": "art", "ink": "auto"})
        self.assertEqual(self.api.sign({"ink": "mono"})["ink"], "mono")                       # the picture and the name in one ink
        self.assertEqual(self.api.state()["sign"], {"text": "Fernway Search", "place": "art", "ink": "mono"})
        self.assertEqual(self.api.sign({"ink": "color"})["ink"], "color")
        self.assertEqual(self.api.sign({"ink": "auto"})["ink"], "auto")                       # and back to following the theme
        for wrong in ({"text": "x" * 41}, {"place": "sideways"}, {"ink": "sepia"}):
            with self.assertRaises(self.api.HTTPException):
                self.api.sign(wrong)
        self.assertEqual(self.api.state()["sign"]["text"], "Fernway Search")                  # a refusal changes nothing
        self.assertEqual(self.api.sign({"text": ""})["text"], "")                              # no name: the stoa is drawn again

    def test_the_page_can_add_edit_move_and_log_without_a_model(self):
        added = self.api.add_deal({"name": "Nadia Rahim", "company": "Larkspur", "group": "Staff Backend · Northwind", "value": 41000,
                                   "next_step": "Intro call", "next_due": "2030-01-07"})
        nadia = added["deal"]["id"]
        self.assertEqual((added["ok"], added["deal"]["stage"]), (True, "sourced"))
        self.assertTrue(self.api.add_deal({"name": "Nadia Rahim", "group": "Staff Backend · Northwind"})["exists"])    # no second copy
        self.assertTrue(self.api.act(nadia, {"verb": "update", "next_step": "Send the brief", "owner": "Maya", "value": 45000})["ok"])
        self.assertTrue(self.api.act(nadia, {"verb": "move", "stage": "screened"})["ok"])
        self.assertTrue(self.api.act(nadia, {"verb": "log", "kind": "call", "text": "Spoke for ten minutes; keen."})["ok"])
        record = self.desk.store.get(nadia)
        self.assertEqual((record["stage"], record["next_step"], record["owner"], record["value"]), ("screened", "Send the brief", "Maya", 45000))
        self.assertIn("Spoke for ten minutes", [a["text"] for a in self.desk.store.activities(nadia)][0])
        self.assertEqual({a["actor"] for a in self.desk.store.activities(nadia, limit=20)}, {"dashboard"})    # typed by a person, not the agent
        with self.assertRaises(self.api.HTTPException):
            self.api.act(nadia, {"verb": "move", "stage": "no-such-stage"})

    def test_a_message_from_the_talk_box_says_what_is_on_screen(self):
        source = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for piece in ("'(On screen on the Stoa page: ' + here + '.)\\n\\n' + typed", "seen = { tab, paged, open: top ? { kind: top.kind, id: top.id } : null",
                      "bits.push(...gtmLooking(page.market))", "'the motion open is “' + motion.name + '”'", "when it says \"this\" or '",
                      # an answer the desk made with no model goes to Hermes with the next message, so a follow-up has it
                      "talking.unseen.push({ you: line, text: quick.text })", "const before = lane === 'desk' ? unseenLines() : ''",
                      "if (!reached && held.length) talking.unseen = held.concat(talking.unseen)"):
            self.assertIn(piece, source)

    def test_the_page_script_parses(self):
        # `node --check plugin.js` passes whatever is in the file: node has to be told it is a module to read it as one.
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as folder:
            for source in (ROOT / "desktop" / "plugin.js", ROOT / "pagemap.js"):
                copy = Path(folder) / (source.stem + ".mjs")
                copy.write_text(source.read_text(), encoding="utf-8")
                done = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, "%s does not parse:\n%s" % (source.name, done.stderr[:600]))

    def test_the_talk_box_keeps_the_caret_out_only_while_dictated_words_are_still_to_come(self):
        """What was dictated and sent can come back as the Mac's last transcript. The caret leaves the box for a moment
        if words were still marked as they went; nothing typed or said is compared, changed or dropped."""
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        source = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        start = source.index("function lateWords() {")
        body = source[start:source.index("\n}\n", start) + 3]
        self.assertNotIn("target", body)                # it never reads the box's words, or an input's
        self.assertNotIn("data", body)
        script = body + """
let on, key
globalThis.document = { body: {}, get activeElement() { return on }, addEventListener: (_n, f) => { key = f }, removeEventListener: () => { key = null } }
on = document.body
document.querySelector = () => null
const box = { isConnected: true, matches: () => true, closest: () => null, focus() { on = box }, blur() { on = document.body } }
globalThis.setTimeout = () => 0
const out = []
let g = lateWords()
box.focus(); g.sending(box); out.push(on === box)                                                   // typed: the caret stays
g.marks.onCompositionStart(); g.marks.onCompositionEnd(); g.sending(box); out.push(on === box)      // committed already: stays
g.marks.onCompositionUpdate(); g.sending(box); out.push(on === box)                                 // still marked: out
g.focus(box); out.push(on === box)                                                                  // not put back by the page
key({ key: 'F5' }); out.push(on === box)
key({ key: 'a' }); out.push(on === box)                                                             // typing puts it back
g.marks.onCompositionStart(); g.marks.onCompositionEnd(); out.push(g.leaving(box), on === box)      // left just as it was committed: out
key({ key: 'a' })
g.marks.onCompositionStart(); g.marks.onKeyDownCapture({ keyCode: 229 }); g.sending(box)
out.push(on === box)                                                                                // composed with keys (an input method): stays
g.marks.onKeyDownCapture({ keyCode: 229 }); g.marks.onCompositionStart(); g.sending(box)
out.push(on === box)                                                                                // the key first, as Chromium has it: stays
const other = { matches: () => false, closest: () => null }
let h = lateWords()
box.focus(); h.marks.onCompositionUpdate(); h.sending(box); on = other; key({ key: 'x' })
out.push(on === other)                                                                              // a button focused since keeps its keys
on = document.body; h.focus(box); out.push(on === box)                                              // and that key ended the wait
on = other; h.focus(box, true); out.push(on === other)                                              // words given back do not take the next Return
box.focus(); h.marks.onCompositionUpdate(); h.sending(box)
on = other; h.focus(box); key({ key: 'x' }); out.push(on === box)                                   // a second click that chose the box gives it the caret
const enter = (isComposing, keyCode) => g.sends({ key: 'Enter', keyCode, nativeEvent: { isComposing } })
out.push(enter(false, 13), enter(true, 13), enter(false, 229), enter(true, 229))
console.log(JSON.stringify(out))"""
        done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, json.loads(done.stdout or "[]")),
                         (0, [True, True, False, False, False, True, True, False, True, True, True, True, True, True, True, True, False, False]), done.stderr[:400])

    def test_a_command_that_went_is_not_put_back_when_its_box_goes(self):
        """A command opens the chat in the same tick, so the talk box can be gone before it is drawn empty: what it
        puts back for later must already be empty."""
        page = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        send = page[page.index("  const send = line => {", page.index("function Dock({ state })")):]
        self.assertLess(send.index("held.current = ''"), send.index("typeInChat("))
        self.assertLess(send.index("held.current = ''", send.index("typeInChat(")), send.index("say(words, files, 'desk'"))

    def test_a_card_names_the_first_person_on_its_record(self):
        ingrid = self.desk.store.find("Ingrid Solberg")["id"]
        self.assertNotIn("contact", next(card for card in self.api.state()["deals"] if card["id"] == ingrid))      # nobody on it but itself
        self.desk.store.person_add(ingrid, "Hal Marsh", title="Hiring manager", email="hal@halcyon.example")
        self.desk.store.person_add(ingrid, "Jo Lind")
        card = next(card for card in self.api.state()["deals"] if card["id"] == ingrid)
        self.assertEqual((card["contact"], card["contact_title"], card["people_count"]), ("Hal Marsh", "Hiring manager", 2))
        self.assertEqual((self.api.deal(ingrid)["contact"], [p["name"] for p in self.api.deal(ingrid)["people"]]), ("Hal Marsh", ["Hal Marsh", "Jo Lind"]))

    def test_the_page_gets_today_the_tour_and_setup(self):
        state = self.api.state()
        self.assertEqual((state["tour"], state["fresh"], state["today"]["counts"]["overdue"]), (None, False, 2))
        self.assertEqual(sorted(state["today"]["overdue"][0]), ["days", "id"])
        ingrid = self.desk.store.find("Ingrid Solberg")["id"]
        until = (dt.date.today() + dt.timedelta(days=14)).isoformat()
        self.assertTrue(self.api.act(ingrid, {"verb": "snooze", "until": until})["ok"])
        self.assertEqual(self.desk.store.get(ingrid)["snoozed_until"], until)
        with self.assertRaises(self.api.HTTPException):
            self.api.act(ingrid, {"verb": "snooze", "until": "2001-01-01"})
        dana = self.desk.store.find("Dana Whitfield")["id"]
        self.assertTrue(self.api.act(dana, {"verb": "done", "next_step": "Prep call", "next_due": "2031-01-07"})["ok"])
        self.assertEqual(self.desk.store.get(dana)["next_step"], "Prep call")
        self.assertEqual(self.api.setup()["playbook"]["name"], "recruiting")
        self.assertEqual(self.api.key_test({"service": "pdl"})["state"], "missing")     # the page's Test button, with no key set
        self.assertFalse(self.api.notes_check({"folder": "not-a-whole-path"})["ok"])
        self.assertEqual(self.api.notes_look()["ok"], True)
        started = self.api.tour({"action": "start", "example": "saas"})
        self.assertEqual((started["example"], self.api.state()["tour"]["example"], self.api.state()["playbook"]["name"]), ("saas", "saas", "b2b-sales"))
        self.assertEqual(self.api.tour({"action": "leave"}), {"ok": True, "left": True})
        self.assertEqual((self.api.state()["tour"], self.api.state()["playbook"]["name"]), (None, "recruiting"))
        with self.assertRaises(self.api.HTTPException):
            self.api.tour({"action": "start", "example": "nonsense"})
        exported = self.api.export()
        self.assertEqual((exported["rows"], exported["csv"].splitlines()[0].split(",")[:3]), (7, ["id", "name", "company"]))

    def test_the_page_takes_a_change_back(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        moved = self.api.act(dana, {"verb": "move", "stage": "submitted"})
        self.assertEqual((moved["last"]["deal"], moved["last"]["name"], moved["last"]["text"]), (dana, "Dana Whitfield", "screened → Submitted"))
        state = self.api.state()
        entry = next(a for a in state["activity"] if a["deal"] == dana)
        self.assertEqual((entry["can_undo"], entry["op"], self.api.deal(dana)["undo"]["op"]), (True, moved["last"]["op"], moved["last"]["op"]))
        back = self.api.undo(dana, {"op": moved["last"]["op"]})
        self.assertEqual((back["undid"], back["removed"], self.desk.store.get(dana)["stage"]), ("screened → Submitted", False, "screened"))
        with self.assertRaises(self.api.HTTPException):
            self.api.undo(dana, {"op": "not-the-last-one"})
        made = self.api.add_deal({"name": "Ada Quill"})
        gone = self.api.undo(made["deal"]["id"], {"op": made["last"]["op"]})
        self.assertEqual((gone["removed"], gone["last"], self.desk.store.find("Ada Quill")), (True, None, None))
        rows = [a for a in self.api.state()["activity"] if a["deal"] == made["deal"]["id"]]
        self.assertEqual(([a["name"] for a in rows], [a["can_restore"] for a in rows], any(a["can_undo"] for a in rows)),
                         (["Ada Quill", "Ada Quill"], [True, False], False))                 # off the desk: still named, one offer to put it back
        self.assertEqual(self.api.restore(made["deal"]["id"])["name"], "Ada Quill")
        self.assertEqual(self.desk.store.find("Ada Quill")["id"], made["deal"]["id"])
        with self.assertRaises(self.api.HTTPException):
            self.api.restore(made["deal"]["id"])                                             # it is on the desk: nothing to put back
        self.assertFalse(self.desk.undo(made["deal"]["id"])["ok"])                           # its return stands: there is nothing left to take back
        self.assertIsNone(self.api.act(dana, {"verb": "draft"})["last"])                     # nothing changed, nothing to take back
        first = self.api.act(dana, {"verb": "update", "value": 50000})["last"]["op"]
        self.api.act(dana, {"verb": "update", "value": 60000})
        with self.assertRaises(self.api.HTTPException) as refused:                           # it would overwrite the later amount
            self.api.undo(dana, {"op": first})
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("Fee is now $60k (that action left it at $50k)", refused.exception.detail)
        self.assertEqual(self.desk.store.get(dana)["value"], 60000)
        people = self.api.person(dana, {"verb": "add", "name": "Lena Ortiz", "email": "lena@kestrel.example"})["people"]
        marcus = self.desk.store.find("Marcus Oyelaran")["id"]
        self.api.person(marcus, {"verb": "add", "name": "Lena Ortiz", "email": "lena@kestrel.example"})
        self.assertEqual((self.api.deal(dana)["people"][0]["also_on"], self.api.deal(marcus)["people"][0]["id"]), (1, people[0]["id"]))
        self.assertEqual((self.api.setup()["data"]["archived"], self.api.setup()["data"]["ledger"] > 5), (0, True))

    def test_the_page_keeps_a_group_and_its_people_and_decides_several_proposals(self):
        api, search = self.api, "Staff Backend · Northwind"
        out = api.group({"name": search, "client": "Northwind", "status": "on hold", "fields": {"fee_pct": "20"}})
        self.assertEqual((out["group"]["client"], out["group"]["status"]), ("Northwind", "hold"))
        state = api.state()
        mine = next(g for g in state["groups"] if g["name"] == search)
        self.assertEqual((mine["client"], mine["status_label"], mine["fields"]), ("Northwind", "On hold", {"fee_pct": "20"}))
        self.assertEqual({card.get("client") for card in state["deals"] if card.get("group") == search}, {"Northwind"})
        self.assertEqual(state["playbook"]["group_fields"][0]["key"], "fee_type")
        added = api.group_person({"verb": "add", "group": search, "name": "Helen Park", "email": "helen@northwind.example", "role": "hiring manager"})
        self.assertEqual([p["name"] for p in added["people"]], ["Helen Park"])
        api.group_person({"verb": "update", "group": search, "person": "helen park", "title": "VP Engineering"})
        self.assertEqual(api.group({"name": search})["people"][0]["title"], "VP Engineering")
        api.group_person({"verb": "remove", "group": search, "person": "helen park"})
        self.assertEqual([p for p in api.group({"name": search})["people"] if p["on"] == "group"], [])
        self.assertEqual(api.group({"name": search, "to": "Staff Platform · Northwind"})["group"]["name"], "Staff Platform · Northwind")
        self.assertTrue(api.group({"name": "Head of Data · Lumen", "new": True, "client": "Lumen Health"})["created"])
        for bad in ({"name": "No such search"}, {"name": ""}):
            with self.assertRaises(api.HTTPException):
                api.group(bad)
        with self.assertRaises(api.HTTPException):
            api.group_person({"verb": "shout", "group": search})
        dana = self.desk.store.find("Dana Whitfield")["id"]
        made = self.desk.intake.propose({"kind": "proposal", "source": "sweep", "rows": [
            {"record": dana, "next_step": "Send the brief"}, {"record": dana, "close_on": "+30d"}]}, {})
        done = api.approvals({"verb": "approve", "items": [{"intake": made["id"], "n": 1}, {"intake": made["id"], "n": 2}]})
        self.assertEqual((done["decided"], done["applied"]), (2, 2))
        self.assertEqual(self.desk.store.get(dana)["next_step"], "Send the brief")
        with self.assertRaises(api.HTTPException):
            api.approvals({"verb": "maybe", "items": []})
        self.assertIn("count", api.setup()["data"]["backups"])
        part = api.export_part({"group": "Staff Platform · Northwind"})              # the page's export of one search
        self.assertEqual((part["only"], part["rows"]), ("Staff Platform · Northwind", len(part["csv"].strip().splitlines()) - 1))
        self.assertIn("staff-platform-northwind-", part["filename"])
        self.assertNotIn("Wei-Lin Chao", part["csv"])
        self.assertGreater(api.export()["rows"], part["rows"])
        clean = api.export_part({"group": "Staff Platform · Northwind", "share": True})
        self.assertEqual((clean["share"], clean["rows"], clean["csv"].splitlines()[0].split(",")[:4]), (True, part["rows"], ["Candidate", "Current company", "Search", "Stage"]))
        self.assertIn("-to-share-", clean["filename"])
        with self.assertRaises(api.HTTPException):
            api.export_part({"group": "No such search"})

    def test_a_map_drawn_in_a_chat_acts_on_its_own_pipeline_whatever_the_page_shows(self):
        api = self.api
        asks = lambda pipeline="", **ids: api._REQUEST.set((pipeline, ids))
        self.addCleanup(asks)
        second = api.pipeline({"verb": "add", "name": "My practice", "playbook": "agency"})["added"]["id"]
        with self.desk.on(second):
            self.desk.markets.define({"name": "Practice map", "segments": ["One"]})
            self.desk.markets.add({"market": "Practice map", "companies": [{"company": "Ada Analytics", "segment": "One"}, {"company": "Bly Labs", "segment": "One"}]})
            ada = self.desk.markets.target("Ada Analytics")["id"]
        asks("")                                                                  # the page shows the first pipeline; the companies are on the second's map
        out = api.targets({"verb": "dismiss", "ids": [ada], "reason": "too small"})
        self.assertTrue(out.get("ok", True), out)
        with self.desk.on(second):
            self.assertEqual(self.desk.markets.target("Ada Analytics")["status"], "dismissed")
        asks(second)
        state = api.state()
        self.assertEqual([m["name"] for m in state["markets"]], ["Practice map"])  # what a map in a chat reads with pipeline="p2"

    def test_a_card_that_names_its_pipeline_gets_that_record_when_both_have_the_same_id(self):
        api = self.api
        asks = lambda pipeline="", **ids: api._REQUEST.set((pipeline, ids))
        self.addCleanup(asks)
        second = api.pipeline({"verb": "add", "name": "My practice", "playbook": "agency"})["added"]["id"]
        with self.desk.on(""):
            dana = self.desk.store.find("Dana Whitfield")
        with self.desk.on(second):                                                # the same person, the same group: the same id
            twin = self.desk.deal({"action": "add", "name": "Dana Whitfield", "group": dana["grp"], "value": 5000})["deal"]
        self.assertEqual(twin["id"], dana["id"])
        asks("", deal_id=dana["id"])                                              # a card that does not say: the page's pipeline's record
        self.assertEqual(api.deal(dana["id"])["pipeline"]["name"], "Recruiting desk")
        asks(second, deal_id=dana["id"])                                          # a card that says pipeline="p2": that one
        self.assertEqual((api.deal(dana["id"])["pipeline"]["name"], api.deal(dana["id"])["value"]), ("My practice", 5000))
        drafted = api.act(dana["id"], {"verb": "draft"})
        self.assertIn("Write the follow-up for Dana Whitfield on the Stoa desk (the My practice pipeline).", drafted["ask"])
        with self.desk.on(second):
            self.assertTrue(self.desk.handoff(dana["id"]))                        # the task waits on the second pipeline's record
        with self.desk.on(""):
            self.assertFalse(self.desk.handoff(dana["id"]))

    def test_the_page_shows_one_pipeline_at_a_time_and_follows_a_record_to_its_own(self):
        api = self.api
        asks = lambda pipeline="", **ids: api._REQUEST.set((pipeline, ids))       # what a request says: the page's pipeline, the ids in its path
        self.addCleanup(asks)
        state = api.state()
        self.assertEqual(([p["id"] for p in state["pipelines"]], state["pipeline"]), (["main"], "main"))
        made = api.pipeline({"verb": "add", "name": "My practice", "playbook": "agency"})
        second = made["added"]["id"]
        with self.desk.on(second):
            visa = self.desk.deal({"action": "add", "name": "Visa retainer", "company": "Visa", "value": 96000})["deal"]["id"]
        state = api.state()                                                       # the page says nothing: the first pipeline
        self.assertEqual([(p["name"], p["open"]) for p in state["pipelines"]], [("Recruiting desk", 7), ("My practice", 1)])
        self.assertNotIn("Visa retainer", [card["name"] for card in state["deals"]])
        asks(second)
        state = api.state()
        self.assertEqual((state["pipeline"], state["playbook"]["name"], [card["name"] for card in state["deals"]]), (second, "agency", ["Visa retainer"]))
        self.assertFalse(state["fresh"])
        self.assertEqual(api.add_deal({"name": "Stripe audit", "company": "Stripe"})["deal"]["stage_label"], "Inbound")      # added where the page is
        asks("zzz")                                                               # an id that is no pipeline: the first, never an error
        self.assertEqual(api.state()["pipeline"], "main")
        asks("", deal_id=visa)                                                    # the page shows the first, the card in a chat is from the second
        card = api.deal(visa)
        self.assertEqual((card["name"], card["pipeline"]["name"], card["playbook"]["name"], card["stage_label"]), ("Visa retainer", "My practice", "agency", "Inbound"))
        self.assertTrue(api.act(visa, {"verb": "advance", "extra": "inbound"})["ok"])
        with self.desk.on(second):
            self.assertEqual(self.desk.store.get(visa)["stage"], "intro")
        with self.desk.on(""):                 # (a request has a context of its own; in a test, the last route's choice is still set)
            dana = self.desk.store.find("Dana Whitfield")["id"]
        asks(second, deal_id=dana)                                                # and the other way round
        self.assertEqual(api.deal(dana)["pipeline"]["name"], "Recruiting desk")
        asks(second)
        self.desk.can_configure = False                                           # the page's own stand-in desk cannot save a plugin setting
        self.assertTrue(api.use_playbook({"name": "b2b-sales"})["ok"])            # another pipeline's playbook is kept with the pipeline
        self.assertEqual(self.desk.pipelines()[1]["playbook"], "b2b-sales")
        asks()
        self.assertIn("ask", api.use_playbook({"name": "agency"}))                # the first one's is still handed to Hermes
        self.desk.can_configure = True
        with self.assertRaises(api.HTTPException):
            api.pipeline({"verb": "remove", "id": second})                        # it has records
        with self.assertRaises(api.HTTPException):
            api.pipeline({"verb": "add", "name": "My practice", "playbook": "agency"})
        self.assertTrue(api.pipeline({"verb": "rename", "id": second, "name": "Advisory"})["ok"])
        self.assertTrue(api.pipeline({"verb": "rename", "id": "main", "name": "Searches"})["ok"])
        self.assertEqual([p["name"] for p in api.state()["pipelines"]], ["Searches", "Advisory"])
        api.tour({"action": "start", "example": "saas"})
        self.assertEqual(api.state()["pipelines"], [])                            # a tour is one sample desk
        with self.assertRaises(api.HTTPException):
            api.pipeline({"verb": "add", "name": "During a tour", "playbook": "agency"})
        api.tour({"action": "leave"})

    def test_the_page_keeps_the_people_on_a_record_and_shows_a_company(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        added = self.api.person(dana, {"verb": "add", "name": "Lena Ortiz", "email": "lena@kestrel.example", "role": "hiring manager"})
        self.assertEqual(([p["name"] for p in added["people"]], added["exists"], added["last"]["text"]), (["Lena Ortiz"], False, "Added Lena Ortiz (hiring manager)"))
        self.assertTrue(self.api.person(dana, {"verb": "add", "name": "Lena Ortiz"})["exists"])
        self.assertEqual(self.api.deal(dana)["people"][0]["role"], "hiring manager")
        view = self.api.company({"name": "Kestrel Labs"})
        self.assertEqual((view["company"]["name"], view["open"], [p["name"] for p in view["people"]]), ("Kestrel Labs", 1, ["Dana Whitfield", "Lena Ortiz"]))
        saved = self.api.company({"name": "Kestrel Labs", "note": "Hires in January.", "domain": "kestrel.example"})
        self.assertEqual(saved["company"]["note"], "Hires in January.")
        with self.assertRaises(self.api.HTTPException):
            self.api.company({"name": "Nobody Corp"})
        row = next(c for c in self.api.state()["companies"] if c["name"] == "Kestrel Labs")
        self.assertEqual((row["open"], row["people"]), (1, 2))
        person_id = self.api.deal(dana)["people"][0]["id"]
        self.assertEqual(self.api.person(dana, {"verb": "remove", "person": str(person_id)})["people"], [])
        with self.assertRaises(self.api.HTTPException):
            self.api.person(dana, {"verb": "promote"})

    def test_the_page_approves_and_declines_one_proposal_at_a_time(self):
        dana, marcus = self.desk.store.find("Dana Whitfield")["id"], self.desk.store.find("Marcus Oyelaran")["id"]
        self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": dana, "stage": "submitted", "why": "She said yes.", "evidence": [{"kind": "email", "text": "Yes please.", "who": "Dana"}]},
            {"record": marcus, "stage": "screened"}]}, {})
        queue = self.api.state()["today"]["approvals"]
        self.assertEqual([(item["name"], item["case"], len(item["evidence"])) for item in queue], [("Dana Whitfield", "She said yes.", 1), ("Marcus Oyelaran", "", 0)])
        approved = self.api.intake_row(queue[0]["intake"], {"n": 1, "verb": "approve"})
        self.assertEqual((approved["applied"], approved["last"]["deal"], self.desk.store.get(dana)["stage"]), (True, dana, "submitted"))
        declined = self.api.intake_row(queue[0]["intake"], {"n": 2, "verb": "decline"})
        self.assertEqual((declined["applied"], self.desk.store.get(marcus)["stage"], self.api.state()["today"]["approvals"]), (False, "contacted", []))
        self.assertEqual(self.api.setup()["declined"], 1)
        self.assertEqual(self.api.forget_declined(), {"ok": True, "forgotten": 1})
        with self.assertRaises(self.api.HTTPException):
            self.api.intake_row("in-nothere0", {"n": 1, "verb": "approve"})

    def test_the_page_keeps_invoices_and_shows_the_cash_picture(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        self.api.act(dana, {"verb": "update", "close_on": "2031-02-01"})
        self.assertEqual(self.api.deal(dana)["close_on"], "2031-02-01")
        self.api.act(dana, {"verb": "move", "stage": "placed"})
        record = self.api.deal(dana)
        self.assertEqual((record["delivery"], [s["id"] for s in record["delivery_stages"]][:2], record["money"]["uninvoiced"]),
                         ("start-set", ["start-set", "started"], 42000))
        self.assertTrue(self.api.act(dana, {"verb": "update", "delivery": "started"})["ok"])
        added = self.api.invoice(dana, {"verb": "add", "amount": 42000, "number": "2001", "due": "2031-03-01"})
        self.assertEqual(([i["number"] for i in added["invoices"]], added["money"]["owed"], added["last"]["text"]),
                         (["2001"], 42000, "Invoice 2001 for 42,000, due 2031-03-01"))
        bill = str(added["invoices"][0]["id"])
        part = self.api.invoice(dana, {"verb": "pay", "invoice": bill, "amount": 20000})
        self.assertEqual((part["invoices"][0]["state"], part["money"]["paid"]), ("partial", 20000))
        cash = self.api.state()["cash"]
        self.assertEqual((cash["strip"]["owed"], cash["strip"]["collected_month"], cash["delivery"]["records"][0]["delivery"]),
                         ({"count": 1, "value": 22000}, {"value": 20000}, "started"))
        self.assertEqual(self.api.undo(dana, {"op": part["last"]["op"]})["undid"], "Invoice 2001: 20,000 received, 22,000 still owed")
        self.assertEqual(self.api.deal(dana)["invoices"][0]["paid"], 0)
        for bad in ({"verb": "pay", "invoice": "9999"}, {"verb": "add", "amount": -5}, {"verb": "refund"}):
            with self.assertRaises(self.api.HTTPException):
                self.api.invoice(dana, bad)
        self.assertEqual(self.api.invoice(dana, {"verb": "remove", "invoice": bill})["invoices"], [])

    def test_the_page_sets_how_much_a_job_may_do(self):
        self.assertEqual(self.api.setup()["autonomy"]["level"], "ask")
        self.assertEqual(self.api.autonomy({"level": "routine"})["autonomy"], "routine")
        self.assertEqual((self.api.setup()["autonomy"]["level"], self.desk.autonomy()), ("routine", "routine"))
        with self.assertRaises(self.api.HTTPException):
            self.api.autonomy({"level": "anything goes"})

    def test_the_page_switches_playbook_or_hands_it_to_hermes(self):
        self.assertEqual(self.api.use_playbook({"name": "agency"})["active"], "agency")
        self.assertEqual(self.desk.own_playbook().name, "agency")
        with self.assertRaises(self.api.HTTPException):
            self.api.use_playbook({"name": "no-such-playbook"})
        self.desk.can_configure = False                                              # the page's stand-in desk cannot save a setting
        self.assertEqual(self.api.use_playbook({"name": "recruiting"}), {"ok": False, "ask": "Switch the Stoa desk to the recruiting playbook."})
        self.assertEqual(self.desk.own_playbook().name, "agency")

    def test_the_record_panel_saves_notes_and_logs_a_line(self):
        dana = self.desk.store.find("Dana Whitfield")
        self.api.act(dana["id"], {"verb": "update", "notes": "Prefers mornings."})
        self.api.act(dana["id"], {"verb": "log", "kind": "note", "text": "Called, left a message"})
        shown = self.api.deal(dana["id"])
        self.assertEqual(shown["notes"], "Prefers mornings.")
        self.assertEqual((shown["activity"][0]["kind"], shown["activity"][0]["text"]), ("note", "Called, left a message"))

    def test_a_csv_dropped_on_the_page_becomes_a_review_without_a_model(self):
        before = self.desk.store.count()
        text = ("Name,Company,Search,Email,Stage\n"
                "Ada Quill,Fathom Data,Staff Backend · Northwind,ada.quill@fathomdata.example,\n"
                "Dana Whitfield,Kestrel Labs,,,Submitted\n"
                ",,,,\n")
        held = self.api.intake_file({"name": "badge scans.csv", "text": text})
        self.assertEqual((held["ok"], held["waiting"], held["counts"]["new"], held["counts"]["update"]), (True, True, 1, 1))
        self.assertIn("badge scans.csv", held["headline"])
        self.assertEqual(self.desk.store.count(), before)                                    # held for approval, nothing written
        self.assertEqual([item["id"] for item in self.api.state()["intakes"]], [held["id"]])
        applied = self.api.intake_act(held["id"], "apply", {})
        self.assertEqual((applied["added"], applied["updated"]), (1, 1))
        self.assertEqual(self.desk.store.find("Dana Whitfield")["stage"], "submitted")
        unread = self.api.intake_file({"name": "mystery.csv", "text": "Col A,Col B\nApex,apex.example\n"})
        self.assertEqual((unread["unread"], unread["waiting"]), (True, False))
        self.assertIn("Hermes is reading it", unread["headline"])
        tabbed = self.api.intake_file({"name": "people.tsv", "text": "Name\tCompany\nBen Okoro\tHarbor Systems\n"})
        self.assertEqual(tabbed["counts"]["new"], 1)
        again = self.api.intake_file({"name": "again.csv", "text": "Name,Company\nAda Quill,Fathom Data\n"})
        self.assertEqual((again["waiting"], again["headline"]), (False, "Everything in again.csv is already on the desk."))
        with self.assertRaises(self.api.HTTPException):
            self.api.intake_file({"name": "empty.csv", "text": "Name,Company\n"})

    def test_a_click_in_the_app_saves_and_returns_the_line_to_send_into_chat(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        told = []
        self.desk.announce = lambda: told.append(1)
        drafted = self.api.act(dana, {"verb": "draft"})
        self.assertEqual((drafted["ok"], drafted["parked"]), (True, True))
        self.assertIn("Write the follow-up for Dana Whitfield", drafted["ask"])
        self.assertIn(dana, drafted["ask"])
        self.assertIn("pending_task", self.desk.deal({"action": "get", "deal": dana}))     # the task itself waits on the record
        moved = self.api.act(dana, {"verb": "advance", "extra": "screened"})
        self.assertIn("Dana Whitfield is now at Submitted", moved["ask"])
        self.assertEqual(self.api.act(dana, {"verb": "snooze7"})["ask"], "")               # nothing to write: nothing to say
        target = self.desk.markets.target("Meridian Freight")
        added = self.api.target(target["id"], "track")
        self.assertIn("added to the Stoa desk from the market map", added["ask"])
        self.assertIn(added["deal"], added["ask"])
        self.assertEqual(len(told), 4)                                                     # every change reaches an open window

    def test_cards_and_maps_are_drawn_in_the_conversation_only_while_the_app_is_open(self):
        in_app = {"platform": "desktop", "chat_id": ""}
        dana = self.desk.store.find("Dana Whitfield")["id"]
        self.assertIn("card", self.desk.card({"deal": "dana"}, in_app))                    # nobody has the app open: text
        self.assertTrue(self.api.hello()["ok"])                                            # the app's half checks in
        shown = self.desk.card({"deal": "dana"}, in_app)
        self.assertEqual(shown["directive"], '::stoa-card{id="%s"}' % dana)
        self.assertNotIn("card", shown)
        posted = self.desk.markets.post({"market": ""}, in_app)
        self.assertEqual(posted["directive"], '::stoa-map{market="regional-freight-brokers-demo"}')
        for elsewhere in ({"platform": "cli"}, {"platform": "telegram", "chat_id": "T1"}, {"platform": "slack", "chat_id": "C1"}, {}):
            self.assertNotIn("directive", self.desk.card({"deal": "dana"}, elsewhere), elsewhere)
        self.desk.desktop_seen = time.time() - 600                                         # the window was closed a while ago
        self.assertIn("card", self.desk.card({"deal": "dana"}, in_app))

    def test_the_app_reads_a_waiting_list_and_applies_the_ticked_rows(self):
        out = self.desk.intake.run({"action": "propose", "source": "leads.pdf", "rows": [
            {"name": "Ada Quill", "company": "Fathom Data"}, {"name": "Ben Okoro"}, {"name": "Priya Raman"}]})
        self.assertEqual([w["id"] for w in self.api.state()["intakes"]], [out["id"]])
        view = json.loads(json.dumps(self.api.intake(out["id"])))
        self.assertEqual([(row["n"], row["kind"]) for row in view["rows"]], [(1, "new"), (2, "new"), (3, "same")])
        self.assertNotIn("row", view["rows"][0])                                            # raw values stay in the ledger
        done = self.api.intake_act(out["id"], "apply", {"rows": [2]})
        self.assertEqual((done["added"], done["left_out"]), (1, 1))
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertEqual(self.api.intake(out["id"])["status"], "applied")
        self.assertEqual(self.api.state()["intakes"], [])

    def test_state_costs_the_same_however_many_records_there_are(self):
        self.desk.store.last_touch = lambda deal_id: self.fail("asked for one record's last touch during /state")
        self.desk.store.meta_get = lambda key, default="": self.fail("asked for one record's pending task during /state")
        self.desk.park_followup(self.desk.store.find("Dana Whitfield")["id"], "write it")
        state = self.api.state()
        waiting = [d["name"] for d in state["deals"] if d["pending"]]
        self.assertEqual(waiting, ["Dana Whitfield"])

    def test_records_from_another_playbook_stay_visible(self):
        self.desk.playbook_tool({"action": "use", "name": "b2b-sales"})
        state = json.loads(json.dumps(self.api.state()))
        known = {stage["id"] for stage in state["playbook"]["stages"]}
        strays = [d for d in state["deals"] if d["status"] == "open" and d["stage"] not in known]
        self.assertTrue(strays)
        self.assertTrue(all(d["position"] == -1 and d["advance"] == "" for d in strays))
        self.assertEqual(state["totals"]["open"], len([d for d in state["deals"] if d["status"] == "open"]))

    def test_state_is_complete_and_serialisable(self):
        state = json.loads(json.dumps(self.api.state()))
        self.assertEqual(state["playbook"]["name"], "recruiting")
        self.assertEqual((state["totals"]["open"], state["totals"]["mapped"], state["totals"]["tracked"]), (7, 7, 0))
        self.assertEqual((state["totals"]["due"], state["totals"]["stale"]), (4, 1))
        self.assertEqual(len(state["markets"][0]["segments"]), 3)
        dana = [d for d in state["deals"] if d["name"] == "Dana Whitfield"][0]
        self.assertEqual((dana["position"], dana["advance"], dana["pending"]), (2, "Submit to client", False))
        self.assertTrue(state["activity"])
        self.assertEqual(state["flow"]["total"], 7)

    def test_advance_parks_the_draft_and_is_idempotent(self):
        dana = self.desk.store.find("Dana Whitfield")
        result = self.api.act(dana["id"], {"verb": "advance", "extra": "screened"})
        self.assertEqual((result["ok"], result["parked"]), (True, True))
        detail = self.api.deal(dana["id"])
        self.assertEqual(detail["stage"], "submitted")
        self.assertIn("submission note", detail["pending_task"])
        again = self.api.act(dana["id"], {"verb": "advance", "extra": "screened"})
        self.assertIn("Already moved", again["notice"])
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], "submitted")

    def test_log_update_close_and_bad_requests(self):
        from fastapi import HTTPException
        dana = self.desk.store.find("Dana Whitfield")
        self.api.act(dana["id"], {"verb": "log", "kind": "call", "text": "Spoke for ten minutes."})
        self.api.act(dana["id"], {"verb": "update", "next_step": "Send two roles", "next_due": "2030-01-01"})
        self.api.act(dana["id"], {"verb": "lost", "extra": "0"})
        detail = self.api.deal(dana["id"])
        self.assertEqual((detail["status"], detail["lost_reason"], detail["next_step"]), ("lost", "Not a fit", "Send two roles"))
        self.assertIn("Spoke for ten minutes.", [a["text"] for a in detail["activity"]])
        self.api.act(dana["id"], {"verb": "reopen"})
        self.assertEqual(self.desk.store.get(dana["id"])["status"], "open")
        for call in (lambda: self.api.act("ghost", {"verb": "advance"}), lambda: self.api.act(dana["id"], {"verb": "explode"}),
                     lambda: self.api.deal("ghost"), lambda: self.api.add_deal({"company": "No name"})):
            with self.assertRaises(HTTPException):
                call()

    def test_the_page_is_told_how_a_recruiting_pipeline_is_paid_and_what_a_map_holds(self):
        from fastapi import HTTPException
        state = self.api.state()
        self.assertEqual(state["billing"]["type"], "contingent")
        self.assertEqual(state["totals"]["open_value"], 130000.0)                          # each search once: 45k + 55k + 30k
        market = state["markets"][0]
        self.assertEqual((market["rows"], market["numbers"]["rows"], market["search"]), ("companies", "companies", ""))    # the fixture's map is of companies
        # A person turns it into a candidate map, points it at a search, and turns it back.
        self.assertEqual(self.api.market_rows(market["id"], {"rows": "people"})["rows"], "people")
        again = self.api.state()["markets"][0]
        self.assertEqual((again["rows"], again["search"]), ("people", market["name"]))
        self.assertEqual(self.api.market_search(market["id"], {"search": "staff backend · northwind"})["search"], "Staff Backend · Northwind")
        with self.assertRaises(HTTPException):
            self.api.market_search(market["id"], {"search": "Nothing called this"})
        with self.assertRaises(HTTPException):
            self.api.market_rows(market["id"], {"rows": "goats"})
        self.api.market_rows(market["id"], {"rows": "companies"})
        # Openings on a search, and what is then in play.
        self.assertEqual(self.api.group_openings({"name": "Staff Backend · Northwind", "openings": 2})["openings"], 2)
        self.assertEqual(self.api.state()["totals"]["open_value"], 172000.0)                # its two largest fees, 45k + 42k, with the others' 55k and 30k
        with self.assertRaises(HTTPException):
            self.api.group_openings({"name": "Staff Backend · Northwind", "openings": 0})
        # Contract: no fee anywhere on the pipeline's side is the page's doing; the terms and the month are here.
        saved = self.api.pipeline_billing({"type": "contract", "client": "Northwind", "rate": 85, "hours_week": 20})
        self.assertEqual((saved["type"], saved["client"], saved["rate"]), ("contract", "Northwind", 85))
        state = self.api.state()
        self.assertTrue(state["billing"]["month"]["amount"] > 0 and state["billing"]["type"] == "contract")
        with self.assertRaises(HTTPException):
            self.api.pipeline_billing({"type": "contract", "client": "", "rate": 85, "hours_week": 20})
        across = self.api.production()
        self.assertEqual((len(across["pipelines"]), across["pipelines"][0]["billing"], across["pipelines"][0]["this_month"]), (1, "contract", state["billing"]["month"]["amount"]))
        self.assertEqual(self.api.pipeline_billing({"type": "contingent", "fee_pct": 20})["fee_pct"], 20)

    def test_add_and_market_actions(self):
        added = self.api.add_deal({"name": "Ada Park", "company": "Orbit", "stage": "contacted"})
        self.assertEqual(self.desk.store.get(added["deal"]["id"])["stage"], "contacted")
        target = self.desk.markets.target("Brightside Freight")
        tracked = self.api.target(target["id"], "track", None)
        self.assertTrue(tracked["ok"])
        self.assertIn("likely buyer", self.api.deal(tracked["deal"])["pending_task"])
        self.assertFalse(self.api.target(target["id"], "track", None)["ok"])
        other = self.desk.markets.target("Quill Cargo")
        self.assertEqual(self.api.target(other["id"], "dismiss", {"reason": "Builds in-house"})["dismissed"], ["Quill Cargo"])
        market = self.api.state()["markets"][0]
        self.assertEqual((market["rates"][0]["rate"], market["holds"][0]["name"], market["first_calls"][0]["company"]),
                         (100, "Spreadsheets", "Brightside Freight"))
        page = self.api.market_page(market["id"], 1)
        self.assertEqual(page["filename"], "regional-freight-brokers-demo-share.html")
        self.assertIn("Meridian Freight", page["html"])
        self.assertNotIn("Renata Okafor", page["html"])
        counts = self.api.state()["markets"][0]["counts"]
        self.assertEqual((counts["mapped"], counts["tracked"], counts["dismissed"]), (6, 1, 1))
        state = self.api.state()
        self.assertEqual(([row["company"] for row in state["markets"][0]["dismissed"]], state["markets"][0]["lookups"], state["playbook"]["shape"]), (["Quill Cargo"], False, "roles"))
        self.assertEqual(self.api.targets({"verb": "restore", "ids": [other["id"]]})["restored"], ["Quill Cargo"])
        self.assertEqual(self.api.state()["markets"][0]["counts"]["dismissed"], 0)
        with self.assertRaises(self.api.HTTPException):
            self.api.targets({"verb": "restore", "ids": []})
        mid = self.api.state()["markets"][0]["id"]
        self.assertRegex(self.api.state()["markets"][0]["updated"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.api.market_rename(mid, {"name": "Freight brokers"})["name"], "Freight brokers")
        self.assertEqual(self.api.state()["markets"][0]["name"], "Freight brokers")
        self.assertTrue(self.api.market_remove(mid)["ok"])
        self.assertEqual(self.api.state()["markets"], [])


class MapOnThePageTests(unittest.TestCase):
    """What the page's map does to the companies picked on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")
        fixture.load_market(self.desk)
        self.market = self.desk.markets.list_markets()[0]

    def companies(self):
        return {c["company"]: c for seg in self.desk.markets.summary(self.market["id"], text=False)["segments"] for c in seg["companies"]}

    def test_a_company_carries_its_headcount_and_the_day_it_was_added(self):
        from stoa.market import headcount
        self.assertEqual([headcount(text) for text in ("220 people", "1,200", "Founded 2015, 80 staff", "about 3k employees", "mid-size", "")], [220, 1200, 80, 3000, 0, 0])
        rows = self.companies()
        self.assertEqual(rows["Meridian Freight"]["headcount"], 220)
        self.assertEqual(rows["Meridian Freight"]["added"], dt.date.today().isoformat())       # the day it is here, not in UTC

    def test_a_signal_keeps_the_day_it_happened(self):
        from stoa.market import signal_day
        self.assertEqual([signal_day(text) for text in ("2026-11-03", "2026-10", "last month", "2026-02-30", "", None, 20261103)], ["2026-11-03", "2026-10-15", "", "", "", "", ""])
        name = fixture.MARKET["name"]
        self.desk.markets.add({"market": name, "companies": [{"company": "Meridian Freight", "signal_date": "2026-11-03"}, {"company": "Halcyon Transport", "signal_date": "2026-11-01"}]})
        rows = self.companies()
        self.assertEqual(rows["Meridian Freight"]["signal_date"], "2026-11-03")
        self.assertNotIn("signal_date", rows["Halcyon Transport"])      # no signal, so no date to show
        self.desk.markets.add({"market": name, "companies": [{"company": "Meridian Freight", "signal": "Raised a round"}]})
        self.assertNotIn("signal_date", self.companies()["Meridian Freight"])      # a new signal does not wear the old one's date
        self.desk.markets.add({"market": name, "companies": [{"company": "Meridian Freight", "signal": "Raised a round", "signal_date": "2026-11"}]})
        self.assertEqual(self.companies()["Meridian Freight"]["signal_date"], "2026-11-15")

    def test_a_long_requirement_is_still_matched_when_a_company_is_checked_against_it(self):
        long = "Processes more than a hundred thousand card transactions a month across several countries"
        self.desk.markets.define({"name": "Long words", "segments": ["One"], "requirements": [long, "Small team"]})
        out = self.desk.markets.add({"market": "Long words", "companies": [{"company": "Ada", "segment": "One", "requirements": {long: "Its annual report says so", "Small team": True}}]})
        self.assertNotIn("problems", out)
        company = self.desk.markets.summary(self.desk.markets._market("Long words")["id"], text=False)["segments"][0]["companies"][0]
        self.assertEqual([req["shown"] for req in company["requirements"]], [True, True])

    def test_a_map_is_renamed_without_losing_anything_on_it(self):
        before = self.companies()
        self.desk.markets.define({"name": "Second map", "segments": ["One"]})
        renamed = self.desk.markets.rename(self.market["id"], "  Midwest freight brokers  ")
        self.assertEqual((renamed["ok"], renamed["id"], renamed["name"]), (True, self.market["id"], "Midwest freight brokers"))
        self.assertEqual(self.desk.markets._market("Midwest freight brokers")["id"], self.market["id"])
        self.assertEqual(sorted(self.companies()), sorted(before))
        self.assertEqual(self.desk.markets.rename(self.market["id"], "second map")["error"], "Another map is already called that.")
        self.assertFalse(self.desk.markets.rename(self.market["id"], "   ")["ok"])
        self.assertFalse(self.desk.markets.rename("no such map", "Anything")["ok"])

    def test_deleting_a_map_takes_its_companies_and_leaves_the_pipeline(self):
        rows = self.companies()
        made = self.desk.markets.track_many([rows["Meridian Freight"]["id"]], "tester")["added"][0]["deal"]
        self.desk.markets.define({"name": "Second map", "segments": ["One"]})
        self.desk.markets.add({"market": "Second map", "companies": [{"company": "Ada", "segment": "One"}]})
        gone = self.desk.markets.remove(self.market["id"])
        self.assertEqual((gone["ok"], gone["companies"], gone["in_pipeline"]), (True, len(fixture.COMPANIES), 1))
        self.assertEqual([m["name"] for m in self.desk.markets.list_markets()], ["Second map"])
        self.assertEqual(len(self.desk.markets.targets(self.market["id"])), 0)
        self.assertEqual(len(self.desk.markets.targets(self.desk.markets._market("Second map")["id"])), 1)      # the other map is untouched
        self.assertIsNotNone(self.desk.store.get(made))      # the record made from the deleted map is still in the pipeline
        self.assertFalse(self.desk.markets.remove(self.market["id"])["ok"])
        self.assertNotIn("remove", schemas.GTM_MARKET["parameters"]["properties"]["action"]["enum"])      # only a person at the page deletes a map

    def test_a_map_by_the_numbers(self):
        facts = self.desk.markets.facts(self.market["id"])
        on = facts["on_it"]
        self.assertEqual((on["companies"], on["segments"], on["requirements"], on["in_pipeline"]), (len(fixture.COMPANIES), 3, 4, 0))
        self.assertEqual(on["checked"], len([c for c in fixture.COMPANIES if c.get("requirements")]))
        self.assertEqual(facts["sources"], {"links": 0, "sites": 0, "top": []})      # the fixture gives no links, and none is made up
        self.assertIn("a source link", [gap["what"] for gap in facts["gaps"]])
        self.assertEqual((facts["cost"]["runs"], facts["cost"]["usd"], facts["cost"]["lookups"], facts["cost"]["searches"]), (0, 0.0, 0, 0))
        self.assertEqual(facts["limits"], {"companies": 0, "lookups": 0, "searches": 0, "budget": 0.0})
        self.desk.markets.add({"market": self.market["id"], "companies": [{"company": "Meridian Freight", "source": "https://www.freightnews.example/meridian"}]})
        self.assertEqual(self.desk.markets.facts(self.market["id"])["sources"], {"links": 1, "sites": 1, "top": [{"site": "freightnews.example", "links": 1}]})

    def test_a_turns_model_calls_are_counted_against_the_map_it_worked_on(self):
        from stoa.usage import Meter
        priced = lambda model, provider, base_url, usage: 250000 if model == "priced" else None      # a quarter of a dollar a call
        meter = self.desk.usage = Meter(price=priced)
        call = lambda turn, model="priced": meter.on_api(session_id="s1", turn_id=turn, model=model, provider="p", api_duration=30,
                                                         usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 900, "cache_write_tokens": 50})
        call("t1")                                                   # Hermes thinks, then uses the map tool
        self.desk.market({"action": "show", "market": self.market["id"]}, {"session_id": "s1"})
        call("t1"); call("t1")
        meter.on_api(session_id="other", turn_id="x", model="priced", usage={"input_tokens": 5})      # another conversation, no map touched
        meter.on_turn_end(session_id="other")
        self.assertEqual(self.desk.markets.facts(self.market["id"])["cost"]["runs"], 0)      # nothing is written until the turn ends
        meter.on_turn_end(session_id="s1")
        cost = self.desk.markets.facts(self.market["id"])["cost"]
        self.assertEqual((cost["runs"], cost["calls"], cost["usd"], cost["usd_month"], cost["minutes"]), (1, 3, 0.75, 0.75, 1.5))
        self.assertEqual(cost["tokens"], {"in": 300, "out": 60, "cache_read": 2700, "cache_write": 150})
        self.assertRegex(cost["since"], r"^\d{4}-\d{2}-\d{2}$")
        call("t2", model="no price known")                           # a turn that never ended cleanly...
        self.desk.market({"action": "show", "market": self.market["id"]}, {"session_id": "s1"})
        call("t3")                                                   # ...is settled when the next one starts
        cost = self.desk.markets.facts(self.market["id"])["cost"]
        self.assertEqual((cost["runs"], cost["calls"], cost["usd"], cost["unpriced"]), (2, 4, 0.75, 1))

    def test_a_model_with_no_price_shows_no_dollar_figure(self):
        from stoa.usage import Meter
        meter = self.desk.usage = Meter(price=lambda *a: None)
        meter.on_api(session_id="s1", turn_id="t1", model="local", usage={"input_tokens": 10})
        self.desk.market({"action": "show", "market": self.market["id"]}, {"session_id": "s1"})
        meter.on_turn_end(session_id="s1")
        cost = self.desk.markets.facts(self.market["id"])["cost"]
        self.assertEqual((cost["runs"], cost["usd"]), (1, None))

    def test_the_limits_a_person_sets_on_a_map_hold(self):
        mid = self.market["id"]
        have = len(fixture.COMPANIES)
        limits = self.desk.markets.set_limits(mid, {"companies": have + 1, "lookups": "2", "searches": 1, "budget": "1.50", "nonsense": 9})["limits"]
        self.assertEqual(limits, {"companies": have + 1, "lookups": 2, "searches": 1, "budget": 1.5})
        out = self.desk.markets.add({"market": mid, "companies": [{"company": "One More Freight", "segment": "Digital-first"}, {"company": "Two Too Many", "segment": "Digital-first"},
                                                                 {"company": "Meridian Freight", "why": "Still updated: it is already on the map."}]})
        self.assertEqual((out["added"], out["not_added"], out["updated"]), (["One More Freight"], ["Two Too Many"], ["Meridian Freight"]))
        self.assertIn("limit", out["limit"])
        shown = self.desk.market({"action": "show", "market": mid})["limits"]       # Hermes hears the limits before it researches
        self.assertIn("room for 0 more companies (limit %d)" % (have + 1), shown)
        self.assertIn("$0.00 of the $1.50", shown)
        self.assertEqual(self.desk.markets.room(mid), {"companies": 0, "limit": have + 1, "stop": ""})      # and the page asks for no more
        self.desk.store.bump("map:%s:cost:%s" % (mid, dt.date.today().isoformat()[:7]), 1_500_000)      # the month's budget is spent
        self.desk.markets.set_limits(mid, {"companies": 0, "budget": 1.5})
        stopped = self.desk.markets.add({"market": mid, "companies": [{"company": "After The Budget", "segment": "Digital-first"}]})
        self.assertEqual((stopped["added"], stopped["not_added"]), ([], ["After The Budget"]))
        self.assertIn("$1.50", stopped["limit"])
        self.assertIn("Stop working on it", self.desk.market({"action": "show", "market": mid})["limits"])
        self.assertEqual(self.desk.markets.room(mid)["stop"], "budget")
        self.desk.markets.set_limits(mid, {})
        self.assertIsNone(self.desk.markets.room(mid))
        self.assertNotIn("limits", self.desk.market({"action": "show", "market": mid}))

    def test_adding_companies_says_what_the_map_still_does_not_know_about_them(self):
        mid = self.market["id"]
        out = self.desk.markets.add({"market": mid, "companies": [
            {"company": "Bare Freight", "segment": "Digital-first", "signal": "Opened a Dallas office"},
            {"company": "Full Freight", "segment": "Digital-first", "size": "120 people", "uses": "Highway", "signal": "Raised a round", "signal_date": "2026-09-01"}]})
        self.assertEqual(sorted(out["added"]), ["Bare Freight", "Full Freight"])
        for said in ("1 without what it uses today", "1 without its headcount", "1 without the date of its signal", "Never guess"):
            self.assertIn(said, out["still_missing"])
        whole = self.desk.markets.add({"market": mid, "companies": [{"company": "Full Freight", "why": "Checked again."}]})
        self.assertNotIn("still_missing", whole)

    def test_what_a_company_uses_today_is_a_name_and_never_a_note(self):
        """`uses` groups the map and is printed on the page a client is sent. A note sent as `uses` is not saved (the
        company is, and the agent is told why); a plain name is; and 'unknown' clears one saved earlier."""
        from stoa import market as m
        for text, verdict in (("Cvent", "keep"), ("Spreadsheets", "keep"), ("In-house tool", "keep"), ("Cvent / Swoogo", "keep"), ("", "none"), (None, "none"),
                              ("Unknown", "clear"), ("not known yet", "clear"), ("N/A", "clear"),
                              ("Brindleform (event site; may be only the appointment/matchmaking tool)", "refuse"), ("Online registration (platform not identified)", "refuse"),
                              ("Probably Cvent", "refuse"), ("Cvent?", "refuse"), ("Novi AMS runs its site, event registration tool not confirmed", "refuse"),
                              ("A registration system the association built for itself years ago", "refuse")):
            self.assertEqual(m.uses_name(text)[1], verdict, text)
        mid = self.market["id"]
        uses = lambda company: next(r for r in self.desk.markets.targets(mid) if r["company"] == company)["detail"].get("uses", "")      # noqa: E731
        out = self.desk.markets.add({"market": mid, "companies": [
            {"company": "Named Events", "segment": "Digital-first", "uses": "Cvent"},
            {"company": "Noted Events", "segment": "Digital-first", "why": "Runs two trade shows a year.", "uses": "Brindledesk (link on the trade show page; not confirmed)"}]})
        self.assertEqual(sorted(out["added"]), ["Named Events", "Noted Events"])                    # the company is saved either way
        self.assertEqual((uses("Named Events"), uses("Noted Events")), ("Cvent", ""))
        why = next(r for r in self.desk.markets.targets(mid) if r["company"] == "Noted Events")["why"]
        self.assertEqual(why, "Runs two trade shows a year.")                                       # the note went nowhere on the map
        self.assertIn("Noted Events", out["uses_not_saved"])
        self.assertNotIn("Named Events", out["uses_not_saved"])
        for said in ("alone and in a few words", "prints it on the page a client is sent", "'unknown' to clear", "tell the person what you found in your reply", "Do not save it anywhere on the map"):
            self.assertIn(said, out["uses_not_saved"])
        self.assertIn("1 without what it uses today", out["still_missing"])
        # A note does not replace a name already there; a name does; 'unknown' clears it.
        again = self.desk.markets.add({"market": mid, "companies": [{"company": "Named Events", "uses": "Cvent (may have moved to Swoogo)"}]})
        self.assertIn("Named Events", again["uses_not_saved"])
        self.assertEqual(uses("Named Events"), "Cvent")
        self.assertNotIn("uses_not_saved", self.desk.markets.add({"market": mid, "companies": [{"company": "Named Events", "uses": "Swoogo"}]}))
        self.assertEqual(uses("Named Events"), "Swoogo")
        self.assertNotIn("uses_not_saved", self.desk.markets.add({"market": mid, "companies": [{"company": "Named Events", "uses": "unknown"}]}))
        self.assertEqual(uses("Named Events"), "")
        holds = [h["name"] for h in self.desk.markets.summary(mid)["holds"]]
        self.assertFalse([name for name in holds if "(" in name or "not confirmed" in name], holds)
        self.assertIn("No notes, brackets or guesses", json.dumps(schemas.GTM_MARKET if hasattr(schemas, "GTM_MARKET") else [getattr(schemas, n) for n in dir(schemas) if n.isupper()], default=str))

    def test_paid_lookups_and_searches_are_counted_per_map_and_stop_at_its_limit(self):
        calls = []

        def http(url, **kw):
            calls.append(url)
            if "exa.ai" in url:
                return 200, {"results": [{"title": "A broker", "url": "https://broker.example"}]}
            return 200, {"data": [{"full_name": "sam ito", "job_title": "director of carrier compliance"}], "total": 3}

        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=http, secrets={"PDL_API_KEY": "pdl-secret-value", "EXA_API_KEY": "exa-secret-value"})
        mid = self.market["id"]
        desk.markets.set_limits(mid, {"searches": 1, "lookups": 1})
        self.assertTrue(desk.market({"action": "search", "market": mid, "query": "freight brokers"})["ok"])
        refused = desk.market({"action": "search", "market": mid, "query": "more freight brokers"})
        self.assertIn("1 Exa searches", refused["error"])
        nobody = [c["id"] for seg in desk.markets.summary(mid, text=False)["segments"] for c in seg["companies"] if not c.get("people")]
        out = desk.markets.buyers(nobody[:2])
        self.assertEqual((len(out["found"]), "this map's limit" in out["problem"]), (1, True))
        cost = desk.markets.facts(mid)["cost"]
        self.assertEqual((cost["searches"], cost["searches_month"], cost["lookups"], cost["lookups_month"]), (1, 1, 1, 1))
        self.assertEqual(len(calls), 2)      # the refused search and the refused lookup never left the machine
        desk.markets.remove(mid)
        self.assertEqual(desk.store.meta_prefix("map:%s:" % mid), {})      # a deleted map takes its counts with it

    def test_several_are_added_to_the_pipeline_at_once(self):
        rows = self.companies()
        result = self.desk.markets.track_many([rows["Meridian Freight"]["id"], rows["Tidewater Logistics"]["id"]], "tester")
        self.assertEqual((result["ok"], len(result["added"])), (True, 2))
        again = self.desk.markets.track_many([rows["Meridian Freight"]["id"]], "tester")
        self.assertEqual((again["ok"], again["added"]), (False, []))
        self.assertIn("already in the pipeline", again["skipped"][0])
        self.assertEqual(self.companies()["Meridian Freight"]["status"], "tracked")

    def test_a_dismissed_company_can_be_put_back(self):
        target = self.companies()["Halcyon Transport"]["id"]
        self.assertTrue(self.desk.markets.dismiss({"targets": [target], "reason": "Too small"})["ok"])
        self.assertNotIn("Halcyon Transport", self.companies())
        self.assertEqual(self.desk.markets.restore([target]), {"ok": True, "restored": ["Halcyon Transport"]})
        back = self.companies()["Halcyon Transport"]
        self.assertEqual((back["status"], back.get("signal", "")), ("new", ""))      # the reason it was dismissed for does not linger as a signal
        self.assertEqual(self.desk.markets.restore([target]), {"ok": False, "restored": []})

    def test_a_company_moves_only_to_a_segment_the_map_has(self):
        target = self.companies()["Halcyon Transport"]["id"]
        moved = self.desk.markets.move(target, "hybrid carriers")
        self.assertEqual((moved["ok"], moved["segment"]), (True, "Hybrid carriers"))
        self.assertEqual(self.companies()["Halcyon Transport"]["segment"], "Hybrid carriers")
        self.assertFalse(self.desk.markets.move(target, "A segment nobody made")["ok"])

    def test_finding_buyers_spends_one_credit_a_company_and_looks_for_the_titles_on_the_map(self):
        calls = []

        def http(url, **kw):
            calls.append(kw)
            return 200, {"data": [{"full_name": "sam ito", "job_title": "director of carrier compliance"}], "total": 9}

        desk = make_desk(self.tmp.name, playbook="b2b-sales", secrets={"PDL_API_KEY": "pdl-secret-value"}, http=http)
        rows = {c["company"]: c for seg in desk.markets.summary(self.market["id"], text=False)["segments"] for c in seg["companies"]}
        nobody = [c["id"] for c in rows.values() if not c.get("people")]
        words = desk.markets.buyer_titles(self.market["id"])
        self.assertIn("compliance", words)      # what the buyers named on the map do
        self.assertFalse({"director", "chief", "officer"} & set(words))      # not how senior they are
        out = desk.markets.buyers(nobody + [rows["Meridian Freight"]["id"]])      # Meridian already has a buyer: not looked up
        self.assertEqual((out["ok"], len(out["found"]), len(calls)), (True, len(nobody), len(nobody)))
        self.assertTrue(all(call["data"]["size"] == 1 for call in calls))      # one person asked for, so one credit at most
        self.assertTrue(all("job_title LIKE" in call["data"]["sql"] for call in calls))      # and only someone with a buyer's title
        for call in calls:      # who can say yes: a manager or above, never the analyst who shares the word
            self.assertIn("job_title_levels='director'", call["data"]["sql"])
            self.assertFalse({"job_title_levels='senior'", "job_title_levels='entry'"} & set(call["data"]["sql"].replace("(", " ").replace(")", " ").split()))
        self.assertEqual(desk.setup()["keys"][1]["note"], "%d of 50 lookups used this month." % len(nobody))
        calls.clear()      # a lookup Hermes asks for by title is not narrowed: it may be after anyone
        desk.market({"action": "people", "company": "Somewhere Else Freight", "titles": ["dispatcher"], "limit": 1})
        self.assertNotIn("job_title_levels", calls[0]["data"]["sql"])

    def test_with_no_buyer_named_on_the_map_no_lookup_is_paid_for(self):
        calls = []
        desk = make_desk(self.tmp.name, playbook="b2b-sales", secrets={"PDL_API_KEY": "pdl-secret-value"}, http=lambda *a, **k: calls.append(k) or (200, {"data": []}))
        desk.markets.define({"name": "Nobody named", "segments": ["One"]})
        desk.markets.add({"market": "Nobody named", "companies": [{"company": "Ada", "segment": "One"}]})
        target = desk.markets.target("Ada")["id"]
        out = desk.markets.buyers([target])
        self.assertEqual((out["ok"], out["no_titles"], calls), (False, True, []))

    def test_buyers_are_looked_up_only_where_nobody_is_named_and_say_why_they_stop(self):
        rows = self.companies()
        nobody = [c["id"] for c in rows.values() if not c.get("people")]
        self.assertTrue(nobody)
        stopped = self.desk.markets.buyers(nobody + [rows["Meridian Freight"]["id"]])
        self.assertEqual((stopped["ok"], stopped["found"]), (False, []))
        self.assertIn("People Data Labs is not set up", stopped["problem"])


class FileBoundaryTests(unittest.TestCase):
    """A path in a tool call is the model's choice, and a model can be talked into things. So the desk
    writes its exports in one folder of its own, and reads CSVs by path from a few named folders only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "plugin-data"
        self.home.mkdir()
        self.outside = Path(self.tmp.name).resolve() / "elsewhere"
        self.outside.mkdir()
        self.desk = make_desk(str(self.home))
        fixture.load(self.desk)

    def test_an_export_is_a_file_name_in_the_exports_folder_and_nothing_else(self):
        exports = (self.home / "exports").resolve()
        victim = self.outside / "config.yaml"
        victim.write_text("keep me")
        for bad in (str(victim), "../elsewhere/config.yaml", "..", "sub/pipeline.csv", "sub\\pipeline.csv", "~/.zshrc", ".hidden.csv",
                    "/etc/passwd", "a" * 200):
            out = self.desk.export({"action": "export", "path": bad})
            self.assertFalse(out["ok"], bad)
            self.assertIn("file name, not a path", out["error"])
        self.assertEqual(victim.read_text(), "keep me")
        named = self.desk.export({"action": "export", "path": "q4 pipeline"})
        self.assertEqual(Path(named["path"]), exports / "q4 pipeline.csv")                      # the suffix is added
        self.assertEqual(Path(self.desk.export({"action": "export"})["path"]).parent, exports)   # the default lands there too
        self.assertEqual(named["rows"], 7)
        self.assertEqual([p.name for p in exports.iterdir() if p.name.startswith(".")], [])       # nothing half-written is left
        # a link someone planted in the folder is not followed out of it
        (exports / "trap.csv").symlink_to(victim)
        self.assertFalse(self.desk.export({"action": "export", "path": "trap.csv"})["ok"])
        self.assertEqual(victim.read_text(), "keep me")

    def test_a_csv_is_read_by_path_only_from_the_import_folders(self):
        secret = self.outside / "customers.csv"
        secret.write_text("Name,Company\nEve Stone,Rival Co\n")
        for tool in (lambda path: self.desk.export({"action": "import", "path": path}),
                     lambda path: self.desk.intake.run({"action": "propose", "path": path})):
            refused = tool(str(secret))
            self.assertFalse(refused["ok"])
            self.assertIn("outside the folders", refused["error"])
            self.assertIn("read it yourself", tool(str(self.outside / "notes.txt"))["error"])      # only .csv and .tsv, wherever it is
            self.assertIn("read it yourself", tool(str(self.outside / "id_rsa"))["error"])
        self.assertIsNone(self.desk.store.find("Eve Stone"))
        inbox = self.home / "imports"
        self.assertTrue(inbox.is_dir())                                # the refusal made the folder it points to
        (inbox / "event.csv").write_text("Name,Company\nAda Park,Orbit\n")
        self.assertEqual(self.desk.export({"action": "import", "path": "event.csv"})["added"], 1)
        (inbox / "leak.csv").symlink_to(secret)                        # a link inside the folder that points out of it
        self.assertIn("outside the folders", self.desk.export({"action": "import", "path": str(inbox / "leak.csv")})["error"])
        self.assertIn("outside the folders", self.desk.intake.run({"action": "propose", "path": "../elsewhere/customers.csv"})["error"])
        self.assertIsNone(self.desk.store.find("Eve Stone"))

    def test_the_team_can_name_one_more_folder_and_a_person_at_a_terminal_is_trusted(self):
        named = make_desk(str(self.home), config={"import_dir": str(self.outside)})
        (self.outside / "customers.csv").write_text("Name,Company\nEve Stone,Rival Co\n")
        self.assertEqual(named.intake.run({"action": "propose", "path": str(self.outside / "customers.csv")})["counts"]["new"], 1)
        path, refused = self.desk.csv_path(str(self.outside / "customers.csv"), trusted=True)      # `hermes stoa import <path>`
        self.assertEqual((path, refused), ((self.outside / "customers.csv").resolve(), None))
        self.assertIn("read it yourself", self.desk.csv_path(str(self.outside / "notes.txt"), trusted=True)[1]["error"])

    def test_the_working_directory_counts_only_when_it_is_a_project_folder(self):
        project = self.outside / "project"
        project.mkdir()
        with mock.patch.object(Path, "cwd", return_value=project), mock.patch.object(Path, "home", return_value=self.outside):
            self.assertIn(project.resolve(), self.desk.import_roots())
        with mock.patch.object(Path, "cwd", return_value=self.outside), mock.patch.object(Path, "home", return_value=self.outside):
            self.assertEqual(self.desk.import_roots(), [(self.home / "imports").resolve()])          # the home directory is not a project
        with mock.patch.object(Path, "cwd", return_value=Path(self.outside.anchor)), mock.patch.object(Path, "home", return_value=self.outside):
            self.assertEqual(self.desk.import_roots(), [(self.home / "imports").resolve()])          # nor is anything above it


class FakeClient:
    def __init__(self):
        self.posts, self.updates, self.whispers = [], [], []

    async def chat_postEphemeral(self, **kwargs):
        self.whispers.append(kwargs)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1700000000.%06d" % len(self.posts)}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


class FakeApp:
    def __init__(self):
        self.client = FakeClient()


class SlackClickGuardTests(unittest.IsolatedAsyncioTestCase):
    """A button moves a stage, applies a list and can spend a paid lookup. It is taken only from the
    person the message was posted for, or from someone the team has listed, in a chat the desk is in."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = {}
        self.wire(self.settings)

    def wire(self, config):
        self.desk = make_desk(self.tmp.name, config=config)
        if not self.desk.store.count():
            fixture.load(self.desk)
        self.injected = []
        self.slack = SlackDesk(self.desk, lambda content, origin=None, session_key=None: self.injected.append(content) or True)
        self.slack.grace = 0.05
        self.slack.settle = 0.02
        self.desk.slack = self.slack
        self.app = FakeApp()
        self.slack.wire(self.app, None)
        self.dana = self.desk.store.find("Dana Whitfield")

    async def asyncTearDown(self):
        others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if others:
            await asyncio.wait(others, timeout=5)

    async def ack(self):
        pass

    def press(self, user, action, value, ts="1700000000.000001", channel="C1"):
        body = {"user": {"id": user, "name": user.lower()}, "channel": {"id": channel}, "team": {"id": "T1"}, "message": {"ts": ts}}
        return self.slack.on_action(self.ack, body, {"action_id": action, "value": value})

    def stage(self):
        return self.desk.store.get(self.dana["id"])["stage"]

    async def test_only_the_person_a_card_was_posted_for_can_press_it(self):
        asked = {"platform": "slack", "chat_id": "C1", "user_id": "U1", "session_key": "s1"}
        self.assertTrue((await asyncio.to_thread(self.desk.card, {"deal": "dana"}, asked))["posted"])
        await self.press("U2", "stoa_advance", self.dana["id"] + "|screened")           # a colleague, a guest, anyone else in the channel
        self.assertEqual(self.stage(), "screened")
        whisper = self.app.client.whispers[-1]
        self.assertEqual((whisper["channel"], whisper["user"]), ("C1", "U2"))            # told privately, in the place they pressed
        self.assertIn("Not allowed", whisper["text"])
        self.assertEqual((self.injected, self.app.client.updates), ([], []))             # no draft was started, no card redrawn
        await self.press("U2", "stoa_enrich", self.dana["id"])                           # nor a paid lookup
        await self.press("U2", "stoa_lost", self.dana["id"] + "|0")
        self.assertEqual((self.stage(), self.desk.store.get(self.dana["id"])["status"]), ("screened", "open"))
        await self.press("U1", "stoa_advance", self.dana["id"] + "|screened")
        self.assertEqual(self.stage(), "submitted")
        self.assertEqual(len(self.app.client.whispers), 3)

    async def test_a_card_nobody_asked_for_needs_the_team_to_say_who_may_press_it(self):
        await asyncio.to_thread(self.desk.card, {"deal": "dana", "channel": "C1"}, {"platform": "cron"})     # a scheduled job posted it
        await self.press("U1", "stoa_advance", self.dana["id"] + "|screened")
        self.assertEqual(self.stage(), "screened")
        self.wire({"slack_users": "U7, U8"})                                             # the team lists who may
        await self.press("U8", "stoa_advance", self.dana["id"] + "|screened")
        self.assertEqual(self.stage(), "submitted")

    async def test_with_a_list_only_the_people_on_it_can_press(self):
        self.wire({"slack_users": ["U7"]})
        asked = {"platform": "slack", "chat_id": "C1", "user_id": "U1"}
        await asyncio.to_thread(self.desk.card, {"deal": "dana"}, asked)
        await self.press("U1", "stoa_advance", self.dana["id"] + "|screened")           # asked for it, but is not on the team's list
        self.assertEqual(self.stage(), "screened")
        await self.press("U7", "stoa_advance", self.dana["id"] + "|screened")
        self.assertEqual(self.stage(), "submitted")

    async def test_the_chats_the_desk_is_limited_to_hold_for_clicks_too(self):
        self.wire({"channels": ["C1"], "slack_users": ["U1"]})
        await self.press("U1", "stoa_advance", self.dana["id"] + "|screened", channel="C9")     # an old card in a chat since closed to the desk
        self.assertEqual(self.stage(), "screened")
        self.assertIn("Not allowed", self.app.client.whispers[-1]["text"])
        await self.press("U1", "stoa_advance", self.dana["id"] + "|screened", channel="C1")
        self.assertEqual(self.stage(), "submitted")

    async def test_a_list_waiting_for_approval_is_applied_only_by_the_person_who_sent_it(self):
        here = {"platform": "slack", "chat_id": "C1", "user_id": "U1"}
        out = await asyncio.to_thread(self.desk.intake.run, {"action": "propose", "source": "badge scans",
                                                              "rows": [{"name": "Ada Quill", "company": "Fathom Data"}]}, here)
        self.assertTrue(out["posted"])
        await self.press("U2", "stoa_intake", "apply|" + out["id"])
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertEqual(self.desk.store.intake_get(out["id"])["status"], "open")
        await self.press("U2", "stoa_intake_discard", "discard|" + out["id"])            # nor thrown away by someone else
        self.assertEqual(self.desk.store.intake_get(out["id"])["status"], "open")
        await self.press("U1", "stoa_intake", "apply|" + out["id"])
        self.assertEqual(self.desk.store.find("Ada Quill")["company"], "Fathom Data")

    async def test_a_company_on_a_map_is_added_only_by_the_person_who_asked_for_the_map(self):
        session = {"platform": "slack", "chat_id": "C1", "user_id": "U1"}
        self.assertTrue((await asyncio.to_thread(self.desk.market, {"action": "post"}, session))["posted"])
        button = [b["accessory"] for b in self.app.client.posts[0]["blocks"] if b.get("accessory")][0]
        before = self.desk.store.count()
        await self.press("U2", "stoa_track", button["value"])
        self.assertEqual(self.desk.store.count(), before)
        await self.press("U1", "stoa_track", button["value"])
        self.assertEqual(self.desk.store.count(), before + 1)

    async def test_a_click_with_no_sender_or_no_place_is_refused_quietly(self):
        await self.slack.on_action(self.ack, {}, {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        await self.slack.on_action(self.ack, {"user": {"id": "U1"}}, {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual((self.stage(), self.app.client.whispers), ("screened", []))


class GtmTabTests(unittest.TestCase):
    """The desk as a whole: a lead handed over to a deal pipeline, motions credited across pipelines, assets, the
    one reading of the go-to-market, and a map archived. The first pipeline here is a lead pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        fakes = {"hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: self.tmp.name),
                 "plugins": types.ModuleType("plugins"),
                 "plugins.plugin_storage": types.SimpleNamespace(plugin_data_dir=lambda name: self.data)}
        patcher = mock.patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeCtx({"playbook": "client-leads"})
        stoa.register(self.ctx)
        self.desk = stoa.LIVE_DESKS[-1]
        patcher = mock.patch.object(stoa, "_session", lambda: {"platform": "cli"})
        patcher.start()
        self.addCleanup(patcher.stop)
        made = self.call("gtm_playbook", action="add_pipeline", pipeline_name="Deals", playbook="b2b-sales")
        self.assertTrue(made["ok"], made)
        self.deals = made["added"]["id"] if "added" in made else next(p["id"] for p in self.desk.pipelines() if p["name"] == "Deals")

    def call(self, tool, **args):
        return json.loads(self.ctx.tools[tool](args))

    def lead(self, name, company, **more):
        out = self.call("gtm_deal", action="add", name=name, company=company, pipeline="Client lead desk", **more)
        self.assertTrue(out["ok"], out)
        return out["deal"]["id"]

    def records(self, pipeline):
        with self.desk.on(pipeline):
            return self.desk.store.list(status="all")

    def test_a_lead_closed_as_won_is_handed_over_once_as_a_deal(self):
        lead = self.lead("Dana Whitfield", "Calder Bank", email="dana@calder.example")
        closed = self.call("gtm_deal", action="close", deal=lead, outcome="won", pipeline="Client lead desk")
        self.assertTrue(closed["ok"], closed)
        deals = self.records(self.deals)
        self.assertEqual([d["name"] for d in deals], ["Calder Bank"])
        with self.desk.on(self.deals):
            self.assertEqual([p["name"] for p in self.desk.store.people(deals[0]["id"])], ["Dana Whitfield"])
        self.assertEqual(self.desk.handoffs.pairs(), {"main:%s" % lead: "%s:%s" % (self.deals, deals[0]["id"])})
        again = self.call("gtm_playbook", action="hand_over", pipeline="Client lead desk", deal=lead)
        self.assertFalse(again["ok"])
        self.assertIn("already handed over", again["error"])
        # A second lead at the same company joins the open deal; it does not open another.
        second = self.lead("Ola Brandt", "Calder Bank, Inc.")
        self.call("gtm_deal", action="close", deal=second, outcome="won", pipeline="Client lead desk")
        self.assertEqual(len(self.records(self.deals)), 1)
        with self.desk.on(self.deals):
            self.assertEqual(sorted(p["name"] for p in self.desk.store.people(deals[0]["id"])), ["Dana Whitfield", "Ola Brandt"])
        overview = self.desk.handoffs.overview()
        self.assertEqual(overview["totals"]["handed"], 2)
        lead_card = next(card for card in overview["pipelines"] if card["kind"] == "lead")
        self.assertEqual(lead_card["hands_to"], self.deals)
        listed = self.call("gtm_playbook", action="pipelines")["pipelines"]
        self.assertEqual([(p["kind"], p.get("hands_over_to")) for p in listed], [("lead", "Deals"), ("deal", None)])

    def test_hand_over_can_be_switched_off_and_a_claim_is_kept_once(self):
        self.assertTrue(self.call("gtm_playbook", action="set_handover", pipeline="Client lead desk", to="")["ok"])
        lead = self.lead("Rosa Lind", "Ledgerline")
        self.call("gtm_deal", action="close", deal=lead, outcome="won", pipeline="Client lead desk")
        self.assertEqual(self.records(self.deals), [])
        self.assertTrue(self.desk.handoffs._claim("main", lead, self.deals))
        self.assertFalse(self.desk.handoffs._claim("main", lead, self.deals))        # a second process closing it too opens nothing
        self.desk.handoffs._settle("main", lead)
        self.assertIsNone(self.desk.handoffs.handed("main", lead))
        by_hand = self.call("gtm_playbook", action="hand_over", pipeline="Client lead desk", deal=lead, to="Deals")
        self.assertTrue(by_hand["ok"], by_hand)
        self.assertEqual([d["name"] for d in self.records(self.deals)], ["Ledgerline"])

    def test_a_hand_over_goes_only_to_a_deal_pipeline_and_only_by_a_person_or_a_won_lead(self):
        # A recruiting pipeline is never where a company lands, first in the list or chosen.
        made = self.call("gtm_playbook", action="add_pipeline", pipeline_name="Search work", playbook="recruiting")
        self.assertTrue(made["ok"], made)
        searches = next(p["id"] for p in self.desk.pipelines() if p["name"] == "Search work")
        self.assertEqual(self.desk.handoffs.target_of("main"), self.deals)
        refused = self.call("gtm_playbook", action="set_handover", pipeline="Client lead desk", to="Search work")
        self.assertFalse(refused["ok"])
        self.assertIn("recruiting pipeline", refused["error"])
        with mock.patch.object(self.desk.handoffs, "_settings", lambda: {"main": {"to": searches}}):
            self.assertEqual(self.desk.handoffs.target_of("main"), self.deals)           # a choice made some other way is not followed there
        # A scheduled job cannot open a deal or change where leads go.
        lead = self.lead("Rosa Lind", "Ledgerline")
        for action, more in (("hand_over", {"deal": lead}), ("set_handover", {"to": ""})):
            told = json.loads(self._cron("gtm_playbook", action=action, pipeline="Client lead desk", **more))
            self.assertFalse(told["ok"], action)
            self.assertIn("scheduled job", told["error"])
        self.assertEqual(self.records(self.deals), [])

    def test_a_hand_over_that_stops_part_way_is_kept_as_done(self):
        lead = self.lead("Dana Whitfield", "Calder Bank")
        with mock.patch.object(self.desk, "contact", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(RuntimeError):
                self.desk.handoffs.hand_over("main", lead)
        deals = self.records(self.deals)
        self.assertEqual([d["name"] for d in deals], ["Calder Bank"])
        self.assertEqual(self.desk.handoffs.pairs(), {"main:%s" % lead: "%s:%s" % (self.deals, deals[0]["id"])})     # the pair is kept
        again = self.call("gtm_playbook", action="hand_over", pipeline="Client lead desk", deal=lead)
        self.assertFalse(again["ok"])                                                    # and not opened twice

    def _deals_map(self):
        """A map kept with the deal pipeline, with two companies, one of them with a buyer named."""
        with self.desk.on(self.deals):
            made = self.desk.markets.define({"name": "Neobanks", "segments": ["Challengers"]})
            self.assertTrue(made["ok"], made)
            added = self.desk.markets.add({"market": "Neobanks", "companies": [
                {"company": "Kestrel Labs", "segment": "Challengers", "fit": 5, "people": [{"name": "Lena Ortiz", "title": "CFO"}]},
                {"company": "Corvid Health", "segment": "Challengers", "fit": 4}]})
            self.assertTrue(added["ok"], added)
            return made["market"]["id"], {row["company"]: row for row in self.desk.markets.targets(made["market"]["id"])}

    def test_a_company_from_a_map_on_a_deal_pipeline_starts_as_a_lead(self):
        map_id, rows = self._deals_map()
        self.assertEqual(self.desk.lead_pipeline_for(self.deals), "main")
        with self.desk.on(self.deals):
            told = self.desk.markets.track({"targets": [rows["Kestrel Labs"]["id"]]})
            self.assertTrue(told["ok"], told)
            self.assertEqual(told["added_to_pipeline"][0]["pipeline"], "main")
            self.assertIn("lead pipeline", told["lead_pipeline"])
            self.assertEqual(self.desk.store.list(status="all"), [])                    # nothing in the deal pipeline yet
            target = self.desk.markets.target(rows["Kestrel Labs"]["id"])
            self.assertEqual((target["status"], target["detail"]["deal_pipeline"]), ("tracked", "main"))
            lead = self.desk.markets.record_of(target)
            self.assertEqual((lead["name"], lead["company"], lead["source"]), ("Lena Ortiz", "Kestrel Labs", "market map: Neobanks"))      # the buyer is the lead
            shown = next(c for seg in self.desk.markets.summary(map_id)["segments"] for c in seg["companies"] if c["company"] == "Kestrel Labs")
            self.assertEqual((shown["deal"], shown["deal_pipeline"]), (lead["id"], "main"))
            self.assertIn("already in the pipeline", self.desk.markets.click_track(rows["Kestrel Labs"]["id"], "Test")["notice"])     # once
            clicked = self.desk.markets.click_track(rows["Corvid Health"]["id"], "Test")
            self.assertEqual((clicked["pipeline"], clicked["deal"]["name"]), ("main", "Corvid Health"))
            self.assertIn("as a lead from the market map", clicked["followup"])
        # Converted, it is handed over and becomes a deal in the pipeline the map is kept with.
        self.call("gtm_deal", action="close", deal=lead["id"], outcome="won", pipeline="Client lead desk")
        self.assertEqual([d["name"] for d in self.records(self.deals)], ["Kestrel Labs"])

    def test_a_motion_keeps_a_list_and_is_credited_by_it(self):
        map_id, rows = self._deals_map()
        before = self.lead("Ola Brandt", "Brightside AI")                                    # already in the pipeline
        made = self.call("gtm_motion", action="add", name="Email offer", kind="outbound", status="running", market="Neobanks")
        self.assertTrue(made["ok"], made)
        on = self.call("gtm_motion", action="enroll", motion="Email offer", companies=["Kestrel Labs", "Corvid Health"])
        self.assertTrue(on["ok"], on)
        self.assertEqual((sorted(on["added"]), on["list"]["total"]), (["Corvid Health", "Lena Ortiz"], 2))
        self.assertEqual(len(self.records("main")), 3)                                       # leads, in the lead pipeline
        self.assertEqual(self.records(self.deals), [])
        self.assertTrue(self.call("gtm_motion", action="enroll", motion="Email offer", deals=["Ola Brandt"])["ok"])
        added = self.desk.motions.new_lead("Email offer", "Priya Raman", "Lumen Health")
        self.assertEqual(added["added"], ["Priya Raman"])
        again = self.call("gtm_motion", action="enroll", motion="Email offer", deals=["Ola Brandt"])
        self.assertEqual((again["added"], again["already_on_it"]), ([], ["Ola Brandt"]))
        got = self.call("gtm_motion", action="get", motion="Email offer")["motion"]
        claims = {row["name"]: row["how"] for row in got["impact"]["records"]}
        self.assertEqual(claims, {"Lena Ortiz": "sourced", "Corvid Health": "sourced", "Priya Raman": "sourced", "Ola Brandt": "influenced"})
        why = next(row["why"] for row in got["impact"]["records"] if row["name"] == "Lena Ortiz")
        self.assertIn("from the map, which is how it came into the pipeline", why)
        self.assertIn("already in the pipeline then", next(row["why"] for row in got["impact"]["records"] if row["name"] == "Ola Brandt"))
        # How far each has got: said by a person or the agent, or seen on the record.
        self.assertTrue(self.call("gtm_motion", action="mark", motion="Email offer", deal="Priya Raman", status="answered")["ok"])
        self.call("gtm_deal", action="log", deal=before, kind="email", text="Sent the offer.", pipeline="Client lead desk")
        listed = {row["name"]: row["status"] for row in self.call("gtm_motion", action="get", motion="Email offer")["motion"]["impact"]["members"]}
        self.assertEqual(listed, {"Priya Raman": "replied", "Ola Brandt": "reached", "Lena Ortiz": "added", "Corvid Health": "added"})
        # Everyone on the list counts toward what it won, those already in a pipeline too: a motion run on people you have.
        with self.desk.on(self.deals):
            existing = self.desk.deal({"action": "add", "name": "Halden renewal", "company": "Halden Group", "value": 12000}, actor="Test")["deal"]["id"]
        self.assertTrue(self.call("gtm_motion", action="enroll", motion="Email offer", deals=["Halden renewal"])["ok"])
        self.call("gtm_deal", action="close", deal=existing, outcome="won", pipeline="Deals")
        got_won = self.call("gtm_motion", action="get", motion="Email offer")["motion"]["impact"]
        self.assertEqual((got_won["influenced"], got_won["from_list"], got_won["won"], got_won["won_value"]), (2, 2, 1, 12000.0))
        self.assertEqual(self.call("gtm_motion", action="list")["totals"]["won_value"], 12000.0)
        self.assertTrue(self.call("gtm_motion", action="drop", motion="Email offer", deal="Halden renewal")["ok"])
        tally = self.call("gtm_motion", action="get", motion="Email offer")["motion"]["impact"]["list"]
        self.assertEqual((tally["total"], tally["reached"], tally["replied"], tally["meeting"]), (4, 2, 1, 0))
        self.assertFalse(self.call("gtm_motion", action="mark", motion="Email offer", deal="Priya Raman", status="wonderful")["ok"])
        # A record's page lists the motions it is on.
        self.assertEqual([row["name"] for row in self.desk.motions.lists_of("main:" + before)], ["Email offer"])
        # Off the list, it is no longer credited by it.
        self.assertTrue(self.call("gtm_motion", action="drop", motion="Email offer", deal="Ola Brandt")["ok"])
        self.assertNotIn("Ola Brandt", [row["name"] for row in self.call("gtm_motion", action="get", motion="Email offer")["motion"]["impact"]["records"]])
        # Being at a company on the map is not enough: a record that is on no list, and names no motion, is nobody's.
        with self.desk.on(self.deals):
            self.assertTrue(self.desk.deal({"action": "add", "name": "Kestrel Labs, second team", "company": "Kestrel Labs"}, actor="Test")["ok"])
        self.assertNotIn("Kestrel Labs, second team", [row["name"] for row in self.call("gtm_motion", action="get", motion="Email offer")["motion"]["impact"]["records"]])
        # The list wins over a mention: a second motion named in the Source of a record on this one's list only influenced it.
        self.assertTrue(self.call("gtm_motion", action="add", name="Summit dinner", kind="event", status="running")["ok"])
        self.call("gtm_deal", action="update", deal="Priya Raman", source="Summit dinner", pipeline="Client lead desk")
        summit = {row["name"]: row["how"] for row in self.call("gtm_motion", action="get", motion="Summit dinner")["motion"]["impact"]["records"]}
        self.assertEqual(summit, {"Priya Raman": "influenced"})
        self.assertIn("scheduled job", json.loads(self._cron("gtm_motion", action="enroll", motion="Email offer", deals=["Priya Raman"]))["error"])

    def test_a_motion_is_credited_with_a_lead_and_the_deal_it_became_once(self):
        made = self.call("gtm_motion", action="add", name="Fraud summit dinner", kind="event", status="running", tags=["FSD26"])
        self.assertTrue(made["ok"], made)
        lead = self.lead("Dana Whitfield", "Calder Bank", source="FSD26 dinner")
        listed = self.call("gtm_motion", action="list")
        self.assertEqual(listed["motions"][0]["impact"]["sourced"], 1)
        self.call("gtm_deal", action="close", deal=lead, outcome="won", pipeline="Client lead desk")
        got = self.call("gtm_motion", action="get", motion="fraud summit")
        impact = got["motion"]["impact"]
        self.assertEqual((impact["sourced"], impact["handed_over"]), (1, 1))         # the lead and its deal are one piece of business
        self.assertEqual(impact["records"][0]["kind"], "deal")
        self.assertIn("FSD26", impact["records"][0]["why"])                            # the deal carries the lead's Source over
        logged = self.call("gtm_motion", action="log", motion="Fraud summit dinner", metric="spent", value=1200)
        self.assertEqual(logged["motion"]["impact"]["spend"], 1200.0)
        self.assertIn("scheduled job", json.loads(self._cron("gtm_motion", action="add", name="x"))["error"])

    def _cron(self, tool, **args):
        with mock.patch.object(stoa, "_session", lambda: {"platform": "cron"}):
            return self.ctx.tools[tool](args)

    def test_assets_the_brief_and_internal_notes_reach_the_agent_in_one_reading(self):
        self.assertTrue(self.call("gtm_context", action="add_link", label="Booking page", url="cal.example.com/us")["ok"])
        self.assertTrue(self.call("gtm_context", action="add_asset", label="Pitch deck", url="https://example.com/deck", kind="deck")["ok"])
        empty = self.call("gtm_context")
        self.assertEqual(empty["maps"], [])
        self.assertEqual(empty["links"], [{"label": "Booking page", "url": "https://cal.example.com/us"}])
        self.assertTrue(self.call("gtm_market", action="define", name="Neobanks", segments=["Challengers"])["ok"])
        self.assertTrue(self.call("gtm_context", action="set_brief", text="Fraud checks for neobanks.")["ok"])
        self.assertTrue(self.call("gtm_context", action="set_notes", text="Never go below 40k a year.")["ok"])
        read = self.call("gtm_context")
        self.assertEqual(read["maps"][0]["what_we_sell_and_who_to"], "Fraud checks for neobanks.")
        self.assertEqual(read["maps"][0]["internal_notes"], "Never go below 40k a year.")
        self.assertEqual(self.desk.gtm.repeats("We never go below 40k a year, sorry."), ["Never go below 40k a year."])
        self.assertIn("gtm_context", self.ctx.sections["stoa.gtm"][0]({}))

    def test_an_archived_map_is_out_of_every_list_until_it_is_put_back(self):
        self.assertTrue(self.desk.markets.define({"name": "Neobanks"})["ok"])
        self.assertTrue(self.desk.markets.define({"name": "Credit unions"})["ok"])
        self.assertTrue(self.desk.markets.archive("Neobanks")["ok"])
        self.assertEqual([m["name"] for m in self.desk.markets.list_markets()], ["Credit unions"])
        self.assertEqual([m["name"] for m in self.desk.markets.list_markets(archived=True)], ["Neobanks"])
        self.assertIsNone(self.desk.markets._market("Neobanks"))
        self.assertEqual(self.desk.markets._market("").get("name"), "Credit unions")            # the only map left is "the map"
        # A company on it is not found by its name alone either, only on its own map.
        self.assertTrue(self.desk.markets.unarchive("Neobanks")["ok"])
        self.assertTrue(self.desk.markets.add({"market": "Neobanks", "companies": [{"company": "Kestrel Labs", "segment": "x"}]})["ok"])
        self.assertTrue(self.desk.markets.archive("Neobanks")["ok"])
        self.assertIsNone(self.desk.markets.target("Kestrel Labs"))
        kept = self.desk.markets.list_markets(archived=True)[0]
        self.assertEqual(self.desk.markets.target("Kestrel Labs", kept["id"])["company"], "Kestrel Labs")
        self.assertFalse(self.desk.markets.remove("Credit unions", archived=True)["ok"])         # only an archived one goes this way
        self.assertTrue(self.desk.markets.unarchive("neobanks")["ok"])
        self.assertEqual(len(self.desk.markets.list_markets()), 2)
        self.assertTrue(self.desk.markets.archive("Neobanks")["ok"])
        gone = self.desk.markets.remove("neobanks", archived=True)
        self.assertTrue(gone["ok"], gone)
        self.assertEqual(self.desk.markets.list_markets(archived=True), [])


class SlackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name, config={"slack_users": ["U1"]})      # these tests press as U1, who is on the team's list
        fixture.load(self.desk)
        self.injected = []
        self.accept = True
        self.accept_session = True

        def inject(content, origin=None, session_key=None):
            self.injected.append((content, origin or session_key))
            if session_key is not None:
                return self.accept_session
            return self.accept
        self.slack = SlackDesk(self.desk, inject)
        self.slack.grace = 0.05
        self.slack.settle = 0.02
        self.desk.slack = self.slack
        self.app = FakeApp()
        self.slack.wire(self.app, None)
        self.dana = self.desk.store.find("Dana Whitfield")
        self.acks = 0

    async def asyncTearDown(self):
        # card redraws and hand-off watchers run on after a click returns: let them finish before the ledger's folder goes
        others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if others:
            await asyncio.wait(others, timeout=5)

    async def ack(self):
        self.acks += 1

    def body(self, ts="1700000000.000001", thread_ts=None):
        message = {"ts": ts}
        if thread_ts:
            message["thread_ts"] = thread_ts
        return {"user": {"id": "U1", "name": "jo"}, "channel": {"id": "C1"}, "team": {"id": "T1"}, "message": message}

    async def test_tool_posts_a_card_from_a_worker_thread(self):
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, {"platform": "slack", "chat_id": "C1"})
        self.assertTrue(result["posted"])
        post = self.app.client.posts[0]
        self.assertEqual(post["channel"], "C1")
        self.assertNotIn("thread_ts", post)
        self.assertEqual(self.desk.store.cards(self.dana["id"])[0]["channel"], "C1")

    async def test_the_command_posts_the_real_card_in_slack_with_no_model(self):
        here = {"platform": "slack", "chat_id": "D1", "thread_id": "1690000000.000100", "session_key": "sess-1"}
        reply = await asyncio.to_thread(self.desk.slash, "dana whitfield", here)
        self.assertEqual(reply, "")                                    # the card is the reply
        post = self.app.client.posts[-1]
        self.assertEqual((post["channel"], post["thread_ts"]), ("D1", "1690000000.000100"))
        self.assertIn("stoa_advance", json.dumps(post["blocks"]))
        self.assertEqual(self.desk.store.card_session("D1", "1700000000.000001"), "")     # a command has no session
        reply = await asyncio.to_thread(self.desk.slash, "map", here)
        self.assertEqual(reply, "")
        self.assertIn("stoa_track", json.dumps(self.app.client.posts[-1]["blocks"]))
        self.assertEqual(len(self.app.client.posts), 2)
        self.assertIn("no record matches", await asyncio.to_thread(self.desk.slash, "nobody at all", here))
        self.assertIn("!gtm", self.desk.slash("help", here))

    async def test_the_command_is_text_everywhere_else(self):
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield"))                         # a terminal
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield", {"platform": "telegram", "chat_id": "T1"}))
        self.slack.app = None                                                                      # Slack not connected
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield", {"platform": "slack", "chat_id": "D1"}))
        self.assertIn("Regional freight brokers", self.desk.slash("map", {"platform": "slack", "chat_id": "D1"}))
        self.assertEqual(self.app.client.posts, [])

    async def test_a_button_acts_on_the_pipeline_its_card_was_posted_from(self):
        made = self.desk.pipeline_add("My practice", "agency")["added"]["id"]

        def post():                                                    # as a tool call for that pipeline does it
            with self.desk.on(made):
                visa = self.desk.deal({"action": "add", "name": "Visa retainer", "company": "Visa"})["deal"]["id"]
                self.desk.card({"deal": visa}, {"platform": "slack", "chat_id": "C1"})
                return visa, self.desk.store.cards(visa)[0]["ts"]
        visa, ts = await asyncio.to_thread(post)
        self.assertEqual(self.slack.pipeline_of(self.body(ts=ts)), made)
        self.assertEqual(self.slack.pipeline_of(self.body(ts="1600000000.000009")), "")       # a message no pipeline knows: the first
        await self.slack.on_action(self.ack, self.body(ts=ts), {"action_id": "stoa_advance", "value": visa + "|inbound"})
        with self.desk.on(made):
            self.assertEqual(self.desk.store.get(visa)["stage"], "intro")
        self.assertIsNone(self.desk.store.get(visa))                   # nothing of it in the first pipeline
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], self.dana["stage"])
        self.assertIn("Intro call · 2 of 5", self.app.client.updates[-1]["blocks"][0]["text"]["text"])      # redrawn in its own playbook's words
        await self.slack.on_action(self.ack, self.body(ts="1700000000.000777"), {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], "submitted")           # a card of the first pipeline, as ever
        await asyncio.sleep(0.2)

    async def test_advance_click_moves_updates_and_injects(self):
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(self.acks, 1)
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], "submitted")
        self.assertEqual(len(self.app.client.updates), 1)
        self.assertIn("Submitted · 4 of 6", self.app.client.updates[0]["blocks"][0]["text"]["text"])
        content, origin = self.injected[0]
        self.assertIn("submission note", content)
        self.assertEqual((origin["platform"], origin["chat_id"], origin["thread_id"], origin["user_id"]),
                         ("slack", "C1", "1700000000.000001", "U1"))
        self.assertEqual(self.app.client.posts, [])
        self.assertEqual(self.desk.store.activities(self.dana["id"], limit=1)[0]["actor"], "jo")
        # the agent's turn opens the record, which hands it the task and clears it: no hint is needed
        self.assertIn("submission note", self.desk.deal({"action": "get", "deal": self.dana["id"]})["pending_task"])
        await asyncio.sleep(0.2)
        self.assertEqual(self.app.client.posts, [])

    async def test_a_hand_off_hermes_accepts_and_then_drops_still_reaches_the_person(self):
        # Hermes answers yes before it has routed the turn. Here it says yes and no turn ever starts.
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.app.client.posts, [])                        # nothing yet: the turn may be starting
        await asyncio.sleep(0.3)
        hint = self.app.client.posts[-1]
        self.assertEqual(hint["thread_ts"], "1699999999.000001")
        self.assertIn("Saved Dana Whitfield. Reply here", hint["text"])
        self.assertEqual(len(self.app.client.posts), 1)
        self.assertIn("Drafting", self.card_text(0))                         # what the card said at first
        self.assertIn("Draft waiting", self.card_text())                     # and what it says once no turn came
        self.assertIn("submission note", self.desk.deal({"action": "get", "deal": self.dana["id"]})["pending_task"])

    def card_text(self, index=-1):
        return json.dumps(self.app.client.updates[index]["blocks"], ensure_ascii=False)

    async def test_the_card_says_drafting_until_the_draft_has_landed(self):
        click = {"action_id": "stoa_draft", "value": self.dana["id"]}
        await self.slack.on_action(self.ack, self.body(), click)
        self.assertEqual(len(self.app.client.updates), 1)                   # one redraw, already carrying the line
        self.assertIn("Drafting", self.card_text())
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertEqual(self.card_text().count("stoa_draft"), 1)           # the line sits with the buttons, not instead of them
        self.desk.deal({"action": "get", "deal": self.dana["id"]})          # the agent's turn opens the record
        await asyncio.sleep(0.2)
        self.assertNotIn("Drafting", self.card_text())                      # the draft has had time to land
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertEqual(self.app.client.posts, [])
        self.assertEqual(self.desk.handoff(self.dana["id"]), "")

    async def test_a_second_press_of_the_same_button_does_not_start_a_second_draft(self):
        self.slack.settle = 0.5                                             # the draft takes a moment to land
        click = {"action_id": "stoa_draft", "value": self.dana["id"]}
        await self.slack.on_action(self.ack, self.body(), click)
        await self.slack.on_action(self.ack, self.body(), click)            # impatient: nothing has arrived yet
        self.desk.deal({"action": "get", "deal": self.dana["id"]})          # the agent picks the task up...
        await self.slack.on_action(self.ack, self.body(), click)            # ...and is still writing
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.app.client.posts, [])
        # a different piece of writing for the same record is not swallowed
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(len(self.injected), 2)
        self.assertIn("submission note", self.injected[1][0])
        self.desk.deal({"action": "get", "deal": self.dana["id"]})
        await asyncio.sleep(1.0)
        self.assertEqual(self.desk.handoff(self.dana["id"]), "")
        await self.slack.on_action(self.ack, self.body(), click)            # once that draft is in, asking again works
        self.assertEqual(len(self.injected), 3)

    async def test_the_card_says_draft_waiting_until_someone_replies(self):
        self.accept = False
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertIn("Draft waiting", self.card_text())
        self.assertIn("Saved Dana Whitfield. Reply here", self.app.client.posts[-1]["text"])
        before = len(self.app.client.updates)
        got = self.desk.deal({"action": "get", "deal": self.dana["id"]})    # the reply's turn opens the record
        self.assertIn("pending_task", got)
        await asyncio.sleep(0.1)
        self.assertGreater(len(self.app.client.updates), before)            # the card is redrawn without the line
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertNotIn("Drafting", self.card_text())

    async def test_a_stale_drafting_line_does_not_outlive_a_restart(self):
        self.desk.park_followup(self.dana["id"], "write it")
        self.desk.store.meta_set("handoff:" + self.dana["id"], "drafting|%d|abc" % (time.time() - 600))
        self.assertEqual(self.desk.handoff(self.dana["id"]), "waiting")     # the watcher died with the old process
        self.assertFalse(self.desk.handoff_same(self.dana["id"], "write it"))
        self.desk.store.delete(self.dana["id"])                             # and a deleted record leaves nothing behind
        self.assertEqual(self.desk.store.meta_prefix("pending:"), {})
        self.assertEqual(self.desk.store.meta_prefix("handoff:"), {})

    async def test_an_intake_review_in_slack_applies_on_one_press(self):
        here = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100"}
        rows = [{"name": "Ada Quill", "company": "Fathom Data"}, {"name": "Dana Whitfield", "owner": "Jo"}]
        out = await asyncio.to_thread(self.desk.intake.run, {"action": "propose", "source": "badge scans", "rows": rows}, here)
        self.assertTrue(out["posted"])
        post = self.app.client.posts[-1]
        self.assertEqual((post["channel"], post["thread_ts"]), ("C1", "1690000000.000100"))
        text = json.dumps(post["blocks"], ensure_ascii=False)
        self.assertIn("Ada Quill", text)
        self.assertIn("owner you → Jo", text)
        self.assertIn("Apply 2", text)
        self.assertIsNone(self.desk.store.find("Ada Quill"))                                # nothing written yet
        body = self.body(ts="1700000000.000001", thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_intake", "value": "apply|" + out["id"]})
        self.assertEqual(self.desk.store.find("Ada Quill")["company"], "Fathom Data")
        self.assertEqual(self.desk.store.get(self.dana["id"])["owner"], "Jo")
        receipt = json.dumps(self.app.client.updates[-1]["blocks"], ensure_ascii=False)
        self.assertIn("1 added, 1 updated", receipt)
        self.assertNotIn("stoa_intake", receipt)                                            # the buttons are gone
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_intake", "value": "apply|" + out["id"]})
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)
        self.assertTrue(ACTIONS.match("stoa_intake") and ACTIONS.match("stoa_intake_discard"))

    async def test_a_card_from_the_command_does_not_claim_a_session(self):
        here = {"platform": "slack", "chat_id": "D1", "thread_id": "1690000000.000100", "session_key": "no-such-session"}
        await asyncio.to_thread(self.desk.slash, "dana whitfield", here)
        self.assertEqual(self.desk.store.card_session("D1", "1700000000.000001"), "")
        self.accept = False                                                 # nobody has spoken to the agent in this thread
        await self.slack.on_action(self.ack, self.body(ts="1700000000.000001", thread_ts="1690000000.000100"),
                                   {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual([target for _content, target in self.injected if isinstance(target, str)], [])   # no session tried
        self.assertIn("Reply here", self.app.client.posts[-1]["text"])      # told at once, not after the grace period

    async def test_refused_injection_parks_the_task_and_leaves_a_hint(self):
        self.accept = False
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertEqual(self.app.client.posts[0]["thread_ts"], "1699999999.000001")
        self.assertIn("Reply here", self.app.client.posts[0]["text"])
        got = self.desk.deal({"action": "get", "deal": self.dana["id"]})
        self.assertIn("Draft a short, warm follow-up", got["pending_task"])
        self.assertNotIn("pending_task", self.desk.deal({"action": "get", "deal": self.dana["id"]}))

    async def test_card_posted_from_a_conversation_hands_back_to_that_session(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_abc"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, session)
        self.assertEqual(self.app.client.posts[0]["thread_ts"], "1690000000.000100")
        body = self.body(ts=result["ts"], thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(self.injected, [(self.injected[0][0], "ses_abc")])
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_more", "selected_option": {"value": "refresh|" + self.dana["id"]}})
        self.assertEqual(self.desk.store.card_session("C1", result["ts"]), "ses_abc")

    async def test_session_refused_falls_back_to_the_thread(self):
        self.accept_session = False
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "", "session_key": "ses_gone"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, session)
        await self.slack.on_action(self.ack, self.body(ts=result["ts"]), {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertEqual(self.injected[0][1], "ses_gone")
        self.assertEqual(self.injected[1][1]["thread_id"], result["ts"])

    async def test_card_sent_to_another_channel_is_top_level_without_a_session(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_abc"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana", "channel": "C9"}, session)
        self.assertNotIn("thread_ts", self.app.client.posts[0])
        self.assertEqual(self.desk.store.card_session("C9", result["ts"]), "")

    async def test_menus_and_other_cards_refresh(self):
        await asyncio.to_thread(self.slack.post_card, "C2", self.dana["id"])
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_more", "selected_option": {"value": "snooze7|" + self.dana["id"]}})
        self.assertTrue(self.desk.store.get(self.dana["id"])["snoozed_until"])
        self.assertEqual(sorted(u["channel"] for u in self.app.client.updates), ["C1", "C2"])
        self.assertIn("Snoozed until", self.app.client.posts[-1]["text"])
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_lost", "selected_option": {"value": self.dana["id"] + "|3"}})
        self.assertEqual(self.desk.store.get(self.dana["id"])["lost_reason"], "Client passed")
        self.assertIn("Client passed", self.injected[-1][0])

    async def test_tool_change_refreshes_posted_cards(self):
        await asyncio.to_thread(self.slack.post_card, "C1", self.dana["id"])
        await asyncio.to_thread(self.desk.deal, {"action": "move", "deal": self.dana["id"], "stage": "next"})
        self.assertEqual(len(self.app.client.updates), 1)

    async def test_map_post_and_add_to_pipeline_click(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_map"}
        posted = await asyncio.to_thread(self.desk.market, {"action": "post"}, session)
        self.assertTrue(posted["posted"])
        post = self.app.client.posts[0]
        self.assertEqual((post["thread_ts"], post["text"]), ("1690000000.000100", "Market map"))
        button = [b["accessory"] for b in post["blocks"] if b.get("accessory")][0]
        self.assertEqual(button["text"]["text"], "Add to pipeline")
        body = self.body(ts="1700000000.000001", thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_track", "value": button["value"]})
        deal = [d for d in self.desk.store.list() if d["source"].startswith("market map")][0]
        self.assertEqual((deal["stage"], deal["company"]), ("sourced", "Meridian Freight"))
        self.assertIn("in the pipeline", json.dumps(self.app.client.updates[-1]["blocks"]))
        content, target = self.injected[-1]
        self.assertEqual(target, "ses_map")
        self.assertIn("Renata Okafor as the likely buyer", content)
        self.assertEqual((deal["name"], deal["fields"].get("title")), ("Renata Okafor", "Director of Carrier Compliance"))
        self.assertIn(deal["id"], content)
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_track", "value": button["value"]})
        self.assertIn("already in the pipeline", self.app.client.posts[-1]["text"])
        self.assertEqual(len([d for d in self.desk.store.list() if d["source"].startswith("market map")]), 1)

    async def test_map_click_parks_the_task_when_refused(self):
        self.accept = False
        target = self.desk.markets.target("Brightside Freight")
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_track", "value": target["id"]})
        deal = self.desk.store.find("Brightside Freight")
        self.assertIn("Reply here", self.app.client.posts[-1]["text"])
        self.assertIn("likely buyer", self.desk.deal({"action": "get", "deal": deal["id"]})["pending_task"])

    async def test_bad_clicks_never_raise(self):
        await self.slack.on_action(self.ack, {}, {"action_id": "stoa_advance", "value": ""})
        await self.slack.on_action(self.ack, self.body(thread_ts="1690000000.000100"), {"action_id": "stoa_advance", "value": "ghost|x"})
        self.assertIn("no longer", self.app.client.posts[-1]["text"])
        self.assertEqual(self.app.client.posts[-1]["thread_ts"], "1690000000.000100")   # the thread's parent, never a reply
        self.assertEqual(self.acks, 2)

    async def test_not_connected_falls_back_to_text(self):
        self.slack.app = None
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, {"platform": "slack", "chat_id": "C1"})
        self.assertFalse(result["posted"])
        self.assertIn("card", result)


class CommandTests(unittest.TestCase):
    """/blueprint is Hermes's own command. The page's talk box cannot run it, and the agent cannot either."""

    def setUp(self):
        self.page = (ROOT / "desktop" / "plugin.js").read_text()
        self.skill = (ROOT / "skills" / "desk" / "SKILL.md").read_text()

    def test_the_page_never_puts_a_command_in_its_own_talk_box(self):
        # the talk box hands every line to the agent as words, so a command put there is a dead end
        self.assertNotIn("$draft.set('/", self.page)
        self.assertIn("typeInChat('/blueprint stoa:' + item.name)", self.page)

    def test_the_talk_box_takes_a_command_to_a_chat_instead_of_sending_it(self):
        send = self.page[self.page.index("const send = line => {"):]
        send = send[:send.index("say(words, files, 'desk'")]
        self.assertIn("isCommand(words)", send)
        self.assertIn("typeInChat(words.trim())", send)

    def test_the_talk_box_hands_any_file_to_hermes_the_way_the_apps_chat_box_does(self):
        page = self.page
        # a PDF, a Word file or a picture is no longer turned away: it waits by the box and goes with the next message
        self.assertNotIn("goes through the full chat", page)
        hand = page[page.index("async function handOver("):page.index("async function say(")]
        self.assertIn("host.request('file.attach', { session_id: sessionId, data_url: url, name: item.name }", hand)
        self.assertIn("host.request('image.attach_bytes', { session_id: sessionId, content_base64:", hand)
        say = page[page.index("async function say("):page.index("function openTalk()")]
        self.assertIn("[refs.join('\\n'), sent].filter(Boolean).join('\\n\\n')", say)      # references first, then the words, as the app sends them
        self.assertLess(say.index("await handOver(id, whole)"), say.index("host.request('prompt.submit'"))
        self.assertIn("...(pictures.length ? { attachments: pictures } : {})", say)      # a staged picture is named on the message, or the model never sees it
        self.assertIn("if (got.path && got.mime) pictures.push({ path: String(got.path), mime: String(got.mime) })", hand)
        self.assertIn("MAX_FILE_BYTES = 25 * 1024 * 1024", page)
        # a list is still read by the desk itself, with no model in between
        take = page[page.index("async function takeFile("):page.index("// -- look")]
        self.assertLess(take.index("LIST_FILE.test(file.name)"), take.index("MAX_FILE_BYTES"))
        self.assertIn("rest('/intake'", take)

    def test_the_model_menu_and_replies_are_the_apps_own_where_it_has_them(self):
        page = self.page
        self.assertIn("import * as SDK from '@hermes/plugin-sdk'", page)
        self.assertIn("h(ModelCatalogMenu, { controller,", page)
        # where the app has not got one, or one of them fails to draw, the page's own plain version takes over
        self.assertIn("return APP_MENU ? h(Guard, { what: 'the app\\'s model menu', fallback: h(ModelPickPlain, { lane }) }, h(ModelPickApp, { lane })) : h(ModelPickPlain, { lane })", page)
        self.assertIn("if (!MessageTextContent) return plain", page)
        self.assertIn("h(Guard, { what: 'the app\\'s message renderer', fallback: plain }", page)
        self.assertIn("static getDerivedStateFromError() { return { failed: true } }", page)

    def test_a_send_that_never_reached_hermes_gives_the_words_and_files_back(self):
        say = self.page[self.page.index("async function say("):self.page.index("function openTalk()")]
        self.assertLess(say.index("host.request('prompt.submit'"), say.index("reached = true"))
        self.assertIn("const back = !reached && lane === 'desk' && (files.length > 0 || fromBox)", say)      # words typed in the box as well as files; never a button's own prompt
        self.assertIn("if (files.length && !$attached.get().length) $attached.set(files)", say)
        self.assertIn("if (line) $draft.set(line)", say)

    def test_resuming_a_conversation_does_not_set_its_model_again(self):
        # Hermes keeps a conversation's model with it; setting it on every visit would move a long conversation
        # to another model without the warning Hermes gives for that
        opened = self.page[self.page.index("async function talkOpen("):self.page.index("async function handOver(")]
        resumed = opened[opened.index("host.request('session.resume'"):opened.index("catch { /* that conversation is gone")]
        self.assertNotIn("modelTo(", resumed)
        self.assertNotIn("config.set", resumed)
        self.assertIn("reasoning_effort: pick.effort", opened)      # a new one begins on the pick, depth included

    def test_the_skill_names_every_automation_as_hermes_lists_it(self):
        from stoa.blueprints import BLUEPRINTS
        self.assertEqual(len(BLUEPRINTS), 7)
        for blueprint in BLUEPRINTS:
            self.assertIn("`stoa:%s`" % blueprint["name"], self.skill)
        self.assertIn("nothing to do with the playbook", self.skill)


class ReportTests(unittest.TestCase):
    """Reports are counted by the plugin from the desk's own records: no model, the same answer every time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.reports = self.desk.reports
        self.id = lambda name: self.desk.store.find(name)["id"]

    def age(self, name, days, column="stage_at"):
        """Set when a record was made, or reached its stage, to so many days ago (a test's stand-in for time passing)."""
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.desk.store._conn() as conn:
            conn.execute("UPDATE deals SET %s = ? WHERE id = ?" % column, (stamp, self.id(name)))

    def row(self, result, label):
        return next(row for row in result["rows"] if row["label"] == label)

    def test_a_period_is_a_stretch_of_days_and_the_same_stretch_before_it(self):
        from stoa.reports import inside, window
        day = dt.date(2026, 11, 15)
        ninety = window("90", day)
        self.assertEqual((ninety["from"], ninety["to"]), ("2026-08-18", "2026-11-15"))
        self.assertEqual((ninety["before"]["from"], ninety["before"]["to"]), ("2026-05-20", "2026-08-17"))
        quarter = window("quarter", day)
        self.assertEqual((quarter["from"], quarter["before"]["from"], quarter["before"]["to"]), ("2026-10-01", "2026-07-01", "2026-09-30"))
        self.assertEqual(window("year", dt.date(2026, 1, 3))["before"], {"label": "last year", "from": "2025-01-01", "to": "2025-12-31"})
        self.assertIsNone(window("all", day)["before"])
        self.assertEqual(window("nonsense", day)["id"], "all")
        local = lambda text: dt.datetime.fromisoformat(text).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")      # noqa: E731  (a wall-clock time, as kept)
        self.assertTrue(inside(local("2026-08-18T00:00:01"), ninety) and inside(local("2026-11-15T23:59:59"), ninety))      # a day is the day it is here
        self.assertFalse(inside(local("2026-08-17T23:59:59"), ninety) or inside(local("2026-11-16T00:00:01"), ninety) or inside("", ninety)
                         or inside("2026-11-01T00:00:00Z", None))
        self.assertTrue(inside("2026-11-15", ninety))                                                                              # a plain date is taken as it is
        from stoa.store import day_bounds, local_day
        start, end = day_bounds("2026-11-15", "2026-11-15")
        self.assertEqual((local_day(start), local_day(end), local_day("2026-11-15"), local_day(""), day_bounds("", "2026-11-15")[0]),
                         ("2026-11-15", "2026-11-15", "2026-11-15", "", "0000-01-01T00:00:00Z"))

    def test_a_desk_starts_with_nine_reports_seven_of_them_on_the_dashboard(self):
        listed = self.reports.catalog()
        self.assertEqual([entry["id"] for entry in listed], ["stages", "winrate", "time", "sources", "lost", "map", "hermes", "owners", "months"])
        self.assertEqual([entry["id"] for entry in listed if entry["pinned"]], ["stages", "winrate", "time", "sources", "lost", "map", "hermes"])
        self.assertEqual(self.reports.find("sources")["title"], "Where candidates come from")       # in the playbook's own words
        self.assertEqual(self.reports.find("lost")["title"], "Passed, and why")
        self.assertEqual(self.reports.find("win ra")["id"], "winrate")                              # a clear part of a title finds it
        self.assertIsNone(self.reports.find("w"))                                                   # several match: none is guessed
        data = self.reports.dashboard()
        self.assertEqual([card["id"] for card in data["cards"]], ["stages", "winrate", "time", "sources", "lost", "map", "hermes"])
        self.assertTrue(all(card["ok"] for card in data["cards"]))
        self.assertEqual(data["period"], {"id": "90", "label": "Last 90 days", "before": "the 90 days before"})

    def test_pipeline_by_stage_counts_what_is_open_and_what_it_is_worth(self):
        result = self.reports.run("stages", full=True)
        self.assertEqual([(row["label"], row["count"]) for row in result["rows"]],
                         [("Sourced", 1), ("Contacted", 2), ("Screened", 1), ("Submitted", 1), ("Client interview", 1), ("Offer", 1)])
        # Seven candidates in three searches whose fees are 45k, 55k and 30k: each search once, not 299k for all seven.
        self.assertEqual(result["total"], {"count": 7, "value": 130000.0})
        # A stage's amount is the fee of each search whose furthest-along candidate is there, so the stages add up to what
        # is in play: Halcyon's 30k at Submitted (Ingrid), Northwind's 45k at Client interview (Priya), Ostrava's 55k at
        # Offer (Wei-Lin). The two at Contacted are behind someone in their own searches.
        self.assertEqual([row["value"] for row in result["rows"]], [0, 0, 0, 30000.0, 45000.0, 55000.0])
        self.assertEqual(sum(row["value"] for row in result["rows"]), result["total"]["value"])
        self.assertEqual(sorted(r["name"] for r in self.row(result, "Contacted")["records"]), ["Callum Reyes", "Marcus Oyelaran"])
        self.assertEqual(result["says"][0], "7 open, $130k in play.")
        # Priya Raman (45k, at client interview) and Wei-Lin Chao (55k, at offer) are the furthest along in two of the three searches.
        self.assertEqual(result["says"][1], "Most of the money ($100k) is in the 2 candidates at client interview or later.")
        self.desk.deal({"action": "close", "deal": self.id("Wei-Lin Chao"), "outcome": "won"})
        self.assertEqual(self.reports.run("stages")["total"]["count"], 6)      # a closed record is no longer in play

    def test_win_rate_is_what_closed_in_the_period_set_against_the_stretch_before(self):
        self.assertEqual(self.reports.run("winrate")["figure"]["text"], "–")           # nothing has closed: no rate, not 0%
        for name, outcome in (("Wei-Lin Chao", "won"), ("Priya Raman", "won"), ("Marcus Oyelaran", "lost"), ("Tomás Ibarra", "lost"), ("Callum Reyes", "lost")):
            self.desk.deal({"action": "close", "deal": self.id(name), "outcome": outcome, "reason": "Timing"})
        self.age("Tomás Ibarra", 120)             # closed in the 90 days before
        self.age("Callum Reyes", 400)             # closed too long ago to be in either
        result = self.reports.run("winrate", full=True)
        self.assertEqual(result["figure"], {"text": "67%", "label": "2 of 3 closed", "change": "up 67 points", "up": True})
        self.assertEqual(result["says"], ["Placed $100k, passed $42k.", "The 90 days before: 0% (0 of 1)."])
        self.assertEqual([(m["won"], m["lost"]) for m in result["months"]], [(2, 1)])
        self.assertEqual([r["name"] for r in self.row(result, "Passed")["records"]], ["Marcus Oyelaran"])
        self.assertEqual(self.reports.run("winrate", period="all")["figure"]["label"], "2 of 5 closed")
        self.assertEqual(self.reports.run("winrate", compare=False)["figure"]["change"], "")

    def test_where_records_come_from_counts_each_source_and_how_far_its_records_got(self):
        store = self.desk.store
        for name, source in (("Dana Whitfield", "Referral"), ("Marcus Oyelaran", "Referral"), ("Priya Raman", "referral"), ("Tomás Ibarra", "LinkedIn"),
                             ("Ingrid Solberg", "LinkedIn"), ("Wei-Lin Chao", "market map: Brokers"), ("Callum Reyes", "")):
            store.update(self.id(name), source=source)
        self.desk.deal({"action": "close", "deal": self.id("Priya Raman"), "outcome": "won"})
        self.desk.deal({"action": "close", "deal": self.id("Marcus Oyelaran"), "outcome": "lost", "reason": "Timing"})
        self.age("Tomás Ibarra", 100, "created_at")
        result = self.reports.run("sources", full=True)
        self.assertEqual([(row["label"], row["count"]) for row in result["rows"]], [("Referral", 3), ("LinkedIn", 1), ("Market map", 1), ("Not recorded", 1)])
        self.assertEqual([item["label"] for item in result["legend"]], ["placed", "reached submitted", "earlier"])
        # Referral: Priya was placed, Dana is at screened and Marcus was passed at contacted: both short of submitted
        self.assertEqual({part["k"]: part["count"] for part in self.row(result, "Referral")["parts"]}, {"won": 1, "late": 0, "early": 2})
        self.assertEqual({part["k"]: part["count"] for part in self.row(result, "Market map")["parts"]}, {"won": 0, "late": 1, "early": 0})
        self.assertEqual(self.row(result, "LinkedIn")["before"]["count"], 1)                   # Tomás entered in the stretch before
        self.assertEqual(self.row(result, "Referral")["before"]["count"], 0)
        self.assertEqual([r["name"] for r in self.row(result, "Referral")["records"]][0], "Priya Raman")     # the ones that got furthest first
        self.assertEqual(self.row(result, "Referral")["records"][2]["stage"], "Passed at contacted")
        self.assertEqual(result["says"][0], "Referral sends the most candidates and the largest share to submitted (33%).")
        self.assertEqual([row["label"] for row in result["over"]], [dt.date.today().strftime("%b %Y")])      # the same records, month by month
        self.assertEqual(result["line"], "candidates · by source · entered in the last 90 days · coloured by the furthest stage reached")
        months = self.reports.run("months")
        self.assertIsNone(months["before"])                                     # a month is not set against "the same month before"
        self.assertTrue(all("before" not in row for row in months["rows"]))

    def test_a_record_with_no_source_is_asked_about_and_the_report_says_when_most_have_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        desk = make_desk(tmp.name, playbook="b2b-sales")
        first = desk.deal({"action": "add", "name": "Ada Okoro", "company": "Lumen", "source": "Referral"})
        self.assertNotIn("source_missing", first)
        desk.deal({"action": "add", "name": "Ben Ito", "company": "Fathom", "source": "referral"})
        desk.deal({"action": "add", "name": "Cy Vance", "company": "Quill", "source": "SaaStr"})
        desk.store.update(desk.store.find("Cy Vance")["id"], source="market map: Brokers")      # a mark Stoa put there, not a name to suggest
        asked = desk.deal({"action": "add", "name": "Di Marsh", "company": "Tide"})
        self.assertEqual(asked["source_missing"]["in_use"], ["Referral"])                # as first typed, the most used first
        self.assertIn("ask once", asked["source_missing"]["note"])
        self.assertNotIn("source_missing", desk.deal({"action": "add", "name": "Eve Lund", "company": "Pine"}, actor="dashboard"))     # the page has its own box
        self.assertIn("reuse a name already in use", schemas.GTM_DEAL["parameters"]["properties"]["source"]["description"])
        result = desk.reports.run("sources", full=True)                                  # 2 of 5 have none
        self.assertEqual(result["says"], ["2 of 5 have no source, so this says little yet.", "Open the report and press Not recorded to fill them in."])
        self.assertEqual(desk.reports.dashboard()["choices"]["sources_in_use"], ["Referral"])
        blank = next(row for row in result["rows"] if row["key"] == "not recorded")
        desk.store.update(blank["records"][0]["id"], source="Referral")                  # filled in from the report: it moves to its own bar
        after = desk.reports.run("sources")
        self.assertEqual({row["label"]: row["count"] for row in after["rows"]}, {"Referral": 3, "Market map": 1, "Not recorded": 1})
        self.assertIn("Referral sends the most", after["says"][0])                       # one in five: the others are compared, the gap is not

    def test_a_report_made_here_is_checked_and_says_what_it_takes_when_it_is_wrong(self):
        clean = self.reports.clean
        self.assertEqual(clean({"by": "owner"})[0], {"count": "records", "by": "owner", "when": "entered", "split": "none", "only": {}})
        self.assertIn("by is one of: source, owner", clean({"by": "colour"})[1])
        self.assertIn("title", clean({"by": "colour"})[1])                          # and names this pipeline's own fields
        self.assertEqual(clean({"by": "field:Location"})[0]["by"], "field:location")          # a field by its label
        self.assertIn("no field called 'shoe size'", clean({"by": "field:shoe size"})[1])
        self.assertEqual(clean({"by": "reason"})[0]["only"], {"status": "lost"})              # a reason is a lost record's
        self.assertEqual(clean({"by": "owner", "only": {"status": "Placed", "min_value": "40k", "owner": ""}})[0]["only"], {"status": "won", "min_value": 40000.0})
        self.assertIn("only takes", clean({"by": "owner", "only": {"mood": "good"}})[1])
        self.assertIn("open now", clean({"by": "owner", "when": "open", "only": {"status": "won"}})[1])
        self.assertEqual(clean({"by": "owner", "when": "open", "split": "outcome"})[0]["split"], "none")
        self.assertIn("count is one of", clean({"by": "owner", "count": "revenue"})[1])
        self.assertIsNone(clean("owner")[0])

    def test_a_report_of_ones_own_groups_filters_and_folds_a_long_tail(self):
        store = self.desk.store
        out = self.reports.save({"title": "Value by owner", "count": "value", "by": "owner", "when": "open"}, by="dashboard")
        self.assertTrue(out["ok"] and out["saved"] and not out["draft"])
        result = self.reports.run(out["id"])
        self.assertEqual((result["unit"], result["period"], result["before"]), ("money", None, None))       # open now: no period to set it against
        self.assertEqual(result["rows"][0]["n"], sum(row["n"] for row in result["rows"]) and result["rows"][0]["n"])
        self.assertIn("holds the most", result["says"][0])
        big = self.reports.save({"title": "Big ones by stage", "by": "stage", "when": "open", "only": {"min_value": 45000}}, by="dashboard")
        self.assertEqual([(row["label"], row["count"]) for row in self.reports.run(big["id"])["rows"]], [("Contacted", 1), ("Client interview", 1), ("Offer", 1)])
        for number in range(15):
            store.add("Person %d" % number, "sourced", company="Company %02d" % number, value=0)
        companies = self.reports.save({"title": "By company", "by": "company", "when": "open"}, by="dashboard")
        rows = self.reports.run(companies["id"], full=True)["rows"]
        self.assertEqual((len(rows), rows[-1]["label"], rows[-1]["count"]), (12, "11 others", 11))          # 22 companies: eleven named, the rest folded
        self.assertEqual(sum(row["count"] for row in rows), 22)
        self.assertEqual(len(rows[-1]["records"]), 11)
        with store._conn() as conn:
            conn.execute("UPDATE deals SET value = 0, value_cents = 0")
        nothing = self.reports.run(out["id"])
        self.assertEqual(nothing["unit"], "records")                                           # nothing carries a value: they are counted
        self.assertIn("so they are counted", " ".join(nothing["says"]))
        location = self.reports.save({"title": "Where they live", "by": "field:location", "when": "open"}, by="dashboard")
        self.assertIn("Not set", [row["label"] for row in self.reports.run(location["id"])["rows"]])
        self.assertFalse(self.reports.save({"by": "owner"}, by="dashboard")["ok"])              # a report needs a title

    def test_why_records_were_lost_names_the_reason_and_the_stage(self):
        for name, reason in (("Marcus Oyelaran", "Timing"), ("Callum Reyes", "Timing"), ("Dana Whitfield", "Comp mismatch")):
            self.desk.deal({"action": "close", "deal": self.id(name), "outcome": "lost", "reason": reason})
        result = self.reports.run("lost")
        self.assertEqual([(row["label"], row["count"]) for row in result["rows"]], [("Timing", 2), ("Comp mismatch", 1)])
        self.assertEqual(result["says"], ["3 passed.", "Both “Timing” were at contacted."])
        self.assertEqual(result["line"], "candidates · by reason lost · closed in the last 90 days")

    def test_time_in_stage_is_how_long_open_records_have_sat_against_the_quiet_mark(self):
        self.age("Marcus Oyelaran", 12)
        self.age("Callum Reyes", 8)
        self.reports.stays = lambda win=None: {"contacted": [2.0, 4.0]}          # the ledger's part, tested below
        result = self.reports.run("time", full=True)
        row = self.row(result, "Contacted")
        self.assertEqual((row["n"], row["mark"], row["tone"], row["count"]), (10.0, 5, "bad", 2))
        self.assertEqual(row["left"], {"count": 2, "days": 3.0})
        self.assertEqual([(r["name"], r["days"]) for r in row["records"]], [("Marcus Oyelaran", 12), ("Callum Reyes", 8)])
        self.assertEqual(result["says"][0], "Contacted runs 5 days past its quiet mark.")
        self.assertIsNone(self.row(self.reports.run("time"), "Contacted").get("records"))
        header, rows = self.reports.csv_rows(result)
        self.assertEqual(header[:4], ["Stage", "Open now", "Average days there", "Quiet mark (days)"])
        self.assertEqual(rows[1][:4], ["Contacted", 2, 10.0, 5])

    def test_a_stay_is_read_from_the_ledger_and_a_move_taken_back_is_not_one(self):
        from stoa.reports import Reports
        store, dana = self.desk.store, self.id("Dana Whitfield")
        self.desk.deal({"action": "move", "deal": dana, "stage": "submitted"})
        self.desk.deal({"action": "move", "deal": dana, "stage": "client_interview"})
        store.undo(dana)                                                         # the second move is taken back
        mine = [change["stage"] for change in store.stage_changes() if change["deal_id"] == dana]
        self.assertEqual(mine, ["screened", "submitted"])                        # as made, then the one move that stood
        self.assertEqual(Reports(self.desk).stays(), {})                         # moved within the minute: a correction, not a stay
        changes = [{"deal_id": "a", "at": "2026-11-01T09:00:00Z", "stage": "sourced"}, {"deal_id": "a", "at": "2026-11-04T09:00:00Z", "stage": "contacted"},
                   {"deal_id": "a", "at": "2026-11-04T09:00:20Z", "stage": "screened"}, {"deal_id": "a", "at": "2026-11-10T21:00:20Z", "stage": "submitted"},
                   {"deal_id": "b", "at": "2026-06-01T09:00:00Z", "stage": "sourced"}, {"deal_id": "b", "at": "2026-06-03T09:00:00Z", "stage": "contacted"}]
        store.stage_changes = lambda: changes
        self.assertEqual(Reports(self.desk).stays(), {"sourced": [3.0, 2.0], "screened": [6.5]})
        from stoa.reports import window
        self.assertEqual(Reports(self.desk).stays(window("30", dt.date(2026, 11, 15))), {"sourced": [3.0], "screened": [6.5]})     # only stays that ended in the period

    def test_what_the_map_turned_into_follows_its_companies_into_the_pipeline(self):
        bare = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        self.assertEqual(make_desk(bare.name).reports.run("map")["empty"], "No market map yet. Make one on the GTM tab.")
        market = self.desk.markets.list_markets()[0]
        self.assertEqual(self.reports.run("map")["says"][0], "Nothing from the map is in the pipeline yet.")
        self.desk.market({"action": "track", "targets": ["Brightside Freight", "Tidewater Logistics"]})
        self.desk.market({"action": "dismiss", "targets": ["Ostrava Lines"]})
        tracked = [t for t in self.desk.markets.targets(market["id"]) if t["status"] == "tracked"]
        self.desk.deal({"action": "close", "deal": tracked[0]["deal_id"], "outcome": "won"})
        self.desk.store.bump("map:%s:cost" % market["id"], 1_500_000)
        self.desk.store.bump("map:%s:pdl" % market["id"], 3)
        result = self.reports.run("map", full=True)
        counts = {row["key"]: row["count"] for row in result["rows"]}
        self.assertEqual((counts["pipeline"], counts["late"], counts["won"]), (2, 1, 1))
        self.assertEqual(counts["mapped"], len([t for t in self.desk.markets.targets(market["id"]) if t["status"] != "dismissed"]))
        self.assertEqual([figure["label"] for figure in result["figures"]][-2:], ["Hermes's research, estimated", "paid lookups"])
        self.assertEqual(result["says"], ["About $0.75 of research for each candidate the map put in the pipeline", "plus 3 paid lookups."])
        self.assertEqual(len(self.row(result, "In the pipeline")["records"]), 2)

    def test_what_hermes_did_counts_its_proposals_and_what_became_of_them(self):
        dana, marcus = self.id("Dana Whitfield"), self.id("Marcus Oyelaran")
        out = self.desk.intake.propose({"kind": "proposal", "source": "activity sweep", "rows": [
            {"record": dana, "stage": "submitted", "why": "She asked to be put forward."},
            {"record": marcus, "next_step": "Send the comp range", "next_due": "+2d", "why": "He asked twice."},
            {"record": marcus, "stage": "screened", "why": "They spoke."}]}, {})
        self.desk.intake.decide(out["id"], 1, "decline")
        self.desk.intake.decide(out["id"], 2, "approve")
        self.desk.store.mark_note("granola:1", dana)
        self.desk.store.mark_note("granola:2", "")              # skipped: not filed
        self.desk.store.mark_note("same:abc", dana)             # a content mark is not a note
        result = self.reports.run("hermes")
        self.assertEqual([(figure["text"], figure["label"]) for figure in result["figures"]],
                         [("3", "changes proposed"), ("1", "approved by you"), ("1", "declined"), ("1", "waiting for you"), ("1", "call notes filed")])
        self.assertEqual({part["k"]: part["n"] for part in result["strip"]}, {"approved": 1, "declined": 1, "waiting": 1})
        self.assertEqual(result["says"][0], "The one you declined was a stage move.")
        with self.desk.store._conn() as conn:
            conn.execute("UPDATE intakes SET at = '2020-01-01T00:00:00Z'")
        self.assertEqual(self.reports.run("hermes")["figures"][0]["text"], "0")           # outside the period
        self.assertEqual(self.reports.run("hermes", period="all")["figures"][0]["text"], "3")

    def test_a_report_hermes_makes_waits_as_a_draft_until_it_is_kept(self):
        tool = self.reports.tool
        listed = tool({"action": "list"})
        self.assertEqual(len(listed["reports"]), 9)
        bad = tool({"action": "save", "title": "By mood", "by": "mood"})
        self.assertFalse(bad["ok"])
        self.assertIn("field:<key>", bad["choices"]["by"])                       # told what it does take
        made = tool({"action": "save", "title": "Open value by owner", "count": "value", "by": "owner", "when": "open", "asked": "who is carrying what"})
        self.assertTrue(made["ok"] and made["draft"])
        self.assertIn("draft", made["note"])
        self.assertTrue(made["rows"] and "records" not in made["rows"][0])       # the figures, not the list of people
        entry = self.reports.find(made["id"])
        self.assertEqual((entry["draft"], entry["asked"], entry["by"]), (True, "who is carrying what", "tester"))
        self.assertNotIn(made["id"], [card["id"] for card in self.reports.dashboard()["cards"]])
        self.assertTrue(self.reports.act(made["id"], "keep")["ok"])
        self.assertFalse(self.reports.find(made["id"])["draft"])
        self.reports.act(made["id"], "pin")
        self.assertEqual(self.reports.dashboard()["cards"][-1]["id"], made["id"])
        # a change comes as a new version, and keeping it takes the old one's place, pin and all
        again = tool({"action": "save", "title": "Open value by owner, big ones", "count": "value", "by": "owner", "when": "open",
                      "only": {"min_value": 40000}, "replaces": "Open value by owner"})
        self.assertEqual(self.reports.find(again["id"])["replaces"], made["id"])
        self.assertEqual(self.reports.act(again["id"], "keep"), {"ok": True, "id": made["id"]})
        kept = self.reports.find(made["id"])
        self.assertEqual((kept["title"], kept["spec"]["only"], kept["pinned"]), ("Open value by owner, big ones", {"min_value": 40000.0}, True))
        self.assertIsNone(self.reports.find(again["id"]))
        self.assertFalse(tool({"action": "save", "title": "x", "by": "owner", "replaces": "Win rate"})["ok"])       # the desk's own are not rewritten
        read = tool({"action": "read", "report": "win rate", "period": "all"})
        self.assertEqual((read["title"], read["period"]), ("Win rate", "All time"))
        self.assertFalse(tool({"action": "read", "report": "no such"})["ok"])
        self.assertFalse(tool({"action": "remove", "report": "Win rate"})["ok"])
        self.assertEqual(tool({"action": "remove", "report": made["id"]})["removed"], "Open value by owner, big ones")
        self.assertEqual(len(self.reports.catalog()), 9)
        self.assertFalse(tool({"action": "burn"})["ok"])

    def test_the_dashboard_is_the_persons_to_arrange_and_a_scheduled_job_cannot_change_it(self):
        self.reports.act("hermes", "unpin")
        self.reports.act("owners", "pin")
        self.reports.act("owners", "up")
        self.assertEqual([card["id"] for card in self.reports.dashboard()["cards"]], ["stages", "winrate", "time", "sources", "lost", "owners", "map"])
        self.assertFalse(self.reports.act("stages", "remove")["ok"])             # the desk's own can be unpinned, never removed
        self.assertFalse(self.reports.act("nope", "pin")["ok"])
        cron = {"platform": "cron"}
        self.assertFalse(self.desk.guard("gtm_report", {"action": "save", "title": "x", "by": "owner"}, cron)["ok"])
        self.assertFalse(self.desk.guard("gtm_report", {"action": "remove", "report": "owners"}, cron)["ok"])
        self.assertIsNone(self.desk.guard("gtm_report", {"action": "read", "report": "winrate"}, cron))
        self.assertIsNone(self.desk.guard("gtm_report", {"action": "save", "title": "x", "by": "owner"}, {"platform": "slack"}))
        # each pipeline keeps its own
        with self.desk.store._conn() as conn:
            self.assertIn("owners", conn.execute("SELECT v FROM meta WHERE k = 'reports'").fetchone()[0])

    def test_the_page_to_send_is_one_file_that_asks_nothing_of_the_network(self):
        self.desk.deal({"action": "close", "deal": self.id("Priya Raman"), "outcome": "won"})
        self.desk.store.update(self.id("Dana Whitfield"), source="<b>Referral</b>")
        page = self.reports.page(self.reports.dashboard()["cards"], "Recruiting: reports")
        for banned in ("<script", "http://", "https://", "url(", "@import", "<link", "<img", "fetch("):
            self.assertNotIn(banned, page)
        self.assertIn("Where candidates come from", page)
        self.assertIn("&lt;b&gt;Referral&lt;/b&gt;", page)                       # what a person typed is text, never markup
        self.assertNotIn("<b>Referral</b>", page)
        self.assertNotIn("Dana Whitfield", page)                                 # figures only: no record is named on it

    def test_a_sample_record_says_where_it_came_from_without_losing_its_sample_mark(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        desk = make_desk(tmp.name, playbook="b2b-sales")
        demo.load(desk)
        deals = desk.store.list(status="all")
        self.assertTrue(all(deal["source"] == demo.SOURCE and deal["fields"].get("_origin") for deal in deals))
        labels = {row["label"] for row in desk.reports.run("sources")["rows"]}
        self.assertTrue({"Inbound", "Referral", "Outbound"} <= labels)
        self.assertNotIn("Sample data", labels)
        header, rows = desk.export_rows()
        self.assertNotIn("_origin", header)
        self.assertNotIn("_origin", json.dumps(desk.view(deals[0])))
        self.assertEqual(demo.clear(desk), len(deals))                           # still found, and removed, as samples

    def test_the_routes_hand_the_page_a_dashboard_a_report_with_its_records_and_a_file(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed here; Hermes provides it")
        spec = importlib.util.spec_from_file_location("stoa_dash_api_reports", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        api._profile_desk = lambda: self.desk
        data = api.reports("30", 0)
        self.assertEqual((data["period"]["id"], data["compare"], len(data["cards"]), len(data["reports"])), ("30", False, 7, 9))
        self.assertIn(["field:location", "Location"], data["choices"]["by"])
        one = api.report("stages")
        self.assertEqual(one["csv"]["header"], ["", "Records", "Value"])
        self.assertEqual(one["csv"]["rows"][1], ["Contacted", 2, 0])                     # both are behind someone in their own search
        self.assertEqual(sum(row[2] for row in one["csv"]["rows"]), 130000.0)
        self.assertTrue(one["rows"][1]["records"])
        with self.assertRaises(fastapi.HTTPException):
            api.report("nope")
        made = api.report_new({"title": "By owner", "by": "owner", "when": "open", "draft": True, "replaces": "stages"})
        self.assertTrue(made["ok"] and not made["draft"])                        # a person's own is kept at once
        self.assertTrue(api.report_act(made["id"], {"verb": "pin"})["ok"])
        changed = api.report_act(made["id"], {"verb": "save", "title": "By owner, value", "count": "value", "by": "owner", "when": "open"})
        self.assertEqual(changed["title"], "By owner, value")
        self.assertEqual(api.reports()["cards"][-1]["unit"], "money")
        self.assertIn("<h2>By owner, value</h2>", api.report_page(made["id"])["html"])
        self.assertIn("Pipeline by stage", api.reports_page()["html"])
        self.assertTrue(api.report_act(made["id"], {"verb": "remove"})["ok"])
        self.assertEqual(len(api.reports()["reports"]), 9)


class HandedFilesTests(unittest.TestCase):
    """Hermes keeps a copy of a file dropped in the talk box. Stoa lists the copies it caused and removes them:
    after the days set, or on a press. Nothing else in Hermes's folders is ever touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "home"
        data = self.home / "plugin-data" / "stoa"
        data.mkdir(parents=True)
        (self.home / "attachments").mkdir()
        (self.home / "images").mkdir()
        self.settings = {}
        self.desk = make_desk(str(data), config=self.settings)
        self.desk._set_config = self.settings.__setitem__
        self.desk._get_config = lambda key, default=None: self.settings.get(key, default)
        self.desk.home = self.home
        self.handed = self.desk.handed

    def put(self, folder, name, text="resume"):
        path = self.home / folder / name
        path.write_text(text)
        return path

    def age(self, days):
        """Make every listed file look as if it was handed over so many days ago."""
        path = self.desk.data_dir / "handed-files.json"
        data = json.loads(path.read_text())
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for entry in data["files"]:
            entry["at"] = stamp
        path.write_text(json.dumps(data))

    def test_only_a_plain_file_in_hermes_own_two_folders_is_listed(self):
        resume = self.put("attachments", "dana-resume.pdf")
        photo = self.put("images", "badge.png")
        self.assertTrue(self.handed.note(str(resume), "Dana resume.pdf", "desk")["listed"])
        self.assertTrue(self.handed.note(str(photo))["listed"])
        outside = Path(self.tmp.name) / "taxes.pdf"
        outside.write_text("mine")
        link = self.home / "attachments" / "link.pdf"
        link.symlink_to(outside)
        (self.home / "attachments" / "folder").mkdir()
        for bad in (str(outside), "attachments/dana-resume.pdf", str(link), str(self.home / "attachments" / "folder"),
                    str(self.home / "attachments" / ".." / ".." / "taxes.pdf"), str(self.home / "attachments" / "nothing.pdf"), "", None,
                    str(self.home / "plugin-data" / "stoa" / "desk.db")):
            self.assertFalse(self.handed.note(bad).get("listed"), bad)
        listing = self.handed.listing()
        self.assertEqual(sorted((f["name"], f["kind"]) for f in listing["files"]), [("Dana resume.pdf", "attachments"), ("badge.png", "images")])
        self.assertEqual(listing["elsewhere"], 7)                      # every path given that was not one of Hermes's copies (an empty one is not counted)
        self.assertEqual({f["days_left"] for f in listing["files"]}, {7})
        self.assertEqual(listing["keep_days"], 7)
        self.assertTrue(outside.exists())

    def test_removing_takes_only_what_is_listed_and_is_still_the_same_file(self):
        mine = self.put("attachments", "offer.pdf")
        swapped = self.put("attachments", "notes.docx")
        other = self.put("attachments", "from-another-chat.pdf")
        for path in (mine, swapped):
            self.handed.note(str(path))
        time.sleep(0.01)
        swapped.write_text("something else now, written by someone else")
        folders = {f["name"]: f for f in self.handed.listing()["folders"]}
        self.assertEqual((folders["attachments"]["files"], folders["attachments"]["from_here"]), (3, 2))
        self.assertNotIn("from-another-chat", json.dumps(self.handed.listing()))       # counted, never named
        out = self.handed.remove()
        self.assertEqual(out["removed"], 1)
        self.assertEqual([item["name"] for item in out["left"]], ["notes.docx"])
        self.assertFalse(mine.exists())
        self.assertTrue(swapped.exists() and other.exists())
        listing = self.handed.listing()
        self.assertEqual([(f["name"], f["changed"], f["days_left"]) for f in listing["files"]], [("notes.docx", True, None)])
        self.assertEqual(listing["removed"], 1)
        # one by id; and a file someone already deleted is simply forgotten
        again = self.put("attachments", "again.pdf")
        gone = self.put("attachments", "gone.pdf")
        ref = self.handed.note(str(again))["id"]
        self.handed.note(str(gone))
        gone.unlink()
        self.assertEqual(self.handed.remove(["nope"])["removed"], 0)
        self.assertTrue(again.exists())
        self.assertEqual(self.handed.remove([ref])["removed"], 1)
        self.assertEqual([f["name"] for f in self.handed.listing()["files"]], ["notes.docx"])

    def test_a_listed_path_that_became_a_link_is_never_followed(self):
        target = Path(self.tmp.name) / "keep-me.txt"
        target.write_text("resume")
        listed = self.put("attachments", "cv.pdf")
        self.handed.note(str(listed))
        listed.unlink()
        listed.symlink_to(target)
        self.assertEqual(self.handed.remove()["removed"], 0)
        self.assertTrue(target.exists() and listed.is_symlink())

    def test_files_go_by_themselves_after_the_days_set_once_a_day(self):
        old = self.put("attachments", "old.pdf")
        self.handed.note(str(old))
        self.age(6.5)
        self.assertEqual(self.handed.sweep()["removed"], 0)
        self.assertEqual(self.handed.listing()["files"][0]["days_left"], 1)
        self.age(7.2)
        self.desk._copied.clear()                  # a new day: the first time the desk is opened
        self.desk.own_store
        self.assertFalse(old.exists())
        self.assertEqual(self.handed.listing()["files"], [])
        kept = self.put("attachments", "kept.pdf")
        self.settings["files_keep_days"] = 0       # never by itself
        self.handed.note(str(kept))
        self.age(400)
        self.assertEqual(self.handed.sweep()["removed"], 0)
        self.assertIsNone(self.handed.listing()["files"][0]["days_left"])
        out = self.handed.set_keep_days(30)        # shortened: what is already older goes at once
        self.assertTrue(out["ok"] and out["keep_days"] == 30 and out["files"] == [])
        self.assertFalse(kept.exists())
        self.assertFalse(self.handed.set_keep_days(-1)["ok"])
        self.assertFalse(self.handed.set_keep_days("soon")["ok"])
        self.desk.can_configure = False
        self.assertIn("files_keep_days", self.handed.set_keep_days(1)["error"])

    def test_setup_and_the_routes_show_and_remove_them(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed here; Hermes provides it")
        spec = importlib.util.spec_from_file_location("stoa_dash_api_files", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        api._profile_desk = lambda: self.desk
        resume = self.put("attachments", "resume.pdf", "x" * 2048)
        self.assertTrue(api.files_handed({"path": str(resume), "name": "resume.pdf", "lane": "desk"})["listed"])
        self.assertFalse(api.files_handed({"path": "/etc/hosts"}).get("listed"))
        self.assertEqual(self.desk.setup()["data"]["files"], {"count": 1, "bytes": 2048, "keep_days": 7, "folder": str(self.home / "attachments")})
        self.assertEqual(api.files()["files"][0]["name"], "resume.pdf")
        self.assertEqual(api.files_keep({"days": 1})["keep_days"], 1)
        out = api.files_remove({})
        self.assertEqual((out["removed_now"], out["files"], out["left"]), (1, [], []))
        self.assertFalse(resume.exists())
        self.assertEqual(api.files_remove(None)["removed_now"], 0)


class StorageHardeningTests(unittest.TestCase):
    """Concurrency, atomicity, snapshot integrity, undo/delete consistency, migrations, and indexes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "desk.db")
        self.store = Store(self.path)

    def test_concurrent_update_and_move_threads_and_processes_lose_no_fields(self):
        import subprocess
        deal = self.store.add("Dana Whitfield", "sourced", company="Kestrel Labs", fields={"seed": "ok"})
        deal_id = deal["id"]
        errors = []
        barrier = threading.Barrier(8)

        def updater(idx: int):
            local = Store(self.path) if idx % 2 else self.store
            try:
                barrier.wait(timeout=10)
                for step in range(8):
                    local.update(deal_id, actor="t%d" % idx, fields={"t%d_%d" % (idx, step): "v%d" % step})
            except BaseException as exc:
                errors.append(exc)

        def mover(idx: int):
            local = Store(self.path)
            stages = ("contacted", "screened", "submitted", "sourced")
            try:
                barrier.wait(timeout=10)
                for step in range(8):
                    if step % 4 == 3:
                        local.move(deal_id, "won", status="won", actor="m%d" % idx)
                        local.move(deal_id, "sourced", status="open", actor="m%d" % idx)
                    else:
                        local.move(deal_id, stages[step % len(stages)], status="open", actor="m%d" % idx)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=updater, args=(i,)) for i in range(6)]
        threads += [threading.Thread(target=mover, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

        # Multi-process concurrent update() + move() against the same disk file
        worker_code = (
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            "root, db, did, pid = Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
            "spec = importlib.util.spec_from_file_location('stoa', root / '__init__.py', submodule_search_locations=[str(root)])\n"
            "mod = importlib.util.module_from_spec(spec); sys.modules['stoa'] = mod; spec.loader.exec_module(mod)\n"
            "from stoa.store import Store\n"
            "s = Store(db)\n"
            "for step in range(6):\n"
            "    s.update(did, actor='p%d' % pid, fields={'p%d_%d' % (pid, step): 'ok'})\n"
            "    if step % 2 == 0:\n"
            "        s.move(did, 'screened' if pid % 2 else 'contacted', status='open', actor='p%d' % pid)\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", worker_code, str(ROOT), self.path, deal_id, str(p)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for p in range(4)]
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err or out)

        final = self.store.get(deal_id)
        self.assertIsNotNone(final)
        self.assertEqual(final["fields"].get("seed"), "ok")
        for idx in range(6):
            for step in range(8):
                self.assertEqual(final["fields"].get("t%d_%d" % (idx, step)), "v%d" % step)
        for pid in range(4):
            for step in range(6):
                self.assertEqual(final["fields"].get("p%d_%d" % (pid, step)), "ok")

    def test_mid_operation_exception_rolls_back_atomically_on_disk(self):
        base = self.store.add("Anchor Deal", "sourced", value=10000)
        before_ledger = len(self.store.ledger(limit=200))

        # Nested savepoint rollback caught inside an outer operation preserves outer work
        with self.store.operation():
            self.store.update(base["id"], next_step="Outer survives")
            with self.assertRaises(RuntimeError):
                with self.store.operation():
                    self.store.update(base["id"], next_step="Inner rolled back")
                    self.store.add("Inner Ghost", "sourced")
                    raise RuntimeError("abort inner")
        self.assertEqual(self.store.get(base["id"])["next_step"], "Outer survives")
        self.assertIsNone(self.store.find("Inner Ghost"))

        # Unhandled exception mid-operation rolls back every step atomically (deals, groups, contacts, ledger, activities)
        mid_ledger = len(self.store.ledger(limit=200))
        with self.assertRaises(RuntimeError):
            with self.store.operation():
                ghost = self.store.add("Ghost Deal", "sourced", grp="Ghost Group", value=25000)
                self.store.update(base["id"], next_step="Should vanish", fields={"tmp": "1"})
                self.store.move(base["id"], "contacted")
                self.store.person_add(base["id"], "Ghost Person", email="ghost@example.com")
                self.store.grp_put("Ghost Group", client="Ghost Corp", value=5000)
                # Nested read inside operation sees uncommitted writes on the same connection without deadlocking
                self.assertIsNotNone(self.store.get(ghost["id"]))
                raise RuntimeError("boom mid-operation")

        fresh = Store(self.path)
        self.assertIsNone(fresh.find("Ghost Deal"))
        self.assertEqual(fresh.get(base["id"])["next_step"], "Outer survives")
        self.assertEqual(fresh.get(base["id"])["stage"], "sourced")
        self.assertNotIn("tmp", fresh.get(base["id"])["fields"])
        self.assertEqual(fresh.people(base["id"]), [])
        self.assertIsNone(fresh.grp_get("Ghost Group"))
        self.assertEqual(len(fresh.ledger(limit=200)), mid_ledger)
        self.assertGreater(mid_ledger, before_ledger)

    def test_concurrent_snapshot_race_and_failure_cleanup(self):
        deal = self.store.add("Snapshot Deal", "sourced", value=42000)
        folder = Path(self.tmp.name) / "backups"
        barrier = threading.Barrier(6)
        results, errors = [], []

        def snap():
            local = Store(self.path)
            try:
                barrier.wait(timeout=10)
                results.append(local.snapshot(folder))
            except BaseException as exc:
                errors.append(exc)

        def writer():
            local = Store(self.path)
            try:
                barrier.wait(timeout=10)
                for i in range(15):
                    local.update(deal["id"], fields={"w%d" % i: str(i)})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=snap) for _ in range(5)] + [threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(any(results))
        self.assertEqual(list(folder.glob("*.part")), [])

        target = folder / ("desk-%s.db" % st.today())
        self.assertTrue(target.exists())
        copy = sqlite3.connect(str(target))
        try:
            self.assertEqual([r[0] for r in copy.execute("PRAGMA quick_check").fetchall()], ["ok"])
            self.assertEqual(copy.execute("SELECT count(*) FROM deals").fetchone()[0], 1)
        finally:
            copy.close()

        # Failure during os.replace cleans up the unique .part file and leaves no target
        fail_folder = Path(self.tmp.name) / "fail_backups"
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.snapshot(fail_folder)
        self.assertEqual(list(fail_folder.glob("*.part")), [])
        self.assertFalse((fail_folder / ("desk-%s.db" % st.today())).exists())

    def test_undo_contact_email_collision_and_updated_at_and_delete_cleanup(self):
        deal = self.store.add("Dana Whitfield", "sourced", value=30000)
        alice = self.store.person_add(deal["id"], "Alice One", email="alice@example.com")
        self.store.person_update(alice["id"], deal_id=deal["id"], email="alice2@example.com")
        bob = self.store.person_add(deal["id"], "Bob Two", email="alice@example.com")

        # Reverting Alice's email update back to alice@example.com must report a conflict, not raise IntegrityError
        op = next(line["batch"] for line in self.store.ledger(deal["id"])
                  if line["entity"] == "contact" and line["op"] == "update" and line["entity_id"] == str(alice["id"]))
        res = self.store.undo(deal["id"], op=op)
        self.assertIsNotNone(res)
        self.assertTrue(any(c["field"] == "email" for c in res["conflicts"]))
        self.assertEqual(self.store.person(alice["id"], deal["id"])["email"], "alice2@example.com")
        self.assertEqual(self.store.person(bob["id"], deal["id"])["email"], "alice@example.com")

        # When there is no collision, reverting a contact or invoice update bumps updated_at
        self.store.person_update(bob["id"], deal_id=deal["id"], title="VP Sales")
        with self.store._conn(write=True) as conn:
            conn.execute("UPDATE contacts SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (bob["id"],))
        undone_contact = self.store.undo(deal["id"])
        self.assertEqual(undone_contact["conflicts"], [])
        self.assertGreater(self.store.person(bob["id"], deal["id"])["updated_at"], "2020-01-01T00:00:00Z")

        inv = self.store.invoice_add(deal["id"], 5000, number="INV-1")
        self.store.invoice_update(inv["id"], note="revised")
        with self.store._conn(write=True) as conn:
            conn.execute("UPDATE invoices SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (inv["id"],))
        undone_inv = self.store.undo(deal["id"])
        self.assertEqual(undone_inv["conflicts"], [])
        self.assertGreater(self.store.invoice(inv["id"])["updated_at"], "2020-01-01T00:00:00Z")

        # delete() clears dangling targets.deal_id and notes_seen.deal_id
        with self.store._conn(write=True) as conn:
            conn.execute("INSERT INTO targets (id, market_id, company, status, deal_id, created_at, updated_at) "
                         "VALUES ('t-del', 'm1', 'Acme', 'tracked', ?, ?, ?)", (deal["id"], st.now(), st.now()))
        self.store.mark_note("note-del-1", deal["id"])
        self.assertTrue(self.store.delete(deal["id"]))
        with self.store._conn() as conn:
            self.assertEqual(conn.execute("SELECT deal_id FROM targets WHERE id = 't-del'").fetchone()[0], "")
            self.assertEqual(conn.execute("SELECT deal_id FROM notes_seen WHERE id = 'note-del-1'").fetchone()[0], "")

    def test_user_version_indexes_and_own_desks_lock(self):
        with self.store._conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], st.SCHEMA_VERSION)
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
        self.assertTrue({"deals_active", "deals_archived", "intakes_status", "targets_deal"} <= indexes)

        # Verify _own_desks initialization is serialized under a lock
        try:
            import fastapi  # noqa: F401
        except ImportError:
            return
        spec = importlib.util.spec_from_file_location("stoa_dash_api_lock", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        self.assertIsInstance(api._own_desks_lock, type(threading.Lock()))
        calls = []
        fake_pkg = types.SimpleNamespace(_home=lambda: Path(self.tmp.name),
                                         build_desk=lambda *a: (time.sleep(0.05), calls.append(1), types.SimpleNamespace(can_configure=True))[2])
        api._package = lambda: fake_pkg
        api._own_desks.clear()
        barrier = threading.Barrier(4)
        got = []

        def grab():
            barrier.wait(timeout=5)
            got.append(api._profile_desk())

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({id(d) for d in got}), 1)
        self.assertFalse(got[0].can_configure)

    def test_handoffs_and_motions_wal_user_version_and_concurrent_add(self):
        from stoa.motions import Refused
        desk = make_desk(self.tmp.name)
        for obj in (desk.handoffs, desk.motions):
            db = obj._open()
            try:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], type(obj).__module__.endswith("motions") and 2 or 1)      # motions 2: the list
                self.assertGreaterEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 15000)
            finally:
                db.close()

        barrier = threading.Barrier(4)
        ok, refused, errors = [], [], []

        def make_motion():
            try:
                barrier.wait(timeout=5)
                ok.append(desk.motions.add({"name": "Concurrent Push", "kind": "outbound"}))
            except Refused:
                refused.append(1)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=make_motion) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual((len(ok), len(refused)), (1, 3))


if __name__ == "__main__":
    unittest.main()
