"""A market map as one self-contained web page: no outside requests, nothing it depends on, one file to host or send.

``render(summary, terms, share)`` takes what ``Markets.summary`` computed and returns HTML. With
``share`` the page leaves out buyers' names and what is in the pipeline, so it can be sent on.

The map at the head of the page is the one the Stoa page draws (three views, pick a company to open it, weigh the
requirements), run by ``pagemap.js``, which is written into the page with the companies beside it. Nothing is fetched
and nothing is sent. A browser that runs no scripts shows the still picture instead, and the rest of the page as before.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from html import escape as esc
from pathlib import Path

CSS = """
:root { --bg:#ffffff; --ink:#1b1c20; --soft:#4b4e58; --mute:#7f8390; --line:#c9ccd4; --accent:#0053fd;
  --h1:#2150cc; --h2:#2477c4; --h3:#1b8f9e; --h4:#259b66; --h5:#b97d16; --h6:#cf6246; --h7:#c4517f; color-scheme: light;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --bar: linear-gradient(90deg, var(--h1), var(--h2) 46%, var(--ink));
  --dots: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); }
@media (prefers-color-scheme: dark) { :root { --bg:#0a0b0d; --ink:#e8e6de; --soft:#b3b1aa; --mute:#7d7e80; --line:#34373e; --accent:#4a84fe;
  --h1:#3d68e0; --h2:#2c86d9; --h3:#30b4c4; --h4:#48c78c; --h5:#f2b24c; --h6:#f4967e; --h7:#f3a9c0; color-scheme: dark; } }
* { box-sizing: border-box; } html, body { margin: 0; background: var(--bg); color: var(--ink); }
body { font: 15px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 1060px; margin: 0 auto; padding: 44px 32px 80px; -webkit-font-smoothing: antialiased; }
.mono, th, .eyebrow, .stat small, .bar em, .tag, h2 { font-family: var(--mono); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--mute); font-style: normal; font-weight: 400; }
.eyebrow::before, h2::before { content: "░▒▓"; margin-right: 9px; letter-spacing: 0; background: var(--bar); -webkit-background-clip: text; background-clip: text; color: transparent; }
h1 { font-size: clamp(34px, 5.2vw, 58px); font-weight: 600; line-height: 1.02; letter-spacing: -.03em; margin: 16px 0 14px; }
h2 { margin: 0 0 14px; padding-top: 14px; border-top: 1px dotted var(--mute); }
p { margin: 0 0 10px; color: var(--soft); max-width: 72ch; } a { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--mute); }
section { margin-top: 44px; }
.rule { height: 2px; margin: 26px 0 0; background: var(--bar); -webkit-mask-image: var(--dots); mask-image: var(--dots); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0; border-top: 1px dotted var(--mute); margin-top: 26px; }
.stat { padding: 16px 16px 4px 0; } .stat b { display: block; font: 600 34px/1 var(--mono); letter-spacing: -.03em; font-variant-numeric: tabular-nums; } .stat small { display: block; margin-top: 9px; color: var(--ink); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; } @media (max-width: 760px) { .two { grid-template-columns: 1fr; } body { padding: 28px 18px 60px; } }
.bar { display: grid; grid-template-columns: 46px 1fr; gap: 10px; align-items: center; padding: 7px 0; border-bottom: 1px dotted var(--line); }
.bar b { font: 600 13px var(--mono); color: var(--ink); letter-spacing: 0; font-variant-numeric: tabular-nums; } .bar span { display: block; }
.bar i { display: block; height: 2px; margin-top: 6px; background: var(--ink); -webkit-mask-image: var(--dots); mask-image: var(--dots); }
.bar em { display: block; margin-top: 4px; text-transform: none; letter-spacing: 0; font-family: inherit; font-size: 12.5px; }
table { width: 100%; border-collapse: collapse; } table.co { table-layout: fixed; } table.co th:nth-child(1) { width: 21%; } table.co th:nth-child(2) { width: 9%; }
table.co th:nth-child(3) { width: 36%; } table.co th:nth-child(4) { width: 15%; } th { text-align: left; padding: 0 12px 8px 0; border-bottom: 1px dotted var(--mute); }
td { vertical-align: top; padding: 11px 12px 11px 0; border-bottom: 1px dotted var(--line); font-size: 13.5px; color: var(--soft); } td strong { color: var(--ink); font-weight: 600; }
.fit { white-space: nowrap; font: 10px var(--mono); letter-spacing: 2px; color: var(--h4); padding-top: 14px; } .fit s { text-decoration: none; color: var(--line); }
.f1 { color: var(--h2); } .f2 { color: var(--h3); } .f4 { color: var(--h5); } .f5 { color: var(--h6); }
.tag { display: inline-block; margin: 4px 10px 0 0; font-size: 10px; letter-spacing: .08em; }
.tag::before { content: "[ ] "; letter-spacing: 0; } .tag.on { color: var(--ink); } .tag.on::before { content: "[x] "; }
ol.calls { margin: 0; padding: 0; list-style: none; counter-reset: c; } ol.calls li { counter-increment: c; display: grid; grid-template-columns: 34px 1fr; padding: 12px 0; border-bottom: 1px dotted var(--line); }
ol.calls li::before { content: counter(c, decimal-leading-zero); font: 11px var(--mono); color: var(--mute); padding-top: 3px; } ol.calls strong { font-weight: 600; }
svg { width: 100%; height: auto; display: block; } svg text { font: 10.5px var(--mono); letter-spacing: .12em; fill: var(--mute); }
svg .name { font: 500 10.5px Inter, system-ui, sans-serif; letter-spacing: 0; fill: var(--soft); } svg .seg { fill: var(--ink); }
svg circle.ring { fill: none; stroke: var(--mute); stroke-dasharray: 1.2 4.6; stroke-linecap: square; } svg circle.co { fill: var(--bg); stroke: var(--ink); } svg circle.in { fill: var(--ink); }
svg .ini { fill: var(--ink); letter-spacing: .06em; font-size: 9.5px; } svg .ini.in { fill: var(--bg); }
footer { margin-top: 56px; padding-top: 14px; border-top: 1px dotted var(--mute); color: var(--mute); font-size: 12.5px; }
[hidden] { display: none !important; }
.sm-sub { color: var(--mute); font-size: 12.5px; max-width: none; } .sm-sub.tagged { margin-top: 6px; }
.sm-bar { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin-bottom: 8px; }
.sm-seg { display: inline-flex; border: 1px solid var(--line); border-radius: 2px; }
.sm-seg button, .sm-quick button, .sm-btn { height: 26px; padding: 0 10px; border: 0; background: transparent; color: var(--soft); font: 500 10.5px var(--mono); letter-spacing: .08em; text-transform: uppercase; cursor: pointer; }
.sm-seg button + button { border-left: 1px solid var(--line); }
.sm-seg button[aria-pressed="true"] { background: var(--ink); color: var(--bg); }
.sm-quick { display: flex; flex-wrap: wrap; gap: 2px; flex: 1 1 auto; }
.sm-quick button { border: 1px solid transparent; border-radius: 2px; text-transform: none; letter-spacing: 0; font-size: 11.5px; } .sm-quick button i { font-style: normal; }
.sm-quick button[aria-pressed="true"] { border-color: var(--line); color: var(--ink); }
.sm-btn { margin-top: 12px; border: 1px solid var(--line); border-radius: 2px; color: var(--ink); }
button:focus-visible, #sm-find:focus-visible, .sm-co:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
#sm-find { height: 26px; width: 180px; max-width: 100%; padding: 0 8px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ink); font: 12px var(--mono); }
.sm-main { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; } @media (min-width: 900px) { .sm-main { grid-template-columns: minmax(0, 1fr) 300px; } }
#sm-map { position: relative; border: 1px solid var(--line); background-image: radial-gradient(var(--line) 1px, transparent 1.2px); background-size: 16px 16px; overflow: hidden; user-select: none; -webkit-user-select: none; }
#sm-svg { position: absolute; inset: 0; width: auto; height: auto; }
.sm-ring { fill: none; stroke: var(--mute); stroke-dasharray: 1.2 4.6; stroke-linecap: square; }
.sm-link { stroke: var(--soft); stroke-dasharray: 1.2 3.6; stroke-linecap: square; } .sm-split { stroke: var(--line); stroke-dasharray: 1.2 4.6; stroke-linecap: square; } .sm-axis { stroke: var(--mute); }
.sm-co { cursor: pointer; outline-offset: 4px; transition: transform .75s cubic-bezier(.2,.75,.2,1), opacity .3s; } .sm-co.dim { opacity: .12; }
.sm-pop { animation: sm-pop .5s cubic-bezier(.2,1.25,.4,1) both; } @keyframes sm-pop { from { transform: scale(0); opacity: 0; } to { transform: scale(1); opacity: 1; } }
.sm-edge { fill: none; opacity: .55; } .sm-co.in .sm-edge { opacity: 0; } .sm-sig { fill: none; stroke: var(--ink); opacity: .75; }
.sm-brk { fill: none; stroke: var(--ink); stroke-width: 1.5; opacity: 0; } .sm-co.sel .sm-brk { opacity: 1; }
svg text.sm-lbl { font: 500 11px Inter, ui-sans-serif, system-ui, sans-serif; letter-spacing: 0; fill: var(--soft); text-anchor: middle; opacity: 0; pointer-events: none;
  transition: transform .75s cubic-bezier(.2,.75,.2,1), opacity .3s; paint-order: stroke; stroke: var(--bg); stroke-width: 4px; stroke-linejoin: round; }
svg text.sm-lbl.on { opacity: 1; } svg text.sm-lbl.sel { fill: var(--ink); font-weight: 600; }
#sm-over { position: absolute; inset: 0; pointer-events: none; } #sm-over > * { position: absolute; }
.sm-cap { transform: translateX(-50%); text-align: center; } .sm-cap b { display: block; font: 600 10.5px/1.35 var(--mono); letter-spacing: .12em; text-transform: uppercase; }
.sm-cap span { font: 11px var(--mono); color: var(--mute); }
.sm-q { font: 10.5px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--mute); } .sm-q strong { color: var(--ink); } .sm-q em { display: block; font: 11.5px Inter, system-ui, sans-serif; letter-spacing: 0; text-transform: none; font-style: normal; }
.sm-hint { right: 10px; top: 8px; font: 10px var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--mute); } #sm-map.nar .sm-hint { display: none; }
#sm-tip { position: absolute; z-index: 5; width: 232px; padding: 9px 11px; border: 1px solid var(--line); background: var(--bg); font-size: 12px; line-height: 1.4; pointer-events: none; } #sm-tip b { display: block; font-size: 13px; }
#sm-lasso { position: absolute; z-index: 4; border: 1px dashed var(--ink); pointer-events: none; }
.sm-legend { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; padding-top: 9px; font: 10.5px var(--mono); color: var(--mute); } .sm-legend .sm-seg button { height: 22px; padding: 0 8px; font-size: 10px; }
.sm-heat { display: inline-block; width: 60px; height: 6px; vertical-align: middle; background: linear-gradient(90deg, var(--h1), var(--h2), var(--h3), var(--h4), var(--h5), var(--h6), var(--h7)); -webkit-mask-image: var(--dots); mask-image: var(--dots); }
#sm-side { border: 1px solid var(--line); padding: 14px 16px; min-width: 0; font-size: 13.5px; overflow-wrap: anywhere; } #sm-tip { overflow-wrap: anywhere; } #sm-side h3 { margin: 6px 0 2px; font-size: 20px; font-weight: 600; letter-spacing: -.015em; } #sm-side p { margin: 10px 0; color: var(--ink); }
.sm-score { display: flex; align-items: baseline; gap: 8px; margin-top: 12px; } .sm-score .mono { flex: 1; } .sm-score b { font: 600 24px var(--mono); letter-spacing: -.02em; }
.sm-req { display: grid; grid-template-columns: 26px minmax(0, 1fr); padding: 6px 0; border-bottom: 1px dotted var(--line); } .sm-req > span:first-child { font: 11px var(--mono); color: var(--line); } .sm-req.on > span:first-child { color: var(--h4); }
.sm-req b { font-weight: 500; } .sm-req:not(.on) b { font-weight: 400; color: var(--mute); }
.sm-facts { display: grid; grid-template-columns: 84px minmax(0, 1fr); gap: 6px 10px; margin: 12px 0 0; } .sm-facts dt { font: 10.5px var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--mute); padding-top: 2px; } .sm-facts dd { margin: 0; overflow-wrap: anywhere; }
.sm-row { display: block; width: 100%; padding: 9px 0; border: 0; border-bottom: 1px dotted var(--line); background: none; color: var(--soft); font: inherit; text-align: left; cursor: pointer; } .sm-row b { color: var(--ink); font-weight: 600; } .sm-row span { display: block; }
.sm-row .fit { display: inline; padding: 0; }
.sm-what { margin-top: 26px; max-width: 560px; } .sm-weight { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 12px; align-items: center; padding: 8px 0; border-bottom: 1px dotted var(--line); font-size: 13.5px; }
.sm-weight small { display: block; font: 11px var(--mono); color: var(--mute); } .sm-weight .sm-seg button { height: 22px; padding: 0 7px; font-size: 10px; }
@media (prefers-reduced-motion: reduce) { .sm-co, .sm-pop, svg text.sm-lbl { transition: none; animation: none; } }
@media print { .sm-bar, .sm-what, .sm-hint, .sm-legend .sm-seg { display: none; } }
"""
SCRIPT = Path(__file__).with_name("pagemap.js")


def _live(summary: dict, share: bool) -> str:
    """The working map: its frame, the companies it draws (as data in the page), and the script that draws them."""
    segments = []
    for seg in summary["segments"]:
        rows = []
        for c in seg["companies"]:
            row = {k: c[k] for k in ("id", "company", "domain", "fit", "size", "location", "why", "signal", "signal_date", "source", "uses", "also",
                                     "headcount", "requirements") if c.get(k) not in (None, "", [])}
            if not share:                                    # the copy to send on names no buyer and says nothing of the pipeline
                if c.get("people"):
                    row["people"] = [{"name": p.get("name", ""), "title": p.get("title", "")} for p in c["people"][:3]]
                if c.get("status") == "tracked":
                    row["status"] = "tracked"
            rows.append(row)
        segments.append({"name": seg["segment"], "companies": rows})
    data = {"name": summary["market"]["name"], "today": dt.date.today().isoformat(), "requirements": summary["market"]["requirements"],
            "segments": segments, "size": "people"}
    packed = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")      # nothing in a company's words can end the block early
    patterns = "".join('<pattern id="sm-st%d" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="2.2" height="2.2" style="fill:var(--h%d)"/></pattern>' % (k, k)
                       for k in range(1, 8))
    quick = [("all", "All"), ("best", "Best fit"), ("reason", "Reason to call")] + ([] if share else [("open", "Not in the pipeline")])
    sizes = [("people", "People there")] + ([] if share else [("named", "People named")]) + [("same", "Same size")]
    return "".join([
        '<div id="sm-live" hidden><div class="sm-bar"><span class="sm-seg" role="group" aria-label="How the map is drawn">',
        '<button data-lens="seg">Segments</button><button data-lens="pri">Priority</button><button data-lens="held">Held by</button></span>',
        '<span class="sm-quick">%s</span>' % "".join('<button data-quick="%s">%s <i></i></button>' % item for item in quick),
        '<input id="sm-find" type="search" placeholder="Find a company" aria-label="Find a company" autocomplete="off"></div>',
        '<p id="sm-how" class="sm-sub"></p><div class="sm-main"><div>',
        '<div id="sm-map"><svg id="sm-svg" role="group" aria-label="Market map"><defs>%s' % patterns,
        '<pattern id="sm-wash" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="1.6" height="1.6" style="fill:var(--h6)" opacity=".5"/></pattern></defs>',
        '<g id="sm-rings"></g><g id="sm-links"></g><g id="sm-nodes"></g><g id="sm-labels"></g></svg><div id="sm-over"></div><div id="sm-tip" hidden></div><div id="sm-lasso" hidden></div></div>',
        '<div class="sm-legend"><span>Size by</span><span class="sm-seg" role="group" aria-label="What a mark\'s size shows">%s</span>'
        % "".join('<button data-size="%s">%s</button>' % item for item in sizes),
        '<span>Fit <i class="sm-heat"></i> cold to hot</span>%s<span>A ring is a reason to call</span></div></div>'
        % ("" if share else "<span>Solid is in the pipeline</span>"),
        '<aside id="sm-side" aria-live="polite"></aside></div>',
        '<div class="sm-what"><h2>What matters</h2><p id="sm-weighnote" class="sm-sub"></p><div id="sm-weights"></div></div></div>' if summary["market"]["requirements"] else
        '<div class="sm-what" hidden><p id="sm-weighnote"></p><div id="sm-weights"></div></div></div>',
        '<script type="application/json" id="sm-data">%s</script>' % packed,
        "<script>%s</script>" % SCRIPT.read_text(encoding="utf-8")])


def _initials(name: str) -> str:
    words = "".join(ch if ch.isalnum() or ch == " " else " " for ch in name).split()
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()


def _sky(segments: list, share: bool) -> str:
    """The bubble map: one cluster per segment, each company sized by fit, filled once it is in the pipeline."""
    count = max(1, len(segments))
    width, height, slot = 1000, 330, 1000 / count
    golden = math.pi * (3 - math.sqrt(5))
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Market map">' % (width, height)]
    for si, seg in enumerate(segments):
        cx, cy = slot * (si + 0.5), 138
        rows = sorted(seg["companies"], key=lambda c: -c["fit"])[:14]
        out.append('<circle class="ring" cx="%.1f" cy="%d" r="%.1f"/>' % (cx, cy, min(slot / 2 - 12, 112)))
        spread = min(34, (slot / 2 - 30) / math.sqrt(max(1, len(rows))))
        for i, c in enumerate(rows):
            radius = 11 + c["fit"] * 3.4
            if len(rows) <= 7:
                mid = 1 if len(rows) >= 5 else 0
                ring, k = len(rows) - mid, i - mid
                dist = 0 if len(rows) == 1 or (mid and i == 0) else min(slot / 2 - 62, 74 + len(rows) * 5)
                angle = (math.pi if ring == 2 else -math.pi / 2) + (2 * math.pi * k) / max(1, ring)
            else:
                dist, angle = (0 if i == 0 else spread * math.sqrt(i) * 1.55), i * golden + si
            x, y = cx + math.cos(angle) * dist, cy + math.sin(angle) * dist * 0.82
            tracked = c.get("status") == "tracked" and not share
            out.append('<circle class="co%s" cx="%.1f" cy="%.1f" r="%.1f"><title>%s · fit %d/5</title></circle>'
                       % (" in" if tracked else "", x, y, radius, esc(c["company"]), c["fit"]))
            out.append('<text class="ini%s" x="%.1f" y="%.1f" text-anchor="middle">%s</text>'
                       % (" in" if tracked else "", x, y + 3.5, esc(_initials(c["company"]))))
            if len(rows) <= 7 or c["fit"] >= 5:
                name = c["company"] if len(c["company"]) <= 20 else c["company"][:19] + "…"
                out.append('<text class="name" x="%.1f" y="%.1f" text-anchor="middle">%s</text>' % (x, y + radius + 13, esc(name)))
        out.append('<text class="seg" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                   % (cx, height - 26, esc(seg["segment"].upper()[:max(10, int(slot / 9))])))
        out.append('<text x="%.1f" y="%d" text-anchor="middle">%d %s</text>'
                   % (cx, height - 8, seg["count"], "COMPANY" if seg["count"] == 1 else "COMPANIES"))
    out.append("</svg>")
    return "".join(out)


def _bars(rows: list, label, value, note=None, total=None) -> str:
    """Rows of ``value  label`` with a thin bar scaled to the largest value (or to ``total``)."""
    top = total or max([value(r) for r in rows] + [1])
    out = []
    for r in rows:
        extra = "<em>%s</em>" % esc(note(r)) if note and note(r) else ""
        out.append('<div class="bar"><b>%s</b><span>%s<i style="width:%d%%"></i>%s</span></div>'
                   % (esc(str(value(r)) + ("%" if total == 100 else "")), esc(label(r)), max(2, round(100 * value(r) / top)), extra))
    return "".join(out)


def _link(url: str, text: str = "") -> str:
    if not url:
        return ""
    href = url if url.startswith(("http://", "https://")) else "https://" + url
    return '<a href="%s" rel="noreferrer noopener">%s</a>' % (esc(href, quote=True), esc(text or url))


def render(summary: dict, terms: dict, share: bool = False) -> str:
    market, numbers = summary["market"], summary["numbers"]
    date = (market.get("updated_at") or "")[:10]
    stats = [(numbers["companies"], "Companies"), (numbers["segments"], "Segments"), (numbers["with_signal"], "With a signal"),
             (numbers["avg_fit"], "Average fit")]
    if not share:
        stats += [(numbers["buyers_named"], "Buyers named"), (numbers["in_pipeline"], "In the pipeline")]
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>%s · Market map</title><style>%s</style></head><body>" % (esc(market["name"]), CSS),
           '<div class="eyebrow">Market map%s</div><h1>%s</h1>' % (" // " + esc(date) if date else "", esc(market["name"]))]
    if market.get("brief"):
        out.append("<p>%s</p>" % esc(market["brief"]))
    out.append('<div class="stats">%s</div><div class="rule"></div>' % "".join(
        '<div class="stat"><b>%s</b><small>%s</small></div>' % (esc(str(v)), esc(l)) for v, l in stats))

    if market.get("reading") or market.get("implications"):
        out.append('<section class="two"><div><h2>Reading the map</h2>%s</div>' % "".join(
            "<p>%s</p>" % esc(par) for par in (market.get("reading") or "").split("\n") if par.strip()))
        out.append("<div><h2>What this means</h2>%s</div></section>" % "".join(
            '<div class="bar" style="grid-template-columns:28px 1fr"><b class="mono">%02d</b><span>%s</span></div>' % (i + 1, esc(x))
            for i, x in enumerate(market.get("implications") or [])))

    if summary["segments"]:
        out.append('<section><h2>The map</h2><div id="sm-static">%s</div>%s</section>' % (_sky(summary["segments"], share), _live(summary, share)))
        rows = "".join(
            "<tr><td><strong>%s</strong><br>%s</td><td>%d (%d%%)</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (esc(s["segment"]), esc(s["description"]), s["count"], s["share"], esc(str(s["avg_fit"])),
               esc(", ".join(s["regions"])), esc(", ".join(s["uses"])))
            for s in summary["segments"])
        out.append("<section><h2>The segments</h2><table><tr><th>Segment</th><th>Share</th><th>Avg fit</th><th>Where</th>"
                   "<th>Use today</th></tr>%s</table></section>" % rows)

    panels = []
    checked = [r for r in summary["requirements"] if r["of"]]
    if checked:
        panels.append("<div><h2>How rare each requirement is</h2><p>Share of the companies checked for each one that show it.</p>%s</div>"
                      % _bars(checked, lambda r: r["label"], lambda r: r["rate"],
                              note=lambda r: "%d of %d checked" % (r["shown"], r["of"]), total=100))
    if summary["holds"]:
        panels.append("<div><h2>Who holds this market today</h2><p>What the %d companies we could place use now.</p>%s</div>"
                      % (summary["holds_of"], _bars(summary["holds"], lambda r: r["name"], lambda r: r["share"],
                                                    note=lambda r: ", ".join(r["companies"][:4]), total=100)))
    if summary["regions"]:
        panels.append("<div><h2>Where they are</h2>%s</div>" % _bars(summary["regions"], lambda r: r["name"], lambda r: r["count"]))
    if summary["sizes"]:
        panels.append("<div><h2>How big they are</h2>%s</div>" % _bars(summary["sizes"], lambda r: r["band"] + " people", lambda r: r["count"]))
    for i in range(0, len(panels), 2):
        out.append('<section class="two">%s</section>' % "".join(panels[i:i + 2]))

    if summary["bridges"]:
        out.append("<section><h2>Where the segments meet</h2><p>Companies that could sit in two segments: the natural second pool "
                   "when one segment runs thin.</p><table><tr><th>Between</th><th>Companies</th></tr>%s</table></section>" % "".join(
                       "<tr><td><strong>%s ↔ %s</strong></td><td>%s</td></tr>"
                       % (esc(b["between"][0]), esc(b["between"][1]), esc(", ".join(b["companies"]))) for b in summary["bridges"]))

    calls = summary["first_calls"]
    if calls:
        out.append('<section><h2>First calls</h2><ol class="calls">%s</ol></section>' % "".join(
            "<li><div><strong>%s</strong> <span class=\"mono\">%s</span><br>%s%s</div></li>"
            % (esc(c["company"]), esc(c["segment"]), esc(c["why"]),
               "<br><span class=\"mono\">Buyer: %s</span>" % esc(c["buyer"]) if c.get("buyer") and not share else "")
            for c in calls))

    for seg in summary["segments"]:
        rows = []
        for c in seg["companies"]:
            tags = "".join('<span class="tag%s">%s</span>' % (" on" if r["shown"] else "", esc(r["label"])) for r in c.get("requirements") or [])
            if c.get("status") == "tracked" and not share:
                tags += '<span class="tag on">In the pipeline</span>'
            people = ""
            if not share and c.get("people"):
                people = "<br>".join(esc(" · ".join(b for b in (p.get("name"), p.get("title")) if b)) for p in c["people"][:3])
            also = "<br><span class=\"mono\">Also fits %s</span>" % esc(c["also"]) if c.get("also") else ""
            rows.append(
                "<tr><td><strong>%s</strong><br>%s%s</td><td class=\"fit f%d\">%s</td><td>%s%s</td><td>%s%s</td><td>%s</td></tr>"
                % (esc(c["company"]), _link(c.get("domain", "")), also, c["fit"],
                   "■" * c["fit"] + "<s>%s</s>" % ("■" * (5 - c["fit"])),
                   esc(c.get("why", "")), "<br>" + tags if tags else "",
                   esc(" · ".join(b for b in (c.get("size"), c.get("location")) if b)),
                   "<br><span class=\"mono\">Uses %s</span>" % esc(c["uses"]) if c.get("uses") else "",
                   "<br>".join(x for x in (
                       esc(c.get("signal", "")),
                       _link(c["source"], "Source") if str(c.get("source", "")).startswith(("http://", "https://")) else esc(c.get("source", "")),
                       people) if x)))
        out.append("<section><h2>%s · %d</h2><table class=\"co\"><tr><th>Company</th><th>Fit</th><th>Why it fits</th><th>Size and place</th>"
                   "<th>Signal%s</th></tr>%s</table></section>"
                   % (esc(seg["segment"]), seg["count"], "" if share else " and buyers", "".join(rows)))

    out.append("<footer>Compiled with Stoa for Hermes Agent%s from public sources. Fit is a judgement against the brief above, "
               "and each row carries the evidence for it. This is a picture of a market, not a census of it; nobody on it was "
               "contacted to make it.</footer></body></html>" % (" on " + esc(date) if date else ""))
    return "".join(out)
