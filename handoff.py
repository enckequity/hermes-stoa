"""Every pipeline on the desk at once, and a lead's way from a lead pipeline into a deal pipeline.

A desk can hold several pipelines, each in a ledger of its own with its own stages. They are of two kinds: a **lead
pipeline** (a long list of people being worked, on the "generate leads" shape) and a **deal pipeline** (fewer,
richer records). Both are ordinary pipelines, so everything in them is in the ledger: every change can be taken back
and a daily copy is kept.

What this module adds is the view across them (the Pipeline tab's overview) and the hand-over between them:

* A lead pipeline hands over to one deal pipeline. When a lead reaches its pipeline's last stage (it is closed as
  won, which a lead pipeline calls "handed over"), a deal is opened for it in that deal pipeline, by itself.
* The deal is named for the company, with the lead as a person on it, and carries where the lead came from, so a
  motion that sourced the lead is credited with the deal.
* Several leads at one company are one deal. When the company already has an open deal in that pipeline, the lead
  is added to it as another person; a second deal is opened only when there is none, or the earlier one is closed.
* Each hand-over is remembered (``handoffs.db``), so a lead is handed over once, and both records say where the
  other is. The row is written first, as a claim, so two processes (the gateway and the dashboard) closing the
  same lead at once open one deal between them.

It happens as the lead is closed (``Desk._move`` calls ``after_won`` once the change is saved), however it was
closed: by the agent, on the page, from a Slack card or by a list. A lead closed before then is not handed over by
itself; ``hand_over`` does one by hand. Where each lead pipeline hands over to is kept in ``pipelines.json``.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import closing, contextmanager
from typing import Any, Optional

from .motions import company_key
from .store import _statements

log = logging.getLogger("hermes.plugins.stoa")
KEEP = "handover"       # in pipelines.json: {lead pipeline id: {"to": deal pipeline id, or "" for none}}
KIND_WORDS = {"lead": "Lead pipeline", "deal": "Deal pipeline"}
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs (
  lead_pipeline TEXT NOT NULL, lead TEXT NOT NULL, deal_pipeline TEXT NOT NULL, deal TEXT NOT NULL, at TEXT NOT NULL, how TEXT NOT NULL DEFAULT 'auto',
  PRIMARY KEY (lead_pipeline, lead));
"""

# What each sample desk is called where pipelines are named (the same names as its button on the page: sampleName).
SAMPLE_NAMES = {"saas": "B2B Sales Data", "recruiting": "Recruiting Data"}


class Refused(Exception):
    """A request that cannot be done, in words for whoever asked."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handoffs:
    def __init__(self, desk):
        self.desk = desk
        self.path = desk.data_dir / "handoffs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                        for stmt in _statements(SCHEMA):
                            db.execute(stmt)
                        db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise

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

    # -- which there are -------------------------------------------------------------------------------------
    def listed(self) -> list:
        """The desk's own pipelines. A tour shows one sample pipeline and none of these."""
        return [] if self.desk.tour() else list(self.desk.pipelines())

    @staticmethod
    def kind_of(playbook: Any) -> str:
        return "lead" if getattr(playbook, "shape", "") == "leads" else "deal"

    def takes(self, pipeline_id: str) -> bool:
        """Whether a lead can be handed over to this pipeline: one whose records are deals. A recruiting pipeline is
        listed with the deal pipelines, but its records are candidates, and a company is never made into one."""
        with self.desk.on(pipeline_id):
            return getattr(self.desk.playbook(), "shape", "") not in ("leads", "roles")

    def _find(self, ref: Any) -> dict:
        wanted = " ".join(str(ref or "").casefold().split())
        rows = self.listed()
        hit = [row for row in rows if row["id"] == str(ref or "").strip() or row["name"].casefold() == wanted] or [row for row in rows if wanted and wanted in row["name"].casefold()]
        if len(hit) != 1:
            raise Refused("%s pipeline matches '%s'. The pipelines are: %s." % ("More than one" if hit else "No", ref, ", ".join(row["name"] for row in rows) or "none"))
        return hit[0]

    def _settings(self) -> dict:
        kept = self.desk._pipes().get(KEEP, {})
        return {str(key): dict(value) for key, value in kept.items() if isinstance(value, dict)} if isinstance(kept, dict) else {}

    def _keep(self, lead_pipeline: str, entry: dict) -> None:
        with self.desk._pipes_lock:
            data = dict(self.desk._pipes())
            kept = dict(data.get(KEEP) or {}) if isinstance(data.get(KEEP), dict) else {}
            kept[lead_pipeline] = entry
            data[KEEP] = kept
            self.desk._pipes_write(data)

    def target_of(self, lead_pipeline: str, kinds: Optional[dict] = None) -> str:
        """The deal pipeline a lead pipeline hands over to: the one chosen, else the desk's first deal pipeline.
        '' when it has been switched off or there is no deal pipeline."""
        kinds = kinds or self.kinds()
        chosen = self._settings().get(lead_pipeline, {})
        if chosen.get("to") == "":
            return ""
        if chosen.get("to") and kinds.get(chosen["to"]) == "deal" and self.takes(chosen["to"]):
            return chosen["to"]
        return next((pipeline_id for pipeline_id, kind in kinds.items() if kind == "deal" and self.takes(pipeline_id)), "")

    def set_target(self, lead_ref: Any, to_ref: Any) -> dict:
        lead = self._find(lead_ref)
        kinds = self.kinds()
        if kinds.get(lead["id"]) != "lead":
            raise Refused("%s is a deal pipeline. Only a lead pipeline hands over." % lead["name"])
        target = ""
        if str(to_ref or "").strip():
            to = self._find(to_ref)
            if kinds.get(to["id"]) != "deal":
                raise Refused("%s is a lead pipeline. A lead pipeline hands over to a deal pipeline." % to["name"])
            if not self.takes(to["id"]):
                raise Refused("%s is a recruiting pipeline: its records are candidates, not deals. A lead pipeline hands over to a deal pipeline." % to["name"])
            target = to["id"]
        entry = dict(self._settings().get(lead["id"], {}))
        entry.update(to=target)
        self._keep(lead["id"], entry)
        return self.overview()

    def kinds(self) -> dict:
        out = {}
        for row in self.listed():
            with self.desk.on(row["id"]):
                out[row["id"]] = self.kind_of(self.desk.playbook())
        return out

    # -- the view across them --------------------------------------------------------------------------------
    def overview(self) -> dict:
        today = dt.date.today().isoformat()
        rows = self.listed()
        tour = self.desk.tour()
        if not rows:            # a tour, or a desk with one pipeline that has never been listed
            rows = [dict(self.desk.current(), id=self.desk.current().get("id") or "main")]
        if tour:                # the sample on show is named for what it is, not for the pipeline it is standing in front of
            rows = [dict(rows[0], name=SAMPLE_NAMES.get(str(tour.get("example") or ""), "Sample data"))]
        kinds, cards = {}, []
        for row in rows:
            with self.desk.on("" if tour else row["id"]):
                playbook = self.desk.playbook()
                deals = self.desk.store.list(status="all")
            kinds[row["id"]] = self.kind_of(playbook)
            open_ = [deal for deal in deals if deal["status"] == "open"]
            won = [deal for deal in deals if deal["status"] == "won"]
            cards.append({
                "id": row["id"], "name": row["name"], "kind": kinds[row["id"]], "kind_label": KIND_WORDS[kinds[row["id"]]], "shape": playbook.shape,
                "record": playbook.terms.get("deal", "record"), "records": playbook.terms.get("deals", "records"), "currency": playbook.currency,
                "open": len(open_), "value": sum(int(deal["value_cents"] or 0) for deal in open_) / 100.0,
                "due": sum(1 for deal in open_ if deal["next_due"] and str(deal["next_due"])[:10] <= today),
                "won": len(won), "won_value": sum(int(deal["value_cents"] or 0) for deal in won) / 100.0, "won_label": playbook.won["label"],
                "lost": sum(1 for deal in deals if deal["status"] == "lost"),
                "stages": [{"id": stage["id"], "label": stage["label"], "count": sum(1 for deal in open_ if deal["stage"] == stage["id"])} for stage in playbook.stages],
            })
        names = {card["id"]: card["name"] for card in cards}
        handed = {row["lead_pipeline"]: row["n"] for row in self._all("SELECT lead_pipeline, COUNT(*) AS n FROM handoffs GROUP BY lead_pipeline")}
        for card in cards:
            if card["kind"] == "lead" and not tour:
                target = self.target_of(card["id"], kinds)
                card.update(hands_to=target, hands_to_name=names.get(target, ""), handed=handed.get(card["id"], 0))
        leads, deals_ = [card for card in cards if card["kind"] == "lead"], [card for card in cards if card["kind"] == "deal"]
        return {"pipelines": cards, "tour": bool(tour), "showing": (self.desk.current() or {}).get("id") or "main",
                "deal_pipelines": [{"id": card["id"], "name": card["name"]} for card in deals_],
                "totals": {"pipelines": len(cards), "open_leads": sum(card["open"] for card in leads), "open_deals": sum(card["open"] for card in deals_),
                           "value": sum(card["value"] for card in deals_), "won_value": sum(card["won_value"] for card in deals_),
                           "handed": sum(handed.values()), "currency": (deals_ or cards)[0]["currency"] if cards else "USD"}}

    def _all(self, sql: str, args: tuple = ()) -> list:
        with closing(self._open()) as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    # -- a lead becomes a deal ---------------------------------------------------------------------------------
    def handed(self, lead_pipeline: str, lead_id: str) -> Optional[dict]:
        rows = self._all("SELECT * FROM handoffs WHERE lead_pipeline = ? AND lead = ?", (lead_pipeline, lead_id))
        return rows[0] if rows else None

    def _claim(self, lead_pipeline: str, lead_id: str, target: str) -> bool:
        """Say that this lead is being handed over, before anything is written. Only one caller gets it."""
        with self._tx() as db:
            return db.execute("INSERT OR IGNORE INTO handoffs (lead_pipeline, lead, deal_pipeline, deal, at, how) VALUES (?,?,?,?,?,?)",
                              (lead_pipeline, lead_id, target, "", now(), "claim")).rowcount == 1

    def _settle(self, lead_pipeline: str, lead_id: str, deal_id: str = "", how: str = "") -> None:
        with self._tx() as db:
            if deal_id:
                db.execute("UPDATE handoffs SET deal = ?, how = ?, at = ? WHERE lead_pipeline = ? AND lead = ?", (deal_id, how, now(), lead_pipeline, lead_id))
            else:                   # it failed: the claim goes, so it can be tried again
                db.execute("DELETE FROM handoffs WHERE lead_pipeline = ? AND lead = ? AND deal = ''", (lead_pipeline, lead_id))

    def after_won(self, lead_pipeline: str, lead_id: str) -> Optional[dict]:
        """A record was just closed as won. If it is a lead in a lead pipeline that hands over, hand it over now."""
        if self.desk.tour() or self.handed(lead_pipeline or "main", lead_id):
            return None
        kinds = self.kinds()
        pipe = lead_pipeline or "main"
        if kinds.get(pipe) != "lead" or not self.target_of(pipe, kinds):
            return None
        try:
            return self.hand_over(pipe, lead_id, how="auto")
        except Refused as exc:
            log.info("stoa: %s was not handed over: %s", lead_id, exc)
        except Exception:
            log.exception("stoa: handing over %s failed", lead_id)
        return None

    def hand_over(self, lead_pipeline: str, lead_ref: str, to: str = "", how: str = "by hand", actor: str = "") -> dict:
        """Open a deal for one lead in a deal pipeline, and close the lead as handed over if it is still open."""
        kinds = self.kinds()
        names = {row["id"]: row["name"] for row in self.listed()}
        if kinds.get(lead_pipeline) != "lead":
            raise Refused("That is not a lead pipeline.")
        target = to or self.target_of(lead_pipeline, kinds)
        if not target or kinds.get(target) != "deal" or not self.takes(target):
            raise Refused("There is no deal pipeline for %s to hand over to. Add one on the Pipeline tab." % names.get(lead_pipeline, "it"))
        with self.desk.on(lead_pipeline):
            try:
                lead = self.desk.store.get(lead_ref) or self.desk.store.find(lead_ref)
            except Exception as exc:
                raise Refused(str(exc))
            if lead is None:
                raise Refused("No lead in %s matches '%s'." % (names[lead_pipeline], lead_ref))
            people = self.desk.store.people(lead["id"])
        done = self.handed(lead_pipeline, lead["id"])
        if done or not self._claim(lead_pipeline, lead["id"], target):
            done = done or self.handed(lead_pipeline, lead["id"]) or {}
            raise Refused("%s was already handed over to %s." % (lead["name"], names.get(done.get("deal_pipeline", ""), "a deal pipeline")))
        try:
            return self._hand_over(lead_pipeline, lead, people, target, names, how, actor)
        except BaseException:
            self._settle(lead_pipeline, lead["id"])
            raise

    def _hand_over(self, lead_pipeline: str, lead: dict, people: list, target: str, names: dict, how: str, actor: str) -> dict:
        # In a deal pipeline the record is the company (or the deal), and people go on it. The lead is its first person.
        company = (lead["company"] or "").strip()
        title = company or lead["name"]
        where = str(lead["source"] or "").strip()
        source = where if self.says_where(where) else "lead pipeline: %s" % names[lead_pipeline]
        joined = False
        with self.desk.on(target):
            # Working several people at one company is usual. If the company already has an open deal here, this
            # lead belongs on it; a deal that is closed is finished business, and a new one is opened beside it.
            wanted = company_key(company)
            existing = next((deal for deal in self.desk.store.list(status="open") if wanted and wanted in (company_key(deal["company"]), company_key(deal["name"]))), None) if company else None
            if existing is not None:
                deal_id, title, joined = existing["id"], existing["name"], True
            else:
                # Named for a company, the deal keeps no email of its own: the lead's details go on the lead as a person.
                # With no company the deal is named for the lead, and their details are the record's own.
                own = {} if company else {"email": lead["email"], "linkedin": lead["linkedin"], "phone": lead.get("phone") or ""}
                made = self.desk.deal(dict({"action": "add", "name": title, "company": company, "group": company, "value": lead["value"] or None, "owner": lead["owner"],
                                            "source": source}, **own), actor=actor or "Stoa")
                if not made.get("ok"):
                    raise Refused("The deal could not be opened in %s: %s" % (names[target], made.get("error") or "unknown"))
                deal_id = made["deal"]["id"] if isinstance(made["deal"], dict) else made["deal"]
                joined = bool(made.get("exists"))           # the ledger found the same record already there
        try:
            return self._finish(lead_pipeline, lead, people, target, names, how, actor, deal_id, title, company, joined)
        except BaseException:
            # The deal is open: the hand-over happened, though not every step of it. It is kept as done (so the lead and
            # its deal still count once, and it is not opened twice), and the log says what was left.
            self._settle(lead_pipeline, lead["id"], deal_id, how + ", not finished")
            log.exception("stoa: handing over %s stopped part-way; the deal %s is open", lead["id"], deal_id)
            raise

    def _finish(self, lead_pipeline: str, lead: dict, people: list, target: str, names: dict, how: str, actor: str,
                deal_id: str, title: str, company: str, joined: bool) -> dict:
        with self.desk.on(target):
            if company:         # the lead, and anyone else on it, as people on the deal
                for person in [{"name": lead["name"], "email": lead["email"], "linkedin": lead["linkedin"], "phone": lead.get("phone") or "", "title": ""}] + people:
                    if str(person.get("name") or "").strip():
                        self.desk.contact({"action": "add", "deal": deal_id, "name": person["name"], "email": person.get("email") or "", "linkedin": person.get("linkedin") or "",
                                           "phone": person.get("phone") or "", "title": person.get("title") or ""}, actor=actor or "Stoa")
            notes = " Notes there: " + str(lead["notes"])[:600] if lead.get("notes") else ""
            self.desk.deal({"action": "log", "deal": deal_id, "kind": "note",
                            "text": ("%s was handed over from the %s pipeline and added to this deal, which was already open.%s" if joined
                                     else "Handed over from the %s pipeline, where %s was qualified.%s") % ((lead["name"], names[lead_pipeline], notes) if joined else (names[lead_pipeline], lead["name"], notes))},
                           actor=actor or "Stoa")
        with self.desk.on(lead_pipeline):
            if lead["status"] == "open":
                self.desk.deal({"action": "close", "deal": lead["id"], "outcome": "won", "reason": "Handed over to %s" % names[target]}, actor=actor or "Stoa")
            self.desk.deal({"action": "log", "deal": lead["id"], "kind": "note",
                            "text": ("Handed over to the %s pipeline, onto the open deal “%s”." if joined else "Handed over to the %s pipeline as “%s”.") % (names[target], title)}, actor=actor or "Stoa")
        self._settle(lead_pipeline, lead["id"], deal_id, how + (", joined" if joined else ""))
        return {"lead": {"id": lead["id"], "name": lead["name"], "pipeline": names[lead_pipeline]}, "joined": joined,
                "deal": {"id": deal_id, "name": title, "pipeline": names[target], "pipeline_id": target}}

    def pairs(self) -> dict:
        """Every hand-over there has been, as ``{the lead's key: the key of the deal it became}``; a key is a
        pipeline's id and a record's id, ``p2:dana-whitfield``. Motions use it so a lead and its deal count once."""
        return {"%s:%s" % (row["lead_pipeline"], row["lead"]): "%s:%s" % (row["deal_pipeline"], row["deal"]) for row in self._all("SELECT * FROM handoffs WHERE deal != ''")}

    def says_where(self, source: Any) -> bool:
        """Whether a Source names where a record came from (see motions.Motions.says_where: the same rule)."""
        low = " ".join(str(source or "").casefold().split())
        return bool(low) and low not in ("stoa-demo", "import") and not low.startswith(("market map", "intake:", "lead pipeline:"))
