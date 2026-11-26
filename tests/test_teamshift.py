"""The fixes this fork carries: mail drafts through the connected app, the script gate, autonomy and channel locks,
no message injection, no hard delete, and neutral text. Run: python3 tests/test_teamshift.py"""
import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stoa import ROOT, make_desk, stoa  # noqa: E402  (loads the package as ``stoa``)
from stoa import blueprints  # noqa: E402
from stoa import desk as desk_module  # noqa: E402

spec = importlib.util.spec_from_file_location("stoa_gate", ROOT / "gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
NOW = dt.datetime.now(dt.timezone.utc)
SKIP = json.dumps({"wakeAgent": False})


class EmailSection(unittest.TestCase):
    def test_drafts_go_through_the_connected_app_never_a_token(self):
        text = (ROOT / "skills" / "desk" / "SKILL.md").read_text()
        email = text[text.index("## Email"):text.index("## Enrichment costs money")]
        self.assertIn("GMAIL_CREATE_EMAIL_DRAFT", email)
        self.assertIn("COMPOSIO_SEARCH_TOOLS", email)
        for banned in ("from_authorized_user_info", "users().drafts().create", "Google Workspace skill", "`python`"):
            self.assertNotIn(banned, email)
        self.assertIn("do not\n   open token files", email)


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")

    def tearDown(self):
        self.tmp.cleanup()

    def run_gate(self, check):
        return gate.run(check, self.data, NOW)

    def test_six_blueprints_are_gated_and_the_script_names_match(self):
        named = {bp["name"]: bp.get("gate") for bp in blueprints.BLUEPRINTS}
        gated = {name for name, script in named.items() if script}
        self.assertEqual(gated, set(gate.GATED))
        self.assertEqual(len(gated), 6)
        self.assertEqual(set(named) - gated, {"sheet-pull"})
        for name in gated:
            self.assertEqual(gate.check_name(["/x/" + named[name]]), name)

    def test_gate_key_is_not_handed_to_the_engine(self):
        seen = []

        class Ctx:
            def register_automation_blueprint(self, key, **fields):
                seen.append(fields)
        source = (ROOT / "__init__.py").read_text()
        self.assertIn('if key not in ("name", "gate")', source)
        for bp in blueprints.BLUEPRINTS:
            Ctx().register_automation_blueprint(bp["name"], **{k: v for k, v in bp.items() if k not in ("name", "gate")})
        self.assertFalse(any("gate" in fields for fields in seen))

    def test_no_desk_means_no_model_call(self):
        for check in gate.GATED:
            self.assertEqual(gate.run(check, self.data / "missing", NOW), SKIP, check)

    def test_empty_desk_skips_every_routine(self):
        self.desk.store.count()      # makes desk.db
        self.assertTrue((self.data / "desk.db").exists())
        for check in ("morning-brief", "notes-sweep", "activity-sweep", "market-watch", "friday-review"):
            self.assertEqual(self.run_gate(check), SKIP, check)

    def test_open_records_wake_the_brief_and_notes(self):
        self.desk.store.add("Ada Lovelace", "lead")
        self.assertIn("1 open records", self.run_gate("morning-brief"))
        self.assertIn("1 open records", self.run_gate("notes-sweep"))
        self.assertIn("changes this week", self.run_gate("friday-review"))
        self.assertEqual(self.run_gate("activity-sweep"), SKIP)     # nobody with an email to match against
        self.desk.store.add("Grace Hopper", "lead", email="grace@example.com")
        self.assertIn("1 open records with an email", self.run_gate("activity-sweep"))

    def test_old_changes_do_not_wake_the_friday_review(self):
        self.desk.store.add("Ada Lovelace", "lead")
        self.assertEqual(gate.run("friday-review", self.data, NOW + dt.timedelta(days=8)), SKIP)

    def test_market_watch_needs_a_map(self):
        self.desk.store.count()
        self.assertEqual(self.run_gate("market-watch"), SKIP)
        self.desk.market({"action": "define", "name": "Payments", "brief": "Card processors.", "segments": ["Payments"]})
        self.desk.market({"action": "add", "companies": [{"company": "Ironbridge", "segment": "Payments", "fit": 4}]})
        self.assertIn("1 market maps", self.run_gate("market-watch"))

    def test_sheet_mirror_wakes_once_per_change(self):
        self.desk.store.add("Ada Lovelace", "lead")
        self.assertNotEqual(self.run_gate("sheet-mirror"), SKIP)
        self.assertEqual(self.run_gate("sheet-mirror"), SKIP)
        self.desk.store.add("Grace Hopper", "lead")
        self.assertNotEqual(self.run_gate("sheet-mirror"), SKIP)

    def test_unreadable_desk_wakes_to_report(self):
        (self.data / "desk.db").write_text("not a database")
        self.assertIn("could not be read", self.run_gate("morning-brief"))

    def test_script_runs_standalone_and_reads_its_name(self):
        scripts = self.data / "scripts"
        scripts.mkdir()
        target = scripts / "stoa_gate_morning-brief.py"
        target.write_text((ROOT / "gate.py").read_text())
        home = self.data / "home"
        (home / "plugin-data" / "stoa").mkdir(parents=True)
        out = subprocess.run([sys.executable, "-I", str(target)], capture_output=True, text=True,
                             env={"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], SKIP)
        bad = subprocess.run([sys.executable, "-I", str(ROOT / "gate.py"), "sheet-pull"], capture_output=True, text=True, timeout=30)
        self.assertEqual(bad.returncode, 2)


class Locks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_autonomy_is_ask_or_routine(self):
        self.assertEqual(set(desk_module.AUTONOMY), {"ask", "routine"})
        d = make_desk(self.tmp.name, playbook="b2b-sales")
        self.assertFalse(d.set_autonomy("full")["ok"])
        self.assertTrue(d.set_autonomy("routine")["ok"])
        d.own_store.meta_set("autonomy", "full")        # a level stored before the lock
        self.assertEqual(d.autonomy(), "ask")

    def test_empty_channels_means_no_chat_and_no_cron(self):
        d = make_desk(self.tmp.name, playbook="b2b-sales")
        for platform in ("", "cli", "tui", "desktop", "local"):
            self.assertTrue(d.allowed({"platform": platform}), platform)
        self.assertFalse(d.allowed({"platform": "slack", "chat_id": "C1"}))
        self.assertFalse(d.allowed({"platform": "cron"}))

    def test_named_channels_and_cron_are_allowed(self):
        d = make_desk(self.tmp.name, playbook="b2b-sales", config={"channels": ["C1", "cron"]})
        self.assertTrue(d.allowed({"platform": "slack", "chat_id": "C1"}))
        self.assertFalse(d.allowed({"platform": "slack", "chat_id": "C2"}))
        self.assertTrue(d.allowed({"platform": "cron"}))

    def test_no_gateway_injection(self):
        source = (ROOT / "__init__.py").read_text()
        self.assertIn("SlackDesk(desk, _no_injection)", source)
        self.assertNotIn("inject_message", source)
        self.assertFalse(stoa._no_injection("text", platform="slack", chat_id="C1"))

    def test_paid_keys_are_not_defaulted(self):
        manifest = (ROOT / "plugin.yaml").read_text()
        for key in ("pdl_api_key", "exa_api_key"):
            block = manifest[manifest.index("  %s:" % key):].split("\n  ", 6)
            self.assertNotIn("default:", "\n".join(block[:5]), key)


class NoHardDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_removed_record_can_only_be_put_back(self):
        deal = self.desk.store.add("Ada Lovelace", "lead")
        self.assertTrue(self.desk.remove(deal["id"])["ok"])
        self.assertFalse(hasattr(self.desk, "destroy"))
        self.assertIsNotNone(self.desk.store.get(deal["id"], archived=True))
        self.assertTrue(self.desk.restore(deal["id"])["ok"])

    def test_the_route_refuses(self):
        api = (ROOT / "dashboard" / "plugin_api.py").read_text()
        route = api[api.index('@router.post("/deal/{deal_id}/destroy")'):][:400]
        self.assertIn("HTTPException(410", route)
        self.assertNotIn(".destroy(", api)
        self.assertNotIn("/destroy'", (ROOT / "desktop" / "plugin.js").read_text())

    def test_a_removed_map_is_kept_in_trash(self):
        self.desk.market({"action": "define", "name": "Payments", "brief": "Card processors.", "segments": ["Payments"]})
        added = self.desk.market({"action": "add", "companies": [{"company": "Ironbridge", "segment": "Payments", "fit": 4}]})
        self.assertTrue(added["ok"], added)
        market_id = self.desk.markets.list_markets()[0]["id"]
        self.assertTrue(self.desk.markets.remove(market_id)["ok"])
        kept = list((Path(self.tmp.name) / "trash").glob("map-*.json"))
        self.assertEqual(len(kept), 1)
        snapshot = json.loads(kept[0].read_text())
        self.assertEqual(snapshot["market"][0]["id"], market_id)
        self.assertEqual([t["company"] for t in snapshot["targets"]], ["Ironbridge"])


class NeutralText(unittest.TestCase):
    RUNTIME = ["*.py", "dashboard/plugin_api.py", "dashboard/dist/index.js", "desktop/plugin.js", "skills/*/SKILL.md",
               "playbooks/*.yaml", "plugin.yaml"]

    def test_no_engine_name_in_runtime_text(self):
        hits = []
        for pattern in self.RUNTIME:
            for path in ROOT.glob(pattern):
                for n, line in enumerate(path.read_text().splitlines(), 1):
                    if re.search(r"\bHermes\b", line) and "Hermes (scheduled)" not in line:
                        hits.append("%s:%d" % (path.relative_to(ROOT), n))
        self.assertEqual(hits, [])

    def test_shared_pages_name_no_product(self):
        page = (ROOT / "page.py").read_text()
        footer = page[page.index("<footer>"):][:120]
        self.assertNotIn("Stoa", footer)
        self.assertNotIn("Hermes", footer)
        reports = (ROOT / "reports.py").read_text()
        self.assertIn("<footer>Counted from the desk's own records", reports)

    def test_scheduled_actor_is_neutral_and_old_entries_still_count(self):
        self.assertEqual(stoa.SCHEDULED, "Assistant (scheduled)")
        self.assertIn('("Assistant (scheduled)", "Hermes (scheduled)")', (ROOT / "reports.py").read_text())


if __name__ == "__main__":
    unittest.main()
