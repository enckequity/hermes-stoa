"""Market maps: who is in a market, how well each company fits, and who to call first.

A map is the step before the pipeline. The agent researches a market (with its own web tools,
or Exa when a key is set) and records what it finds here: companies sorted into segments, each
checked against the market's requirements with the evidence, what each uses today, and who
buys. The desk then reads the map back: how rare each requirement is, who holds the market,
where the segments meet, and the first calls. One click turns a company into a pipeline record.
No Hermes imports.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Optional

from . import sources
from . import store as st

EXA_KEY = "EXA_API_KEY"
PDL_KEY = "PDL_API_KEY"
STATUSES = ("new", "tracked", "dismissed")
MAP_CARD = "map:"  # cards.deal_id prefix for a posted map message
FIELDS = ("domain", "segment", "size", "location", "why", "signal", "source")
ACCOUNT_WORDS = ("account", "client", "company", "customer", "buyer")
SIZE_BANDS = ((50, "1–49"), (200, "50–199"), (1000, "200–999"), (10 ** 12, "1,000+"))
MAX_PEOPLE, MAX_CRITERIA, MAX_FIRST_CALLS = 6, 8, 7
# Words in a job title that say how senior someone is or where they sit, not what they do.
TITLE_NOISE = frozenset("""manager managing head director senior junior vice president chief officer lead leader principal associate assistant
    executive global regional national north south east west america american americas europe emea apac latam international interim
    acting deputy staff team group general founder cofounder partner owner""".split())


def _norm(company: str) -> str:
    name = re.sub(r"[^a-z0-9 ]+", " ", (company or "").lower())
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc|sa|ag)\b", " ", name)
    return " ".join(name.split())


def target_id(market_id: str, company: str) -> str:
    return "t" + hashlib.sha1(("%s|%s" % (market_id, _norm(company))).encode()).hexdigest()[:10]


def dots(fit: int) -> str:
    fit = max(1, min(int(fit or 3), 5))
    return "●" * fit + "○" * (5 - fit)


def _clean(value: Any, limit: int = 400) -> str:
    """Tidy text from a tool argument. Anything that is not text or a number (``false``, a list) counts as blank."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not value:
        return ""
    return " ".join(str(value).split())[:limit]


def _key(label: str) -> str:
    """A stable key for a requirement's wording, in any script."""
    return re.sub(r"[^\w]+", "_", str(label or "").lower(), flags=re.UNICODE).strip("_")[:80]


def region_of(location: str) -> str:
    """The broad place a company is in: the last part of 'City, State' or 'City, Country'."""
    parts = [p.strip() for p in re.sub(r"\([^)]*\)", "", location or "").split(",") if p.strip()]
    return parts[-1][:40] if parts else ""


def signal_day(value: Any) -> str:
    """The day a signal happened, as ``YYYY-MM-DD``, from ``YYYY-MM-DD`` or ``YYYY-MM`` (taken as the middle of that
    month). Anything else, or a day that does not exist, is no date at all."""
    text = _clean(value, 20)
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if not match:
        return ""
    import datetime as dt
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 15)).isoformat()
    except ValueError:
        return ""


def headcount(size: str) -> int:
    """How many people work there, when the size text says ('220 people', '1,200', 'Founded 2015, 80 staff'); else 0."""
    text = (size or "").lower().replace(",", "")
    number = r"(\d+(?:\.\d+)?)(k\b)?"
    match = re.search(number + r"(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?k?\b)?\+?(?:\s+[a-z-]+){0,2}?\s*"
                      r"(?:people|employees?|staff|headcount|ftes?|persons?)\b", text)
    if not match and re.fullmatch(r"[\d\s.k+–-]+", text):
        match = re.search(number, text)
    return int(float(match.group(1)) * (1000 if match.group(2) else 1)) if match else 0


def size_band(size: str) -> str:
    """A headcount band, when the size text gives a headcount ('220 people', 'Founded 2015, 80 staff', '1,200').

    The headcount is the number that sits with the word for people, not the first number in the text.
    """
    text = (size or "").lower().replace(",", "")
    number = r"(\d+(?:\.\d+)?)(k\b)?"
    match = re.search(number + r"(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?k?\b)?\+?(?:\s+[a-z-]+){0,2}?\s*"
                      r"(?:people|employees?|staff|headcount|ftes?|persons?)\b", text)
    if not match and re.fullmatch(r"[\d\s.k+–-]+", text):      # a bare number or range is a headcount too
        match = re.search(number, text)
    if not match:
        return ""
    count = float(match.group(1)) * (1000 if match.group(2) else 1)
    for limit, label in SIZE_BANDS:
        if count < limit:
            return label
    return SIZE_BANDS[-1][1]


def _refs(args: dict) -> list:
    """The companies an action names: ``targets`` (a list or one string) or ``target``."""
    raw = args.get("targets") if args.get("targets") is not None else args.get("target")
    if isinstance(raw, str):
        raw = [raw]
    return [str(r).strip() for r in raw if isinstance(r, (str, int)) and str(r).strip()] if isinstance(raw, (list, tuple)) else []


def _whole(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(low, min(number, high))


def _people(raw: Any) -> list:
    """Buyers at a company, as a clean list of {name, title, linkedin, email, why}: one entry per person.

    The same person seen again (by name, or by LinkedIn URL) updates their entry rather than adding another,
    so what is learned later fills in or corrects what was known.
    """
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            name, _, title = item.partition(" — ") if " — " in item else item.partition(" - ")
            item = {"name": name, "title": title}
        if not isinstance(item, dict):
            continue
        person = {k: v for k, v in (("name", _clean(item.get("name"), 80)), ("title", _clean(item.get("title"), 100)),
                                    ("linkedin", _clean(item.get("linkedin"), 200)), ("email", _clean(item.get("email"), 120)),
                                    ("why", _clean(item.get("why"), 200))) if v}
        if not person.get("name"):
            continue
        same = next((p for p in out if p["name"].lower() == person["name"].lower()
                     or (person.get("linkedin") and p.get("linkedin", "").lower() == person["linkedin"].lower())), None)
        if same is not None:
            same.update(person)
        elif len(out) < MAX_PEOPLE:
            out.append(person)
    return out


class Markets:
    def __init__(self, desk):
        self.desk = desk

    @property
    def store(self) -> st.Store:
        return self.desk.store

    # -- storage --------------------------------------------------------------------------------
    def _market(self, ref: Any) -> Optional[dict]:
        ref = str(ref or "").strip()
        with self.store._conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM markets ORDER BY updated_at DESC").fetchall()]
        if not rows:
            return None
        if not ref:
            return _unpack(rows[0]) if len(rows) == 1 else None
        slug = st.slugify(ref)
        for row in rows:
            if row["id"] in (ref, slug) or row["name"].lower() == ref.lower():
                return _unpack(row)
        close = [r for r in rows if ref.lower() in r["name"].lower()]
        return _unpack(close[0]) if len(close) == 1 else None

    def _missing(self, ref: Any) -> dict:
        names = [m["name"] for m in self.list_markets()]
        if not names:
            return {"ok": False, "error": "no market maps yet; start one with gtm_market(action='define')"}
        return {"ok": False, "error": "which market? pass `market`" if not ref else "no market map matches '%s'" % ref,
                "markets": names}

    def list_markets(self) -> list:
        with self.store._conn() as conn:
            rows = conn.execute(
                "SELECT m.*, (SELECT count(*) FROM targets t WHERE t.market_id = m.id AND t.status != 'dismissed') AS companies, "
                "(SELECT count(*) FROM targets t WHERE t.market_id = m.id AND t.status = 'tracked') AS tracked "
                "FROM markets m ORDER BY m.updated_at DESC").fetchall()
        return [{"id": r["id"], "name": r["name"], "brief": r["brief"], "companies": r["companies"], "in_pipeline": r["tracked"]}
                for r in rows]

    def targets(self, market_id: str, status: str = "", segment: str = "") -> list:
        sql, params = "SELECT * FROM targets WHERE market_id = ?", [market_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if segment:
            sql += " AND lower(segment) LIKE ?"
            params.append("%" + segment.lower() + "%")
        with self.store._conn() as conn:
            return [_unpack_target(dict(r)) for r in conn.execute(sql + " ORDER BY fit DESC, company", params).fetchall()]

    def target(self, ref: str, market_id: str = "") -> Optional[dict]:
        ref = str(ref or "").strip()
        with self.store._conn() as conn:
            row = conn.execute("SELECT * FROM targets WHERE id = ?", (ref,)).fetchone()
            if row is None:
                sql, params = "SELECT * FROM targets WHERE (lower(company) = lower(?) OR lower(domain) = lower(?))", [ref, ref]
                if market_id:
                    sql += " AND market_id = ?"
                    params.append(market_id)
                rows = conn.execute(sql + " ORDER BY status = 'dismissed', updated_at DESC", params).fetchall()
                row = rows[0] if rows else None
        return _unpack_target(dict(row)) if row else None

    def _save_target_detail(self, target_id_: str, detail: dict) -> None:
        with self.store._conn() as conn:
            conn.execute("UPDATE targets SET detail = ?, updated_at = ? WHERE id = ?", (json.dumps(detail), st.now(), target_id_))

    # -- actions --------------------------------------------------------------------------------
    def run(self, args: dict, session: Optional[dict] = None) -> dict:
        action = str(args.get("action") or "show").lower()
        handler = {"define": self.define, "list": self.list_action, "add": self.add, "show": self.show, "track": self.track,
                   "dismiss": self.dismiss, "search": self.search, "people": self.people, "reading": self.reading,
                   "export": self.export}.get(action)
        if action == "post":
            return self.post(args, session or {})
        if handler is None:
            return {"ok": False, "error": "unknown action '%s'" % action,
                    "actions": ["define", "list", "add", "show", "reading", "search", "people", "track", "dismiss", "post", "export"]}
        result = handler(args)
        self._touch(args, session)
        return result

    def _touch(self, args: dict, session: Optional[dict]) -> None:
        """Tell the meter which map this turn is working on, so the turn's cost is counted against it."""
        meter, session_id = getattr(self.desk, "usage", None), (session or {}).get("session_id")
        if meter is None or not session_id:
            return
        try:
            market = self._market(args.get("market") or args.get("name"))
            if market is None and args.get("target"):
                target = self.target(str(args.get("target")))
                market = self._market(target["market_id"]) if target else None
            if market is not None:
                meter.touch(session_id, self.store, market["id"])
        except Exception:
            pass

    # -- what a map has on it, what it has cost, and the limits set on it -------------------------
    @staticmethod
    def _limits(market: dict) -> dict:
        raw = market["detail"].get("limits") if isinstance(market["detail"].get("limits"), dict) else {}
        out = {name: _whole(raw.get(name) or 0, 0, 0, 100000) for name in ("companies", "lookups", "searches")}
        try:
            out["budget"] = max(0.0, min(float(raw.get("budget") or 0), 100000.0))
        except (TypeError, ValueError):
            out["budget"] = 0.0
        return out

    def _count(self, market_id: str, name: str) -> int:
        value = self.store.meta_get("map:%s:%s" % (market_id, name), "0")
        return int(value) if str(value).lstrip("-").isdigit() else 0

    def _over_budget(self, market: dict) -> str:
        """Why Hermes should stop working on this map for now, or "" while it is inside the budget set for it."""
        budget = self._limits(market)["budget"]
        if not budget:
            return ""          # no budget set: nothing to read
        spent = self._count(market["id"], "cost:" + st.today()[:7]) / 1_000_000
        if spent >= budget:
            return ("This map has used the $%.2f set for Hermes's work on it this month ($%.2f so far). Stop working on it here and "
                    "tell the person; they can raise the limit under Map facts." % (budget, spent))
        return ""

    def set_limits(self, ref: Any, body: dict) -> dict:
        """The limits a person sets on one map: how many companies it may hold, how many paid lookups and searches a
        month, and how much of Hermes's work a month. 0 means no limit."""
        market = self._market(ref)
        if market is None:
            return {"ok": False, "error": "That map is no longer on the desk."}
        detail = dict(market["detail"])
        detail["limits"] = self._limits({"detail": {"limits": body if isinstance(body, dict) else {}}})
        with self.store._conn() as conn:
            conn.execute("UPDATE markets SET detail = ? WHERE id = ?", (json.dumps(detail), market["id"]))
        return {"ok": True, "limits": detail["limits"]}

    def facts(self, ref: Any) -> dict:
        """A map by the numbers: what is on it and how complete it is, where it came from, what it has cost, and its limits."""
        market = self._market(ref)
        if market is None:
            return {"ok": False, "error": "That map is no longer on the desk."}
        everything = self.targets(market["id"])
        rows = [r for r in everything if r["status"] != "dismissed"]
        total = len(rows)
        has = lambda test: len([r for r in rows if test(r)])
        linked = [r for r in rows if str(r["source"]).startswith(("http://", "https://"))]
        sites = Counter(sources.domain_of(r["source"]) for r in linked)
        on_it = {"companies": total, "segments": len({r["segment"] for r in rows if r["segment"]}),
                 "in_pipeline": has(lambda r: r["status"] == "tracked"), "dismissed": len(everything) - total,
                 "requirements": len(market["detail"].get("criteria") or []),
                 "checked": has(lambda r: r["detail"].get("criteria")), "sourced": len(linked),
                 "signals": has(lambda r: r["signal"]), "dated": has(lambda r: r["signal"] and r["detail"].get("signal_date")),
                 "buyers": has(lambda r: r["detail"].get("people")), "sized": has(lambda r: headcount(r["size"])),
                 "uses": has(lambda r: r["detail"].get("uses"))}
        gaps = [(total - on_it["checked"], "checked against the requirements", "check it against the map's requirements, with the evidence"),
                (total - on_it["sourced"], "a source link", "give the page the evidence came from"),
                (total - on_it["uses"], "what it uses today", "say what it uses today for the job we do"),
                (total - on_it["sized"], "its headcount", "give its headcount"),
                (on_it["signals"] - on_it["dated"], "the date of its signal", "give the date its signal happened"),
                (total - on_it["buyers"], "a buyer named", "")]
        month = st.today()[:7]
        n = lambda name: self._count(market["id"], name)
        calls, unpriced = n("calls"), n("unpriced")
        return {"ok": True, "id": market["id"], "name": market["name"], "created": (market.get("created_at") or "")[:10],
                "updated": (market.get("updated_at") or "")[:10], "on_it": on_it,
                "sources": {"links": len(linked), "sites": len(sites), "top": [{"site": site, "links": count} for site, count in sites.most_common(6)]},
                "gaps": [{"missing": missing, "what": what, "ask": ask} for missing, what, ask in gaps if missing > 0],
                "cost": {"since": self.store.meta_get("map:%s:since" % market["id"], "")[:10], "last": self.store.meta_get("map:%s:last" % market["id"], ""),
                         "runs": n("runs"), "calls": calls, "minutes": round(n("secs") / 60, 1),
                         "tokens": {"in": n("input_tokens"), "out": n("output_tokens"), "cache_read": n("cache_read_tokens"), "cache_write": n("cache_write_tokens")},
                         # None where Hermes had no price for any call: a figure is never made up
                         "usd": None if calls and unpriced >= calls else round(n("cost") / 1_000_000, 2),
                         "usd_month": round(n("cost:" + month) / 1_000_000, 2), "unpriced": unpriced,
                         "lookups": n("pdl"), "lookups_month": n("pdl:" + month), "searches": n("exa"), "searches_month": n("exa:" + month)},
                "limits": self._limits(market)}

    def define(self, args: dict) -> dict:
        name = str(args.get("name") or args.get("market") or "").strip()
        if not name:
            return {"ok": False, "error": "define needs a name, for example 'Commercial solar installers, US Southwest'"}
        existing = self._market(name)
        segments = []
        for raw in args.get("segments") or []:
            if isinstance(raw, str):
                raw = {"name": raw}
            if isinstance(raw, dict) and raw.get("name"):
                segments.append({"name": _clean(raw["name"], 60), "description": _clean(raw.get("description"), 240)})
        criteria, seen = [], set()
        for raw in args.get("requirements") or args.get("criteria") or []:
            label = _clean(raw.get("label") or raw.get("name") if isinstance(raw, dict) else raw, 80)
            if label and _key(label) not in seen:
                seen.add(_key(label))
                criteria.append({"key": _key(label), "label": label})
        stamp = st.now()
        with self.store._conn() as conn:
            if existing:
                detail = dict(existing["detail"])
                if criteria:
                    detail["criteria"] = criteria[:MAX_CRITERIA]
                conn.execute("UPDATE markets SET brief = ?, segments = ?, detail = ?, updated_at = ? WHERE id = ?",
                             (str(args.get("brief") or existing["brief"])[:1200],
                              json.dumps(segments or existing["segments"]), json.dumps(detail), stamp, existing["id"]))
                market_id = existing["id"]
            else:
                market_id, n = st.slugify(name), 1
                while conn.execute("SELECT 1 FROM markets WHERE id = ?", (market_id,)).fetchone():
                    n += 1
                    market_id = st.slugify(name)[:st.ID_MAX - 3] + "-%d" % n
                conn.execute("INSERT INTO markets (id, name, brief, segments, detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                             (market_id, name[:120], str(args.get("brief") or "")[:1200], json.dumps(segments),
                              json.dumps({"criteria": criteria[:MAX_CRITERIA]}), stamp, stamp))
        market = self._market(market_id)
        self._refresh(market_id)
        return {"ok": True, "market": {"id": market["id"], "name": market["name"], "brief": market["brief"],
                                       "segments": market["segments"], "requirements": [c["label"] for c in market["detail"].get("criteria", [])]},
                "updated": bool(existing),
                "next": "Research companies for each segment, then gtm_market(action='add') with the evidence for each."}

    def list_action(self, args: dict) -> dict:
        return {"ok": True, "markets": self.list_markets()}

    def add(self, args: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        companies = args.get("companies") or []
        if isinstance(companies, dict):
            companies = [companies]
        current = market["detail"].get("criteria") or []
        known = {c["key"]: c["key"] for c in current}
        known.update({_key(c["label"]): c["key"] for c in current})
        added, updated, problems = [], [], []
        stamp = st.now()
        stop = self._over_budget(market)
        cap = self._limits(market)["companies"]
        room = cap - len([r for r in self.targets(market["id"]) if r["status"] != "dismissed"]) if cap else None
        held = []      # companies not added because the map is at the limit a person set
        with self.store._conn() as conn:
            for raw in companies[:60]:
                if isinstance(raw, str):
                    raw = {"company": raw}
                company = str((raw or {}).get("company") or (raw or {}).get("name") or "").strip()
                if not company:
                    problems.append("an entry had no company name")
                    continue
                values = {k: _clean(raw.get(k)) for k in FIELDS}
                values["domain"] = sources.domain_of(values["domain"] or str(raw.get("website") or ""))
                fit = raw.get("fit")
                tid = target_id(market["id"], company)
                row = conn.execute("SELECT * FROM targets WHERE id = ?", (tid,)).fetchone()
                if row is None and (stop or (room is not None and room <= 0)):
                    held.append(company)
                    continue
                if row is None and room is not None:
                    room -= 1
                detail = _unpack_target(dict(row))["detail"] if row else {}
                for key in ("uses", "also"):
                    if _clean(raw.get(key), 80):
                        detail[key] = _clean(raw.get(key), 80)
                if values["signal"] and row and values["signal"] != row["signal"]:
                    detail.pop("signal_date", None)                # a new signal does not keep the old one's date
                if signal_day(raw.get("signal_date")):
                    detail["signal_date"] = signal_day(raw.get("signal_date"))
                given = raw.get("requirements") if isinstance(raw.get("requirements"), dict) else raw.get("criteria")
                if isinstance(given, dict):
                    checked = dict(detail.get("criteria") or {})
                    for label, evidence in given.items():
                        key = known.get(_key(_clean(label, 80)))      # a requirement is kept to 80 characters, so match it the same length
                        if key is None:
                            problems.append("%s: '%s' is not one of this market's requirements" % (company, label))
                            continue
                        checked[key] = True if evidence is True else _clean(evidence, 240) if evidence else ""
                    detail["criteria"] = checked
                people = _people((detail.get("people") or []) + (raw.get("people") if isinstance(raw.get("people"), list) else []))
                if people:
                    detail["people"] = people
                if isinstance(fit, bool) or not isinstance(fit, (int, float)):
                    fit = None
                if fit is not None:
                    detail["fit_set"] = True                       # scored by hand: leave it alone from now on
                elif isinstance(given, dict) and current and not detail.get("fit_set"):
                    shown = len([c for c in current if detail.get("criteria", {}).get(c["key"])])   # read it off the requirements
                    fit = max(1, int(5 * shown / len(current) + 0.5))
                if row:
                    sets = {k: v for k, v in values.items() if v and v != row[k]}
                    if fit is not None:
                        sets["fit"] = max(1, min(int(fit), 5))
                    sets["detail"] = json.dumps(detail)
                    conn.execute("UPDATE targets SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % k for k in sets),
                                 list(sets.values()) + [stamp, tid])
                    updated.append(company)
                else:
                    conn.execute(
                        "INSERT INTO targets (id, market_id, company, domain, segment, fit, size, location, why, signal, source, "
                        "detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, market["id"], company[:120], values["domain"], values["segment"],
                         max(1, min(int(fit), 5)) if fit is not None else 3, values["size"], values["location"],
                         values["why"], values["signal"], values["source"], json.dumps(detail), stamp, stamp))
                    added.append(company)
            conn.execute("UPDATE markets SET updated_at = ? WHERE id = ?", (stamp, market["id"]))
        out = {"ok": True, "market": market["name"], "added": added, "updated": updated,
               "total": len(self.targets(market["id"]))}
        if problems:
            out["problems"] = problems[:20]
        # What the map still does not know about the companies just recorded. Said once, so it can be filled in while the
        # sources are to hand; never a reason to guess.
        touched = set(added) | set(updated)
        mine = [r for r in self.targets(market["id"]) if r["company"] in touched]
        short = [(len([r for r in mine if not r["detail"].get("uses")]), "what it uses today for this job (`uses`)"),
                 (len([r for r in mine if not headcount(r["size"])]), "its headcount (`size`)"),
                 (len([r for r in mine if r["signal"] and not r["detail"].get("signal_date")]), "the date of its signal (`signal_date`)")]
        if any(count for count, _ in short):
            out["still_missing"] = ("Of the companies just recorded: " + "; ".join("%d without %s" % item for item in short if item[0])
                                    + ". Add these where a source says so; leave them out where none does. Never guess.")
        if held:
            out["not_added"] = held[:60]
            out["limit"] = stop or ("This map is at the %d companies a person set as its limit, so these were not added. Do not work "
                                    "round it: tell the person; they can raise the limit under Map facts, or dismiss some." % cap)
        self._refresh(market["id"])
        return out

    def reading(self, args: dict) -> dict:
        """Save what the map means: a short reading, what to do about it, and the first calls."""
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        detail = dict(market["detail"])
        if args.get("reading") is not None:
            detail["reading"] = str(args.get("reading") or "").strip()[:1600]
        if isinstance(args.get("implications"), list):
            detail["implications"] = [_clean(x, 260) for x in args["implications"] if _clean(x)][:5]
        unknown = []
        if isinstance(args.get("first_calls"), list):
            calls = []
            for raw in args["first_calls"][:MAX_FIRST_CALLS]:
                if isinstance(raw, str):
                    raw = {"target": raw}
                target = self.target(str((raw or {}).get("target") or (raw or {}).get("company") or ""), market["id"])
                if target is None:
                    unknown.append(str((raw or {}).get("target") or (raw or {}).get("company") or ""))
                    continue
                calls.append({"target": target["id"], "why": _clean(raw.get("why"), 240)})
            detail["first_calls"] = calls
        with self.store._conn() as conn:
            conn.execute("UPDATE markets SET detail = ?, updated_at = ? WHERE id = ?", (json.dumps(detail), st.now(), market["id"]))
        self._refresh(market["id"])
        out = {"ok": True, "market": market["name"], "saved": [k for k in ("reading", "implications", "first_calls") if k in args]}
        if unknown:
            out["not_on_the_map"] = unknown
        return out

    def show(self, args: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        summary = self.summary(market["id"], segment=str(args.get("segment") or ""), status=str(args.get("status") or "").lower())
        for seg in summary["segments"]:          # keep the tool result a readable size
            if len(seg["companies"]) > 25:
                seg["more"] = len(seg["companies"]) - 25
                seg["companies"] = seg["companies"][:25]
        note = self._room(market)
        if note:
            summary["limits"] = note          # said before the research, not after it
        return dict(summary, ok=True)

    def room(self, ref: Any) -> Optional[dict]:
        """For the page: how many more companies this map may take and why Hermes should not be asked for more, or None with no limit in the way."""
        market = self._market(ref)
        if market is None:
            return None
        cap, stop = self._limits(market)["companies"], self._over_budget(market)
        if not cap and not stop:
            return None
        held = len([r for r in self.targets(market["id"]) if r["status"] != "dismissed"])
        return {"companies": max(0, cap - held) if cap else None, "limit": cap, "stop": "budget" if stop else ""}

    def _room(self, market: dict) -> str:
        """What the limits a person set on this map leave room for, in a line Hermes reads before it starts. "" with none set."""
        limits, month, said = self._limits(market), st.today()[:7], []
        stop = self._over_budget(market)
        if stop:
            return stop
        if limits["companies"]:
            held = len([r for r in self.targets(market["id"]) if r["status"] != "dismissed"])
            said.append("room for %d more companies (limit %d)" % (max(0, limits["companies"] - held), limits["companies"]))
        if limits["budget"]:
            said.append("$%.2f of the $%.2f set for Hermes's work this month used" % (self._count(market["id"], "cost:" + month) / 1_000_000, limits["budget"]))
        if limits["lookups"]:
            said.append("%d of %d paid lookups this month used" % (self._count(market["id"], "pdl:" + month), limits["lookups"]))
        if limits["searches"]:
            said.append("%d of %d searches this month used" % (self._count(market["id"], "exa:" + month), limits["searches"]))
        return ("Limits a person set on this map: " + "; ".join(said) + ". Plan inside them, and say so if they stop you.") if said else ""

    def summary(self, market_id: str, segment: str = "", status: str = "", text: bool = True, with_rows: bool = False) -> dict:
        """Everything the map says, computed from what has been recorded. Used by the tool, the web tab and the page."""
        market = self._market(market_id)
        everything = self.targets(market["id"], "", segment)
        rows = [r for r in everything if (r["status"] == status if status in STATUSES else r["status"] != "dismissed")]
        criteria = market["detail"].get("criteria") or []
        total = len(rows)
        place = {r["id"]: region_of(r["location"]) for r in rows}
        band = {r["id"]: size_band(r["size"]) for r in rows}
        segments = []
        for name, group in by_segment(market, rows):
            regions = Counter(place[r["id"]] for r in group if place[r["id"]])
            segments.append({
                "segment": name,
                "description": next((s.get("description", "") for s in market["segments"] if s["name"].lower() == name.lower()), ""),
                "count": len(group), "share": round(100 * len(group) / total) if total else 0,
                "in_pipeline": len([r for r in group if r["status"] == "tracked"]),
                "avg_fit": round(sum(r["fit"] for r in group) / len(group), 1),
                "regions": [k for k, _ in regions.most_common(3)],
                "uses": [k for k, _ in _tally(r["detail"].get("uses", "") for r in group)[:3]],
                "companies": [_view(r, criteria, name) for r in group]})
        # a requirement's rarity is measured only among the companies actually checked for it
        requirements = []
        for c in criteria:
            checked = [r for r in rows if c["key"] in (r["detail"].get("criteria") or {})]
            shown = len([r for r in checked if r["detail"]["criteria"].get(c["key"])])
            requirements.append({"label": c["label"], "shown": shown, "of": len(checked),
                                 "rate": round(100 * shown / len(checked)) if checked else 0})
        keys = {c["key"] for c in criteria}
        assessed = len([r for r in rows if keys & set(r["detail"].get("criteria") or {})])
        using = [r for r in rows if r["detail"].get("uses")]
        holds = []
        for name, count in _tally(r["detail"]["uses"] for r in using)[:8]:
            holds.append({"name": name, "count": count, "share": round(100 * count / len(using)),
                          "companies": [r["company"] for r in using if r["detail"]["uses"].lower() == name.lower()][:6]})
        seg_names = {s["segment"].lower(): s["segment"] for s in segments}
        seg_names.update({s["name"].lower(): s["name"] for s in market["segments"]})
        pairs: dict = {}
        for r in rows:
            other = seg_names.get(str(r["detail"].get("also", "")).lower())
            own = r["segment"] or "Unsorted"
            if other and other.lower() != own.lower():
                pairs.setdefault(tuple(sorted((own, other), key=str.lower)), []).append(r["company"])
        bridges = [{"between": list(pair), "count": len(names), "companies": names[:8]}
                   for pair, names in sorted(pairs.items(), key=lambda kv: -len(kv[1]))]
        calls = self.first_calls(market, rows)
        out = {
            "market": {"id": market["id"], "name": market["name"], "brief": market["brief"],
                       "requirements": [c["label"] for c in criteria], "reading": market["detail"].get("reading", ""),
                       "implications": market["detail"].get("implications") or [], "updated_at": market["updated_at"]},
            "numbers": {"companies": total, "segments": len(segments),
                        "in_pipeline": len([r for r in rows if r["status"] == "tracked"]),
                        "with_signal": len([r for r in rows if r["signal"]]),
                        "buyers_named": sum(len(r["detail"].get("people") or []) for r in rows),
                        "assessed": assessed, "bridges": sum(b["count"] for b in bridges),
                        "avg_fit": round(sum(r["fit"] for r in rows) / total, 1) if total else 0,
                        "dismissed": len([r for r in everything if r["status"] == "dismissed"])},
            "segments": segments,
            "requirements": requirements,
            "holds": holds, "holds_of": len(using),
            "regions": [{"name": k, "count": v} for k, v in Counter(v for v in place.values() if v).most_common(8)],
            "sizes": [{"band": label, "count": list(band.values()).count(label)} for _, label in SIZE_BANDS if label in band.values()],
            "bridges": bridges,
            "first_calls": calls,
        }
        if text:
            out["map"] = map_text(market, rows, requirements, holds, bridges, calls)
        if with_rows:
            out["_rows"] = rows
        return out

    def first_calls(self, market: dict, rows: list) -> list:
        """The few companies to ring first: the ones the agent chose, else the strongest fits, spread across segments."""
        by_id = {r["id"]: r for r in rows}
        chosen = [(by_id[c["target"]], c.get("why", "")) for c in market["detail"].get("first_calls") or [] if c.get("target") in by_id]
        if not chosen:
            per_segment: Counter = Counter()
            ranked = sorted((r for r in rows if r["status"] == "new"),
                            key=lambda r: (-r["fit"], not r["signal"], -len([v for v in (r["detail"].get("criteria") or {}).values() if v]),
                                           r["company"].lower()))
            for r in ranked:
                if per_segment[r["segment"]] < 2 and r["fit"] >= 3:
                    per_segment[r["segment"]] += 1
                    chosen.append((r, r["signal"] or r["why"]))
                if len(chosen) >= 5:
                    break
        return [{"id": r["id"], "company": r["company"], "segment": r["segment"] or "Unsorted", "fit": r["fit"], "why": why,
                 "status": r["status"], "buyer": ((r["detail"].get("people") or [{}])[0]).get("name", "")}
                for r, why in chosen[:MAX_FIRST_CALLS]]

    def search(self, args: dict) -> dict:
        key = self.desk.secret(EXA_KEY)
        if not key:
            return {"ok": False, "error": "Exa is not set up (EXA_API_KEY), so use your own web search tool to find companies, "
                                          "then record them with gtm_market(action='add')."}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "search needs a query describing the kind of company"}
        market, month = self._market(args.get("market")), st.today()[:7]
        if market is not None:
            stop = self._over_budget(market)
            cap = self._limits(market)["searches"]
            if not stop and cap and self._count(market["id"], "exa:" + month) >= cap:
                stop = ("This map has used the %d Exa searches a person set as its limit for the month. Stop searching for it and tell "
                        "the person; they can raise the limit under Map facts." % cap)
            if stop:
                return {"ok": False, "error": stop}
        result = sources.exa_companies(key, query, _whole(args.get("limit") or 8, 8, 1, 15), http=self.desk.http)
        if result.get("ok") and market is not None:
            self.store.bump("map:%s:exa" % market["id"])
            self.store.bump("map:%s:exa:%s" % (market["id"], month))
        if result.get("ok"):
            result["note"] = ("These are candidates, not yet on the map. Judge each against the market brief and add the ones "
                              "that fit with gtm_market(action='add'), giving the reason and the source URL.")
        return result

    def people(self, args: dict) -> dict:
        key = self.desk.secret(PDL_KEY)
        if not key:
            return {"ok": False, "error": "People Data Labs is not set up (PDL_API_KEY). Use your web search tool to find the "
                                          "right contact instead, and record them with gtm_market(action='add', companies=[{company, people}])."}
        target = self.target(str(args.get("target") or args.get("company") or ""))
        company = target["company"] if target else str(args.get("company") or "")
        domain = target["domain"] if target else sources.domain_of(str(args.get("website") or ""))
        limit = _whole(args.get("limit") or 3, 3, 1, 5)
        month = "pdl_calls:" + st.today()[:7]
        cap = _whole(self.desk.setting("pdl_monthly_cap", 50), 50, 0, 10 ** 6)
        used = _whole(self.store.meta_get(month, "0"), 0, 0, 10 ** 9)
        if cap and used + limit > cap:
            return {"ok": False, "error": "This would pass the monthly People Data Labs cap (%d used of %d). Raise "
                                          "pdl_monthly_cap in the plugin's settings to continue." % (used, cap)}
        own = self._market(target["market_id"]) if target else None
        if own is not None:
            map_cap, map_used = self._limits(own)["lookups"], self._count(own["id"], "pdl:" + st.today()[:7])
            if map_cap and map_used + limit > map_cap:
                return {"ok": False, "error": "This would pass the %d lookups a month a person set as this map's limit (%d used). "
                                              "They can raise it under Map facts." % (map_cap, map_used)}
        titles = args.get("titles") or []
        if isinstance(titles, str):
            titles = [t.strip() for t in titles.split(",")]
        result = sources.pdl_people(key, company=company, domain=domain, titles=titles, limit=limit,
                                    deciders=bool(args.get("deciders")), http=self.desk.http)
        if result.get("ok") and result.get("credits_used"):
            self.store.bump(month, result["credits_used"])
            if own is not None:
                self.store.bump("map:%s:pdl" % own["id"], result["credits_used"])
                self.store.bump("map:%s:pdl:%s" % (own["id"], st.today()[:7]), result["credits_used"])
        if result.get("ok"):
            result["company"] = company
            if target and result.get("people"):      # keep who was found on the map, so nobody pays to find them twice
                detail = dict(target["detail"])
                detail["people"] = _people((detail.get("people") or []) + [
                    {"name": p.get("name"), "title": p.get("title"), "linkedin": p.get("linkedin")} for p in result["people"]])
                self._save_target_detail(target["id"], detail)
                result["saved_to_map"] = True
                self._refresh(target["market_id"])
        return result

    def track(self, args: dict, actor: str = "") -> dict:
        """Turn targets into pipeline records at the playbook's first stage."""
        market = self._market(args.get("market"))
        refs = _refs(args)
        if not refs:
            return {"ok": False, "error": "track needs `targets`: ids or company names from the map"}
        playbook = self.desk.playbook()
        actor = actor or self.desk.actor()
        made, already, unknown = [], [], []
        for ref in refs[:25]:
            target = self.target(ref, market["id"] if market else "")
            if target is None:
                unknown.append(str(ref))
                continue
            if target["status"] == "tracked" and target["deal_id"] and self.store.get(target["deal_id"]):
                already.append(target["company"])
                continue
            owner_market = self._market(target["market_id"]) or {"name": ""}
            buyer = (target["detail"].get("people") or [{}])[0]          # the first buyer found, unless one is named now
            named = _clean(args.get("contact"), 80) if len(refs) == 1 else ""
            contact = named or buyer.get("name", "")
            person = buyer
            if named:        # start from what the map knows about that person, then apply what was passed now
                person = dict(next((p for p in target["detail"].get("people") or [] if p["name"].lower() == named.lower()), {}))
                person.update({k: _clean(args.get(k), 200) for k in ("title", "email", "linkedin") if _clean(args.get(k), 200)})
            # Where it goes: the group asked for; else what the playbook says; else, for a playbook that does not say,
            # a guess from its word for a group (an "account" is the company itself, a "search" is the map).
            rule = playbook.map_group or ("company" if playbook.terms["group"].lower() in ACCOUNT_WORDS else "map")
            group = self.desk._group_spelling(_clean(args.get("group"), 200) or (target["company"] if rule == "company" else owner_market["name"]))
            keys = {f["key"] for f in playbook.fields}
            deal = self.store.add(
                contact or target["company"], playbook.first_stage(), actor=actor, company=target["company"], grp=group,
                email=person.get("email", "") if contact else "", linkedin=person.get("linkedin", "") if contact else "",
                owner=str(args.get("owner") or ""), source="market map: %s" % owner_market["name"],
                next_step="First outreach" if contact else "Find the right contact",
                fields={k: v for k, v in (("title", person.get("title", "") if contact else ""),
                                          ("location", target["location"]), ("company_size", target["size"]),
                                          ("competitor", target["detail"].get("uses", "")),
                                          ("trigger", target["signal"])) if v and k in keys})      # why now, where the playbook keeps it
            note = " ".join(b for b in (target["why"], "Signal: %s." % target["signal"] if target["signal"] else "",
                                        "Uses today: %s." % target["detail"]["uses"] if target["detail"].get("uses") else "",
                                        "Source: %s" % target["source"] if target["source"] else "") if b)
            if note:
                self.store.log(deal["id"], "note", "From the %s map. %s" % (owner_market["name"], note), actor=actor)
            with self.store._conn() as conn:
                conn.execute("UPDATE targets SET status = 'tracked', deal_id = ?, updated_at = ? WHERE id = ?",
                             (deal["id"], st.now(), target["id"]))
            made.append({"company": target["company"], "deal": deal["id"], "market_id": target["market_id"], "contact": contact, "group": group})
        for market_id in {m["market_id"] for m in made}:
            self._refresh(market_id)
        out = {"ok": bool(made or already),
               "added_to_pipeline": [{k: v for k, v in (("company", m["company"]), ("deal", m["deal"]), ("contact", m["contact"]),
                                                         ("group", m["group"])) if v} for m in made]}
        if already:
            out["already_in_pipeline"] = already
        if unknown:
            out["not_on_a_map"] = unknown
        if not out["ok"]:
            out["error"] = "none of those are on a market map"
        return out

    def dismiss(self, args: dict) -> dict:
        refs = _refs(args)
        market = self._market(args.get("market"))
        done, touched = [], set()
        for ref in refs[:50]:
            target = self.target(ref, market["id"] if market else "")
            if target and target["status"] != "tracked":
                with self.store._conn() as conn:
                    conn.execute("UPDATE targets SET status = 'dismissed', signal = ?, updated_at = ? WHERE id = ?",
                                 (("Dismissed: %s" % args["reason"])[:400] if args.get("reason") else target["signal"],
                                  st.now(), target["id"]))
                done.append(target["company"])
                touched.add(target["market_id"])
        for market_id in touched:
            self._refresh(market_id)
        if done:
            return {"ok": True, "dismissed": done}
        return {"ok": False, "error": "nothing matched (records already in the pipeline cannot be dismissed)"}

    def rename(self, ref: Any, name: Any) -> dict:
        """Call a map something else. Its id, and so every company's, stays as it was."""
        market = self._market(ref)
        if market is None:
            return {"ok": False, "error": "That map is no longer on the desk."}
        name = _clean(name, 120)
        if not name:
            return {"ok": False, "error": "A map needs a name."}
        taken = [m for m in self.list_markets() if m["id"] != market["id"] and (m["name"].lower() == name.lower() or m["id"] == st.slugify(name))]
        if taken:
            return {"ok": False, "error": "Another map is already called that."}
        with self.store._conn() as conn:
            conn.execute("UPDATE markets SET name = ?, updated_at = ? WHERE id = ?", (name, st.now(), market["id"]))
        self._refresh(market["id"])
        return {"ok": True, "id": market["id"], "name": name}

    def remove(self, ref: Any) -> dict:
        """Delete a map and every company on it. Records already made from it stay in the pipeline. This is not on the
        ledger and cannot be taken back, so only a person at the page does it: no tool action leads here."""
        market = self._market(ref)
        if market is None:
            return {"ok": False, "error": "That map is no longer on the desk."}
        with self.store._conn() as conn:
            companies = conn.execute("SELECT count(*) FROM targets WHERE market_id = ?", (market["id"],)).fetchone()[0]
            kept = conn.execute("SELECT count(*) FROM targets WHERE market_id = ? AND status = 'tracked'", (market["id"],)).fetchone()[0]
            conn.execute("DELETE FROM targets WHERE market_id = ?", (market["id"],))
            conn.execute("DELETE FROM markets WHERE id = ?", (market["id"],))
            prefix = "map:%s:" % market["id"]
            conn.execute("DELETE FROM meta WHERE substr(k, 1, ?) = ?", (len(prefix), prefix))
        return {"ok": True, "id": market["id"], "name": market["name"], "companies": companies, "in_pipeline": kept}

    def restore(self, refs: list) -> dict:
        """Put dismissed companies back on the map."""
        done, touched = [], set()
        for ref in list(refs)[:50]:
            target = self.target(ref)
            if target and target["status"] == "dismissed":
                with self.store._conn() as conn:
                    conn.execute("UPDATE targets SET status = 'new', signal = ?, updated_at = ? WHERE id = ?",
                                 ("" if target["signal"].startswith("Dismissed: ") else target["signal"], st.now(), target["id"]))
                done.append(target["company"])
                touched.add(target["market_id"])
        for market_id in touched:
            self._refresh(market_id)
        return {"ok": bool(done), "restored": done}

    def move(self, ref: str, segment: str) -> dict:
        """Put a company in another of its map's segments."""
        target = self.target(ref)
        if target is None:
            return {"ok": False, "error": "That company is no longer on the map."}
        market = self._market(target["market_id"])
        names = {s["name"].lower(): s["name"] for s in (market or {}).get("segments") or []}
        name = names.get(str(segment or "").strip().lower())
        if not name:
            return {"ok": False, "error": "This map has no segment called %s." % _clean(segment, 80)}
        detail = dict(target["detail"])
        if str(detail.get("also") or "").lower() == name.lower():
            detail.pop("also", None)
        with self.store._conn() as conn:
            conn.execute("UPDATE targets SET segment = ?, detail = ?, updated_at = ? WHERE id = ?", (name, json.dumps(detail), st.now(), target["id"]))
        self._refresh(target["market_id"])
        return {"ok": True, "company": target["company"], "segment": name}

    def track_many(self, refs: list, actor: str) -> dict:
        """Add several companies to the pipeline in one go: a record each. No draft is asked for; that is one press away on each."""
        added, already = [], []
        for ref in list(refs)[:25]:
            result = self.click_track(str(ref), actor)
            if result.get("deal"):
                added.append({"deal": result["deal"]["id"], "name": result["deal"]["name"]})
            elif result.get("notice"):
                already.append(result["notice"])
        return {"ok": bool(added), "added": added, "skipped": already}

    def buyer_titles(self, market_id: str) -> list:
        """What a buyer's title looks like on this map: the words that say what the buyers already named here do
        ("fraud", "risk", "payments"), commonest first. A whole title as someone wrote it ("Manager, Fraud
        Investigations (US & Latam)") matches nobody else; the words in it do. Rank and region words are left out."""
        seen: Counter = Counter()
        for row in self.targets(market_id):
            for person in row["detail"].get("people") or []:
                for word in set(re.findall(r"[a-z]{4,}", _clean(person.get("title"), 80).lower())):
                    if word not in TITLE_NOISE:
                        seen[word] += 1
        return [word for word, _ in seen.most_common(6)]

    def buyers(self, refs: list) -> dict:
        """Name one buyer at each company that has none: one People Data Labs lookup each, for one person whose title
        is like those of the buyers already named on the map and whom People Data Labs ranks manager or above (a fraud
        analyst matches the word "fraud" as well as a head of fraud does, and is not the buyer), inside the monthly cap.
        Where nobody of that rank matches, nobody is saved and nothing is spent. One person is one credit, so the
        most this can spend is the number of companies. With no buyer named anywhere on the map there is no title to
        look for, and it says so rather than pay for whoever comes back. Stops at the first refusal and says why."""
        found, none, problem, titles = [], [], "", None
        for ref in list(refs)[:25]:
            target = self.target(str(ref))
            if target is None or (target["detail"].get("people") or []):
                continue
            if titles is None:
                titles = self.buyer_titles(target["market_id"])
                if not titles:
                    return {"ok": False, "found": [], "none": [], "no_titles": True,
                            "problem": "No buyer is named on this map yet, so there is no job title to look for."}
            result = self.people({"target": target["id"], "limit": 1, "titles": titles, "deciders": True})
            if not result.get("ok"):
                problem = result.get("error") or "the lookup failed"
                break
            (found if result.get("people") else none).append(target["company"])
        return {"ok": bool(found) or not problem, "found": found, "none": none, "problem": problem, "titles": titles or []}

    def post(self, args: dict, session: dict) -> dict:
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        text = self.summary(market["id"])["map"]
        slack = self.desk.slack
        channel = str(args.get("channel") or "").strip()
        if not channel and session.get("platform") == "slack":
            channel = session.get("chat_id") or ""
        if not channel and self.desk.desktop_open(session):
            return {"ok": True, "posted": False, "directive": "::stoa-map{market=\"%s\"}" % market["id"],
                    "note": "Show the map by writing that directive as a paragraph by itself, exactly as given. The app "
                            "draws the live map there, with a button on each company. Then add your reading in a few lines."}
        if slack is None or not slack.available or not channel:
            return {"ok": True, "posted": False, "map": text, "note": "Slack is not available here, so show this map as text."}
        here = session.get("platform") == "slack" and channel == session.get("chat_id")
        result = slack.post_map(channel, market["id"], session.get("thread_id") or "" if here else "",
                                session.get("session_key") or "" if here else "",
                                session.get("user_id") or "" if session.get("platform") == "slack" else "")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "Slack did not accept the map", "map": text}
        return {"ok": True, "posted": True, "note": "The map is posted with an Add to pipeline button on each company. "
                                                    "Do not repeat it; one line is enough."}

    def export(self, args: dict) -> dict:
        """Write the map as one self-contained web page: the thing to send to a colleague or a client."""
        market = self._market(args.get("market"))
        if market is None:
            return self._missing(args.get("market"))
        from . import page
        share = bool(args.get("share"))
        html = page.render(self.summary(market["id"]), self.desk.playbook().terms, share=share)
        folder = self.desk.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s%s-%s.html" % (market["id"], "-share" if share else "", st.today()))
        path.write_text(html, encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": len(html.encode("utf-8")),
                "note": "One file, no outside requests. %s" % (
                    "This version leaves out buyers' names and pipeline status." if share
                    else "This version names buyers and shows what is in the pipeline; pass share=true for one that does not.")}

    # -- for the Slack half ---------------------------------------------------------------------
    def blocks(self, market_id: str) -> list:
        market = self._market(market_id)
        if market is None:
            return []
        summary = self.summary(market_id, text=False, with_rows=True)
        return map_blocks(market, summary["_rows"], summary)

    def _refresh(self, market_id: str) -> None:
        slack = self.desk.slack
        if slack is not None:
            try:
                slack.refresh(MAP_CARD + market_id)
            except Exception:
                pass

    def click_track(self, target_ref: str, actor: str) -> dict:
        """A tap on Add to pipeline: create the record, then ask the agent to find or confirm the buyer."""
        target = self.target(target_ref)
        if target is None:
            return {"deal": None, "notice": "That company is no longer on the map."}
        result = self.track({"targets": [target["id"]]}, actor=actor)
        if not result.get("added_to_pipeline"):
            return {"deal": None, "market_id": target["market_id"], "notice": "%s is already in the pipeline." % target["company"]}
        made = result["added_to_pipeline"][0]
        deal = self.store.get(made["deal"])
        if made.get("contact"):
            task = ("The map already names %s as the likely buyer, and they are on the record. Check that against the record and "
                    "its log, then reply here with one line on why they are the right person and a first-touch note of under 90 "
                    "words that leads with the evidence from the map." % made["contact"])
        else:
            how = ("gtm_market(action=\"people\", target=\"%s\", titles=[...]) (each person returned costs a credit, so ask for at "
                   "most three)" % target["id"] if self.desk.secret(PDL_KEY) else "your web search tool")
            task = ("Work out who at %s is the most likely buyer for us, using %s. Put that person on the record with "
                    "gtm_deal(action=\"update\"), then reply here with who you picked and why, and a first-touch note of under 90 "
                    "words." % (target["company"], how))
        followup = ("[Stoa desk] %s added %s to the pipeline from the market map. The record is already created (id %s), so do "
                    "not add it again.\n\nYour task: %s Do not send anything." % (actor or "Someone", target["company"], deal["id"], task))
        return {"deal": deal, "market_id": target["market_id"], "followup": followup, "notice": ""}


def _unpack(row: dict) -> dict:
    for key, empty in (("segments", "[]"), ("detail", "{}")):
        try:
            row[key] = json.loads(row.get(key) or empty)
        except ValueError:
            row[key] = json.loads(empty)
    if not isinstance(row["detail"], dict):
        row["detail"] = {}
    return row


def _unpack_target(row: dict) -> dict:
    try:
        row["detail"] = json.loads(row.get("detail") or "{}")
    except ValueError:
        row["detail"] = {}
    if not isinstance(row["detail"], dict):
        row["detail"] = {}
    return row


def _tally(values) -> list:
    """Counts of free-text values, grouped without regard to case, shown in their most common spelling."""
    spellings: dict = {}
    for value in values:
        value = (value or "").strip()
        if value:
            spellings.setdefault(value.lower(), Counter())[value] += 1
    return sorted(((c.most_common(1)[0][0], sum(c.values())) for c in spellings.values()), key=lambda kv: (-kv[1], kv[0].lower()))


def _view(row: dict, criteria: Optional[list] = None, segment: str = "") -> dict:
    out = {k: row[k] for k in ("id", "company", "domain", "fit", "size", "location", "why", "signal", "source", "status")}
    detail = row.get("detail") or {}
    out["uses"], out["also"] = detail.get("uses", ""), detail.get("also", "")
    if row["signal"] and detail.get("signal_date"):
        out["signal_date"] = detail["signal_date"]
    if row["deal_id"]:
        out["deal"] = row["deal_id"]
    out = {k: v for k, v in out.items() if v not in ("", None)}
    if segment:
        out["segment"] = segment
    checked = detail.get("criteria") or {}
    if criteria and checked:
        out["requirements"] = [{"label": c["label"], "shown": bool(checked.get(c["key"])),
                                "evidence": checked.get(c["key"]) if isinstance(checked.get(c["key"]), str) else ""}
                               for c in criteria if c["key"] in checked]
    if detail.get("people"):
        out["people"] = detail["people"]
    count = headcount(row["size"])
    if count:
        out["headcount"] = count
    out["added"] = (row.get("created_at") or "")[:10]
    out["added_at"] = row.get("created_at") or ""
    return out


def by_segment(market: dict, rows: list) -> list:
    """``[(segment name, rows)]`` in the market's declared order, then any others, then unsorted."""
    order = [s["name"] for s in market.get("segments") or []]
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["segment"] or "Unsorted", []).append(row)
    lowered = {name.lower(): name for name in groups}
    names = [lowered[o.lower()] for o in order if o.lower() in lowered]
    names += sorted(n for n in groups if n not in names and n != "Unsorted")
    if "Unsorted" in groups and "Unsorted" not in names:
        names.append("Unsorted")
    return [(name, groups[name]) for name in names]


def _facts(row: dict) -> str:
    return " · ".join(b for b in (row["size"], row["location"]) if b)


def numbers_line(rows: list) -> str:
    tracked = len([r for r in rows if r["status"] == "tracked"])
    bits = ["%d companies" % len(rows), "%d in the pipeline" % tracked]
    signals = len([r for r in rows if r["signal"]])
    buyers = sum(len((r.get("detail") or {}).get("people") or []) for r in rows)
    if signals:
        bits.append("%d with a signal" % signals)
    if buyers:
        bits.append("%d buyers named" % buyers)
    return " · ".join(bits)


def map_text(market: dict, rows: list, requirements: Optional[list] = None, holds: Optional[list] = None,
             bridges: Optional[list] = None, first_calls: Optional[list] = None) -> str:
    lines = ["Market map · %s" % market["name"], numbers_line(rows)]
    if market.get("brief"):
        lines.append(market["brief"])
    if not rows:
        lines.append("")
        lines.append("Nothing on the map yet.")
    for name, group in by_segment(market, rows):
        lines.append("")
        lines.append("%s (%d)" % (name, len(group)))
        for row in group[:15]:
            facts = _facts(row)
            line = "  %s %s%s" % (dots(row["fit"]), row["company"], " · " + facts if facts else "")
            if row["status"] == "tracked":
                line += "  [in pipeline]"
            lines.append(line)
            if row["why"]:
                lines.append("        %s" % row["why"][:160])
        if len(group) > 15:
            lines.append("  … and %d more" % (len(group) - 15))
    if requirements and any(r["of"] for r in requirements):
        lines += ["", "How rare each requirement is"]
        lines += ["  %3d%%  %s (%d of %d checked)" % (r["rate"], r["label"], r["shown"], r["of"]) for r in requirements if r["of"]]
    if holds:
        lines += ["", "Who holds this market today"]
        lines += ["  %3d%%  %s (%d)" % (h["share"], h["name"], h["count"]) for h in holds[:6]]
    if bridges:
        lines += ["", "Where the segments meet"]
        lines += ["  %s ↔ %s: %s" % (b["between"][0], b["between"][1], ", ".join(b["companies"][:4])) for b in bridges[:5]]
    if first_calls:
        lines += ["", "First calls"]
        lines += ["  %d. %s%s" % (i + 1, c["company"], " — " + c["why"][:110] if c["why"] else "") for i, c in enumerate(first_calls)]
    return "\n".join(lines)


def map_blocks(market: dict, rows: list, summary: Optional[dict] = None, budget: int = 44) -> list:
    """Block Kit for a map: one row per company with an Add to pipeline button."""
    from .cards import PREFIX, _escape
    head = "*Market map · %s*\n`%s`" % (_escape(market["name"]), numbers_line(rows))
    if market.get("brief"):
        head += "\n%s" % _escape(market["brief"][:600])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    facts = []
    if summary:
        if summary["holds"]:
            facts.append("*Holds the market:* " + ", ".join("%s %d%%" % (_escape(h["name"]), h["share"]) for h in summary["holds"][:3]))
        scarce = sorted((r for r in summary["requirements"] if r["of"]), key=lambda r: r["rate"])[:1]
        if scarce:
            facts.append("*Rarest requirement:* %s (%d%%)" % (_escape(scarce[0]["label"]), scarce[0]["rate"]))
        calls = [c for c in summary["first_calls"] if c["status"] != "tracked"][:3]
        if calls:
            facts.append("*First calls:* " + ", ".join(_escape(c["company"]) for c in calls))
    if facts:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f[:1900]} for f in facts]})
    groups = by_segment(market, rows)
    per_segment = max(2, (budget - len(blocks) - 2 * len(groups)) // max(1, len(groups)))
    hidden = 0
    for name, group in groups:
        if len(blocks) + 3 > budget:
            hidden += len(group)
            continue
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "*%s*  ·  %d" % (_escape(name.upper()), len(group))}]})
        for row in group[:per_segment]:
            if len(blocks) + 1 > budget:
                hidden += 1
                continue
            facts_line = _facts(row)
            text = "`%s`  *%s*%s" % (dots(row["fit"]), _escape(row["company"]), "  ·  " + _escape(facts_line) if facts_line else "")
            if row["why"]:
                text += "\n%s" % _escape(row["why"][:220])
            block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
            if row["status"] == "tracked":
                block["text"]["text"] += "\n:white_check_mark: _in the pipeline_"
            else:
                block["accessory"] = {"type": "button", "action_id": PREFIX + "track", "value": row["id"],
                                      "text": {"type": "plain_text", "text": "Add to pipeline"}}
            blocks.append(block)
        hidden += max(0, len(group) - per_segment)
    if hidden:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "%d more on the map. Ask me to show a segment." % hidden}]})
    return blocks
