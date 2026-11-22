"""The go-to-market in one reading: what an agent needs to know before it writes or plans for the business.

The GTM tab keeps how a team sells in three places. Market maps say what the business sells and who to (a map's
brief), where it plays (its segments), how it wins (what the map means) and what has to be true of a company (its
requirements). Motions are the plays under way. Assets are what gets sent and shown. A person reads those on the
tab; an agent should not have to make five calls to learn the same thing, or ask the person for what the desk
already says.

So this module gathers it once, from every pipeline, and gives it to an agent two ways:

* The agent reads it with one call: ``gtm_context``. Its instructions say so (``prompt_line``), in a line, since a
  plugin's part of the system prompt is kept short.
* ``text`` gives the same reading as a section of instructions, for a host that has room for it (the Stoa Mac app).

What the business sells and who to is the brief of a market map. A person writes it on the Maps view; an agent
saves it, on the person's word, with ``gtm_context`` (action ``set_brief``). Where no map says it, the reading
says so plainly, so the agent asks instead of guessing.

A map is something people send on. The plugin prints a map's brief, its reading and what it means on the page it
downloads (the copy to share included), in its text export and when it posts the map to Slack. So those three
are public words, and both the page and the agents are told so: nothing internal belongs in them.

So a map carries two notes. The external one is its brief: printed on the map, written for a client to read. The
internal one is kept apart (in ``hub.json``, never in a map's ledger, so nothing a map exports can carry it):
positioning, pricing, what to say and what to leave out.

"Internal" means never printed and never sent to a client. It does not mean the words never leave the Mac: they
are part of what the agent works from, so they go to whichever model is connected. And an agent that is told not
to quote something may still quote it. So every answer is checked by rule, not by a model, against the internal
notes (``repeats``): if a draft carries a figure or a run of words out of them, the person is told under the
answer, before anything is sent on.

Nothing is kept here. The maps stay in the pipelines' ledgers, the motions in ``motions.db`` and the assets and the
internal notes in ``hub.json``; this only reads them, and writes a brief through the map's own way of defining it.
"""
from __future__ import annotations

import json
from typing import Any

from typing import Optional

from .motions import Refused

MAPS_IN_FULL = 3            # maps given in the instructions; the tool gives a few more
TEXT_CHARS = 4500           # the most the instructions are given, however much the desk holds
NOT_WRITTEN = "Not written yet. Ask the person what they sell and who to, in a sentence or two, and save it with gtm_context (action set_brief)."
SHARED = ("Each map has two notes. The external one is its brief: it, the map's reading and what it means are printed on the map when the person downloads, exports "
          "or posts it, so they may reach a client. The internal notes are never printed or sent: they are for you and the person only. Let them guide what you "
          "write, but do not quote them to anyone outside, and never save what belongs in them (pricing, internal positioning, a view of a named account) into a "
          "brief, a reading or an implication.")
NOTE_CHARS = 4000

TOOL = {
    "name": "gtm_context",
    "description": (
        "Read the go-to-market in one call. For each market map: what the business sells and who to, where it plays (its "
        "segments), how it wins, what has to be true of a company for it to be worth a call, and who the business would be replacing. "
        "Then the motions running and planned, and the links and key assets to point at. "
        "Call it once before you draft anything that describes the business (an outreach note, a follow-up, a proposal) and before you "
        "plan a motion, and work from it; do not ask the person for what it already says. "
        "Actions: get (the default); set_brief, which saves the map's external note, what the business sells and who to, in the "
        "person's own words (`map`, `text`); and set_notes, which saves the map's internal notes (`map`, `text`; an empty `text` clears "
        "them). Use either only on the person's word. The brief is printed on the map when it is downloaded, exported or posted, so it "
        "may reach a client. The internal notes are never printed or sent: pricing, internal positioning and anything about a named "
        "account go there, not in the brief. "
        "For more than the reading: gtm_market has the companies, gtm_motion the figures, gtm_hub the assets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set_brief", "set_notes"]},
            "map": {"type": "string", "description": "set_brief/set_notes: the market map, by name or id. Leave it out when there is only one."},
            "text": {"type": "string", "description": "set_brief: what the business sells and who to, in a sentence or two, fit for a client to read. set_notes: the internal notes, in the person's words."},
        },
    },
}


STOP = frozenset("""a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it its just may me might more most my
no nor not of on or our out over she should so some such than that the their them then there these they this those to too under up us was we were what when where which who whom why
will with would you your about after again against all also always any because before being between both each few further here itself never only other own same
than very via per""".split())


def _amounts(text: str) -> set:
    """The figures in a sentence, as plain numbers: "$40,000", "40k" and "40 thousand" are all 40000."""
    import re
    found = set()
    for number, unit in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|bn|thousand|million|billion|%)?", text.lower()):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        value *= {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}.get(unit, 1)
        if value >= 100 or unit == "%":
            found.add(("%g%%" % value) if unit == "%" else "%g" % value)
    return found


def _sentences(text: str) -> list:
    import re
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", str(text or "")) if part.strip()]


def _words(text: str) -> list:
    import re
    return re.findall(r"[a-z][a-z'’-]{2,}", text.lower())


def _short(text: Any, most: int) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= most else words[:most - 1].rstrip() + "…"


class Gtm:
    def __init__(self, desk):
        self.desk = desk

    @property
    def motions(self):
        return self.desk.motions

    @property
    def base(self):
        return self.desk.assets

    # -- reading ---------------------------------------------------------------------------------------------
    def maps(self, most: int = 6) -> list:
        """Every market map on the desk, whichever pipeline it is kept with, the most lately changed first."""
        out, books, notes = [], self.motions.books(), self.notes()
        for pipeline, name in books:
            with self.desk.on(pipeline):
                for listed in self.desk.markets.list_markets():
                    summary = self.desk.markets.summary(listed["id"], text=False)
                    market, numbers = summary["market"], summary["numbers"]
                    out.append({
                        "id": market["id"], "key": "%s:%s" % (pipeline, market["id"]), "name": market["name"], "pipeline": name if len(books) > 1 else "",
                        "updated": str(market.get("updated_at") or ""),
                        "what_we_sell_and_who_to": _short(market.get("brief"), 1200),
                        "internal_notes": notes.get("%s:%s" % (pipeline, market["id"]), ""),
                        "companies": numbers.get("companies", 0), "in_the_pipeline": numbers.get("in_pipeline", 0), "with_a_reason_to_call": numbers.get("with_signal", 0),
                        "where_we_play": [{"segment": segment["segment"], "about": segment.get("description") or "", "companies": segment["count"], "average_fit": segment.get("avg_fit")}
                                          for segment in summary["segments"]],
                        "how_we_win": [_short(line, 260) for line in (market.get("implications") or [])][:5],
                        "reading": _short(market.get("reading"), 700),
                        "what_has_to_be_true": [{"requirement": rate["label"], "shown_by": "%d of %d" % (rate["shown"], rate["of"]) if rate.get("of") else "not checked yet"}
                                                for rate in (summary.get("requirements") or [])],
                        "who_we_would_replace": [{"name": hold["name"], "share": "%d%%" % hold["share"]} for hold in (summary.get("holds") or [])[:4]],
                        "call_first": [call["company"] for call in (summary.get("first_calls") or []) if call.get("status") != "tracked"][:3],
                    })
        out.sort(key=lambda one: one["updated"], reverse=True)
        return out[:most]

    def plays(self, maps: list) -> dict:
        """The motions under way, without their figures (``gtm_motion`` counts those): what each is, and who it is aimed at."""
        names = {one["key"]: one["name"] for one in maps}
        rows = self.motions.rows()

        def said(row: dict) -> dict:
            target = names.get("%s:%s" % (row.get("market_pipeline") or "", row["market"]), "") if row["market"] else ""
            aimed = (", ".join(row["segments"]) + " (on the map %s)" % target if row["segments"] else "all of " + target) if target else "no market map yet"
            return {"name": row["name"], "kind": row["kind"], "status": row["status"], "aimed_at": aimed, "goal": _short(row["goal"], 220),
                    "dates": " to ".join(day for day in (row["start_on"], row["end_on"]) if day)}
        return {"running": [said(row) for row in rows if row["status"] == "running"][:8], "planned": [said(row) for row in rows if row["status"] == "planned"][:6],
                "ideas": [row["name"] for row in rows if row["status"] == "idea"][:8]}

    def context(self) -> dict:
        maps, kept = self.maps(), self.base.view()
        return {"maps": maps, "motions": self.plays(maps),
                "links": [{"label": link["label"], "url": link["url"]} for link in kept["links"]],
                "assets": [{"label": asset["label"], "kind": asset["kind_label"], "url": asset["url"] or "(a file kept in the Stoa app)"} for asset in kept["assets"]]}

    def text(self) -> str:
        """The same reading as a section of an agent's instructions. It is short on purpose: enough to write and plan
        from, with the tools named for the rest."""
        try:
            seen = self.context()
        except Exception:               # a desk that cannot be read is no reason for the agent not to start
            return ""
        lines = ["## The go-to-market (the GTM tab)",
                 "What the business sells, who to and how, as the desk has it today. Work from this when you write or plan for the person, and do not ask "
                 "for what it already says. gtm_market has the companies, gtm_motion the figures, gtm_hub the assets.", SHARED]
        if not seen["maps"]:
            lines.append("There is no market map yet, so nothing on the desk says what the business sells or who to. Ask the person before you describe "
                         "the business, then map the market with gtm_market.")
        for one in seen["maps"][:MAPS_IN_FULL]:
            lines.append("")
            lines.append("Map: %s%s (%d companies, %d in the pipeline, %d with a reason to call)" % (
                one["name"], " · kept with the %s pipeline" % one["pipeline"] if one["pipeline"] else "", one["companies"], one["in_the_pipeline"], one["with_a_reason_to_call"]))
            lines.append("What we sell and who to (external, printed on the map): " + (_short(one["what_we_sell_and_who_to"], 500) or NOT_WRITTEN))
            if one["internal_notes"]:
                lines.append("Internal notes (never shared): " + _short(one["internal_notes"], 700))
            if one["where_we_play"]:
                lines.append("Where we play: " + "; ".join("%s (%d)" % (segment["segment"], segment["companies"]) for segment in one["where_we_play"][:8]))
            if one["how_we_win"]:
                lines.append("How we win: " + " ".join("(%d) %s" % (index + 1, _short(line, 200)) for index, line in enumerate(one["how_we_win"][:4])))
            elif one["reading"]:
                lines.append("Reading of the map: " + _short(one["reading"], 400))
            if one["what_has_to_be_true"]:
                lines.append("What has to be true of a company: " + "; ".join("%s (%s)" % (rate["requirement"], rate["shown_by"]) for rate in one["what_has_to_be_true"][:6]))
            if one["who_we_would_replace"]:
                lines.append("Who we would replace: " + ", ".join("%s %s" % (hold["name"], hold["share"]) for hold in one["who_we_would_replace"]))
            if one["call_first"]:
                lines.append("Call first: " + ", ".join(one["call_first"]))
        if len(seen["maps"]) > MAPS_IN_FULL:
            lines.append("Other maps: " + ", ".join(one["name"] for one in seen["maps"][MAPS_IN_FULL:]) + ".")
        plays = seen["motions"]
        if plays["running"] or plays["planned"] or plays["ideas"]:
            lines.append("")
            for label, rows in (("Motions running", plays["running"]), ("Motions planned", plays["planned"])):
                if rows:
                    lines.append(label + ": " + "; ".join("%s (%s, aimed at %s)" % (row["name"], row["kind"], row["aimed_at"]) for row in rows))
            if plays["ideas"]:
                lines.append("Motion ideas: " + ", ".join(plays["ideas"]) + ".")
        if seen["links"] or seen["assets"]:
            lines.append("")
            if seen["assets"]:
                lines.append("Key assets: " + ", ".join("%s (%s)" % (asset["label"], asset["kind"]) for asset in seen["assets"][:8]))
            if seen["links"]:
                lines.append("Links: " + "; ".join("%s %s" % (link["label"], link["url"]) for link in seen["links"][:8]))
        return _cut("\n".join(lines), TEXT_CHARS)

    def repeats(self, text: Any) -> list:
        """What an answer carries out of the internal notes, found by rule: a run of five words lifted from one, or a
        figure from one beside two of the words it was written with. Gives back the sentences of the notes that were
        drawn on, shortened. It finds what was quoted, not what was hinted at."""
        answer = str(text or "")
        if not answer.strip():
            return []
        said = " ".join(_words(answer))
        answer_sentences = [(set(_words(part)) - STOP, _amounts(part)) for part in _sentences(answer)]
        hits: list = []
        for note in self.notes().values():
            for sentence in _sentences(note):
                words = _words(sentence)
                lifted = any(" ".join(words[at:at + 5]) in said for at in range(0, max(0, len(words) - 4)))
                figures, own = _amounts(sentence), set(words) - STOP
                beside = bool(figures) and bool(own) and any(figures & theirs and len(own & theirs_words) >= min(2, len(own)) for theirs_words, theirs in answer_sentences)
                if (lifted or beside) and sentence not in hits:
                    hits.append(sentence)
        return [_short(hit, 120) for hit in hits[:3]]

    # -- writing ---------------------------------------------------------------------------------------------
    def notes(self) -> dict:
        """The internal notes of this desk's maps (or of the tour's), as ``{map key: words}``."""
        kept = self.base.file.get("map_notes", {})
        mine = kept.get(self.motions.scope(), {}) if isinstance(kept, dict) else {}
        return {str(key): str(words) for key, words in mine.items() if words} if isinstance(mine, dict) else {}

    def _one(self, ref: Any) -> tuple:
        """(the pipeline a map is kept with, its id), for the map named, or for the only one when none is named."""
        maps = self.motions.maps()
        if not maps:
            raise Refused("There is no market map to say it on yet. Map a market first, with gtm_market.")
        if not str(ref or "").strip():
            if len(maps) > 1:
                raise Refused("Say which market map: %s." % ", ".join(one["name"] for one in maps))
            ref = maps[0]["key"]
        return self.motions._market(ref)

    def set_notes(self, ref: Any, text: Any) -> dict:
        """Keep a map's internal notes: what only the person and the agents read. Empty words clear them."""
        pipeline, market_id = self._one(ref)
        words = str(text or "").strip()[:NOTE_CHARS]
        kept = self.base.file.get("map_notes", {})
        kept = dict(kept) if isinstance(kept, dict) else {}
        mine = dict(kept.get(self.motions.scope()) or {})
        key = "%s:%s" % (pipeline, market_id)
        if words:
            mine[key] = words
        else:
            mine.pop(key, None)
        kept[self.motions.scope()] = mine
        self.base.file.set("map_notes", kept)
        return {"map": key, "notes": words}

    def set_brief(self, ref: Any, text: Any) -> dict:
        """Say what the business sells and who to, as the brief of one market map."""
        words = " ".join(str(text or "").split())[:1200]
        if not words:
            raise Refused("Say what the business sells and who to, in a sentence or two.")
        pipeline, market_id = self._one(ref)
        with self.desk.on(pipeline):
            done = self.desk.markets.define({"name": market_id, "brief": words})
        if not done.get("ok"):
            raise Refused(str(done.get("error") or "The brief was not saved."))
        return {"map": done["market"]["name"], "brief": done["market"]["brief"]}

    # -- the tool an agent uses -------------------------------------------------------------------------------
    def tool(self, args: dict, session: Optional[dict] = None) -> dict:
        """``gtm_context``, as the agent calls it."""
        args = dict(args or {})
        action = str(args.get("action") or "get").strip().lower()
        try:
            if action == "get":
                return dict(self.context(), ok=True, note="A map with no 'what_we_sell_and_who_to' has not been told: ask the person, then save it with set_brief. " + SHARED)
            if action not in ("set_brief", "set_notes"):
                raise Refused("gtm_context does not have an action called %s." % action)
            if (session or {}).get("platform") == "cron":
                raise Refused("A scheduled job may read the go-to-market but not change it.")
            if action == "set_notes":
                saved = self.set_notes(args.get("map"), args.get("text"))
                return dict(saved, ok=True, note="They are under Maps on the GTM tab of the Stoa page, marked internal. They are never printed on the map or sent.")
            saved = self.set_brief(args.get("map"), args.get("text"))
        except Refused as exc:
            return {"ok": False, "error": str(exc)}
        return dict(saved, ok=True, note="It is under Maps on the GTM tab of the Stoa page, at the head of 'Where we play'. It is also printed on the map when it is shared.")


PROMPT_LINE = ("Go-to-market: market maps (what the business sells, who to, how it wins), motions (the plays under way) and assets (links, decks) "
               "are on the GTM tab. Call gtm_context once before you write about the business or plan a motion, and work from it.")


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[:most].rsplit("\n", 1)[0] + "\n(There is more: gtm_context gives all of it.)"
