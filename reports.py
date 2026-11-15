"""Reports: what the desk's own records add up to.

Every figure here is counted by the plugin from the desk's database. No model is called to make one and nothing is
sent anywhere, so a report costs nothing to open and reads the same every time. A report is a small description
(what is counted, grouped by what, over which records) and the arithmetic below. Nine come with the desk; a person
on the page, or Hermes when asked in words, can add more from the same few choices. One that Hermes makes waits as
a draft until the person keeps it.

Reports belong to a pipeline: its stages are what most of them are measured against.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from typing import Any, Optional

from . import cards
from . import store as st

PERIODS = (("30", "Last 30 days"), ("90", "Last 90 days"), ("quarter", "This quarter"), ("year", "This year"), ("all", "All time"))
COUNTS = ("records", "value")
WHENS = ("entered", "closed", "open")
SPLITS = ("none", "furthest", "outcome")
BYS = ("source", "owner", "stage", "group", "company", "reason", "month")
ONLY_KEYS = ("status", "owner", "source", "stage", "group", "company", "min_value")
MOST_ROWS = 12          # a chart with more bars than this folds the smallest into "Others"
MOST_CUSTOM = 40
SHORT_STAY = 60         # seconds: a record moved on within a minute was a correction, not a stay

# The reports every desk starts with. `spec` marks the ones made from the same choices a person's own are.
BUILT_IN = (
    {"id": "stages", "kind": "stages", "title": "Pipeline by stage"},
    {"id": "winrate", "kind": "winrate", "title": "Win rate"},
    {"id": "time", "kind": "time", "title": "Time in stage"},
    {"id": "sources", "kind": "groups", "title": "Where {deals} come from",
     "spec": {"count": "records", "by": "source", "when": "entered", "split": "furthest", "only": {}}},
    {"id": "lost", "kind": "groups", "title": "Why we lost",
     "spec": {"count": "records", "by": "reason", "when": "closed", "split": "none", "only": {"status": "lost"}}},
    {"id": "map", "kind": "map", "title": "What the map turned into"},
    {"id": "hermes", "kind": "hermes", "title": "What Hermes did"},
    {"id": "owners", "kind": "groups", "title": "Who holds what", "unpinned": True,
     "spec": {"count": "value", "by": "owner", "when": "open", "split": "none", "only": {}}},
    {"id": "months", "kind": "groups", "title": "New {deals} by month", "unpinned": True,
     "spec": {"count": "records", "by": "month", "when": "entered", "split": "outcome", "only": {}}},
)
BUILT_IN_IDS = tuple(entry["id"] for entry in BUILT_IN)


def _iso(day: dt.date) -> str:
    return day.isoformat()


def window(period: Any, today: Optional[dt.date] = None) -> dict:
    """The days a period covers, and the same stretch before it to set it against (none for all time)."""
    today = today or dt.date.today()
    period = str(period or "90")
    labels = dict(PERIODS)
    if period in ("30", "90"):
        days = int(period)
        start = today - dt.timedelta(days=days - 1)
        return {"id": period, "label": labels[period], "words": "the last %d days" % days, "from": _iso(start), "to": _iso(today),
                "before": {"label": "the %d days before" % days, "from": _iso(start - dt.timedelta(days=days)), "to": _iso(start - dt.timedelta(days=1))}}
    if period == "quarter":
        start = dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        last = start - dt.timedelta(days=1)
        return {"id": period, "label": labels[period], "words": "this quarter", "from": _iso(start), "to": _iso(today),
                "before": {"label": "last quarter", "from": _iso(dt.date(last.year, 3 * ((last.month - 1) // 3) + 1, 1)), "to": _iso(last)}}
    if period == "year":
        return {"id": period, "label": labels[period], "words": "this year", "from": _iso(dt.date(today.year, 1, 1)), "to": _iso(today),
                "before": {"label": "last year", "from": _iso(dt.date(today.year - 1, 1, 1)), "to": _iso(dt.date(today.year - 1, 12, 31))}}
    return {"id": "all", "label": labels["all"], "words": "all time", "from": "", "to": _iso(today), "before": None}


def inside(stamp: str, win: Optional[dict]) -> bool:
    day = str(stamp or "")[:10]
    return bool(day) and win is not None and (not win["from"] or day >= win["from"]) and day <= win["to"]


def _pct(part: float, whole: float) -> Optional[int]:
    return round(100 * part / whole) if whole else None


def _count(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


def _days(value: float) -> str:
    return "%d day%s" % (round(value), "" if round(value) == 1 else "s")


def origin(deal: dict, sample_source: str = "") -> str:
    """Where a record came from, as a report groups it: every market map is one source, and a record nobody gave a
    source says so. A sample record carries a made-up one, so a tour's report has something to show."""
    made_up = str((deal.get("fields") or {}).get("_origin") or "").strip()
    if made_up:
        return made_up
    source = str(deal.get("source") or "").strip()
    if not source:
        return "Not recorded"
    if sample_source and source == sample_source:
        return "Sample data"
    if source.lower().startswith("market map"):
        return "Market map"
    if source.lower().startswith("intake:"):
        source = source.split(":", 1)[1].strip() or "A list"
    return source[:1].upper() + source[1:]


class Reports:
    def __init__(self, desk):
        self.desk = desk

    @property
    def store(self) -> st.Store:
        return self.desk.store

    # -- what is kept ---------------------------------------------------------------------------
    def _saved(self) -> dict:
        try:
            data = json.loads(self.store.meta_get("reports") or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
        pins = data.get("pins") if isinstance(data.get("pins"), list) else None
        return {"pins": pins, "custom": custom}

    def _write(self, data: dict) -> None:
        self.store.meta_set("reports", json.dumps({"pins": data["pins"], "custom": data["custom"]}, sort_keys=True))

    def _pins(self, saved: dict) -> list:
        if saved["pins"] is None:
            return [entry["id"] for entry in BUILT_IN if not entry.get("unpinned")]
        known = set(BUILT_IN_IDS) | set(saved["custom"])
        return [ref for ref in saved["pins"] if ref in known]

    def catalog(self) -> list:
        """Every report this pipeline has: the ones that come with the desk, then the ones made here."""
        saved, playbook = self._saved(), self.desk.playbook()
        pins = self._pins(saved)
        out = []
        for entry in BUILT_IN:
            title = entry["title"].replace("{deals}", playbook.terms["deals"])
            if entry["id"] == "lost" and "lost" not in playbook.lost["label"].lower():
                title = "%s, and why" % playbook.lost["label"]
            out.append({"id": entry["id"], "kind": entry["kind"], "title": title, "spec": entry.get("spec"), "custom": False,
                        "draft": False, "pinned": entry["id"] in pins})
        for ref, kept in sorted(saved["custom"].items(), key=lambda item: str(item[1].get("at") or "")):
            spec, _ = self.clean(kept.get("spec"))
            if spec is None:
                continue
            out.append({"id": ref, "kind": "groups", "title": str(kept.get("title") or "Report"), "spec": spec, "custom": True,
                        "draft": bool(kept.get("draft")), "replaces": str(kept.get("replaces") or ""), "asked": str(kept.get("asked") or ""),
                        "by": str(kept.get("by") or ""), "at": str(kept.get("at") or ""), "pinned": ref in pins,
                        "period": str(kept.get("period") or "")})
        return out

    def find(self, ref: Any) -> Optional[dict]:
        """A report by its id, else by its title or a clear part of it."""
        wanted = str(ref or "").strip().lower()
        if not wanted:
            return None
        listed = self.catalog()
        for entry in listed:
            if entry["id"].lower() == wanted:
                return entry
        named = [entry for entry in listed if entry["title"].lower() == wanted] or [entry for entry in listed if wanted in entry["title"].lower()]
        return named[0] if len(named) == 1 else None

    # -- the choices a report is made from -------------------------------------------------------
    def choices(self) -> dict:
        """What a report can count, group by and be narrowed to, in this pipeline's own words."""
        playbook = self.desk.playbook()
        deals = self.store.list(status="all")
        sample = self._sample()
        return {"count": [["records", playbook.terms["deals"].capitalize()], ["value", "Value"]],
                "by": [["source", "Source"], ["owner", "Owner"], ["stage", "Stage"], ["group", playbook.terms["group"].capitalize()],
                       ["company", "Company"], ["reason", "Reason lost"], ["month", "Month"]]
                      + [["field:" + field["key"], field["label"]] for field in playbook.fields if not str(field["key"]).startswith("_")],
                "when": [["entered", "Entered in the period"], ["closed", "Closed in the period"], ["open", "Open now"]],
                "split": [["none", "Nothing"], ["furthest", "Furthest stage reached"], ["outcome", "How it ended"]],
                "periods": [list(item) for item in PERIODS],
                "status": [["", "Any"], ["open", "Open"], ["won", playbook.won["label"]], ["lost", playbook.lost["label"]]],
                "owners": sorted({d["owner"] for d in deals if d["owner"]}),
                "sources": sorted({origin(d, sample) for d in deals}),
                "stages": [[s["id"], s["label"]] for s in playbook.stages]}

    def clean(self, raw: Any) -> tuple:
        """A report's description, checked: ``(spec, None)`` or ``(None, what is wrong)``. Anything it does not
        know is refused by name, with what it does take, so a model that guessed is told and can try again."""
        if not isinstance(raw, dict):
            return None, "a report needs count, by and when"
        playbook = self.desk.playbook()
        fields = {str(field["key"]) for field in playbook.fields}
        count = str(raw.get("count") or "records").lower()
        when = str(raw.get("when") or "entered").lower()
        split = str(raw.get("split") or "none").lower()
        by = str(raw.get("by") or "").strip()
        by = by if by.startswith("field:") else by.lower()
        if count not in COUNTS:
            return None, "count is one of: %s" % ", ".join(COUNTS)
        if when not in WHENS:
            return None, "when is one of: %s" % ", ".join(WHENS)
        if split not in SPLITS:
            return None, "split is one of: %s" % ", ".join(SPLITS)
        if by.startswith("field:"):
            key = by[6:]
            match = key if key in fields else next((str(f["key"]) for f in playbook.fields if str(f["label"]).lower() == key.lower()), "")
            if not match:
                return None, "no field called '%s'. Fields here: %s" % (key, ", ".join(sorted(fields)) or "none")
            by = "field:" + match
        elif by not in BYS:
            return None, "by is one of: %s, or field:<key> for one of this pipeline's fields (%s)" % (
                ", ".join(BYS), ", ".join(sorted(fields)) or "none")
        only_raw = raw.get("only") if isinstance(raw.get("only"), dict) else {}
        only: dict = {}
        for key, value in only_raw.items():
            if value in (None, ""):
                continue
            if key not in ONLY_KEYS:
                return None, "only takes: %s" % ", ".join(ONLY_KEYS)
            if key == "min_value":
                number = st._number(value)
                if number > 0:
                    only[key] = number
            elif key == "status":
                status = str(value).lower()
                status = "won" if status == playbook.won["label"].lower() else "lost" if status == playbook.lost["label"].lower() else status
                if status not in ("open", "won", "lost"):
                    return None, "only.status is one of: open, won, lost"
                only[key] = status
            elif key == "stage":
                stage = playbook.resolve_stage(str(value)) if hasattr(playbook, "resolve_stage") else str(value)
                if not stage:
                    return None, "no stage called '%s'. Stages: %s" % (value, ", ".join(playbook.stage_ids()))
                only[key] = stage
            else:
                only[key] = str(value).strip()[:120]
        if by == "reason":
            only["status"] = "lost"            # a reason is something only a lost record has
        if when == "open":
            if only.get("status") in ("won", "lost"):
                return None, "when 'open' counts records that are open now; use when 'closed' for won or lost ones"
            split = "none" if split == "outcome" else split
        return {"count": count, "by": by, "when": when, "split": split, "only": only}, None

    # -- shared arithmetic ----------------------------------------------------------------------
    def _sample(self) -> str:
        from . import demo
        return demo.SOURCE

    @staticmethod
    def _far(playbook, deal: dict) -> int:
        """How far a record got: the index of its furthest open stage, or one past the last for a won one."""
        if deal["status"] == "won":
            return len(playbook.stages)
        if deal["status"] == "open":
            return max(0, playbook.position(deal["stage"]))
        return max(0, playbook.position(str(deal["fields"].get("_prev_stage") or "")))

    @staticmethod
    def _mid(playbook) -> int:
        """The stage that starts the second half of the pipeline: a record that reached it got somewhere."""
        return (len(playbook.stages) + 1) // 2

    def _legend(self, playbook, split: str) -> list:
        if split == "furthest":
            mid = self._mid(playbook)
            late = "reached %s" % playbook.stages[mid]["label"].lower() if mid < len(playbook.stages) else ""
            return [{"k": "won", "label": playbook.won["label"].lower()}] + ([{"k": "late", "label": late}] if late else []) + [{"k": "early", "label": "earlier"}]
        if split == "outcome":
            return [{"k": "won", "label": playbook.won["label"].lower()}, {"k": "lost", "label": playbook.lost["label"].lower()}, {"k": "open", "label": "still open"}]
        return []

    def _part(self, playbook, deal: dict, split: str) -> str:
        if split == "furthest":
            far = self._far(playbook, deal)
            return "won" if far >= len(playbook.stages) else "late" if far >= self._mid(playbook) else "early"
        if split == "outcome":
            return deal["status"] if deal["status"] in ("won", "lost") else "open"
        return ""

    def _brief(self, playbook, deal: dict, part: str = "") -> dict:
        where = playbook.label(deal["stage"])
        if deal["status"] == "lost":
            was = str(deal["fields"].get("_prev_stage") or "")
            where = "%s%s" % (playbook.lost["label"], " at " + playbook.label(was).lower() if was else "")
        return {"id": deal["id"], "name": deal["name"], "company": deal["company"], "group": deal["grp"], "stage": where,
                "status": deal["status"], "value": deal["value"], "entered": deal["created_at"][:10],
                "closed": deal["stage_at"][:10] if deal["status"] in ("won", "lost") else "", "owner": deal["owner"],
                "reason": deal["lost_reason"], "k": part}

    def _money(self, playbook, value: float) -> str:
        return cards.money(value, playbook.currency) or "0"

    # -- reports made from the choices ----------------------------------------------------------
    def _population(self, deals: list, spec: dict, win: Optional[dict], playbook) -> list:
        when, only, sample = spec["when"], spec["only"], self._sample()
        out = []
        for deal in deals:
            if when == "open":
                if deal["status"] != "open":
                    continue
            elif when == "closed":
                if deal["status"] not in ("won", "lost") or not inside(deal["stage_at"], win):
                    continue
            elif not inside(deal["created_at"], win):
                continue
            if only.get("status") and deal["status"] != only["status"]:
                continue
            if only.get("owner") and deal["owner"].strip().lower() != only["owner"].lower():
                continue
            if only.get("source") and origin(deal, sample).lower() != only["source"].lower():
                continue
            if only.get("stage") and deal["stage"] != only["stage"]:
                continue
            if only.get("group") and st.group_key(deal["grp"]) != st.group_key(only["group"]):
                continue
            if only.get("company") and deal["company"].strip().lower() != only["company"].lower():
                continue
            if only.get("min_value") and deal["value"] < only["min_value"]:
                continue
            out.append(deal)
        return out

    def _key(self, playbook, deal: dict, spec: dict) -> tuple:
        """Which bar a record is counted in: ``(what the bars are ordered by, the bar's name)``."""
        by = spec["by"]
        if by == "source":
            return None, origin(deal, self._sample())
        if by == "owner":
            return None, deal["owner"].strip() or "Nobody"
        if by == "group":
            return None, deal["grp"].strip() or "No %s" % playbook.terms["group"]
        if by == "company":
            return None, deal["company"].strip() or "No company"
        if by == "reason":
            return None, deal["lost_reason"].strip() or "No reason given"
        if by == "stage":
            if deal["status"] == "won":
                return len(playbook.stages), playbook.won["label"]
            if deal["status"] == "lost":
                return len(playbook.stages) + 1, playbook.lost["label"]
            return max(0, playbook.position(deal["stage"])), playbook.label(deal["stage"])
        if by == "month":
            stamp = deal["stage_at"] if spec["when"] == "closed" else deal["created_at"]
            when = st.parse_ts(stamp)
            return stamp[:7], when.strftime("%b %Y") if when else stamp[:7]
        value = str(deal["fields"].get(by[6:]) or "").strip()
        return None, value or "Not set"

    def _bars(self, playbook, deals: list, spec: dict, full: bool) -> list:
        legend = [item["k"] for item in self._legend(playbook, spec["split"])]
        buckets: dict = {}
        for deal in deals:
            order, label = self._key(playbook, deal, spec)
            bucket = buckets.setdefault(label.lower(), {"label": label, "order": order, "deals": []})
            bucket["deals"].append(deal)
        rows = []
        for key, bucket in buckets.items():
            mine = bucket["deals"]
            parts = {k: [] for k in legend}
            for deal in mine:
                part = self._part(playbook, deal, spec["split"])
                if part in parts:
                    parts[part].append(deal)
            size = (lambda group: st.total(group)) if spec["count"] == "value" else len
            row = {"key": key, "label": bucket["label"], "order": bucket["order"], "n": size(mine), "count": len(mine), "value": st.total(mine),
                   "parts": [{"k": k, "n": size(parts[k]), "count": len(parts[k])} for k in legend]}
            if full:
                row["records"] = [self._brief(playbook, deal, self._part(playbook, deal, spec["split"]))
                                  for deal in sorted(mine, key=lambda d: (-self._far(playbook, d), -d["value"], d["name"]))[:200]]
            rows.append(row)
        if spec["by"] in ("stage", "month"):
            rows.sort(key=lambda row: row["order"])
        else:
            rows.sort(key=lambda row: (-row["n"], -row["count"], row["label"].lower()))
            if len(rows) > MOST_ROWS:
                kept, rest = rows[:MOST_ROWS - 1], rows[MOST_ROWS - 1:]
                other = {"key": "(others)", "label": "%d others" % len(rest), "order": None, "n": sum(r["n"] for r in rest),
                         "count": sum(r["count"] for r in rest), "value": sum(r["value"] for r in rest),
                         "parts": [{"k": k, "n": sum(r["parts"][i]["n"] for r in rest), "count": sum(r["parts"][i]["count"] for r in rest)}
                                   for i, k in enumerate(legend)]}
                if full:
                    other["records"] = [record for r in rest for record in r.get("records", [])][:200]
                rows = kept + [other]
        for row in rows:
            row.pop("order", None)
        return rows

    def _words(self, playbook, spec: dict, win: dict) -> tuple:
        """A report's description as one line, and as the lines of 'How this report is made'."""
        deals = playbook.terms["deals"]
        choice = self.choices()
        by = dict((key, label) for key, label in choice["by"]).get(spec["by"], spec["by"])
        counted = "value" if spec["count"] == "value" else deals
        when = {"entered": "entered in %s" % win["words"], "closed": "closed in %s" % win["words"], "open": "open now"}[spec["when"]]
        coloured = {"furthest": "the furthest stage reached", "outcome": "how it ended", "none": ""}[spec["split"]]
        only = []
        for key, value in spec["only"].items():
            if key == "status" and spec["by"] == "reason":
                continue                    # a reason is only ever a lost record's: saying so again is noise
            if key == "status":
                only.append({"open": "open", "won": playbook.won["label"].lower(), "lost": playbook.lost["label"].lower()}[value])
            elif key == "min_value":
                only.append("worth %s or more" % self._money(playbook, value))
            elif key == "stage":
                only.append("at %s" % playbook.label(value).lower())
            else:
                only.append("%s %s" % (key if key != "group" else playbook.terms["group"], value))
        line = " · ".join([counted, "by " + by.lower(), when] + (["only " + ", ".join(only)] if only else [])
                          + (["coloured by " + coloured] if coloured else []))
        made = [["Counts", counted.capitalize()], ["Grouped by", by]]
        if coloured:
            made.append(["Coloured by", coloured.capitalize()])
        made.append(["Which", "%s %s" % (deals.capitalize(), when)])
        if only:
            made.append(["Only", ", ".join(only).capitalize()])
        if spec["when"] != "open":
            made.append(["Period", win["label"] + (", against " + win["before"]["label"] if win.get("before") else "")])
        return line, made

    def _groups(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        playbook, spec = self.desk.playbook(), entry["spec"]
        deals = self.store.list(status="all")
        mine = self._population(deals, spec, win, playbook)
        fell_back = False
        if spec["count"] == "value" and mine and not any(deal["value"] for deal in mine):
            spec, fell_back = dict(spec, count="records"), True       # nothing here carries a value: count them instead
        rows = self._bars(playbook, mine, spec, full)
        timed = spec["when"] != "open"
        before = win.get("before") if compare and timed and spec["by"] != "month" else None      # a month has no "same month before"
        if before:
            earlier = {row["key"]: row for row in self._bars(playbook, self._population(deals, spec, before, playbook), spec, False)}
            for row in rows:
                was = earlier.get(row["key"])
                row["before"] = {"n": was["n"], "count": was["count"], "parts": was["parts"]} if was else {"n": 0, "count": 0, "parts": []}
        line, made = self._words(playbook, spec, win)
        unit = "money" if spec["count"] == "value" else "records"
        out = {"draw": "bars", "unit": unit, "rows": rows, "legend": self._legend(playbook, spec["split"]), "line": line, "made": made,
               "total": {"count": len(mine), "value": st.total(mine)}, "squares": unit == "records" and max([r["count"] for r in rows] or [0]) <= 60,
               "period": win if timed else None, "before": {"label": before["label"]} if before else None,
               "says": self._says_groups(playbook, spec, rows, mine, win, fell_back)}
        if not mine:
            out["empty"] = "No %s %s." % (playbook.terms["deals"], {"entered": "entered in " + win["words"], "closed": "closed in " + win["words"],
                                                                     "open": "are open"}[spec["when"]])
        if full and timed and spec["by"] != "month":
            out["over"] = self._bars(playbook, mine, dict(spec, by="month"), False)
        return out

    def _says_groups(self, playbook, spec: dict, rows: list, deals: list, win: dict, fell_back: bool) -> list:
        """One or two plain sentences on what the bars show: ``[the point, the rest]``."""
        if not rows:
            return ["", ""]
        named = [row for row in rows if row["key"] != "(others)"]
        tail = "Nothing here carries a value, so they are counted." if fell_back else ""
        if spec["by"] == "reason":
            top = named[0]
            lost = [deal for deal in deals if (deal["lost_reason"].strip() or "No reason given").lower() == top["key"]]
            stages: dict = {}
            for deal in lost:
                was = str(deal["fields"].get("_prev_stage") or "")
                if was:
                    stages[was] = stages.get(was, 0) + 1
            point = "%s %s." % (len(deals), playbook.lost["label"].lower())
            rest = ""
            stage, count = max(stages.items(), key=lambda item: item[1]) if stages else ("", 0)
            if count > 1 and top["count"] > 1:
                share = "Both" if count == 2 == top["count"] else "All %d" % count if count == top["count"] else "%d of the %d" % (count, top["count"])
                rest = "%s “%s” were at %s." % (share, top["label"], playbook.label(stage).lower())
            else:
                everywhere: dict = {}
                for deal in deals:
                    was = str(deal["fields"].get("_prev_stage") or "")
                    if was:
                        everywhere[was] = everywhere.get(was, 0) + 1
                stage, count = max(everywhere.items(), key=lambda item: item[1]) if everywhere else ("", 0)
                if count > 1:
                    rest = "%d of them were at %s." % (count, playbook.label(stage).lower())
                elif top["count"] == 1 and len(named) > 1:
                    rest = "No reason comes up twice."
            return [point, rest]
        if spec["split"] == "furthest" and spec["count"] == "records" and self._mid(playbook) < len(playbook.stages):
            mid = playbook.stages[self._mid(playbook)]["label"].lower()
            reached = lambda row: sum(part["count"] for part in row["parts"] if part["k"] in ("won", "late"))     # noqa: E731
            big = max(named, key=lambda row: row["count"])
            able = [row for row in named if row["count"] >= 3]
            if able and any(reached(row) for row in able):
                best = max(able, key=lambda row: (reached(row) / row["count"], row["count"]))
                best_share, big_share = _pct(reached(best), best["count"]), _pct(reached(big), big["count"])
                if best["key"] == big["key"]:
                    return ["%s sends the most %s and the largest share to %s (%d%%)." % (best["label"], playbook.terms["deals"], mid, best_share), tail]
                if best_share > big_share:
                    how = "more than twice the share" if big_share * 2 < best_share else "a larger share"
                    return ["%s sends fewer %s than %s and %s to %s" % (best["label"], playbook.terms["deals"], big["label"], how, mid),
                            "(%d%% against %d%%)." % (best_share, big_share)]
            return ["%s sends the most: %d of %d." % (big["label"], big["count"], len(deals)),
                    tail or ("None has reached %s yet." % mid if not any(reached(row) for row in named) else "")]
        top = max(named, key=lambda row: row["n"]) if spec["by"] in ("stage", "month") else named[0]
        if spec["count"] == "value":
            whole = st.total(deals)
            return ["%s holds the most: %s of %s." % (top["label"], self._money(playbook, top["n"]), self._money(playbook, whole)),
                    "%s in all." % _count(len(deals), playbook.terms["deal"], playbook.terms["deals"])]
        return ["%s has the most: %d of %d." % (top["label"], top["count"], len(deals)), tail]

    # -- the reports that come with the desk ------------------------------------------------------
    def _stages(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        playbook = self.desk.playbook()
        live = self.store.list(status="open")
        rows = []
        for index, stage in enumerate(playbook.stages):
            here = [deal for deal in live if deal["stage"] == stage["id"]]
            row = {"key": stage["id"], "label": stage["label"], "n": len(here), "count": len(here), "value": st.total(here), "parts": [], "heat": index}
            if full:
                row["records"] = [self._brief(playbook, deal) for deal in sorted(here, key=lambda d: (-d["value"], d["name"]))[:200]]
            rows.append(row)
        total, count = st.total(live), len(live)
        deals = playbook.terms["deals"]
        point = "%d open%s." % (count, ", %s in play" % self._money(playbook, total) if total else "")
        rest = ""
        if total:
            running = 0.0
            for index in range(len(rows) - 1, 0, -1):
                running += rows[index]["value"]
                if running * 2 >= total:
                    held = sum(row["count"] for row in rows[index:])
                    share = "Just over half" if running * 2 < total * 1.2 else "Most" if running < total else "All"
                    rest = "%s of the money (%s) is in the %s at %s or later." % (share, self._money(playbook, running),
                                                                                 _count(held, playbook.terms["deal"], deals), rows[index]["label"].lower())
                    break
            else:
                rest = "More than half the money is still at %s." % rows[0]["label"].lower() if rows else ""
        elif count:
            top = max(rows, key=lambda row: row["count"])
            rest = "The most are at %s (%d)." % (top["label"].lower(), top["count"])
        return {"draw": "bars", "unit": "records", "rows": rows, "legend": [], "line": "open %s · by stage · now" % deals, "staged": True,
                "made": [["Counts", "Open %s, and what they are worth" % deals], ["Grouped by", "Stage"], ["Which", "Everything open now"]],
                "total": {"count": count, "value": total}, "squares": False, "period": None, "before": None, "says": [point, rest],
                **({} if count else {"empty": "Nothing is open."})}

    def _winrate(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        playbook = self.desk.playbook()
        closed = [deal for deal in self.store.list(status="all") if deal["status"] in ("won", "lost")]
        mine = sorted((deal for deal in closed if inside(deal["stage_at"], win)), key=lambda deal: deal["stage_at"])
        won = [deal for deal in mine if deal["status"] == "won"]
        lost = [deal for deal in mine if deal["status"] == "lost"]
        rate = _pct(len(won), len(mine))
        months: list = []
        for deal in mine:
            when = st.parse_ts(deal["stage_at"])
            label = when.strftime("%b") if when else deal["stage_at"][:7]
            if not months or months[-1]["key"] != deal["stage_at"][:7]:
                months.append({"key": deal["stage_at"][:7], "label": label, "won": 0, "lost": 0})
            months[-1][deal["status"]] += 1
        before = win.get("before") if compare else None
        change, earlier_words = "", ""
        if before:
            earlier = [deal for deal in closed if inside(deal["stage_at"], before)]
            earlier_won = len([deal for deal in earlier if deal["status"] == "won"])
            was = _pct(earlier_won, len(earlier))
            if was is None:
                earlier_words = "Nothing closed in %s." % before["label"]
            else:
                earlier_words = "%s: %d%% (%d of %d)." % (before["label"][:1].upper() + before["label"][1:], was, earlier_won, len(earlier))
                if rate is not None and rate != was:
                    change = "%s %d point%s" % ("up" if rate > was else "down", abs(rate - was), "" if abs(rate - was) == 1 else "s")
        rows = [{"key": "won", "label": playbook.won["label"], "n": len(won), "count": len(won), "value": st.total(won), "parts": []},
                {"key": "lost", "label": playbook.lost["label"], "n": len(lost), "count": len(lost), "value": st.total(lost), "parts": []}]
        if full:
            rows[0]["records"] = [self._brief(playbook, deal, "won") for deal in won[:200]]
            rows[1]["records"] = [self._brief(playbook, deal, "lost") for deal in lost[:200]]
        money = ""
        if st.total(won) or st.total(lost):
            money = "%s %s, %s %s." % (playbook.won["label"], self._money(playbook, st.total(won)), playbook.lost["label"].lower(), self._money(playbook, st.total(lost)))
        return {"draw": "rate", "unit": "records", "rows": rows, "legend": [], "months": months[-12:],
                "line": "%s closed in %s · the share that were %s" % (playbook.terms["deals"], win["words"], playbook.won["label"].lower()),
                "made": [["Counts", "%s closed as %s, out of all that closed" % (playbook.terms["deals"].capitalize(), playbook.won["label"].lower())],
                         ["Which", "Closed in %s" % win["words"]], ["Period", win["label"] + (", against " + before["label"] if before else "")]],
                "figure": {"text": "%d%%" % rate if rate is not None else "–", "label": "%d of %d closed" % (len(won), len(mine)) if mine else "nothing closed",
                           "change": change, "up": change.startswith("up")},
                "total": {"count": len(mine), "value": st.total(mine)}, "period": win, "before": {"label": before["label"]} if before else None,
                "says": [money, earlier_words], **({} if mine else {"empty": "Nothing closed in %s." % win["words"]})}

    def stays(self, win: Optional[dict] = None) -> dict:
        """From the ledger: for each stage, the stays that ended in the period, as ``{stage id: [days, …]}``."""
        by_deal: dict = {}
        for change in self.store.stage_changes():
            by_deal.setdefault(change["deal_id"], []).append(change)
        out: dict = {}
        for changes in by_deal.values():
            for was, then in zip(changes, changes[1:]):
                if was["stage"] == then["stage"] or (win is not None and not inside(then["at"], win)):
                    continue
                start, end = st.parse_ts(was["at"]), st.parse_ts(then["at"])
                if start is None or end is None or (end - start).total_seconds() < SHORT_STAY:
                    continue
                out.setdefault(was["stage"], []).append((end - start).total_seconds() / 86400.0)
        return out

    def _time(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        playbook = self.desk.playbook()
        live, touches = self.store.list(status="open"), self.store.touches()
        left = self.stays(win)
        rows, worst = [], None
        for index, stage in enumerate(playbook.stages):
            here = [deal for deal in live if deal["stage"] == stage["id"]]
            waits = [days for days in (st.days_since(deal["stage_at"]) for deal in here) if days is not None]
            average = sum(waits) / len(waits) if waits else None
            mark = playbook.stale_days(stage["id"])
            quiet = [deal for deal in here if cards.is_stale(playbook, deal, touches.get(deal["id"], ""))]
            gone = left.get(stage["id"]) or []
            row = {"key": stage["id"], "label": stage["label"], "n": round(average, 1) if average is not None else None, "count": len(here),
                   "value": st.total(here), "parts": [], "mark": mark, "quiet": len(quiet), "tone": "bad" if average is not None and average > mark else "",
                   "left": {"count": len(gone), "days": round(sum(gone) / len(gone), 1)} if gone else None}
            if full:
                row["records"] = [dict(self._brief(playbook, deal), days=int(st.days_since(deal["stage_at"]) or 0))
                                  for deal in sorted(here, key=lambda d: d["stage_at"])[:200]]
            rows.append(row)
            if average is not None and average > mark and (worst is None or average - mark > worst[1]):
                worst = (row, average - mark)
        if worst:
            row = worst[0]
            point = "%s runs %s past its quiet mark." % (row["label"], _days(worst[1]))
            rest = "%s there %s had nothing logged for longer than the mark allows." % (
                _count(row["quiet"], playbook.terms["deal"], playbook.terms["deals"]), "has" if row["quiet"] == 1 else "have") if row["quiet"] else ""
        else:
            quiet = sum(row["quiet"] for row in rows)
            point = "No stage is past its quiet mark on average." if live else ""
            rest = "%s gone quiet all the same." % _count(quiet, playbook.terms["deal"] + " has", playbook.terms["deals"] + " have") if quiet else ""
        return {"draw": "bars", "unit": "days", "rows": rows, "legend": [], "staged": True,
                "line": "open %s · the average days each has been in its stage · against the stage's quiet mark" % playbook.terms["deals"],
                "made": [["Counts", "Days since each open %s reached its stage, averaged" % playbook.terms["deal"]], ["Grouped by", "Stage"],
                         ["The mark", "Each stage's quiet-after days, from the playbook"],
                         ["Left after", "From the ledger: the stays that ended in %s. A move made within a minute is left out" % win["words"]]],
                "total": {"count": len(live), "value": st.total(live)}, "squares": False, "period": None, "before": None, "says": [point, rest],
                **({} if live else {"empty": "Nothing is open."})}

    def _map(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        playbook, markets = self.desk.playbook(), self.desk.markets
        maps = markets.list_markets()
        mapped, records = 0, []
        cost, priced, lookups = 0, False, 0
        for market in maps:
            targets = [target for target in markets.targets(market["id"]) if target["status"] != "dismissed"]
            mapped += len(targets)
            for target in targets:
                deal = self.store.get(target["deal_id"]) if target["status"] == "tracked" and target["deal_id"] else None
                if deal:
                    records.append(deal)
            spent = self.store.meta_get("map:%s:cost" % market["id"])
            if spent:
                cost, priced = cost + int(spent or 0), True
            lookups += int(self.store.meta_get("map:%s:pdl" % market["id"]) or 0) + int(self.store.meta_get("map:%s:exa" % market["id"]) or 0)
        mid = self._mid(playbook)
        late = [deal for deal in records if self._far(playbook, deal) >= mid]
        won = [deal for deal in records if deal["status"] == "won"]
        rows = [{"key": "mapped", "label": "Companies mapped", "n": mapped, "count": mapped, "value": 0, "parts": [], "heat": 0},
                {"key": "pipeline", "label": "In the pipeline", "n": len(records), "count": len(records), "value": st.total(records), "parts": [], "heat": 1}]
        if mid < len(playbook.stages):
            rows.append({"key": "late", "label": "Reached %s" % playbook.stages[mid]["label"].lower(), "n": len(late), "count": len(late),
                         "value": st.total(late), "parts": [], "heat": 3})
        rows.append({"key": "won", "label": playbook.won["label"], "n": len(won), "count": len(won), "value": st.total(won), "parts": [], "heat": 6})
        if full:
            for row, group in zip(rows[1:], [records] + ([late] if mid < len(playbook.stages) else []) + [won]):
                row["records"] = [self._brief(playbook, deal) for deal in sorted(group, key=lambda d: (-self._far(playbook, d), -d["value"]))[:200]]
        figures = []
        if st.total(won):
            figures.append({"text": self._money(playbook, st.total(won)), "label": "%s from the map" % playbook.won["label"].lower()})
        if priced:
            figures.append({"text": "$%.2f" % (cost / 1_000_000), "label": "Hermes's research, estimated"})
        if lookups:
            figures.append({"text": str(lookups), "label": "paid lookups"})
        point, rest = "", ""
        if not maps:
            point = "No market map yet."
        elif not records:
            point = "Nothing from the map is in the pipeline yet."
        elif priced:
            point = "About $%.2f of research for each %s the map put in the pipeline" % (cost / 1_000_000 / len(records), playbook.terms["deal"])
            rest = ("plus %s." % _count(lookups, "paid lookup", "paid lookups")) if lookups else "."
            point, rest = (point, rest) if lookups else (point + ".", "")
        else:
            share = _pct(len(records), mapped)
            point = "%d%% of the companies mapped are in the pipeline." % share if share is not None else ""
        return {"draw": "bars", "unit": "records", "rows": rows, "legend": [], "figures": figures, "staged": True,
                "line": "companies on %s · how many became %s, and how far those got" % ("the map" if len(maps) == 1 else "%d maps" % len(maps), playbook.terms["deals"]),
                "made": [["Counts", "Companies on every map of this pipeline, not the dismissed ones"],
                         ["Then", "The ones put in the pipeline, how far each got, and which were %s" % playbook.won["label"].lower()],
                         ["Cost", "What Hermes estimates its own work on the maps cost, as on Map facts. Not an invoice"], ["Which", "All time"]],
                "total": {"count": mapped, "value": st.total(won)}, "squares": mapped <= 60, "period": None, "before": None, "says": [point, rest],
                **({} if maps else {"empty": "No market map yet. Make one on the Market map tab."})}

    @staticmethod
    def _proposal_kind(row: dict) -> str:
        if row.get("kind") == "new":
            return "new record"
        if row.get("stage"):
            return "stage move"
        if row.get("person"):
            return "person"
        if row.get("invoice") or row.get("payment"):
            return "invoice"
        if row.get("next_step") or row.get("next_due"):
            return "next step"
        return "update"

    def _hermes(self, entry: dict, win: dict, compare: bool, full: bool) -> dict:
        from .intake import APPLICABLE
        start = (win["from"] or "0000-01-01") + "T00:00:00Z"
        end = win["to"] + "T23:59:59Z"
        approved = declined = waiting = 0
        turned_down: dict = {}
        for intake in self.store.intakes_between(start, end, "proposal"):
            for row in intake["rows"]:
                if row.get("kind") not in APPLICABLE:
                    continue
                done = row.get("done")
                if done == "applied":
                    approved += 1
                elif done == "declined":
                    declined += 1
                    kind = self._proposal_kind(row)
                    turned_down[kind] = turned_down.get(kind, 0) + 1
                elif not done and intake["status"] == "open":
                    waiting += 1
        proposed = approved + declined + waiting
        notes = self.store.notes_between(start, end)
        filed = len([note for note in notes if note["deal_id"]])
        actions = self.store.actions_between(start, end)
        alone = sum(count for actor, count in actions["by"].items() if actor.startswith("Hermes (scheduled)"))
        figures = [{"text": str(proposed), "label": "changes proposed"}, {"text": str(approved), "label": "approved by you"},
                   {"text": str(declined), "label": "declined", "tone": "mute"}, {"text": str(waiting), "label": "waiting for you", "tone": "warn" if waiting else ""},
                   {"text": str(filed), "label": "call notes filed"}]
        if alone:
            figures.append({"text": str(alone), "label": "done on a schedule, unasked"})
        if actions["taken_back"]:
            figures.append({"text": str(actions["taken_back"]), "label": "taken back", "tone": "mute"})
        point, rest = "", ""
        if declined:
            kind, count = max(turned_down.items(), key=lambda item: item[1])
            plural = {"new record": "new records", "stage move": "stage moves", "person": "people to add", "invoice": "invoices",
                      "next step": "next steps", "update": "updates"}[kind]
            point = ("The one you declined was a %s." % kind) if declined == 1 else "%d of the %d you declined were %s." % (count, declined, plural)
            rest = "What Hermes may do unasked is under Setup."
        elif approved:
            point = "You approved everything Hermes proposed that you have decided."
            rest = "%d still waiting on Today." % waiting if waiting else ""
        elif waiting:
            point = "%s waiting for you on Today." % _count(waiting, "proposal is", "proposals are")
        elif not filed and not alone:
            point = "Hermes proposed nothing in %s." % win["words"]
            rest = "It proposes from scheduled jobs: switch them on under Setup."
        return {"draw": "figures", "unit": "records", "rows": [], "legend": [], "figures": figures,
                "strip": [{"k": "approved", "n": approved, "label": "approved"}, {"k": "declined", "n": declined, "label": "declined"},
                          {"k": "waiting", "n": waiting, "label": "waiting"}],
                "line": "what Hermes proposed in %s, and what you did with it" % win["words"],
                "made": [["Proposed", "Changes a scheduled job held for your approval, one for each row"],
                         ["Approved, declined", "What you pressed on Today or in Slack"],
                         ["Call notes filed", "Notes from your folders, keys and connections that were put on a record"],
                         ["Unasked", "Changes a scheduled job made by itself, as far as the desk's autonomy setting allows"],
                         ["Period", win["label"]]],
                "total": {"count": proposed, "value": 0}, "period": win, "before": None, "says": [point, rest]}

    # -- running one, or the dashboard ------------------------------------------------------------
    def run(self, ref: Any, period: Any = "", compare: bool = True, full: bool = False) -> dict:
        entry = ref if isinstance(ref, dict) else self.find(ref)
        if entry is None:
            return {"ok": False, "error": "no report matches '%s'" % ref, "reports": [item["title"] for item in self.catalog()]}
        known = dict(PERIODS)
        period = str(period or entry.get("period") or "90")
        win = window(period if period in known else "90")
        maker = {"stages": self._stages, "winrate": self._winrate, "time": self._time, "groups": self._groups, "map": self._map,
                 "hermes": self._hermes}[entry["kind"]]
        out = maker(entry, win, bool(compare), full)
        out.update({"ok": True, "id": entry["id"], "title": entry["title"], "kind": entry["kind"], "custom": entry["custom"],
                    "draft": entry["draft"], "pinned": entry["pinned"], "spec": entry.get("spec"), "asked": entry.get("asked", ""),
                    "replaces": entry.get("replaces", ""), "window": win["id"]})
        return out

    def dashboard(self, period: Any = "", compare: bool = True) -> dict:
        """The Reports tab in one answer: the pinned reports worked out, and the list of all of them."""
        listed = self.catalog()
        saved = self._saved()
        order = {ref: index for index, ref in enumerate(self._pins(saved))}
        pinned = sorted((entry for entry in listed if entry["pinned"] and not entry["draft"]), key=lambda entry: order.get(entry["id"], 99))
        win = window(period if str(period) in dict(PERIODS) else "90")
        cards_ = []
        for entry in pinned:
            try:
                cards_.append(self.run(entry, win["id"], compare))
            except Exception as exc:       # one report that cannot be worked out does not take the page down
                cards_.append({"ok": False, "id": entry["id"], "title": entry["title"], "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"period": {"id": win["id"], "label": win["label"], "before": (win.get("before") or {}).get("label", "")}, "compare": bool(compare),
                "cards": cards_, "choices": self.choices(),
                "reports": [{"id": e["id"], "title": e["title"], "custom": e["custom"], "draft": e["draft"], "pinned": e["pinned"],
                             "replaces": e.get("replaces", ""), "asked": e.get("asked", ""), "by": e.get("by", ""),
                             "line": self._line(e, win)} for e in listed]}

    def _line(self, entry: dict, win: dict) -> str:
        if entry.get("spec"):
            return self._words(self.desk.playbook(), entry["spec"], win)[0]
        deals = self.desk.playbook().terms["deals"]
        return {"stages": "open %s · by stage · now" % deals, "winrate": "the share of closed %s that were won" % deals,
                "time": "how long open %s have sat in each stage, against its quiet mark" % deals,
                "map": "what the companies on your maps became", "hermes": "what Hermes proposed, and what you did with it"}[entry["kind"]]

    # -- making, changing and removing --------------------------------------------------------------
    def save(self, args: dict, by: str = "", draft: bool = False) -> dict:
        """Make a report from the choices, or change one made here. ``draft`` (a report Hermes made) leaves it
        waiting for the person to keep; with ``replaces`` it is a new version of that report, not a second one."""
        spec, error = self.clean(args.get("spec") if isinstance(args.get("spec"), dict) else args)
        if error:
            return {"ok": False, "error": error}
        saved = self._saved()
        ref = str(args.get("id") or "").strip()
        replaces = str(args.get("replaces") or "").strip()
        if replaces:
            target = self.find(replaces)
            if target is None or not target["custom"] or target["draft"]:
                return {"ok": False, "error": "replaces names a report made here; the ones that come with the desk cannot be changed. Save a new one instead."}
            replaces = target["id"]
        title = re.sub(r"\s+", " ", str(args.get("title") or "")).strip()[:80]
        period = str(args.get("period") or "")
        period = period if period in dict(PERIODS) else ""
        if ref:
            kept = saved["custom"].get(ref)
            if kept is None:
                return {"ok": False, "error": "no report made here has the id '%s'" % ref}
            kept.update(spec=spec, title=title or kept.get("title") or "Report", **({"period": period} if period else {}))
        else:
            if not title:
                return {"ok": False, "error": "a report needs a title: a few words saying what it shows"}
            if len(saved["custom"]) >= MOST_CUSTOM:
                return {"ok": False, "error": "this pipeline already has %d reports of its own; remove one first" % MOST_CUSTOM}
            ref = "r" + st.slugify(title)[:24].strip("-") + "-" + st.now()[11:19].replace(":", "")
            saved["custom"][ref] = {"title": title, "spec": spec, "at": st.now(), "by": by, "draft": bool(draft), "replaces": replaces,
                                    "asked": str(args.get("asked") or "")[:300], "period": period}
        self._write(saved)
        out = self.run(ref, period)
        return out if not out.get("ok") else dict(self.brief(out), ok=True, saved=True, draft=bool(saved["custom"][ref].get("draft")))

    def act(self, ref: Any, verb: str) -> dict:
        """A person's press on the page: pin, unpin, keep or discard a draft, remove one made here."""
        entry = self.find(ref)
        if entry is None:
            return {"ok": False, "error": "no such report"}
        saved = self._saved()
        pins = self._pins(saved)
        if verb in ("pin", "unpin"):
            pins = [item for item in pins if item != entry["id"]] + ([entry["id"]] if verb == "pin" else [])
            saved["pins"] = pins
        elif verb in ("up", "down"):
            if entry["id"] in pins:
                at = pins.index(entry["id"])
                to = max(0, min(len(pins) - 1, at + (-1 if verb == "up" else 1)))
                pins.insert(to, pins.pop(at))
                saved["pins"] = pins
        elif verb == "keep" and entry["custom"] and entry["draft"]:
            kept = saved["custom"][entry["id"]]
            old = kept.pop("replaces", "") or ""
            kept["draft"] = False
            if old and old in saved["custom"]:                      # the new version takes the old one's place, pin and all
                saved["custom"][old].update(title=kept["title"], spec=kept["spec"], asked=kept.get("asked", ""), period=kept.get("period", ""))
                del saved["custom"][entry["id"]]
                self._write(saved)
                return {"ok": True, "id": old}
        elif verb in ("discard", "remove") and entry["custom"]:
            del saved["custom"][entry["id"]]
            for other in saved["custom"].values():
                if other.get("replaces") == entry["id"]:
                    other["replaces"] = ""
            saved["pins"] = [item for item in pins if item != entry["id"]]
        else:
            return {"ok": False, "error": "that cannot be done to this report"}
        self._write(saved)
        return {"ok": True, "id": entry["id"]}

    # -- for Hermes ---------------------------------------------------------------------------------
    @staticmethod
    def brief(result: dict) -> dict:
        """A worked-out report as Hermes is handed it: the figures and the sentence, without the list of records."""
        out = {key: result[key] for key in ("id", "title", "line", "says", "figure", "figures", "strip", "months", "empty") if result.get(key)}
        out["says"] = " ".join(part for part in result.get("says") or [] if part)
        out["rows"] = [dict({"label": row["label"], "n": row["n"]}, **{key: row[key] for key in ("count", "value", "mark", "quiet", "left", "before") if row.get(key) not in (None, 0, [], {})},
                            **({"parts": {part["k"]: part["count"] for part in row["parts"]}} if row.get("parts") else {}))
                       for row in result.get("rows") or []]
        if result.get("period"):
            out["period"] = result["period"]["label"]
        return out

    def tool(self, args: dict, session: Optional[dict] = None) -> dict:
        action = str(args.get("action") or "list").lower()
        if action == "list":
            win = window("90")
            return {"ok": True, "reports": [{"id": e["id"], "title": e["title"], "what": self._line(e, win), "made_here": e["custom"],
                                             "draft": e["draft"], "on_dashboard": e["pinned"]} for e in self.catalog()],
                    "note": "Read one with action 'read'. Every figure is counted by Stoa from the desk; do not work them out again yourself."}
        if action == "read":
            result = self.run(args.get("report"), args.get("period"))
            if not result.get("ok"):
                return result
            return dict(self.brief(result), ok=True, note="These are Stoa's own counts. Say what they show in a line or two; the page has the chart.")
        if action == "save":
            out = self.save(args, by=self.desk.actor(), draft=True)
            if out.get("ok"):
                out["note"] = ("Saved as a draft on the Reports tab: the person keeps it or discards it there. Say in one line what it shows, "
                               "from the figures here. Do not save it again.")
            else:
                out["choices"] = {"count": list(COUNTS), "by": list(BYS) + ["field:<key>"], "when": list(WHENS), "split": list(SPLITS),
                                  "only": list(ONLY_KEYS), "period": [key for key, _ in PERIODS]}
            return out
        if action == "remove":
            entry = self.find(args.get("report"))
            if entry is None or not entry["custom"]:
                return {"ok": False, "error": "only a report made here can be removed; the ones that come with the desk can be taken off the dashboard on the page"}
            return dict(self.act(entry["id"], "remove"), removed=entry["title"])
        return {"ok": False, "error": "action is one of: list, read, save, remove"}

    # -- a page to keep or send -----------------------------------------------------------------------
    def csv_rows(self, result: dict) -> tuple:
        """One report as a header and rows, for a spreadsheet."""
        legend = [item["label"] for item in result.get("legend") or []]
        if result.get("draw") == "figures":
            return ["What", "Count"], [[figure["label"], figure["text"]] for figure in result.get("figures") or []]
        if result.get("unit") == "days":
            return (["Stage", "Open now", "Average days there", "Quiet mark (days)", "Gone quiet", "Left in the period", "Average days before leaving"],
                    [[row["label"], row["count"], "" if row["n"] is None else row["n"], row.get("mark", ""), row.get("quiet", 0),
                      (row.get("left") or {}).get("count", 0), (row.get("left") or {}).get("days", "")] for row in result["rows"]])
        header = ["", "Records", "Value"] + legend
        rows = [[row["label"], row["count"], row["value"]] + [part["count"] for part in row.get("parts") or []] for row in result.get("rows") or []]
        return header, rows

    def page(self, results: list, title: str) -> str:
        """Reports as one file that asks nothing of the network: no script, no outside font or image. It prints."""
        playbook = self.desk.playbook()
        esc = html.escape
        ink = {"won": "#1a1d23", "late": "#2e9a67", "early": "#2c86d9", "lost": "#c8cdd6", "open": "#2c86d9", "approved": "#2c86d9",
               "declined": "#c8cdd6", "waiting": "#e3a33a"}
        parts = []
        for result in results:
            if not result.get("ok"):
                continue
            body = []
            if result.get("figure"):
                figure = result["figure"]
                body.append('<p class="big">%s <small>%s%s</small></p>' % (esc(figure["text"]), esc(figure["label"]),
                                                                           " · " + esc(figure["change"]) if figure.get("change") else ""))
            if result.get("figures"):
                body.append('<p class="figs">%s</p>' % "".join("<span><b>%s</b> %s</span>" % (esc(f["text"]), esc(f["label"])) for f in result["figures"]))
            rows = result.get("rows") or []
            if result.get("draw") == "bars" and rows:
                most = max([row["n"] or 0 for row in rows] + [row.get("mark") or 0 for row in rows] + [1])
                lines = []
                for row in rows:
                    n = row["n"] or 0
                    if row.get("parts"):
                        bar = "".join('<i style="width:%.1f%%;background:%s"></i>' % (100 * part["n"] / most, ink.get(part["k"], "#2c86d9"))
                                      for part in row["parts"] if part["n"])
                    else:
                        bar = '<i style="width:%.1f%%;background:%s"></i>' % (100 * n / most, "#d2553f" if row.get("tone") == "bad" else "#2c86d9")
                    if result.get("unit") == "days":
                        right = ("%sd" % round(n) if row["n"] is not None else "–") + " · mark %sd" % row.get("mark", "")
                    elif result.get("unit") == "money":
                        right = self._money(playbook, n)
                    else:
                        right = str(row["count"]) + (" · " + self._money(playbook, row["value"]) if row.get("value") else "")
                    lines.append('<tr><th>%s</th><td class="bar"><div>%s</div></td><td class="n">%s</td></tr>' % (esc(row["label"]), bar, esc(right)))
                body.append("<table>%s</table>" % "".join(lines))
                if result.get("legend"):
                    body.append('<p class="key">%s</p>' % "".join('<span><i style="background:%s"></i>%s</span>' % (ink.get(item["k"], "#2c86d9"), esc(item["label"]))
                                                                  for item in result["legend"]))
            if result.get("empty"):
                body.append('<p class="none">%s</p>' % esc(result["empty"]))
            says = " ".join(part for part in result.get("says") or [] if part)
            if says:
                body.append('<p class="says">%s</p>' % esc(says))
            parts.append('<section><h2>%s</h2><p class="line">%s</p>%s</section>' % (esc(result["title"]), esc(result.get("line") or ""), "".join(body)))
        style = ("body{margin:0;padding:40px 24px;background:#f7f7f4;color:#1a1d23;font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}"
                 "main{max-width:820px;margin:0 auto}h1{margin:0;font-size:26px;font-weight:600}header p{margin:6px 0 0;color:#5b6370}"
                 "section{margin-top:22px;padding:18px 20px;background:#fff;border:1px solid #dfe2e7;break-inside:avoid}"
                 "h2{margin:0;font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase}"
                 ".line{margin:4px 0 12px;color:#5b6370;font:12px ui-monospace,Menlo,monospace}.big{margin:0 0 10px;font:600 40px/1 ui-monospace,Menlo,monospace}"
                 ".big small{font:13px ui-monospace,Menlo,monospace;color:#5b6370}.figs{display:flex;flex-wrap:wrap;gap:6px 22px;margin:0 0 10px}"
                 ".figs b{font:600 20px ui-monospace,Menlo,monospace}table{width:100%;border-collapse:collapse}th{width:26%;padding:5px 12px 5px 0;text-align:left;font-weight:500;vertical-align:middle}"
                 "td{padding:5px 0}.bar div{display:flex;height:14px;background:#eef0f3}.bar i{display:block;height:14px}"
                 ".n{width:24%;padding-left:12px;text-align:right;font:12px ui-monospace,Menlo,monospace;white-space:nowrap}"
                 ".key{display:flex;flex-wrap:wrap;gap:4px 16px;margin:10px 0 0;font:11px ui-monospace,Menlo,monospace;color:#5b6370}"
                 ".key i{display:inline-block;width:9px;height:9px;margin-right:6px}.says{margin:12px 0 0;padding-top:10px;border-top:1px dotted #c8cdd6}"
                 ".none{color:#5b6370}footer{margin-top:22px;color:#5b6370;font-size:12px}@media print{body{background:#fff;padding:0}section{border-color:#bbb}}")
        return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>%s</title><style>%s</style></head><body><main><header><h1>%s</h1><p>%s · %s</p></header>%s"
                "<footer>Counted by Stoa from the desk's own records on %s. Nothing on this page asks anything of the network.</footer></main></body></html>\n"
                % (esc(title), style, esc(title), esc(playbook.title if hasattr(playbook, "title") else ""), esc(dt.date.today().strftime("%B %d, %Y").replace(" 0", " ")),
                   "".join(parts), esc(dt.date.today().isoformat())))
