"""The database: every account, deal and person on the desk as rows.

The Pipeline tab shows work by stage; this shows everything that is kept, to look through: filter it, sort it,
group it, and keep a way of looking as a view. The rows are worked out here from what the desk already holds
(records, the people on them, companies, market maps); nothing new is stored but the views a person saves.
The page does the filtering and sorting itself, so a view is only what to show: which rows, in what order,
with which columns.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import cards
from . import store as st

OBJECTS = ("accounts", "deals", "people")
OPS = ("is", "is_not", "contains", "not_contains", "empty", "not_empty", "gt", "lt", "before", "after", "last", "next")
MOST_VIEWS = 40


def _text(value: Any, limit: int = 120) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())[:limit]


def _field(value: Any) -> str:
    """A column's name as the page sends it: a word, or ``fields.<key>`` for one of the playbook's own."""
    text = _text(value, 80)
    return text if re.fullmatch(r"[a-z_]+(\.[A-Za-z0-9_\-]+)?", text) else ""


class Database:
    def __init__(self, desk):
        self.desk = desk

    @property
    def store(self) -> st.Store:
        return self.desk.store

    # -- the rows -------------------------------------------------------------------------------
    def rows(self) -> dict:
        playbook = self.desk.playbook()
        deals = self.store.list(status="all")
        touches = self.store.touches()
        as_deal = playbook.shape == "deals"
        on: dict = {}                              # the people on each record
        for person in self.store.people():
            on.setdefault(person["deal_id"], []).append(person)
        by_id = {deal["id"]: deal for deal in deals}

        def own(deal: dict) -> bool:
            """Whether the record is itself a person: always where records are people, and on a pipeline of deals
            only an older record still named for someone (it has a name that is not its company's)."""
            if not as_deal:
                return True
            return bool(deal["name"]) and st.org_key(deal["name"]) not in (st.org_key(deal["company"]), st.org_key(deal["grp"])) \
                and bool(deal["email"] or deal["linkedin"] or deal.get("phone"))

        deal_rows = []
        for deal in deals:
            here = on.get(deal["id"], [])
            first = here[0] if here else None
            mine = own(deal)
            contact = deal["name"] if mine else (first["name"] if first else "")
            working = cards.working(playbook, deal)
            deal_rows.append({
                "id": deal["id"], "name": deal["name"], "company": deal["company"], "group": deal["grp"],
                "stage": playbook.label(deal["stage"]) if deal["status"] == "open" else "",
                "stage_id": deal["stage"] if deal["status"] == "open" else "", "working": working,
                "delivery_id": (deal.get("delivery") or "") if deal["status"] == "won" else "",
                "status": {"open": "Open", "won": playbook.won["label"], "lost": playbook.lost["label"]}[deal["status"]],
                "state": deal["status"],
                "delivery": playbook.delivery_label(deal.get("delivery") or "") if deal["status"] == "won" and playbook.delivery else "",
                "value": deal["value"], "owner": deal["owner"],
                "next_step": deal["next_step"] if working else "", "next_due": cards.due_date(deal) if working else "",
                "due": cards.is_due(deal, playbook), "source": deal["source"], "close_on": deal.get("close_on") or "",
                "created_at": st.local_day(deal["created_at"]), "last_touch": st.local_day(touches.get(deal["id"], "") or deal["updated_at"]),
                "stage_at": st.local_day(deal["stage_at"]), "lost_reason": deal["lost_reason"],
                "people": len(here) + (1 if mine else 0), "contact": contact,
                "contact_title": str(deal["fields"].get("title") or "") if mine else (first["title"] if first else ""),
                "email": deal["email"] if mine or not first else first["email"],
                "phone": (deal.get("phone") or "") if mine or not first else first["phone"],
                "linkedin": deal["linkedin"] if mine or not first else first["linkedin"],
                "notes": (deal.get("notes") or "")[:300],
                "fields": {k: v for k, v in deal["fields"].items() if not k.startswith("_") and v not in ("", None)}})

        # -- people: each person once, however many records they are on --------------------------------
        people: dict = {}

        def person_row(key: str, source: dict, kind: str, edit: dict) -> dict:
            return people.setdefault(key, {
                "key": key, "edit": edit, "name": source.get("name") or "", "title": source.get("title") or "", "email": source.get("email") or "",
                "phone": source.get("phone") or "", "linkedin": source.get("linkedin") or "", "company": "", "role": "", "kind": kind,
                "records": [], "open": 0, "last_touch": "", "created_at": st.local_day(source.get("created_at")), "note": (source.get("note") or "")[:300]})

        def on_record(row: dict, deal: dict, role: str, there: bool) -> None:
            if not any(item["id"] == deal["id"] for item in row["records"]):
                row["records"].append({"id": deal["id"], "name": deal["name"], "state": deal["status"]})
                row["open"] += 1 if cards.working(playbook, deal) else 0
            row["last_touch"] = max(row["last_touch"], st.local_day(touches.get(deal["id"], "") or deal["updated_at"]))
            if role and role not in row["role"].split(" · "):
                row["role"] = " · ".join(part for part in (row["role"], role) if part)
            if there and not row["company"]:
                row["company"] = deal["company"]

        for deal in deals:
            if own(deal):
                row = person_row("r:" + deal["id"], {"name": deal["name"], "title": deal["fields"].get("title"), "email": deal["email"],
                                                      "phone": deal.get("phone"), "linkedin": deal["linkedin"], "created_at": deal["created_at"]},
                                 "Person" if as_deal else playbook.terms["deal"].capitalize(), {"record": deal["id"]})
                on_record(row, deal, "", True)
            for person in on.get(deal["id"], []):
                row = person_row("c:%s" % person["id"], person, "Person", {"deal": deal["id"], "person": str(person["id"])})
                # On a deal, its people are at its company. On a candidate's record a hiring manager is not at the
                # candidate's employer: there the company is theirs only when their address agrees.
                there = as_deal or bool(st.email_domain(person.get("email")) and st.email_domain(person.get("email")) == st.email_domain(deal["email"]))
                on_record(row, deal, person.get("role") or "", there)
        groups = {row["key"]: row for row in self.store.grps()}
        for person in self.store.grp_people():
            group = groups.get(person["grp_key"]) or {}
            row = person_row("c:%s" % person["id"], person, "Person", {"group": group.get("name") or "", "person": str(person["id"])})
            side = "%s side: %s" % (playbook.terms["group"].capitalize(), group.get("name") or "")
            if side not in row["role"]:
                row["role"] = " · ".join(part for part in (row["role"], person.get("role") or "", side) if part)
            row["company"] = row["company"] or group.get("client") or (group.get("name") or "" if playbook.group_is_company() else "")
        people_rows = sorted(people.values(), key=lambda row: row["name"].lower())
        for row in people_rows:
            row["record_count"] = len(row["records"])
            row["record_names"] = ", ".join(item["name"] for item in row["records"][:6])
            row["records"] = row["records"][:6]

        # -- accounts: every company with a record, and every company on a map --------------------------
        accounts: dict = {}

        def account(name: str) -> dict:
            key = st.org_key(name)
            return accounts.setdefault(key, {
                "key": key, "name": name, "group": "", "domain": "", "open": 0, "open_value": 0.0, "won": 0, "won_value": 0.0, "lost": 0,
                "deals": 0, "people": 0, "owners": [], "sources": [], "last_touch": "", "next_due": "", "next_step": "", "created_at": "",
                "location": "", "size": "", "segment": "", "fit": 0, "maps": [], "map_id": "", "signal": "", "note": "", "in_pipeline": False,
                "targets": [], "_who": set()})

        for deal in deals:
            if not st.org_key(deal["company"]):
                continue
            row = account(deal["company"])
            row["in_pipeline"] = True
            row["deals"] += 1
            row[deal["status"]] += 1
            if deal["status"] != "lost":
                row[deal["status"] + "_value"] += deal["value"]
            if not row["group"] and st.org_key(deal["grp"]) == row["key"]:
                row["group"] = deal["grp"]
            for bucket, value in (("owners", deal["owner"]), ("sources", deal["source"])):
                if value and value not in row[bucket]:
                    row[bucket].append(value)
            row["last_touch"] = max(row["last_touch"], st.local_day(touches.get(deal["id"], "") or deal["updated_at"]))
            row["created_at"] = min(row["created_at"] or "9999", st.local_day(deal["created_at"]))
            due = cards.due_date(deal) if cards.working(playbook, deal) else ""
            if due and (not row["next_due"] or due < row["next_due"]):
                row["next_due"], row["next_step"] = due, deal["next_step"]
            elif not row["next_due"] and not row["next_step"] and cards.working(playbook, deal):
                row["next_step"] = deal["next_step"]
            row["_who"].update({"r:" + deal["id"]} if own(deal) else set())
            row["_who"].update("c:%s" % person["id"] for person in on.get(deal["id"], []))
        for org in self.store.orgs():
            if org["key"] in accounts:
                accounts[org["key"]]["domain"] = org.get("domain") or ""
                accounts[org["key"]]["note"] = (org.get("note") or "")[:300]
        for market in self.desk.markets.list_markets():
            for target in self.desk.markets.targets(market["id"]):
                if target["status"] == "dismissed" or not st.org_key(target["company"]):
                    continue
                known = st.org_key(target["company"]) in accounts
                linked = by_id.get(target["deal_id"]) if target["deal_id"] else None
                row = accounts[st.org_key(linked["company"])] if linked and st.org_key(linked["company"]) in accounts else account(target["company"])
                if market["name"] not in row["maps"]:
                    row["maps"].append(market["name"])
                row["map_id"] = row["map_id"] or market["id"]
                for key in ("domain", "location", "size", "segment", "signal"):
                    row[key] = row[key] or target.get(key) or ""
                row["fit"] = max(row["fit"], int(target.get("fit") or 0))
                if target["status"] != "tracked" and not known and not linked:
                    row["targets"].append(target["id"])
                if not known and not linked:
                    row["created_at"] = min(row["created_at"] or "9999", st.local_day(target["created_at"]))
        account_rows = []
        for row in accounts.values():
            row["people"] = len(row.pop("_who"))
            row["status"] = ("Customer" if row["won"] else "Open" if row["open"] else "Lost" if row["lost"] else "On a map")
            row["owners"], row["sources"], row["maps"] = ", ".join(row["owners"]), ", ".join(row["sources"]), ", ".join(row["maps"])
            account_rows.append(row)
        account_rows.sort(key=lambda row: (not row["in_pipeline"], -row["open_value"], -row["open"], row["name"].lower()))
        return {"ok": True, "accounts": account_rows, "deals": deal_rows, "people": people_rows, "views": self.views(),
                "fields": playbook.fields, "currency": playbook.currency,
                "stages": [stage["label"] for stage in playbook.stages], "delivery": [item["label"] for item in playbook.delivery]}

    # -- one thing done to several records ---------------------------------------------------------
    SETTABLE = ("owner", "source", "next_step", "next_due", "close_on")

    def change(self, body: dict, actor: str = "") -> dict:
        """The same change to each of the records picked in the table: facts set (``set``: owner, source, next step
        and its date, expected close), a move to a stage (``stage``), or closed as lost with a reason (``lost``).
        Each goes through the desk's own tool path, one record at a time, so each is logged on its record and can be
        taken back there. A record the change does not fit (a next step on a closed one, a stage for one already
        won) is left as it is and named in ``skipped``."""
        body = body if isinstance(body, dict) else {}
        ids = [_text(item, 200) for item in (body.get("ids") if isinstance(body.get("ids"), list) else []) if _text(item, 200)][:200]
        if not ids:
            return {"ok": False, "error": "No records were picked."}
        playbook, actor = self.desk.playbook(), actor or self.desk.actor()
        wanted = body.get("set") if isinstance(body.get("set"), dict) else {}
        values = {key: _text(wanted[key], 400) for key in self.SETTABLE if key in wanted and isinstance(wanted[key], (str, int, float))}
        stage, lost = _text(body.get("stage"), 60), body.get("lost")
        if stage and stage not in playbook.stage_ids():
            return {"ok": False, "error": "That is not a stage of this pipeline."}
        if not values and not stage and lost is None:
            return {"ok": False, "error": "Say what to change."}
        changed, skipped = [], []
        for deal_id in ids:
            deal = self.store.get(deal_id)
            if deal is None:
                skipped.append({"name": deal_id, "why": "no longer on the desk"})
                continue
            if (stage or lost is not None) and deal["status"] != "open":
                skipped.append({"name": deal["name"], "why": "already %s" % (playbook.won["label"] if deal["status"] == "won" else playbook.lost["label"]).lower()})
                continue
            mine = dict(values)
            if not cards.working(playbook, deal):       # a closed record has no next step to plan
                mine.pop("next_step", None)
                mine.pop("next_due", None)
                if values and not mine:
                    skipped.append({"name": deal["name"], "why": "closed, so it has no next step"})
                    continue
            if stage and stage == deal["stage"] and not mine:
                skipped.append({"name": deal["name"], "why": "already there"})
                continue
            results = []
            if mine:
                results.append(self.desk.deal(dict(mine, action="update", deal=deal_id), actor=actor))
            if stage and stage != deal["stage"]:
                results.append(self.desk.deal({"action": "move", "deal": deal_id, "stage": stage}, actor=actor))
            if lost is not None:
                results.append(self.desk.deal({"action": "close", "deal": deal_id, "outcome": "lost", "reason": _text(lost, 200)}, actor=actor))
            bad = next((result for result in results if not result.get("ok")), None)
            if bad:
                skipped.append({"name": deal["name"], "why": _text(bad.get("error"), 160) or "could not be saved"})
            else:
                changed.append(deal_id)
        return {"ok": bool(changed), "changed": len(changed), "ids": changed, "skipped": skipped,
                "error": "" if changed else "Nothing was changed: %s." % (skipped[0]["why"] if skipped else "no record fitted")}

    # -- the views a person keeps ------------------------------------------------------------------
    def views(self) -> list:
        try:
            kept = json.loads(self.store.meta_get("database_views") or "[]")
        except ValueError:
            kept = []
        return [view for view in kept if isinstance(view, dict) and view.get("id") and view.get("object") in OBJECTS] if isinstance(kept, list) else []

    def _keep(self, views: list) -> None:
        self.store.meta_set("database_views", json.dumps(views, sort_keys=True))

    def save_view(self, body: dict) -> dict:
        """Keep a way of looking: which rows (filters, search), in what order, grouped how, with which columns.
        With the ``id`` of a view already kept, that one is replaced; otherwise it is a new one."""
        body = body if isinstance(body, dict) else {}
        name, thing = _text(body.get("name"), 60), _text(body.get("object"), 20)
        if thing not in OBJECTS:
            return {"ok": False, "error": "a view is of accounts, deals or people"}
        if not name:
            return {"ok": False, "error": "A view needs a name."}
        filters = []
        for item in (body.get("filters") if isinstance(body.get("filters"), list) else [])[:12]:
            if isinstance(item, dict) and _field(item.get("field")) and _text(item.get("op"), 20) in OPS:
                filters.append({"field": _field(item["field"]), "op": _text(item["op"], 20), "value": _text(item.get("value"), 200)})
        sort = body.get("sort") if isinstance(body.get("sort"), dict) else {}
        view = {"object": thing, "name": name, "filters": filters, "search": _text(body.get("search"), 120),
                "sort": {"field": _field(sort.get("field")), "dir": "desc" if sort.get("dir") == "desc" else "asc"} if _field(sort.get("field")) else {},
                "group": _field(body.get("group")),
                "columns": [_field(key) for key in (body.get("columns") if isinstance(body.get("columns"), list) else [])[:40] if _field(key)]}
        views, wanted = self.views(), _text(body.get("id"), 60)
        at = next((index for index, kept in enumerate(views) if kept["id"] == wanted), None) if wanted else None
        if any(kept["object"] == thing and kept["name"].lower() == name.lower() and (at is None or kept["id"] != wanted) for kept in views):
            return {"ok": False, "error": "Another view of %s is already called that." % thing}
        if at is None:
            if len(views) >= MOST_VIEWS:
                return {"ok": False, "error": "The desk keeps up to %d views. Remove one to keep another." % MOST_VIEWS}
            base = st.slugify(name)[:40] or "view"
            view["id"], n = base, 1
            while any(kept["id"] == view["id"] for kept in views):
                n += 1
                view["id"] = "%s-%d" % (base, n)
            views.append(view)
        else:
            view["id"] = wanted
            views[at] = view
        self._keep(views)
        return {"ok": True, "view": view, "views": views}

    def remove_view(self, view_id: Any) -> dict:
        views = self.views()
        left = [view for view in views if view["id"] != _text(view_id, 60)]
        if len(left) == len(views):
            return {"ok": False, "error": "That view is no longer kept."}
        self._keep(left)
        return {"ok": True, "views": left}
