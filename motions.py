"""Motions: the plays a team runs to break into a market, and what each one brought in.

A motion is one go-to-market play: an outbound push, an event, a run of digital ads, a piece of content, a
partnership. It is aimed at a market map (all of it, or some of its segments), so the map says who the motion is
for. What it did is tracked three ways:

* **Results** are numbers someone logs as the motion runs: what was spent, how many people it reached, how many
  answered, how many meetings came of it.
* **Sourced** records are the ones the motion brought into the pipeline.
* **Influenced** records are the ones it helped along without bringing in.

Motions belong to the desk as a whole, not to one pipeline. A motion reads every pipeline at once, so one that
brings leads into a lead pipeline is still credited when those leads are handed over and become deals somewhere
else: a lead and the deal it became count as one piece of business, and it is the deal's value and outcome that
are counted.

Each motion keeps a **list**: who it is being run on. Anyone can be put on it, wherever they came from: companies
picked on a market map (each is added as a lead as it is put on the list: in the desk's lead pipeline where the map
is kept with a pipeline of deals, and it becomes a deal when it is handed over), records already in any pipeline, or
one person on a record. A map company goes into a pipeline as it is put on the list, because there is no other place
to keep it; anyone else on the list is a record already. Each one on the list has a status: added, reached, replied,
meeting. A call or email logged on the record after it was added counts as reached, and a meeting as a meeting,
without anyone saying so; replied is said by a person or the agent.

What a motion is credited with follows the list, and Stoa says why each time:

1. A record on the list that being put on it brought into the pipeline (a company picked on the map, or a new lead
   added there) was sourced by the motion. One that was already in a pipeline when it was added was influenced.
2. A record off the list whose Source names the motion, or one of the words the motion watches for, was sourced by it.
3. So was one with a note, call or email that mentions it, if the record came in while the motion ran; one that was
   already in a pipeline is influenced.
4. A deal that was handed over from a lead the motion is credited with is credited the same way.

Being at a company on the map the motion is aimed at is not enough: the map says who a motion is for, and its
companies are put on the list by picking them. Where two motions would both be credited with bringing in one record,
the list wins over a mention, and between two mentions the motion that started last before the record came in.

A person can overrule any of it: confirm a record, change sourced to influenced, or set a record aside so the
motion never claims it again. What a person says always wins over what Stoa worked out.

Records and maps stay where they are, in the desk's own ledgers; this module keeps only the motions, in
``motions.db`` beside them (and a separate set for a tour's sample data). A record is named here by the pipeline it
is in and its id, ``p2:dana-whitfield``, because two pipelines can each have a record with the same id.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from typing import Any, Optional

from .store import _statements


KINDS = {"outbound": "Outbound", "event": "Event", "ads": "Digital ads", "content": "Content", "partner": "Partnership",
         "referral": "Referral", "community": "Community", "other": "Other"}
STATUSES = {"idea": "Idea", "planned": "Planned", "running": "Running", "done": "Done", "stopped": "Stopped"}
METRICS = {"spend": "Spent", "reach": "Reached", "responses": "Responses", "meetings": "Meetings"}
STAGES = {"added": "Added", "reached": "Reached", "replied": "Replied", "meeting": "Meeting"}      # one on a motion's list, in order
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}
# How one came onto a list. Only "map" and "new" brought a record into the pipeline: the motion sourced it.
VIA = {"map": "from the map", "new": "as a new lead", "record": "from the pipeline", "agent": "by the agent"}
BROUGHT = ("map", "new")
MOST_AT_ONCE = 50           # put on a list in one go
FIELDS = ("name", "kind", "status", "goal", "market", "segments", "owner", "start_on", "end_on", "budget", "tags")
DESK = "desk"           # the scope of a desk's own motions; a tour's are kept under its sample file's name
GRACE = 30              # days after a motion ends in which a record coming in is still put down to it
WATCH_MIN = 4           # a watched word shorter than this would match far too much
SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "plc", "sa", "ag", "bv", "the"}
TOUCH_KINDS = ("note", "call", "email", "meeting", "task")      # what a person or an agent logs; stage moves are kept apart
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA_VERSION = 2          # 2: the list (members)

SCHEMA = """
CREATE TABLE IF NOT EXISTS motions (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other', status TEXT NOT NULL DEFAULT 'idea',
  goal TEXT NOT NULL DEFAULT '', market TEXT NOT NULL DEFAULT '', market_pipeline TEXT NOT NULL DEFAULT '', segments TEXT NOT NULL DEFAULT '[]',
  owner TEXT NOT NULL DEFAULT '', start_on TEXT NOT NULL DEFAULT '', end_on TEXT NOT NULL DEFAULT '', budget REAL NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, motion TEXT NOT NULL, on_date TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS results_motion ON results (motion, on_date);
CREATE TABLE IF NOT EXISTS links (motion TEXT NOT NULL, deal TEXT NOT NULL, how TEXT NOT NULL DEFAULT 'sourced', created_at TEXT NOT NULL,
  PRIMARY KEY (motion, deal));
CREATE TABLE IF NOT EXISTS members (motion TEXT NOT NULL, deal TEXT NOT NULL, person TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'added',
  via TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (motion, deal));
"""

TOOL = {
    "name": "gtm_motion",
    "description": (
        "Go-to-market motions (an outbound push, an event, ads, content, a partnership) on the GTM tab. Each keeps a list of who it is run "
        "on, and Stoa credits it, across every pipeline, with what that list and any mention of it brought in, saying why. Don't redo its sums. "
        "Actions: list, get (one motion: its list, records and impact), add (a suggestion takes status idea), update, enroll (put on its list: "
        "`deals`, records in any pipeline; or `companies` from its map, each added as a lead), mark (one on the list: `status`), drop (off the "
        "list), log (spend, reach, responses, meetings, or your own `metric`), link (credit a record by hand, `how`), unlink, remove."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "add", "update", "enroll", "mark", "drop", "log", "link", "unlink", "remove"]},
            "motion": {"type": "string", "description": "Its name or id."},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "status": {"type": "string", "description": "add/update: " + ", ".join(STATUSES) + ". mark: " + ", ".join(STAGES) + "."},
            "goal": {"type": "string"},
            "market": {"type": "string", "description": "The market map it is aimed at (who it is for)."},
            "segments": {"type": "array", "items": {"type": "string"}},
            "owner": {"type": "string"},
            "start_on": {"type": "string", "description": "YYYY-MM-DD"},
            "end_on": {"type": "string", "description": "YYYY-MM-DD"},
            "budget": {"type": "number"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Words that credit a record whose Source or notes mention one."},
            "deals": {"type": "array", "items": {"type": "string"}, "description": "enroll: records, by name or id."},
            "companies": {"type": "array", "items": {"type": "string"}, "description": "enroll: companies on the motion's map."},
            "person": {"type": "string", "description": "enroll: who on the record it is aimed at."},
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "on": {"type": "string", "description": "log: YYYY-MM-DD."},
            "note": {"type": "string"},
            "deal": {"type": "string", "description": "mark/drop/link/unlink: the record."},
            "how": {"type": "string", "enum": ["sourced", "influenced", "none"]},
            "pipeline": {"type": "string", "description": "The record's pipeline, when two share a name."},
        },
        "required": ["action"],
    },
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def company_key(name: Any) -> str:
    """A company's name as two spellings of it would both come out: "Calder Bank, Inc." and "calder bank" are one."""
    words = [word for word in re.sub(r"[^0-9a-z]+", " ", str(name or "").casefold()).split() if word not in SUFFIXES]
    return " ".join(words)


def brought_via(key: str, brought: Any, via: str) -> str:
    """How one came onto a list: "map" or "new" where the adding made the record (``brought`` maps those keys), else ``via``."""
    return brought.get(key, via) if isinstance(brought, dict) else via


def domain_of(address: Any) -> str:
    """The part of an email address after the @, without a leading www."""
    return str(address or "").strip().lower().rpartition("@")[2].removeprefix("www.")


class Refused(Exception):
    """A request that cannot be done, in words for whoever asked."""


class Motions:
    """The desk's motions, kept in ``motions.db`` in the desk's folder. The page (in the dashboard's process) and the
    agent (in the gateway's) both use the file, so each read or write opens it afresh and waits its turn."""

    def __init__(self, desk):
        self.desk = desk
        self.path = desk.data_dir / "motions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                        for stmt in _statements(SCHEMA):
                            db.execute(stmt)
                        have = {row["name"] for row in db.execute("PRAGMA table_info(motions)")}
                        for column in ("tags TEXT NOT NULL DEFAULT '[]'", "market_pipeline TEXT NOT NULL DEFAULT ''"):      # a file from an earlier version
                            if column.split()[0] not in have:
                                try:
                                    db.execute("ALTER TABLE motions ADD COLUMN " + column)
                                except sqlite3.OperationalError as exc:      # another process added it a moment ago
                                    if "duplicate column" not in str(exc).lower():
                                        raise
                        db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
        self._gather()

    def pairs(self) -> dict:
        """{a lead's key: the key of the deal it was handed over as} (handoff.py)."""
        return self.desk.handoffs.pairs()

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _tx(self):
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            else:
                if db.in_transaction:
                    db.execute("COMMIT")
        finally:
            db.close()

    def _all(self, sql: str, args: tuple = ()) -> list:
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def _run(self, sql: str, args: tuple = ()) -> None:
        with self._tx() as db:
            db.execute(sql, args)

    def _gather(self) -> None:
        """Motions were first kept one set to a pipeline. Bring any such sets together as the desk's own: each keeps
        its map by remembering which pipeline the map is in, and its hand-made links by naming that pipeline too."""
        if not self._all("SELECT 1 FROM motions WHERE scope != ? AND scope NOT LIKE 'tour-%' LIMIT 1", (DESK,)):
            return
        pipes = self.desk.pipelines()
        files = {entry.get("file"): entry["id"] for entry in pipes}
        with self._tx() as db:
            taken = {fold(row["name"]) for row in db.execute("SELECT name FROM motions WHERE scope = ? AND removed = 0", (DESK,)).fetchall()}
            for row in db.execute("SELECT id, scope, name FROM motions WHERE scope != ? AND scope NOT LIKE 'tour-%'", (DESK,)).fetchall():
                pipeline = files.get(row["scope"], "main")
                name = row["name"]
                if fold(name) in taken:
                    name = "%s (%s)" % (name, next((entry["name"] for entry in pipes if entry["id"] == pipeline), pipeline))
                taken.add(fold(name))
                db.execute("UPDATE motions SET scope = ?, market_pipeline = ?, name = ? WHERE id = ?", (DESK, pipeline, name, row["id"]))
                for link in db.execute("SELECT deal FROM links WHERE motion = ? AND deal NOT LIKE '%:%'", (row["id"],)).fetchall():
                    db.execute("UPDATE OR REPLACE links SET deal = ? WHERE motion = ? AND deal = ?", (pipeline + ":" + link["deal"], row["id"], link["deal"]))

    # -- which set of motions is in hand, and the pipelines they read ---------------------------------------------
    def scope(self) -> str:
        """The desk's own motions, or a tour's: sample data keeps a set of its own."""
        tour = self.desk.tour()
        return str(tour["file"]) if tour else DESK

    def books(self) -> list:
        """The pipelines a motion reads: every one on the desk, or the single sample one during a tour.
        ``[(id, name)]``; a tour's has the id ''."""
        if self.desk.tour():
            return [("", (self.desk.current() or {}).get("name") or "Sample desk")]
        return [(entry["id"], entry["name"]) for entry in self.desk.pipelines()] or [("main", "Pipeline")]

    def _row(self, row: dict) -> dict:
        for key in ("segments", "tags"):
            try:
                row[key] = [str(item) for item in json.loads(row.get(key) or "[]")]
            except ValueError:
                row[key] = []
        return row

    def rows(self) -> list:
        return [self._row(row) for row in self._all("SELECT * FROM motions WHERE scope = ? AND removed = 0 ORDER BY "
                                                    "CASE status WHEN 'running' THEN 0 WHEN 'planned' THEN 1 WHEN 'idea' THEN 2 WHEN 'done' THEN 3 ELSE 4 END, "
                                                    "start_on DESC, created_at DESC", (self.scope(),))]

    def find(self, ref: Any) -> dict:
        wanted, rows = fold(ref), self.rows()
        if not wanted:
            raise Refused("Say which motion: its name or its id.")
        exact = [row for row in rows if row["id"] == str(ref).strip() or fold(row["name"]) == wanted]
        close = exact or [row for row in rows if wanted in fold(row["name"])]
        if not close:
            raise Refused("No motion matches '%s'. There %s: %s." % (ref, "is" if len(rows) == 1 else "are", ", ".join(row["name"] for row in rows) or "none yet"))
        if len(close) > 1:
            raise Refused("More than one motion matches '%s': %s. Say which." % (ref, ", ".join(row["name"] for row in close)))
        return close[0]

    # -- the maps a motion can be aimed at -------------------------------------------------------------------
    def maps(self) -> list:
        """Every market map on the desk, whichever pipeline it was made in."""
        out = []
        books = self.books()
        for pipeline, name in books:
            with self.desk.on(pipeline):
                for market in self.desk.markets.list_markets():
                    summary = self.desk.markets.summary(market["id"], text=False)
                    out.append({"id": market["id"], "key": "%s:%s" % (pipeline, market["id"]), "name": market["name"], "pipeline": pipeline,
                                "pipeline_name": name if len(books) > 1 else "", "companies": summary["numbers"].get("companies", 0),
                                "segments": [{"name": segment["segment"], "companies": segment["count"], "in_pipeline": segment["in_pipeline"]}
                                             for segment in summary["segments"]]})
        return out

    def _market(self, ref: Any) -> tuple:
        """(the pipeline the map is in, its id) for the map named, or ('', '') for none."""
        wanted = fold(ref)
        if not wanted:
            return "", ""
        maps = self.maps()
        found = [one for one in maps if str(ref).strip() in (one["id"], one["key"]) or fold(one["name"]) == wanted] or [one for one in maps if wanted in fold(one["name"])]
        if not found and ":" in str(ref):          # "pipeline:id" as a page writes it, where the first pipeline may be '' or 'main'
            pipe, _, market_id = str(ref).strip().partition(":")
            found = [one for one in maps if one["id"] == market_id and (one["pipeline"] == pipe or {one["pipeline"], pipe} <= {"", "main"})]
        if len(found) != 1:
            raise Refused("%s market map matches '%s'. The maps are: %s." % ("More than one" if found else "No", ref, ", ".join(one["name"] for one in maps) or "none yet"))
        return found[0]["pipeline"], found[0]["id"]

    def audience(self, motion: dict) -> dict:
        """Who the motion is for: the companies of its map, narrowed to its segments."""
        if not motion["market"]:
            return {"market": "", "name": "", "segments": [], "companies": []}
        try:
            with self.desk.on(motion.get("market_pipeline") or ""):
                summary = self.desk.markets.summary(motion["market"], text=False)
        except Exception:
            return {"market": motion["market"], "name": "", "segments": motion["segments"], "companies": [], "gone": True}
        chosen = {fold(name) for name in motion["segments"]}
        companies = [{"id": company["id"], "company": company["company"], "domain": company.get("domain") or "", "segment": company["segment"],
                      "status": company["status"], "fit": company.get("fit"), "signal": company.get("signal") or ""}
                     for segment in summary["segments"] if not chosen or fold(segment["segment"]) in chosen for company in segment["companies"]]
        return {"market": motion["market"], "name": summary["market"]["name"], "segments": motion["segments"], "companies": companies}

    # -- writing -------------------------------------------------------------------------------------------
    def _clean(self, fields: dict, current: Optional[dict] = None) -> dict:
        out: dict = {}
        for key in FIELDS:
            if key not in fields or fields[key] is None:
                continue
            value = fields[key]
            if key == "name":
                value = " ".join(str(value).split())[:120]
                if not value:
                    raise Refused("A motion needs a name.")
            elif key == "kind":
                value = str(value).strip().lower()
                if value not in KINDS:
                    raise Refused("A motion's kind is one of: %s." % ", ".join(KINDS))
            elif key == "status":
                value = str(value).strip().lower()
                if value not in STATUSES:
                    raise Refused("A motion's status is one of: %s." % ", ".join(STATUSES))
            elif key == "market":
                out["market_pipeline"], value = self._market(value)
            elif key == "segments":
                value = [" ".join(str(item).split()) for item in (value if isinstance(value, (list, tuple)) else [value]) if str(item).strip()]
            elif key == "tags":
                items = value if isinstance(value, (list, tuple)) else re.split(r"[,;\n]", str(value))
                value = list(dict.fromkeys(" ".join(str(item).split()) for item in items if len(" ".join(str(item).split())) >= WATCH_MIN))[:12]
            elif key in ("start_on", "end_on"):
                value = str(value).strip()
                if value and not DATE.match(value):
                    raise Refused("A date is written YYYY-MM-DD.")
            elif key == "budget":
                try:
                    value = max(0.0, float(value or 0))
                except (TypeError, ValueError):
                    raise Refused("A budget is a number.")
            else:
                value = str(value).strip()[:2000]
            out[key] = value
        market = out.get("market", (current or {}).get("market", ""))
        where = out.get("market_pipeline", (current or {}).get("market_pipeline", ""))
        if "segments" in out and out["segments"]:
            if not market:
                raise Refused("Say which market map the segments belong to.")
            known = {fold(segment["name"]): segment["name"] for one in self.maps() if one["id"] == market and one["pipeline"] == where for segment in one["segments"]}
            missing = [name for name in out["segments"] if fold(name) not in known]
            if missing:
                raise Refused("That map has no segment called %s. Its segments are: %s." % (", ".join(missing), ", ".join(known.values()) or "none"))
            out["segments"] = [known[fold(name)] for name in out["segments"]]
        if "market" in out and not out["market"]:
            out["segments"] = []
        start, end = out.get("start_on", (current or {}).get("start_on", "")), out.get("end_on", (current or {}).get("end_on", ""))
        if start and end and end < start:
            raise Refused("A motion cannot end before it starts.")
        return out

    @staticmethod
    def _stored(clean: dict) -> list:
        return [json.dumps(clean[key]) if key in ("segments", "tags") else clean[key] for key in clean]

    def add(self, fields: dict) -> dict:
        clean = self._clean(dict(fields, name=fields.get("name") or ""))
        motion_id, stamp = "m" + uuid.uuid4().hex[:8], now()
        clean.setdefault("kind", "other")
        clean.setdefault("status", "idea")
        columns = ["id", "scope", "created_at", "updated_at"] + list(clean)
        scope = self.scope()
        with self._tx() as db:
            existing = db.execute("SELECT name FROM motions WHERE scope = ? AND removed = 0", (scope,)).fetchall()
            if any(fold(row["name"]) == fold(clean["name"]) for row in existing):
                raise Refused("There is already a motion called %s." % clean["name"])
            db.execute("INSERT INTO motions (%s) VALUES (%s)" % (", ".join(columns), ", ".join("?" * len(columns))),
                       tuple([motion_id, scope, stamp, stamp] + self._stored(clean)))
        return self.find(motion_id)

    def update(self, ref: Any, fields: dict) -> dict:
        motion = self.find(ref)
        clean = self._clean(fields, motion)
        if clean:
            scope = self.scope()
            with self._tx() as db:
                if "name" in clean:
                    existing = db.execute("SELECT id, name FROM motions WHERE scope = ? AND removed = 0", (scope,)).fetchall()
                    if any(fold(row["name"]) == fold(clean["name"]) and row["id"] != motion["id"] for row in existing):
                        raise Refused("There is already a motion called %s." % clean["name"])
                db.execute("UPDATE motions SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % key for key in clean),
                           tuple(self._stored(clean)) + (now(), motion["id"]))
        return self.find(motion["id"])

    def remove(self, ref: Any) -> dict:
        motion = self.find(ref)
        self._run("UPDATE motions SET removed = 1, updated_at = ? WHERE id = ?", (now(), motion["id"]))
        return motion

    def log(self, ref: Any, metric: Any, value: Any, on: Any = "", note: Any = "") -> dict:
        motion = self.find(ref)
        name = " ".join(str(metric or "").split()).lower()[:40]
        name = {"spent": "spend", "cost": "spend", "reached": "reach", "response": "responses", "replies": "responses", "meeting": "meetings"}.get(name, name)
        if not name:
            raise Refused("Say what the number is: spend, reach, responses, meetings, or a name of your own.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise Refused("The value is a number.")
        day = str(on or "").strip() or dt.date.today().isoformat()
        if not DATE.match(day):
            raise Refused("A date is written YYYY-MM-DD.")
        self._run("INSERT INTO results (motion, on_date, metric, value, note, created_at) VALUES (?,?,?,?,?,?)",
                  (motion["id"], day, name, number, str(note or "").strip()[:300], now()))
        return motion

    def unlog(self, ref: Any, result_id: Any) -> dict:
        motion = self.find(ref)
        self._run("DELETE FROM results WHERE id = ? AND motion = ?", (int(result_id), motion["id"]))
        return motion

    def _record(self, ref: Any, pipeline: Any = "") -> tuple:
        """(key, the record) for a record named by its key, its id or its name, looked for in every pipeline."""
        wanted = str(ref or "").strip()
        books = self.books()
        if fold(pipeline):
            books = [book for book in books if fold(book[1]) == fold(pipeline) or book[0] == str(pipeline).strip()] or books
        found = []
        for pid, name in books:
            with self.desk.on(pid):
                local = wanted[len(pid) + 1:] if wanted.startswith(pid + ":") else wanted
                try:
                    deal = self.desk.store.get(local) or (self.desk.store.find(local) if local == wanted else None)
                except Exception as exc:        # several records in that pipeline match: the ledger says which
                    raise Refused(str(exc))
            if deal is not None:
                found.append(("%s:%s" % (pid, deal["id"]), deal, name))
        if not found:
            raise Refused("No record on the desk matches '%s'." % wanted)
        if len(found) > 1:
            exact = [one for one in found if one[0] == wanted]
            if len(exact) != 1:
                raise Refused("'%s' is in more than one pipeline: %s. Say which with `pipeline`." % (wanted, ", ".join(one[2] for one in found)))
            found = exact
        return found[0][0], found[0][1]

    def link(self, ref: Any, deal_ref: Any, how: Any = "sourced", pipeline: Any = "") -> tuple:
        motion = self.find(ref)
        how = str(how or "sourced").strip().lower()
        how = {"not": "none", "no": "none", "dismiss": "none", "aside": "none"}.get(how, how)
        if how not in ("sourced", "influenced", "none"):
            raise Refused("A record is linked as sourced or influenced, or set aside with none.")
        key, deal = self._record(deal_ref, pipeline)
        self._run("INSERT INTO links (motion, deal, how, created_at) VALUES (?,?,?,?) ON CONFLICT(motion, deal) DO UPDATE SET how = excluded.how",
                  (motion["id"], key, how, now()))
        return motion, deal

    def unlink(self, ref: Any, deal_ref: Any, pipeline: Any = "") -> dict:
        motion = self.find(ref)
        try:
            key, _ = self._record(deal_ref, pipeline)
        except Refused:
            key = str(deal_ref or "")           # the record itself is gone: forget what was said about it all the same
        self._run("DELETE FROM links WHERE motion = ? AND deal = ?", (motion["id"], key))
        return motion

    # -- the list: who a motion is being run on ---------------------------------------------------------------
    def enroll(self, ref: Any, deals: Any, pipeline: Any = "", person: Any = "", via: str = "record", brought: Any = ()) -> dict:
        """Put records on a motion's list. ``deals`` is one record or several, by key, id or name, in any pipeline.
        Each is added once: one already on the list keeps the day it was added. ``{motion, added, already, unknown}``."""
        motion = self.find(ref)
        refs = [one for one in (deals if isinstance(deals, (list, tuple)) else [deals]) if str(one or "").strip()]
        if not refs:
            raise Refused("Say who to put on the list: a record by name, or companies from the map.")
        if len(refs) > MOST_AT_ONCE:
            raise Refused("Put up to %d on a list at a time." % MOST_AT_ONCE)
        found, unknown = [], []
        for one in refs:
            try:
                key, deal = self._record(one, pipeline)
            except Refused as exc:
                unknown.append("%s (%s)" % (one, exc) if "more than one pipeline" in str(exc) else str(one))
                continue
            found.append((key, deal))
        who = " ".join(str(person or "").split())[:120]
        added, already, stamp = [], [], now()
        via = via if via in VIA and via not in BROUGHT else "record"
        brought = dict(brought) if isinstance(brought, dict) else {}      # the keys this adding made, and how: the motion brought them in
        with self._tx() as db:
            for key, deal in found:
                done = db.execute("INSERT OR IGNORE INTO members (motion, deal, person, status, via, added_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                  (motion["id"], key, who if len(found) == 1 else "", "added", brought_via(key, brought, via), stamp, stamp)).rowcount
                (added if done else already).append(deal["name"])
                if not done and who and len(found) == 1:
                    db.execute("UPDATE members SET person = ?, updated_at = ? WHERE motion = ? AND deal = ?", (who, stamp, motion["id"], key))
        return {"motion": motion, "added": added, "already": already, "unknown": unknown}

    def enroll_from_map(self, ref: Any, companies: Any, map_ref: Any = "", actor: str = "") -> dict:
        """Put companies from a market map on a motion's list. Each is added to the pipeline first, the way Add to pipeline
        adds it: as a lead in the desk's lead pipeline where the map is kept with a pipeline of deals (it becomes a deal when
        it is handed over), else at the first stage of the map's own pipeline. One already in a pipeline keeps its record."""
        motion = self.find(ref)
        refs = [str(one).strip() for one in (companies if isinstance(companies, (list, tuple)) else [companies]) if str(one or "").strip()]
        if not refs:
            raise Refused("Say which companies on the map to put on the list.")
        if len(refs) > 25:
            raise Refused("Put up to 25 companies from a map on a list at a time.")
        pipe, market_id = self._market(map_ref) if fold(map_ref) else (motion.get("market_pipeline") or "", motion["market"])
        if not market_id:
            raise Refused("This motion is not aimed at a map. Say which map the companies are on with `market`.")
        made, unknown, fresh = [], [], set()
        book = pipe or ("" if self.desk.tour() else "main")         # the pipeline's id as the list names records (books())
        with self.desk.on(pipe):
            ids = []
            for one in refs:
                target = self.desk.markets.target(one, market_id)
                if target is None:
                    unknown.append(one)
                elif target["status"] == "tracked" and target.get("deal_id") and self.desk.markets.record_of(target):
                    made.append("%s:%s" % ((target.get("detail") or {}).get("deal_pipeline") or book, target["deal_id"]))     # already in a pipeline: that record
                else:
                    ids.append(target["id"])
            if ids:
                tracked = self.desk.markets.track_many(ids, actor or self.desk.actor() or "Stoa")
                for row in tracked.get("added") or []:            # in a lead pipeline, where it went there
                    key = "%s:%s" % (row.get("pipeline") or book, row["deal"])
                    made.append(key)
                    if not row.get("existing"):
                        fresh.add(key)
        if not made:
            raise Refused("None of those is on the map%s." % (" (%s)" % ", ".join(unknown) if unknown else ""))
        out = self.enroll(motion["id"], made, via="record", brought={key: "map" for key in fresh})
        out["unknown"] = unknown + out["unknown"]
        return out

    def new_lead(self, ref: Any, name: Any, company: Any = "", actor: str = "") -> dict:
        """Someone found anywhere but the map, put on a motion's list as a new lead: in the desk's lead pipeline, where
        it has one (it becomes a deal when it is handed over), else the first pipeline at its first stage."""
        motion = self.find(ref)
        name, company = " ".join(str(name or "").split())[:120], " ".join(str(company or "").split())[:120]
        if not name:
            raise Refused("A new lead needs a name.")
        pipe = self.desk.lead_pipeline_for("") if not self.desk.tour() else ""
        with self.desk.on(pipe):
            made = self.desk.deal({"action": "add", "name": name, "company": company}, actor=actor or self.desk.actor() or "Stoa")
            if not made.get("ok"):
                raise Refused(made.get("error") or "The lead could not be added.")
            deal = made["deal"] if isinstance(made["deal"], dict) else self.desk.store.get(made["deal"])
        book = pipe or ("" if self.desk.tour() else "main")
        key = "%s:%s" % (book, deal["id"])
        return self.enroll(motion["id"], [key], via="record", brought={key: "new"})

    def _member(self, motion: dict, deal_ref: Any, pipeline: Any = "") -> str:
        try:
            key, _ = self._record(deal_ref, pipeline)
        except Refused:
            key = str(deal_ref or "")           # the record itself is gone: it can still come off the list
        if not self._all("SELECT 1 FROM members WHERE motion = ? AND deal = ?", (motion["id"], key)):
            raise Refused("'%s' is not on the list of %s." % (deal_ref, motion["name"]))
        return key

    def mark(self, ref: Any, deal_ref: Any, status: Any, pipeline: Any = "") -> dict:
        """Say how far one on the list has got: added, reached, replied, meeting."""
        motion = self.find(ref)
        status = {"contacted": "reached", "sent": "reached", "answered": "replied", "responded": "replied", "booked": "meeting", "meetings": "meeting"}.get(
            fold(status), fold(status))
        if status not in STAGES:
            raise Refused("One on a list is %s." % ", ".join(STAGES))
        self._run("UPDATE members SET status = ?, updated_at = ? WHERE motion = ? AND deal = ?", (status, now(), motion["id"], self._member(motion, deal_ref, pipeline)))
        return motion

    def drop(self, ref: Any, deal_ref: Any, pipeline: Any = "") -> dict:
        """Take one off the list. The record stays where it is."""
        motion = self.find(ref)
        self._run("DELETE FROM members WHERE motion = ? AND deal = ?", (motion["id"], self._member(motion, deal_ref, pipeline)))
        return motion

    def lists_of(self, key: str) -> list:
        """The motions a record is on the list of: ``[{id, name, status, person}]``."""
        rows = self._all("SELECT m.id, m.name, m.status AS motion_status, l.status, l.person, l.added_at FROM members l JOIN motions m ON m.id = l.motion "
                         "WHERE l.deal = ? AND m.removed = 0 AND m.scope = ? ORDER BY l.added_at", (key, self.scope()))
        return [{"id": row["id"], "name": row["name"], "status": row["status"], "status_label": STAGES.get(row["status"], row["status"]),
                 "person": row["person"], "added": str(row["added_at"])[:10]} for row in rows]

    # -- what a motion did: worked out from the desk ---------------------------------------------------------
    def results(self, motion_id: str) -> list:
        return self._all("SELECT id, on_date, metric, value, note FROM results WHERE motion = ? ORDER BY on_date DESC, id DESC", (motion_id,))

    def reading(self) -> dict:
        """Everything the tracking reads, fetched once: every record in every pipeline, what was logged on each, the
        motions, who each is for, which lead became which deal, and what people have said by hand."""
        books, deals, logged = {}, [], {}
        for pid, name in self.books():
            with self.desk.on(pid):
                playbook = self.desk.playbook()
                for deal in self.desk.store.list(status="all"):
                    deals.append(dict(deal, _pid=pid, _key="%s:%s" % (pid, deal["id"])))
                for row in self.desk.store.activities("", limit=500000):
                    if not row.get("undone"):
                        logged.setdefault("%s:%s" % (pid, row["deal_id"]), []).append({"at": str(row["at"]), "kind": str(row["kind"]), "text": str(row["text"] or "")})
            books[pid] = {"name": name, "kind": "lead" if playbook.shape == "leads" else "deal", "playbook": playbook}
        for entries in logged.values():
            entries.sort(key=lambda entry: entry["at"])
        motions = self.rows()
        first = next(iter(books), "")
        said: dict = {}
        for row in self._all("SELECT motion, deal, how FROM links"):
            key = row["deal"] if ":" in row["deal"] else "%s:%s" % (first, row["deal"])        # a link from before records were named by pipeline
            said.setdefault(row["motion"], {})[key] = row["how"]
        by_key = {deal["_key"]: deal for deal in deals}
        members: dict = {}
        for row in self._all("SELECT motion, deal, person, status, via, added_at FROM members"):
            members.setdefault(row["motion"], {})[row["deal"]] = row
        pairs = {} if self.desk.tour() else {lead: deal for lead, deal in dict(self.pairs()).items() if lead in by_key and deal in by_key}
        reading = {"deals": deals, "by_key": by_key, "logged": logged, "motions": motions, "books": books, "pairs": pairs,
                   "audience": {motion["id"]: self.audience(motion) for motion in motions}, "said": said, "members": members,
                   "currency": next((book["playbook"].currency for book in books.values() if book["kind"] == "deal"), next(iter(books.values()))["playbook"].currency),
                   "terms": next((book["playbook"].terms for book in books.values() if book["kind"] == "deal"), next(iter(books.values()))["playbook"].terms)}
        reading["claims"] = self._claims(reading)
        return reading

    @staticmethod
    def window(motion: dict) -> tuple:
        """(first day, last day) a record coming in is put down to the motion; '' for an open end, or for no window."""
        start = motion["start_on"] or (str(motion["created_at"])[:10] if motion["status"] in ("running", "done", "stopped") else "")
        end = ""
        if motion["end_on"]:
            end = (dt.date.fromisoformat(motion["end_on"]) + dt.timedelta(days=GRACE)).isoformat()
        return start, end

    def says_where(self, source: Any) -> bool:
        """Whether a record's Source names where it came from. An empty one does not, and nor do the marks Stoa itself
        leaves (added from a market map, brought in by an import or a list, handed over from a lead pipeline, sample
        data): those say how the record was made, not what won it."""
        low = fold(source)
        from . import demo
        sample = fold(demo.SOURCE)
        return bool(low) and low not in (sample, "import") and not low.startswith(("market map", "intake:", "lead pipeline:"))

    @staticmethod
    def watched(motion: dict) -> list:
        """The words that mean this motion when they turn up in a record's Source or in a note: its name, and any it was given."""
        return [word for word in dict.fromkeys([fold(motion["name"])] + [fold(tag) for tag in motion["tags"]]) if len(word) >= WATCH_MIN]

    def _judge(self, motion: dict, deal: dict, reading: dict) -> Optional[dict]:
        """What this record owes this motion, on the evidence, or None. ``rule`` says which kind of evidence it was."""
        member = reading["members"].get(motion["id"], {}).get(deal["_key"])
        if member is not None:
            # On the list: sourced if being put on it is what brought it into the pipeline (from the map, or as a new
            # lead); influenced if it was in a pipeline already.
            added = str(member["added_at"])
            came = member["via"] in BROUGHT
            how = {"map": "from the map, which is how it came into the pipeline", "new": "as a new lead"}.get(member["via"]) or VIA.get(member["via"], "")
            return {"how": "sourced" if came else "influenced", "rule": 1,
                    "why": "On this motion's list since %s%s%s." % (added[:10], " (%s)" % how if how else "",
                                                                    "" if came else "; it was already in the pipeline then")}
        start, end = self.window(motion)
        created = str(deal["created_at"])[:10]
        inside = lambda day: bool(start) and day >= start and (not end or day <= end)        # noqa: E731
        words = self.watched(motion)
        source = fold(deal["source"])
        if source and (source == fold("motion: " + motion["name"]) or any(word in source for word in words)):
            return {"how": "sourced", "rule": 2, "why": "Its Source reads “%s”." % str(deal["source"]).strip()[:80]}
        entries = reading["logged"].get(deal["_key"], [])
        spoken = [("note", str(deal.get("notes") or ""), "")] + [(entry["kind"], entry["text"], entry["at"][:10]) for entry in entries if entry["kind"] in TOUCH_KINDS]
        for kind, text, day in spoken:
            hit = next((word for word in words if word in fold(text)), "")
            if hit:
                came_in = inside(created) if start else created >= str(motion["created_at"])[:10]
                return {"how": "sourced" if came_in else "influenced", "rule": 3,
                        "why": "A %s on it%s mentions “%s”." % (kind, " on " + day if day else "", hit)}
        return None

    def _claims(self, reading: dict) -> dict:
        """For every motion, the records it is credited with: ``{motion id: {record key: {how, why, auto}}}``.

        What a person said by hand comes first and is never overruled. The rest is worked out. A deal handed over
        from a credited lead is credited as the lead was. Where two motions would both be credited with bringing a
        record in, the one whose list it is on wins over a mention; between two mentions, the motion that started last
        before the record came in. The other is marked as having influenced it."""
        found: dict = {}
        by_key = reading["by_key"]
        for motion in reading["motions"]:
            said = reading["said"].get(motion["id"], {})
            mine: dict = {}
            for deal in reading["deals"]:
                key = deal["_key"]
                if said.get(key) == "none":
                    continue
                verdict = self._judge(motion, deal, reading)
                if key in said:
                    mine[key] = {"how": said[key], "rule": 0, "auto": False, "why": "Confirmed by hand." + (" Stoa had found: " + verdict["why"] if verdict else "")}
                elif verdict:
                    mine[key] = dict(verdict, auto=True)
            for lead, deal in reading["pairs"].items():         # the deal a credited lead became is credited as the lead was
                if lead in mine and deal not in mine and said.get(deal) != "none":
                    book = reading["books"].get(by_key[lead]["_pid"], {})
                    mine[deal] = {"how": mine[lead]["how"], "rule": 6, "auto": True,
                                  "why": "It was handed over from the lead %s in %s, which this motion %s." % (by_key[lead]["name"], book.get("name", "a lead pipeline"), mine[lead]["how"])}
            found[motion["id"]] = mine
        motions = {motion["id"]: motion for motion in reading["motions"]}
        for deal in reading["deals"]:
            key = deal["_key"]
            rivals = [motion_id for motion_id, mine in found.items() if mine.get(key, {}).get("how") == "sourced" and mine[key]["auto"]]
            if len(rivals) < 2:
                continue
            created = str(deal["created_at"])[:10]
            started = lambda motion_id: self.window(motions[motion_id])[0] or str(motions[motion_id]["created_at"])[:10]      # noqa: E731
            winner = max(rivals, key=lambda motion_id: (found[motion_id][key]["rule"] == 1, started(motion_id) <= created, started(motion_id)))
            for motion_id in rivals:
                if motion_id != winner:
                    found[motion_id][key] = {"how": "influenced", "rule": found[motion_id][key]["rule"], "auto": True,
                                             "why": found[motion_id][key]["why"] + " But “%s” is credited with bringing it in, so this one influenced it." % motions[winner]["name"]}
        return found

    def impact(self, motion: dict, reading: Optional[dict] = None, detail: bool = False) -> dict:
        reading = reading or self.reading()
        by_key, books = reading["by_key"], reading["books"]
        claims = reading["claims"].get(motion["id"], {})
        audience = reading["audience"].get(motion["id"]) or self.audience(motion)
        # A lead and the deal it became are one piece of business: where both are credited, the deal stands for it.
        became = {lead: deal for lead, deal in reading["pairs"].items() if lead in claims and deal in claims}
        counted_keys = [key for key in claims if key not in became]
        sourced = [by_key[key] for key in counted_keys if claims[key]["how"] == "sourced"]
        influenced = [by_key[key] for key in counted_keys if claims[key]["how"] == "influenced"]
        kind = lambda deal: books[deal["_pid"]]["kind"]        # noqa: E731

        def money(rows: list) -> float:
            return sum(int(row["value_cents"] or 0) for row in rows) / 100.0

        # What it is worked for: what it sourced, and everyone on its list, though some were in a pipeline already (a
        # motion run on people you have, an email offer to leads, is judged by what they do). A mention alone is not.
        on_it = {key for key in counted_keys if claims[key]["how"] == "influenced" and key in reading["members"].get(motion["id"], {})}
        worked = sourced + [by_key[key] for key in counted_keys if key in on_it]
        deals = [deal for deal in worked if kind(deal) == "deal"]
        leads = [deal for deal in sourced if kind(deal) == "lead"]
        open_, won, lost = ([deal for deal in deals if deal["status"] == status] for status in ("open", "won", "lost"))
        totals: dict = {}
        logged = self.results(motion["id"])
        for row in logged:
            totals[row["metric"]] = totals.get(row["metric"], 0.0) + float(row["value"])
        spend = totals.get("spend", 0.0)
        pipeline_value, won_value = money(open_), money(won)
        # Kept for the totals across motions, which count a record on two motions' lists once.
        reading.setdefault("worked", {})[motion["id"]] = {"open": {deal["_key"]: int(deal["value_cents"] or 0) for deal in open_},
                                                           "won": {deal["_key"]: int(deal["value_cents"] or 0) for deal in won}}

        # What the desk itself shows was done while the motion ran: nobody has to log these.
        start, end = self.window(motion)
        inside = lambda day: bool(start) and day >= start and (not end or day <= end)        # noqa: E731
        names = {company_key(company["company"]) for company in audience["companies"]} - {""}
        counted = {"meetings": 0, "calls": 0, "emails": 0, "companies_reached": 0}
        reached: set = set()
        for deal in reading["deals"]:
            company = company_key(deal["company"] or deal["grp"])
            for entry in reading["logged"].get(deal["_key"], []):
                if entry["kind"] not in ("meeting", "call", "email") or not inside(entry["at"][:10]):
                    continue
                if deal["_key"] in claims:
                    counted[entry["kind"] + "s"] += 1
                if company in names:
                    reached.add(company)
        counted["companies_reached"] = len(reached)
        in_pipeline = {company_key(deal["company"] or deal["grp"]) for deal in reading["deals"]} & names
        listed = self.listed(motion, reading)
        on_list = {stage: sum(1 for row in listed if STAGE_ORDER[row["status"]] >= STAGE_ORDER[stage]) for stage in STAGES}

        def stages(pid: str) -> list:
            mine = [deal for deal in sourced if deal["_pid"] == pid and deal["status"] == "open"]
            return [{"id": stage["id"], "label": stage["label"], "count": sum(1 for deal in mine if deal["stage"] == stage["id"])} for stage in books[pid]["playbook"].stages]

        out = {
            "sourced": len(sourced), "influenced": len(influenced),
            "leads": sum(1 for deal in leads if deal["status"] == "open"), "leads_closed": sum(1 for deal in leads if deal["status"] != "open"),
            "handed_over": len(became), "open": len(open_), "won": len(won), "lost": len(lost),
            "found": sum(1 for key in counted_keys if claims[key]["auto"]), "confirmed": sum(1 for key in counted_keys if not claims[key]["auto"]),
            "set_aside": sum(1 for how in reading["said"].get(motion["id"], {}).values() if how == "none"),
            "pipeline_value": pipeline_value, "won_value": won_value, "influenced_value": money([deal for deal in influenced if kind(deal) == "deal"]),
            "from_list": len(on_it),            # influenced, on its list, and counted in what it won
            "win_rate": round(len(won) / (len(won) + len(lost)), 3) if won or lost else None,
            "spend": spend, "budget": float(motion["budget"] or 0), "totals": totals, "counted": counted,
            "response_rate": round(totals["responses"] / totals["reach"], 4) if totals.get("reach") and "responses" in totals else None,
            "cost_per_record": round(spend / len(sourced), 2) if spend and sourced else None,
            "cost_per_win": round(spend / len(won), 2) if spend and won else None,
            "return": round(won_value / spend, 2) if spend and won_value else None,
            "pipeline_return": round((pipeline_value + won_value) / spend, 2) if spend and (pipeline_value or won_value) else None,
            "audience": len(audience["companies"]), "audience_in_pipeline": len(in_pipeline),
            # The list: how many are on it, and how many have got at least that far (reached counts those who replied too).
            "list": dict(on_list, total=len(listed), won=sum(1 for row in listed if row["record_status"] == "won")),
            "funnel": [{"pipeline": pid, "name": books[pid]["name"], "kind": books[pid]["kind"], "stages": stages(pid)}
                       for pid in books if any(deal["_pid"] == pid and deal["status"] == "open" for deal in sourced)],
            "currency": reading["currency"], "tracking": bool(start) or bool(self.watched(motion)),
        }
        if detail:
            order = {"won": 0, "open": 1, "lost": 2}
            came_from = {deal: lead for lead, deal in became.items()}
            several = len(books) > 1

            def row(deal: dict) -> dict:
                claim, book = claims[deal["_key"]], books[deal["_pid"]]
                lead = by_key.get(came_from.get(deal["_key"], ""))
                return {"key": deal["_key"], "id": deal["id"], "pipeline": deal["_pid"], "pipeline_name": book["name"] if several else "", "kind": book["kind"],
                        "name": deal["name"], "company": deal["company"], "status": deal["status"], "stage": book["playbook"].label(deal["stage"]),
                        "won_label": book["playbook"].won["label"], "value": int(deal["value_cents"] or 0) / 100.0, "created": str(deal["created_at"])[:10],
                        "how": claim["how"], "why": claim["why"], "auto": claim["auto"], "from_lead": lead["name"] if lead else ""}
            out["records"] = sorted([row(deal) for deal in sourced + influenced],
                                    key=lambda item: (item["how"] != "sourced", item["kind"] != "deal", order.get(item["status"], 3), -item["value"]))
            record_at: dict = {}
            for deal in reading["deals"]:
                record_at.setdefault(company_key(deal["company"] or deal["grp"]), {"id": deal["id"], "pipeline": deal["_pid"]})
            out["companies"] = [dict(company, record=record_at.get(company_key(company["company"]), {}).get("id", ""),
                                     record_pipeline=record_at.get(company_key(company["company"]), {}).get("pipeline", "")) for company in audience["companies"]]
            out["results"] = logged
            out["members"] = listed
            out["aside"] = [{"key": key, "id": by_key[key]["id"], "name": by_key[key]["name"]}
                            for key, how in reading["said"].get(motion["id"], {}).items() if how == "none" and key in by_key]
        return out

    def listed(self, motion: dict, reading: dict) -> list:
        """Who is on a motion's list, each with how far they have got. A call or email logged on the record since it was
        added counts as reached, and a meeting as a meeting, whatever was said; what a person or the agent said counts
        when it is further on."""
        by_key, books = reading["by_key"], reading["books"]
        several = len(books) > 1
        out = []
        for key, member in reading["members"].get(motion["id"], {}).items():
            deal = by_key.get(key)
            if deal is None:            # the record has gone from the desk; it stays on the list until taken off, unseen
                continue
            status, added = member["status"] if member["status"] in STAGES else "added", str(member["added_at"])
            for entry in reading["logged"].get(key, []):
                if entry["at"] >= added and entry["kind"] in ("call", "email", "meeting"):
                    seen = "meeting" if entry["kind"] == "meeting" else "reached"
                    if STAGE_ORDER[seen] > STAGE_ORDER[status]:
                        status = seen
            book = books[deal["_pid"]]
            out.append({"key": key, "id": deal["id"], "pipeline": deal["_pid"], "pipeline_name": book["name"] if several else "",
                        "name": deal["name"], "company": deal["company"], "person": member["person"], "status": status,
                        "status_label": STAGES[status], "said": member["status"], "via": member["via"], "added": added[:10],
                        "record_status": deal["status"], "stage": book["playbook"].label(deal["stage"]) if deal["status"] == "open" else
                        (book["playbook"].won["label"] if deal["status"] == "won" else deal["status"])})
        out.sort(key=lambda row: (-STAGE_ORDER[row["status"]], row["added"], row["name"].casefold()))
        return out

    def view(self, motion: dict, reading: Optional[dict] = None, detail: bool = False) -> dict:
        reading = reading or self.reading()
        audience = reading["audience"].get(motion["id"]) or self.audience(motion)
        start, end = self.window(motion)
        return {"id": motion["id"], "name": motion["name"], "kind": motion["kind"], "kind_label": KINDS.get(motion["kind"], motion["kind"]),
                "status": motion["status"], "status_label": STATUSES.get(motion["status"], motion["status"]), "goal": motion["goal"],
                "market": motion["market"], "market_key": "%s:%s" % (motion.get("market_pipeline") or "", motion["market"]) if motion["market"] else "",
                "market_name": audience["name"], "segments": motion["segments"], "owner": motion["owner"],
                "start_on": motion["start_on"], "end_on": motion["end_on"], "budget": float(motion["budget"] or 0), "tags": motion["tags"],
                "watching": self.watched(motion), "window": {"from": start, "to": end},
                "updated_at": motion["updated_at"], "impact": self.impact(motion, reading, detail)}

    def listing(self) -> dict:
        reading = self.reading()
        motions = [self.view(motion, reading) for motion in reading["motions"]]
        live = [motion for motion in motions if motion["status"] in ("running", "done")]
        return {"motions": motions, "maps": self.maps(), "kinds": KINDS, "statuses": STATUSES, "metrics": METRICS, "stages": STAGES,
                "currency": reading["currency"], "terms": reading["terms"],
                "totals": {"running": sum(1 for motion in motions if motion["status"] == "running"),
                           "spend": sum(motion["impact"]["spend"] for motion in motions),
                           "sourced": sum(motion["impact"]["sourced"] for motion in motions),
                           "leads": sum(motion["impact"]["leads"] for motion in motions),
                           "influenced": sum(motion["impact"]["influenced"] for motion in motions),
                           # each record once, though it is on the lists of two motions
                           "pipeline_value": sum({key: cents for motion in live for key, cents in reading["worked"][motion["id"]]["open"].items()}.values()) / 100.0,
                           "won_value": sum({key: cents for motion in motions for key, cents in reading["worked"][motion["id"]]["won"].items()}.values()) / 100.0}}

    # -- the tool an agent uses -------------------------------------------------------------------------------
    def tool(self, args: dict, session: Optional[dict] = None) -> dict:
        """``gtm_motion``, as the agent calls it."""
        args = dict(args or {})
        action = str(args.get("action") or "").strip().lower()
        try:
            if (session or {}).get("platform") == "cron" and action not in ("list", "get"):
                raise Refused("A scheduled job may read motions but not change them. Say what you would have done in your reply instead.")
            return self._act(action, args)
        except Refused as exc:
            return {"ok": False, "error": str(exc)}

    def _act(self, action: str, args: dict) -> dict:
        if action == "list":
            listing = self.listing()
            return {"ok": True, "motions": listing["motions"], "totals": listing["totals"],
                    "maps": [{"name": one["name"], "pipeline": one["pipeline_name"], "segments": [s["name"] for s in one["segments"]]} for one in listing["maps"]],
                    "note": "Every figure is counted by Stoa from the whole desk. 'sourced' are records a motion brought in and 'influenced' ones it helped along; "
                            "a lead and the deal it was handed over as count once. action='get' gives the reason for each record."}
        if action == "get":
            return {"ok": True, "motion": self.view(self.find(args.get("motion") or args.get("name")), detail=True)}
        if action == "add":
            motion = self.add({key: args[key] for key in FIELDS if key in args})
            return {"ok": True, "motion": self.view(motion), "note": "It is under Motions on the GTM tab of the Stoa page."}
        if action == "update":
            return {"ok": True, "motion": self.view(self.update(args.get("motion"), {key: args[key] for key in FIELDS if key in args}))}
        if action == "log":
            motion = self.log(args.get("motion"), args.get("metric"), args.get("value"), args.get("on"), args.get("note"))
            return {"ok": True, "motion": self.view(motion)}
        if action == "enroll":
            motion = args.get("motion") or args.get("name")
            done = {"added": [], "already": [], "unknown": []}
            if args.get("companies"):
                done = self.enroll_from_map(motion, args.get("companies"), args.get("market"))
            if args.get("deals") or args.get("deal"):
                more = self.enroll(motion, args.get("deals") or [args.get("deal")], args.get("pipeline"), args.get("person"), via="agent")
                done = {key: done[key] + more[key] for key in ("added", "already", "unknown")}
            if not any(done[key] for key in ("added", "already", "unknown")):
                raise Refused("enroll needs `deals` (records) or `companies` (from the motion's map).")
            found = self.find(motion)
            return {"ok": bool(done["added"] or done["already"]), "added": done["added"], "already_on_it": done["already"], "not_found": done["unknown"],
                    "list": self.view(found)["impact"]["list"],
                    "note": "Companies from the map were added to the pipeline as leads first." if args.get("companies") else ""}
        if action == "mark":
            return {"ok": True, "motion": self.view(self.mark(args.get("motion"), args.get("deal"), args.get("status"), args.get("pipeline")))}
        if action == "drop":
            return {"ok": True, "motion": self.view(self.drop(args.get("motion"), args.get("deal"), args.get("pipeline")))}
        if action == "link":
            motion, deal = self.link(args.get("motion"), args.get("deal"), args.get("how"), args.get("pipeline"))
            return {"ok": True, "linked": {"id": deal["id"], "name": deal["name"]}, "motion": self.view(motion)}
        if action == "unlink":
            return {"ok": True, "motion": self.view(self.unlink(args.get("motion"), args.get("deal"), args.get("pipeline")))}
        if action == "remove":
            return {"ok": True, "removed": self.remove(args.get("motion"))["name"]}
        raise Refused("gtm_motion does not have an action called %s." % (action or "(none)"))
