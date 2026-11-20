"""How a deal looks: Slack Block Kit cards with buttons, and plain text for every other surface."""
from __future__ import annotations

from typing import Optional

from . import store as st

PREFIX = "stoa_"
ACTION_ADVANCE = PREFIX + "advance"
ACTION_DRAFT = PREFIX + "draft"
ACTION_ENRICH = PREFIX + "enrich"
ACTION_LOST = PREFIX + "lost"
ACTION_MORE = PREFIX + "more"
ACTION_REOPEN = PREFIX + "reopen"


def exact(value: float, currency: str = "USD") -> str:
    """An amount to the cent, as on an invoice: $16,720 or $15,200.50."""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    text = "{:,.2f}".format(float(value or 0))
    text = text[:-3] if text.endswith(".00") else text
    return symbol + text if symbol else "%s %s" % (text, currency)


def money(value: float, currency: str = "USD") -> str:
    if not value:
        return ""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    if value >= 1_000_000:
        text = "%.1fM" % (value / 1_000_000)
    elif value >= 10_000:
        text = "%.0fk" % (value / 1000)
    else:
        text = "{:,.0f}".format(value)
    return symbol + text if symbol else "%s %s" % (text, currency)


def age(stamp: str) -> str:
    days = st.days_since(stamp)
    if days is None:
        return ""
    if days < 1 / 24:
        return "just now"
    if days < 1:
        return "%dh ago" % int(days * 24)
    if days < 2:
        return "yesterday"
    return "%dd ago" % int(days)


def meter(playbook, deal: dict) -> str:
    """``▰▰▰▱▱  Screened · 3 of 5`` — where the deal sits in the playbook."""
    if deal["status"] == "won":
        return "▰" * len(playbook.stages) + "  %s" % playbook.label(deal["stage"])
    if deal["status"] == "lost":
        reason = " · %s" % deal["lost_reason"] if deal.get("lost_reason") else ""
        return "▱" * len(playbook.stages) + "  %s%s" % (playbook.label(deal["stage"]), reason)
    pos = playbook.position(deal["stage"])
    if pos < 0:
        return deal["stage"]
    total = len(playbook.stages)
    return "%s%s  %s · %d of %d" % ("▰" * (pos + 1), "▱" * (total - pos - 1), playbook.label(deal["stage"]), pos + 1, total)


def snoozed(deal: dict) -> bool:
    """Put aside until a date: until then it is on neither the due list nor the quiet list."""
    return bool(deal.get("snoozed_until")) and deal["snoozed_until"] > st.today()


def is_stale(playbook, deal: dict, last_touch: str = "") -> bool:
    if deal["status"] != "open" or snoozed(deal):
        return False
    days = st.days_since(last_touch or deal["updated_at"])
    return days is not None and days > playbook.stale_days(deal["stage"])


def due_date(deal: dict) -> str:
    """The record's due date as YYYY-MM-DD ("" if none, or if an old free-text value cannot be read)."""
    due = deal.get("next_due") or ""
    return due if st.ISO_DATE.match(due) else (st.parse_due(due) or "")


def in_delivery(playbook, deal: dict) -> bool:
    """Won, and the work after the win is not finished: the record is short of the playbook's last delivery stage
    (Onboarding, where the last is Live). Its next step still counts until then. A playbook with one delivery
    stage, or none, has nowhere to finish, so a win there is simply closed."""
    if playbook is None or deal["status"] != "won" or len(playbook.delivery) < 2:
        return False
    return (deal.get("delivery") or playbook.delivery[0]["id"]) != playbook.delivery[-1]["id"]


def working(playbook, deal: dict) -> bool:
    """Still being worked, so its next step shows and comes due: open, or won and not through delivery yet."""
    return deal["status"] == "open" or in_delivery(playbook, deal)


def is_due(deal: dict, playbook=None) -> bool:
    due = due_date(deal)
    return working(playbook, deal) and bool(due) and due <= st.today() and not snoozed(deal)


def facts(playbook, deal: dict) -> list:
    """(label, value) pairs worth showing, in display order."""
    out = []
    if deal.get("grp"):
        out.append((playbook.terms["group"].capitalize(), deal["grp"]))
    if deal.get("value"):
        out.append((playbook.terms["value"], money(deal["value"], playbook.currency)))
    if deal.get("owner"):
        out.append((playbook.terms["owner"], deal["owner"]))
    for field in playbook.fields:
        value = deal["fields"].get(field["key"])
        if value:
            out.append((field["label"], str(value)))
    if deal.get("email"):
        out.append(("Email", deal["email"]))
    if deal.get("linkedin"):
        out.append(("LinkedIn", deal["linkedin"]))
    return out


def text_card(playbook, deal: dict, activities: Optional[list] = None, last_touch: str = "") -> str:
    """The same card as plain text (CLI, Telegram, and the notification line in Slack)."""
    head = deal["name"] + (" · " + deal["company"] if deal.get("company") else "")
    lines = [head, meter(playbook, deal)]
    for label, value in facts(playbook, deal):
        lines.append("%s: %s" % (label, value))
    if deal.get("next_step"):
        due = " (due %s%s)" % (deal["next_due"], ", overdue" if is_due(deal, playbook) and due_date(deal) < st.today() else "") \
            if deal.get("next_due") else ""
        lines.append("Next: %s%s" % (deal["next_step"], due))
    if is_stale(playbook, deal, last_touch):
        lines.append("Quiet for %s" % age(last_touch or deal["updated_at"]).replace(" ago", ""))
    for act in (activities or [])[:3]:
        lines.append("- %s  %s%s" % (st.local_day(act["at"]), act["text"], " (%s)" % act["actor"] if act.get("actor") else ""))
    lines.append("id: %s" % deal["id"])
    return "\n".join(lines)


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


DRAFT_LINES = {"drafting": ":writing_hand: Drafting\u2026",
               "waiting": ":speech_balloon: Draft waiting. Reply \"draft it\" in this thread."}


def card_blocks(playbook, deal: dict, activities: Optional[list] = None, last_touch: str = "",
                can_enrich: bool = False, draft: str = "") -> list:
    """Block Kit for one deal. Every interactive element carries the deal id in its value.

    ``draft`` is where a requested piece of writing stands (``drafting``, ``waiting`` or nothing);
    it shows as one line under the buttons, where the person who clicked is looking.
    """
    status = [{"type": "context", "elements": [{"type": "mrkdwn", "text": DRAFT_LINES[draft]}]}] if draft in DRAFT_LINES else []
    deal_id = deal["id"]
    title = "*%s*" % _escape(deal["name"])
    if deal.get("company"):
        title += "  ·  %s" % _escape(deal["company"])
    flags = []
    if is_due(deal, playbook):
        flags.append(":small_orange_diamond: next step due")
    if is_stale(playbook, deal, last_touch):
        flags.append(":hourglass: quiet %s" % age(last_touch or deal["updated_at"]).replace(" ago", ""))
    if snoozed(deal) and deal["status"] == "open":
        flags.append(":zzz: snoozed to %s" % deal["snoozed_until"])
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": title + "\n`%s`" % meter(playbook, deal)}}]
    pairs = facts(playbook, deal)
    if pairs:
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": "*%s*\n%s" % (_escape(label), _escape(value))} for label, value in pairs[:10]]})
    if deal.get("next_step") and deal["status"] == "open":
        due = "  _(due %s)_" % deal["next_due"] if deal.get("next_due") else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Next*  %s%s" % (_escape(deal["next_step"]), due)}})
    trail = []
    for act in (activities or [])[:2]:
        who = " · %s" % _escape(act["actor"]) if act.get("actor") else ""
        trail.append("%s  %s%s" % (age(act["at"]), _escape(act["text"])[:140], who))
    context = flags + trail
    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line} for line in context[:6]]})

    if deal["status"] != "open":
        blocks.append({"type": "actions", "block_id": PREFIX + "actions", "elements": [
            {"type": "button", "action_id": ACTION_REOPEN, "value": deal_id, "text": {"type": "plain_text", "text": "Reopen"}}]})
        return blocks + status

    elements = []
    nxt = playbook.next_stage(deal["stage"])
    if nxt:
        # the stage rides along so a second click on a stale card cannot advance twice
        elements.append({"type": "button", "style": "primary", "action_id": ACTION_ADVANCE,
                         "value": "%s|%s" % (deal_id, deal["stage"]),
                         "text": {"type": "plain_text", "text": playbook.advance_label(deal["stage"])[:70]}})
    elements.append({"type": "button", "action_id": ACTION_DRAFT, "value": deal_id,
                     "text": {"type": "plain_text", "text": "Draft follow-up"}})
    if can_enrich and playbook.enrich != "none" and not deal["fields"].get("enriched"):
        elements.append({"type": "button", "action_id": ACTION_ENRICH, "value": deal_id,
                         "text": {"type": "plain_text", "text": "Enrich"}})
    elements.append({
        "type": "static_select", "action_id": ACTION_LOST,
        "placeholder": {"type": "plain_text", "text": "%s…" % playbook.lost["label"][:60]},
        "options": [{"text": {"type": "plain_text", "text": reason[:70]}, "value": "%s|%d" % (deal_id, i)}
                    for i, reason in enumerate(playbook.lost["reasons"])],
        "confirm": {"title": {"type": "plain_text", "text": "Close this %s?" % playbook.terms["deal"][:40]},
                    "text": {"type": "plain_text", "text": "%s will be marked %s. You can reopen it later."
                                                           % (deal["name"][:80], playbook.lost["label"][:40])},
                    "confirm": {"type": "plain_text", "text": "Yes, close it"},
                    "deny": {"type": "plain_text", "text": "Cancel"}}})
    elements.append({"type": "overflow", "action_id": ACTION_MORE, "options": [
        {"text": {"type": "plain_text", "text": "Snooze 3 days"}, "value": "snooze3|" + deal_id},
        {"text": {"type": "plain_text", "text": "Snooze 1 week"}, "value": "snooze7|" + deal_id},
        {"text": {"type": "plain_text", "text": "Refresh card"}, "value": "refresh|" + deal_id}]})
    blocks.append({"type": "actions", "block_id": PREFIX + "actions", "elements": elements})
    return blocks + status


def board_text(playbook, deals: list, title: str = "", total=None, staged=None) -> str:
    """The whole pipeline as a compact text board. ``total``: how the pipeline adds records up (the desk's ``sum``, which
    counts a search once on a contingent recruiting pipeline); the plain sum if not given. ``staged``: each stage's
    amount by stage id (the desk's ``stage_sums``, where a search counts in the stage of its furthest-along
    candidate, so the stages add up to the head); each stage added up by itself if not given."""
    lines = [title or playbook.title]
    open_deals = [d for d in deals if d["status"] == "open"]
    add = total or st.total
    total = add(open_deals)
    summary = "%d open %s" % (len(open_deals), playbook.terms["deals"])
    if total:
        summary += " · %s" % money(total, playbook.currency)
    lines.append(summary)
    known = set(playbook.stage_ids())
    for stage in playbook.stages:
        here = [d for d in open_deals if d["stage"] == stage["id"]]
        value = staged.get(stage["id"], 0) if staged is not None else add(here)
        lines.append("")
        lines.append("%s (%d)%s" % (stage["label"], len(here), "  " + money(value, playbook.currency) if value else ""))
        for deal in here[:12]:
            lines.append("  " + _row(deal, playbook))
        if len(here) > 12:
            lines.append("  … and %d more" % (len(here) - 12))
    strays = [d for d in open_deals if d["stage"] not in known]
    if strays:
        lines.append("")
        lines.append("Other stages (%d) — from a different playbook" % len(strays))
        for deal in strays[:12]:
            lines.append("  %s [%s]" % (_row(deal, playbook), deal["stage"]))
    return "\n".join(lines)


def _row(deal: dict, playbook) -> str:
    bits = [deal["name"]]
    if deal.get("company"):
        bits.append(deal["company"])
    if deal.get("grp"):
        bits.append(deal["grp"])
    if deal.get("value"):
        bits.append(money(deal["value"], playbook.currency))
    row = " · ".join(bits)
    if deal.get("next_step"):
        row += "  → %s" % deal["next_step"][:60]
        if deal.get("next_due"):
            row += " (%s)" % deal["next_due"]
    return row
