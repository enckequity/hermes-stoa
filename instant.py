"""Answers that need no model.

Some things people ask the desk every day are not questions for a model at all. "What needs me today?" is a lookup:
the desk already knows, and already draws it. Sending it to a model costs thousands of tokens (the instructions and
every tool's description are read again on each step) to be handed back what the desk had to begin with.

So a handful of standing questions are answered here, from the desk, at once:

    today    what needs me today            overdue, due today, waiting for a yes, gone quiet
    quiet    who has gone quiet             the records past their stage's quiet mark
    week     what moved this week           new, moved, won and lost in the last seven days
    brief    the morning brief              today, what is due, and who has gone quiet
    report   the pipeline as a report       the stage report, with the desk's own sentence

Two ways in. The talk box on the Stoa page catches these exact questions (its suggestion buttons, and the same words
typed), when nothing is attached; anything else goes to Hermes as before, and the answer has an "Ask Hermes instead".
And ``/gtm today``, ``/gtm quiet``, ``/gtm week``, ``/gtm brief`` and ``/gtm report`` give the same answer in any Hermes chat.

Hermes gives a plugin no way to answer a message typed in its own chat, so a question typed there still goes to the
model. Every figure is the desk's own (the views behind ``gtm_pipeline`` and ``gtm_report``): nothing is counted
twice or differently here. Each pipeline is answered in turn, under its name, on a desk with more than one.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import cards

# What is caught, as it is after folding (lower case, no punctuation, single spaces).
ASKS = {
    "today": ("what needs me today", "what needs me", "what is due today", "whats due today", "whats on my plate", "what is on my plate",
              "whats on my plate today"),
    "quiet": ("who has gone quiet", "who went quiet", "whos gone quiet"),
    "week": ("what moved this week", "what changed this week", "what happened this week"),
    "brief": ("give me the morning brief", "the morning brief", "morning brief"),
    "report": ("show me the pipeline as a report", "the pipeline as a report", "pipeline report"),
}
FOLDED = {phrase: kind for kind, phrases in ASKS.items() for phrase in phrases}
KINDS = tuple(ASKS)
SAID = "Read from the desk · no model used"
MOST = 8                    # rows in a list before "and N more"


def fold(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", "", str(text or "").lower().replace("’", "").replace("'", "")).split())


def match(text: Any) -> str:
    """Which standing question this is, or ''."""
    return FOLDED.get(fold(text), "")


def count(n: int, one: str, many: Optional[str] = None) -> str:
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


class Instant:
    def __init__(self, desk):
        self.desk = desk

    def answer(self, kind: str) -> str:
        """The answer to one standing question, for every pipeline on the desk."""
        if kind not in KINDS:
            return ""
        listed = self.desk.pipelines()
        parts = []
        for entry in listed:
            with self.desk.on(entry["id"]):
                piece = getattr(self, "_" + kind)()
            if piece:
                parts.append(("### %s\n\n" % entry["name"] if len(listed) > 1 else "") + piece)
        out = "\n\n".join(parts) or "The desk has nothing to show for that yet."
        return ("*Sample data: nothing here is real.*\n\n" + out) if self.desk.tour() else out

    # -- reading the desk ---------------------------------------------------------------------------------------
    def _money(self, value: Any) -> str:
        return cards.money(value, self.desk.playbook().currency) if value else ""

    @staticmethod
    def _who(row: dict) -> str:
        name, company = str(row.get("name") or ""), str(row.get("company") or "")
        return "**%s**%s" % (name, " (%s)" % company if company and company != name else "")

    @staticmethod
    def _more(rows: list, lines: list) -> list:
        return lines[:MOST] + (["- and %d more" % (len(rows) - MOST)] if len(rows) > MOST else [])

    def _today(self) -> str:
        data = self.desk.pipeline({"view": "today"})
        counts = data.get("counts") or {}
        said = [count(counts.get("overdue", 0), "overdue", "overdue"), count(counts.get("due_today", 0), "due today", "due today"),
                count(counts.get("quiet", 0), "gone quiet", "gone quiet")]
        out = [(", ".join(said) + ".") if any(counts.get(key) for key in ("overdue", "due_today", "quiet")) else "Nothing is overdue, due today or quiet."]
        if counts.get("approvals"):
            out[0] += " %s waiting for your yes." % count(counts["approvals"], "change is", "changes are")
        for title, key, line in (
                ("Overdue", "overdue", lambda d: "%s · %s · %s overdue" % (self._who(d), d.get("next_step") or "next step", count(int(d.get("days") or 0), "day"))),
                ("Due today", "due_today", lambda d: "%s · %s" % (self._who(d), d.get("next_step") or "next step")),
                ("Gone quiet", "quiet", lambda d: "%s · %s · quiet %s" % (self._who(d), d.get("stage_label") or "", count(int(d.get("days") or 0), "day")))):
            rows = data.get(key) or []
            if rows:
                out.append("%s\n\n%s" % (title, "\n".join(self._more(rows, ["- " + line(row) for row in rows]))))
        return "\n\n".join(out)

    def _quiet(self) -> str:
        rows = self.desk.pipeline({"view": "brief"}).get("stale") or []
        if not rows:
            return "Nobody has gone quiet."
        lines = []
        for row in rows:
            bits = [self._who(row), str(row.get("stage_label") or row.get("stage") or "")]
            if row.get("quiet_days") is not None:
                bits.append("quiet %s" % count(int(row["quiet_days"]), "day"))
            bits.append("next: %s" % row["next_step"] if row.get("next_step") else "no next step")
            bits.append(self._money(row.get("value")))
            lines.append("- " + " · ".join(bit for bit in bits if bit))
        return "%s\n\n%s" % ("One has gone quiet." if len(rows) == 1 else "%d have gone quiet." % len(rows), "\n".join(self._more(rows, lines)))

    def _week(self) -> str:
        week = self.desk.pipeline({"view": "brief"}).get("this_week") or {}
        won, lost = week.get("won") or [], week.get("lost") or []
        out = ["**In the last seven days:** %s, %s, %d won, %d lost." % (count(int(week.get("new") or 0), "new record"),
                                                                         count(int(week.get("stage_moves") or 0), "stage move"), len(won), len(lost))]
        if won:
            out.append("Won\n\n" + "\n".join(self._more(won, ["- **%s**%s" % (row.get("name", ""), " · " + self._money(row.get("value")) if row.get("value") else "") for row in won])))
        if lost:
            out.append("Lost\n\n" + "\n".join(self._more(lost, ["- **%s**%s" % (row.get("name", ""), " · " + str(row["reason"]) if row.get("reason") else "") for row in lost])))
        moves = [entry for entry in (self.desk.pipeline({"view": "activity"}).get("activity") or []) if entry.get("kind") == "stage"]
        if moves:
            names = {deal["id"]: deal["name"] for deal in self.desk.store.list(status="all")}
            out.append("Moved\n\n" + "\n".join(self._more(moves, ["- **%s** · %s" % (names.get(entry.get("deal"), entry.get("deal", "")), entry.get("text", "")) for entry in moves])))
        return "\n\n".join(out)

    def _brief(self) -> str:
        brief = self.desk.pipeline({"view": "brief"})
        playbook = self.desk.playbook()
        out = ["%s open %s%s." % (brief.get("open", 0), playbook.terms["deal"] if brief.get("open") == 1 else playbook.terms["deals"],
                                  ", " + self._money(brief.get("open_value")) + " in play" if brief.get("open_value") else "")]
        out.append(self._today())                   # overdue, due today and gone quiet, each with its next step
        week = brief.get("this_week") or {}
        if week:
            out.append("This week: %s, %d won, %d lost." % (count(int(week.get("new") or 0), "new record"), len(week.get("won") or []), len(week.get("lost") or [])))
        if brief.get("notes_waiting"):
            out.append("%s waiting to be filed." % count(int(brief["notes_waiting"]), "note is", "notes are"))
        return "\n\n".join(out)

    def _report(self) -> str:
        seen = self.desk.reports.tool({"action": "read", "report": "stages"})
        rows = seen.get("rows") or []
        if not seen.get("ok", True) or not rows:
            return ""
        lines = ["- **%s** · %d%s" % (row.get("label", ""), int(row.get("count") or 0), " · " + self._money(row.get("value")) if row.get("value") else "") for row in rows]
        return "\n\n".join(part for part in (str(seen.get("says") or ""), "%s\n\n%s" % (seen.get("title") or "Pipeline by stage", "\n".join(lines))) if part)
