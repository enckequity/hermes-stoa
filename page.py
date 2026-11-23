"""A market map as one self-contained web page, drawn the way the GTM tab draws it: no outside requests, nothing it
depends on, one file to host or send.

``render(summary, terms, share)`` takes what ``Markets.summary`` computed and returns HTML. With ``share`` the page
leaves out buyers' names and what is in the pipeline, so it can be sent on. Motions and the internal notes are never on it.

The map is the dial, with what it says set out above it (where we play, how we win, what has to be true). Nothing here
is a second copy of the design: the dial's styles are read out of the page's own stylesheet (``CSS_GTM`` in
``desktop/plugin.js``), and where every mark sits is ``dialLayout`` from the same file, written into the page's script
(``pagemap.js``). The colours are Hermes's default theme, light and dark, written down here because a page opened on
its own has no Hermes around it. A map on a recruiting pipeline is a talent map, in a talent map's words.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from html import escape as esc
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PAGE = HERE / "desktop" / "plugin.js"
SCRIPT = HERE / "pagemap.js"

# What the page needs besides the GTM tab's own styles: its frame as a report (a bar to move about by, a head, sections
# with real headings, cards), and the few classes the dial borrows from the Stoa page, which is not here to supply
# them. It is written to be read by someone outside: body text at 15px in the strongest ink, grey kept for labels.
PAGE_CSS = """
* { box-sizing: border-box; } html, body { height: auto; margin: 0; }
html { background: var(--background); scroll-behavior: smooth; scroll-padding-top: 68px; }
body { font: 15px/1.6 var(--font-sans); text-rendering: optimizeLegibility; }
a { color: inherit; text-decoration: none; border-bottom: 1px solid color-mix(in srgb, var(--ui-accent) 45%, transparent); } a:hover { border-bottom-color: var(--ui-accent); color: var(--ui-accent); }
button { font: inherit; color: inherit; } [hidden] { display: none !important; }
.pg-wrap { max-width: 1240px; margin: 0 auto; padding: 0 40px; }
.pg-card { background: var(--ui-bg-elevated); border: 1px solid var(--ui-stroke-secondary); border-radius: 3px; box-shadow: 0 10px 28px -22px rgb(0 0 0 / .35); }
.stoa-eyebrow, .pg th { font: 500 11px var(--font-mono); letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12.5px; }

.pg-nav { position: sticky; top: 0; z-index: 20; border-bottom: 1px solid var(--ui-stroke-secondary); background: color-mix(in srgb, var(--background) 86%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); }
.pg-nav .pg-wrap { display: flex; align-items: center; gap: 14px; height: 50px; }
.pg-nav b { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; font-size: 13.5px; letter-spacing: -.01em; }
.pg-nav nav { display: flex; gap: 2px; flex: none; }
.pg-nav nav a { padding: 7px 11px; border: 0; border-radius: 2px; font: 500 11px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-secondary); white-space: nowrap; }
.pg-nav nav a:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }

.pg-hero { padding: 64px 0 0; }
.pg h1 { max-width: 1040px; margin: 16px 0 20px; font: 600 clamp(32px, 4.5vw, 56px)/1.04 var(--font-sans); letter-spacing: -.032em; text-wrap: balance; }
.pg-lede { max-width: 74ch; margin: 0; font-size: 18px; line-height: 1.55; color: var(--ui-text-secondary); text-wrap: pretty; }
.pg-stats { display: flex; flex-wrap: wrap; gap: 1px; margin-top: 40px; overflow: hidden; background: var(--ui-stroke-secondary); }
.pg-stat { flex: 1 1 150px; padding: 22px 24px 20px; background: var(--ui-bg-elevated); }
.pg-stat b { display: block; font: 600 36px/1 var(--font-mono); letter-spacing: -.035em; font-variant-numeric: tabular-nums; }
.pg-stat small { display: block; margin-top: 10px; font: 500 11px var(--font-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-tertiary); }

.pg-s { margin-top: 84px; }
.pg-head { margin-bottom: 24px; } .pg-head h2 { margin: 8px 0 0; font: 600 28px/1.15 var(--font-sans); letter-spacing: -.025em; }
.pg-head p { max-width: 74ch; margin: 8px 0 0; font-size: 15.5px; color: var(--ui-text-secondary); }
.pg h3 { margin: 0; font: 600 19px/1.25 var(--font-sans); letter-spacing: -.015em; }
.pg p { margin: 0 0 12px; }

/* The three answers, as three cards. */
.pg .sd-strat { gap: 16px; margin: 0; border: 0; }
.pg .sd-strat-col, .pg .sd-strat-col:first-child, .pg .sd-strat-col:last-child { padding: 24px 26px 26px; border: 1px solid var(--ui-stroke-secondary); border-radius: 3px; background: var(--ui-bg-elevated); box-shadow: 0 10px 28px -22px rgb(0 0 0 / .35); }
.pg .sd-strat-n { font-size: 11px; color: var(--ui-accent); } .pg .sd-strat h3 { margin: 6px 0 16px; font-size: 21px; }
.pg .sd-strat li { grid-template-columns: 28px minmax(0, 1fr); padding: 10px 0; font-size: 14.5px; line-height: 1.5; } .pg .sd-strat li::before { padding-top: 3px; font-size: 11px; color: var(--ui-accent); }
.pg .sd-strat-lead { display: block; overflow: visible; -webkit-line-clamp: unset; max-width: none; font-size: 14.5px; }
.pg .sd-strat-sub { margin: 22px 0 9px; font-size: 10.5px; }
.pg .sd-strat-chips button { font-size: 13px; padding: 5px 10px; } .pg .sd-strat-chips button small { font-size: 11px; }
.pg .sd-strat-row { padding: 9px 0; font-size: 14px; } .pg .sd-strat-row > span { white-space: normal; } .pg .sd-strat-rate { grid-template-columns: minmax(0, 1fr) 72px 44px; }
.pg .sd-strat-row b { font-size: 13px; } .pg .sd-strat-link { margin-top: 14px; font-size: 12px; }
.pg-segrow { display: block; padding: 11px 0; border: 0; border-top: 1px dotted var(--ui-stroke-primary); color: inherit; } .pg-segrow:first-of-type { border-top: 0; padding-top: 0; } a.pg-segrow:hover { color: inherit; } a.pg-segrow:hover b { color: var(--ui-accent); }
.pg-segrow-top { display: flex; align-items: baseline; gap: 10px; } .pg-segrow-top b { flex: 1; min-width: 0; font-weight: 600; font-size: 14.5px; }
.pg-segrow em { display: block; margin-top: 2px; font: 500 11.5px var(--font-mono); font-style: normal; color: var(--dial-good); } .pg-segrow em:empty { display: none; }
.pg-segnote { margin: 16px 0 0; padding-top: 14px; border-top: 1px solid var(--ui-stroke-secondary); font-size: 13.5px; color: var(--ui-text-secondary); }
.pg-count { font: 600 13px var(--font-mono); font-variant-numeric: tabular-nums; }
.pg-reading { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px 40px; margin-top: 40px; padding-top: 28px; border-top: 1px solid var(--ui-stroke-secondary); }
.pg-reading > div { max-width: 76ch; } .pg-reading p { font-size: 16.5px; line-height: 1.65; color: var(--ui-text-primary); text-wrap: pretty; }

/* The map. */
.pg-bar { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; margin-bottom: 10px; }
.pg-seg { display: inline-flex; border: 1px solid var(--ui-stroke-primary); border-radius: 2px; background: var(--ui-bg-elevated); }
.pg-seg button, .pg-quick button, .pg-btn { height: 30px; padding: 0 12px; border: 0; background: none; color: var(--ui-text-secondary); font: 500 11px var(--font-mono); letter-spacing: .08em; text-transform: uppercase; cursor: pointer; }
.pg-seg button + button { border-left: 1px solid var(--ui-stroke-primary); }
.pg-seg button[aria-pressed="true"] { background: var(--ui-text-primary); color: var(--background); }
.pg-quick { display: flex; flex-wrap: wrap; gap: 2px; flex: 1 1 auto; }
.pg-quick button { border: 1px solid transparent; border-radius: 2px; text-transform: none; letter-spacing: 0; font-size: 12.5px; } .pg-quick button i { font-style: normal; color: var(--ui-text-tertiary); }
.pg-quick button[aria-pressed="true"] { border-color: var(--ui-stroke-primary); background: var(--ui-bg-elevated); color: var(--ui-text-primary); }
.pg-btn { display: inline-block; margin-top: 16px; border: 1px solid var(--ui-stroke-primary); border-radius: 2px; color: var(--ui-text-primary); }
.pg-btn:hover, .pg-seg button:not([aria-pressed="true"]):hover, .pg-quick button:hover { background: var(--ui-control-hover-background); }
button:focus-visible, #sm-find:focus-visible, .stoa-mapco:focus-visible, summary:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
#sm-find { height: 30px; width: 210px; max-width: 100%; padding: 0 10px; border: 1px solid var(--ui-stroke-primary); border-radius: 2px; background: var(--ui-bg-elevated); color: var(--ui-text-primary); font: 13px var(--font-sans); }
#sm-how { max-width: none; margin: 0 0 14px; font-size: 14px; color: var(--ui-text-secondary); }
.pg-main { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; }
#sm-card { overflow: hidden; }
.pg .stoa-map { position: relative; border: 0; overflow: hidden; user-select: none; -webkit-user-select: none; }
.stoa-mapsvg { position: absolute; inset: 0; display: block; }
.stoa-mapco { cursor: pointer; outline-offset: 4px; transition: opacity .3s; }
.stoa-mappop { animation: pg-pop .5s cubic-bezier(.2,1.25,.4,1) both; } @keyframes pg-pop { from { transform: scale(0); opacity: 0; } to { transform: scale(1); opacity: 1; } }
.stoa-mapbrk { fill: none; stroke: var(--ui-text-primary); stroke-width: 1.5; opacity: 0; } .stoa-mapco[data-sel="1"] .stoa-mapbrk { opacity: 1; }
.stoa-mapover { position: absolute; inset: 0; pointer-events: none; } .stoa-mapover > * { position: absolute; }
.stoa-mapq { font: 10.5px var(--font-mono); letter-spacing: .14em; text-transform: uppercase; } .stoa-mapq strong { color: var(--ui-text-primary); font-weight: 600; }
.stoa-maphint { right: 12px; top: 10px; font: 10px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); } .stoa-map[data-narrow="1"] .stoa-maphint { display: none; }
.pg .sd-dial-lbl { font-size: 12px; } .pg .sd-dial-cap b { font-size: 11px; } .pg .sd-dial-cap > span { font-size: 11.5px; }
.stoa-maptip { position: absolute; z-index: 5; width: 240px; padding: 11px 13px; border: 1px solid var(--ui-stroke-primary); border-radius: 3px; font-size: 13px; line-height: 1.45; pointer-events: none; overflow-wrap: anywhere; }
.stoa-maptip b { display: block; font-size: 14px; }
.stoa-fit { white-space: nowrap; font: 10px var(--font-mono); letter-spacing: 1.5px; } .stoa-fit s { text-decoration: none; color: var(--ui-stroke-primary); }
.stoa-chip { display: inline; padding: 1px 6px; border: 1px solid color-mix(in oklab, var(--dial-amber) 50%, transparent); border-radius: 2px; color: var(--dial-amber); font-size: 12px; }
.pg-legend { padding: 12px 16px; border-top: 1px solid var(--ui-stroke-secondary); font: 11.5px var(--font-mono); color: var(--ui-text-secondary); }
.pg-legend .sd-dial-legend { gap: 8px 20px; } .pg-legend .sd-dial-legend span { display: inline-flex; align-items: center; gap: 7px; }
#sm-side { min-width: 0; padding: 22px 24px; font-size: 14px; line-height: 1.55; overflow: auto; overflow-wrap: anywhere; }
#sm-side h3 { margin: 6px 0 4px; font-size: 21px; } #sm-side p { margin: 12px 0; font-size: 14.5px; }
.pg-bands { margin: 12px 0 4px; } .pg-band { display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 9px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.pg-band:first-child { border-top: 0; } .pg-band i { width: 14px; height: 14px; border: 1.6px solid var(--dial-weak); border-radius: 50%; }
.pg-band[data-band="1"] i { border-color: var(--dial-good); background: var(--dial-zone); box-shadow: 0 0 0 3px color-mix(in oklab, var(--dial-good) 16%, transparent); } .pg-band[data-band="2"] i { border-color: var(--dial-okay); }
.pg-band b { display: block; font-size: 14px; font-weight: 600; } .pg-band small { display: block; font-size: 12.5px; color: var(--ui-text-secondary); } .pg-band em { font: 600 18px var(--font-mono); font-style: normal; font-variant-numeric: tabular-nums; }
.pg-near { margin-top: 18px; } .pg-row { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; gap: 8px; align-items: baseline; width: 100%; padding: 8px 0; border: 0; border-top: 1px dotted var(--ui-stroke-primary); background: none; text-align: left; cursor: pointer; }
.pg-row:hover { background: var(--ui-row-hover-background); } .pg-row i { font: 500 11px var(--font-mono); font-style: normal; color: var(--ui-text-tertiary); }
.pg-row b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; font-size: 14px; }
.pg-score { display: flex; align-items: baseline; gap: 10px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--ui-stroke-secondary); } .pg-score .stoa-eyebrow { flex: 1; } .pg-score b { font: 600 26px var(--font-mono); letter-spacing: -.02em; }
.pg-req { display: grid; grid-template-columns: 24px minmax(0, 1fr); padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); font-size: 13.5px; } .pg-req > span:first-child { font: 13px var(--font-mono); color: var(--ui-text-quaternary); }
.pg-req[data-on="1"] > span:first-child { color: var(--dial-good); } .pg-req b { font-weight: 600; } .pg-req[data-on="0"] b { font-weight: 400; color: var(--ui-text-tertiary); } .pg-req .stoa-sub { font-size: 13px; color: var(--ui-text-secondary); }
.pg-facts { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 8px 12px; margin: 16px 0 0; padding-top: 14px; border-top: 1px solid var(--ui-stroke-secondary); }
.pg-facts dt { padding-top: 3px; font: 500 10.5px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); } .pg-facts dd { margin: 0; }
.pg-tune { margin-top: 16px; } .pg-tune summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; padding: 15px 20px; cursor: pointer; list-style: none; font-weight: 600; } .pg-tune summary::-webkit-details-marker { display: none; }
.pg-tune summary::before { content: "+"; width: 14px; font: 500 15px var(--font-mono); color: var(--ui-accent); } .pg-tune[open] summary::before { content: "–"; }
.pg-tune summary small { font-weight: 400; font-size: 13.5px; color: var(--ui-text-secondary); } .pg-tune-in { padding: 0 20px 20px 48px; } .pg-tune-in > p { margin: 0 0 6px; font-size: 13.5px; color: var(--ui-text-secondary); }
#sm-weights { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 0 40px; }
.pg-weight { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 10px 0; border-top: 1px dotted var(--ui-stroke-primary); font-size: 14px; } .pg-weight > span:first-child { min-width: 0; }
.pg-weight small { display: block; font: 11.5px var(--font-mono); color: var(--ui-text-tertiary); } .pg-weight .pg-seg { flex: none; } .pg-weight .pg-seg button { height: 26px; padding: 0 8px; font-size: 10px; }

/* Who to call first. */
.pg-calls { display: flex; flex-wrap: wrap; gap: 16px; } .pg-calls > * { flex: 1 1 440px; min-width: 0; }
.pg-call { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 0 6px; padding: 24px 26px 22px; }
.pg-call-n { font: 600 26px/1 var(--font-mono); letter-spacing: -.03em; color: var(--ui-accent); } .pg-call h3 { margin: 6px 0 8px; }
.pg-call p { margin: 0; font-size: 15px; line-height: 1.6; text-wrap: pretty; } .pg-call .pg-mono { display: block; margin-top: 12px; }
.pg-open { padding: 0; border: 0; border-bottom: 1px solid transparent; background: none; font: inherit; letter-spacing: inherit; text-align: left; cursor: pointer; } .pg-open:hover { color: var(--ui-accent); border-bottom-color: var(--ui-accent); }
.pg-mono { font: 500 11px var(--font-mono); letter-spacing: .06em; color: var(--ui-text-tertiary); }

/* The segments. */
.pg-segs { display: flex; flex-wrap: wrap; gap: 16px; } .pg-segs > * { flex: 1 1 340px; min-width: 0; }
.pg-segcard { display: flex; flex-direction: column; padding: 24px 26px 20px; } .pg-segcard header { display: flex; align-items: baseline; gap: 12px; } .pg-segcard h3 { flex: 1; min-width: 0; }
.pg-segcard header b { font: 600 26px/1 var(--font-mono); letter-spacing: -.03em; } .pg-share { height: 4px; margin: 14px 0 14px; background: var(--ui-stroke-secondary); } .pg-share i { display: block; height: 100%; background: var(--ui-text-primary); }
.pg-segcard p { flex: 1; font-size: 14.5px; color: var(--ui-text-secondary); } .pg-segcard dl { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 7px 12px; margin: 6px 0 14px; padding-top: 14px; border-top: 1px dotted var(--ui-stroke-primary); font-size: 14px; }
.pg-segcard dt { padding-top: 3px; font: 500 10.5px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); } .pg-segcard dd { margin: 0; } .pg-segcard > a { align-self: flex-start; font-size: 13.5px; }
.pg-two { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 16px; margin-top: 16px; } .pg-two > * { flex: 1 1 340px; min-width: 0; } .pg-panel { padding: 22px 26px 20px; } .pg-panel h3 { margin-bottom: 12px; font-size: 16px; } .pg-panel p { font-size: 14px; color: var(--ui-text-secondary); }
.pg-barrow { display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 12px; align-items: center; padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); font-size: 14px; }
.pg-barrow b { font: 600 14px var(--font-mono); font-variant-numeric: tabular-nums; } .pg-barrow i { display: block; height: 4px; margin-top: 6px; background: var(--ui-text-primary); }
.pg-bridge { padding: 10px 0; border-top: 1px dotted var(--ui-stroke-primary); font-size: 14px; } .pg-bridge b { display: block; font-weight: 600; }

/* The companies. */
.pg-key { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 6px 40px; margin: 0 0 8px; padding: 18px 24px; list-style: none; font-size: 14px; }
.pg-key li { display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: baseline; }
.pg-num { display: inline-grid; place-items: center; width: 20px; height: 20px; border: 1.4px solid var(--ui-text-quaternary); border-radius: 50%; font: 600 10.5px var(--font-mono); color: var(--ui-text-tertiary); }
.pg-num[data-on="1"] { border-color: var(--dial-good); background: var(--dial-good); color: var(--ui-bg-elevated); } .pg-key .pg-num { border-color: var(--ui-text-secondary); color: var(--ui-text-secondary); }
.pg-seghead { display: flex; align-items: baseline; gap: 14px; margin: 48px 0 0; padding-bottom: 12px; border-bottom: 2px solid var(--ui-text-primary); } .pg-seghead h3 { font-size: 21px; } .pg-seghead span { font: 500 12px var(--font-mono); color: var(--ui-text-tertiary); }
.pg-co { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 8px 44px; padding: 24px 0; border-bottom: 1px solid var(--ui-stroke-secondary); break-inside: avoid; }
.pg-co h4 { margin: 0 0 8px; font: 600 17px/1.3 var(--font-sans); letter-spacing: -.012em; } .pg-co-fit { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin-bottom: 10px; }
.pg-bandtag { font: 600 10.5px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); } .pg-bandtag[data-band="1"] { color: var(--dial-good); } .pg-bandtag:empty { display: none; }
.pg-co-meta { font-size: 13.5px; line-height: 1.65; color: var(--ui-text-secondary); } .pg-co-meta b { font-weight: 500; color: var(--ui-text-primary); }
.pg-co-b > p { max-width: 78ch; font-size: 15px; line-height: 1.62; text-wrap: pretty; }
.pg-now { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; margin: 14px 0 0; padding: 12px 14px; border-left: 3px solid var(--dial-amber); background: color-mix(in oklab, var(--dial-amber) 7%, transparent); font-size: 14.5px; line-height: 1.55; }
.pg-now > span:first-child, .pg-who > span:first-child { padding-top: 3px; font: 600 10.5px var(--font-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--dial-amber); }
.pg-now small { display: block; margin-top: 3px; font-size: 13px; color: var(--ui-text-secondary); }
.pg-who { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; margin: 12px 0 0; padding: 0 14px; font-size: 14.5px; } .pg-who > span:first-child { color: var(--ui-text-tertiary); } .pg-who i { font-style: normal; color: var(--ui-text-secondary); }
.pg .pg-src { margin: 10px 0 0; font-size: 13px; color: var(--ui-text-secondary); }
.pg-reqs { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-top: 12px; } .pg-reqs small { margin-right: 6px; font: 500 11px var(--font-mono); color: var(--ui-text-tertiary); }
.pg-foot { margin: 84px 0 0; padding: 22px 0 70px; border-top: 1px solid var(--ui-stroke-secondary); color: var(--ui-text-tertiary); font-size: 13px; } .pg-foot p { max-width: 86ch; margin: 0; }

/* -- Stoa's own look ------------------------------------------------------------------------------------------
   What makes the desk look like itself, and so this page: the stoa drawn in dots, figures set in dots the way a sign
   board spells them, the one ramp of colour as a dotted rule, flat squared boxes, and mono for anything counted. */
.pg { --stoa-h1: #2150cc; --stoa-h2: #2c86d9; --stoa-h3: #30b4c4; --stoa-h4: #48c78c; --stoa-h5: #f2b24c; --stoa-h6: #f4967e; --stoa-h7: #f3a9c0; --stoa-page: var(--background);
  --stoa-bar: linear-gradient(90deg, var(--stoa-h1), var(--stoa-h2) 46%, var(--ui-text-primary)); --stoa-dots: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); }
.pg-card { border-radius: 2px; background: none; box-shadow: none; }
.stoa-rule, .pg-s::before { content: ""; display: block; height: 2px; background: var(--stoa-bar); -webkit-mask-image: var(--stoa-dots); mask-image: var(--stoa-dots); }
.pg-s::before { margin-bottom: 30px; } .pg-s { margin-top: 72px; } .pg-hero + .pg-s { margin-top: 44px; } .pg-hero + .pg-s::before { display: none; }
.pg-head .stoa-eyebrow::before, .pg-hero > .stoa-eyebrow::before { content: "░▒▓"; margin-right: 9px; letter-spacing: 0; font-weight: 400; background: var(--stoa-bar); -webkit-background-clip: text; background-clip: text; color: transparent; }
.stoa-dotfig { display: block; width: auto; overflow: visible; } .stoa-dotfig rect { fill: currentColor; }
.pg-nav .pg-mark { flex: none; height: 11px; color: var(--ui-text-primary); } .pg-nav .stoa-eyebrow { flex: none; }
.pg-nav nav { gap: 0; border: 1px solid var(--ui-stroke-secondary); border-radius: 2px; background: var(--background); }
.pg-nav nav a { display: inline-flex; align-items: center; height: 30px; padding: 0 14px; border-radius: 0; font-size: 12px; letter-spacing: 0; text-transform: none; }
.pg-nav nav a + a { border-left: 1px solid var(--ui-stroke-tertiary); } .pg-nav nav a:hover { background: var(--ui-text-primary); color: var(--background); }
.pg-hero { padding-top: 56px; } .pg-hero .stoa-rule { margin-top: 18px; }
.pg-headband { position: relative; display: grid; grid-template-columns: minmax(0, max-content) minmax(0, 1fr); gap: 0 48px; align-items: end; min-height: 150px; margin-top: 34px; }
.pg-fig { display: flex; align-items: flex-end; gap: 14px; } .pg-fig .stoa-dotfig { height: 50px; } .pg-fig span { padding-bottom: 1px; font: 13px var(--font-mono); color: var(--ui-text-secondary); }
.pg-headstats { display: flex; flex-wrap: wrap; gap: 12px 38px; margin-top: 22px; } .pg-headstats small { display: block; font: 500 10.5px var(--font-mono); letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.pg-headstats b { display: block; margin-top: 4px; font: 600 21px/1.1 var(--font-mono); font-variant-numeric: tabular-nums; }
.pg-headart { position: relative; align-self: stretch; min-width: 0; pointer-events: none; -webkit-mask-image: linear-gradient(90deg, transparent, #000 90px); mask-image: linear-gradient(90deg, transparent, #000 90px); }
.pg-headart canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
.pg .sd-strat { gap: 0; border-top: 1px solid var(--ui-stroke-secondary); border-bottom: 1px solid var(--ui-stroke-secondary); }
.pg .sd-strat-col, .pg .sd-strat-col:first-child, .pg .sd-strat-col:last-child { padding: 26px 30px 28px; border: 0; border-left: 1px dotted var(--ui-stroke-primary); border-radius: 0; background: none; box-shadow: none; }
.pg .sd-strat-col:first-child { padding-left: 0; border-left: 0; } .pg .sd-strat-col:last-child { padding-right: 0; }
.pg .sd-strat-n, .pg .sd-strat li::before { color: var(--ui-text-tertiary); }
.pg-reading { margin-top: 0; padding-top: 30px; border-top: 0; }
.pg-calls { gap: 0 48px; } .pg-call { padding: 24px 0 22px; border-top: 1px dotted var(--ui-stroke-primary); } .pg-calls > .pg-call:first-child, .pg-calls > .pg-call:nth-child(2) { border-top-style: solid; border-top-color: var(--ui-stroke-secondary); }
.pg-call { grid-template-columns: 58px minmax(0, 1fr); } .pg-call-n { padding-top: 3px; color: var(--ui-text-primary); } .pg-call-n .stoa-dotfig { height: 22px; }
.pg-segcard header b { font: inherit; color: var(--ui-text-primary); } .pg-segcard header b .stoa-dotfig { height: 22px; }
.pg-num, .pg-key .pg-num { border-radius: 0; width: 19px; height: 19px; border-width: 1px; } .pg-num[data-on="1"] { background: var(--ui-text-primary); border-color: var(--ui-text-primary); color: var(--background); }
.pg-seghead { border-bottom: 0; padding-bottom: 14px; } .pg-seghead + .pg-co { border-top: 2px solid var(--ui-text-primary); }
.pg-now { border-left-width: 2px; } .pg-co { border-bottom-style: dotted; border-bottom-color: var(--ui-stroke-primary); }
.pg-foot { display: flex; flex-wrap: wrap; gap: 14px 28px; align-items: flex-start; } .pg-foot .pg-mark { flex: none; height: 13px; margin-top: 4px; color: var(--ui-text-secondary); } .pg-foot p { flex: 1 1 420px; }
@media (max-width: 860px) { .pg-headband { grid-template-columns: minmax(0, 1fr); min-height: 0; } .pg-headart { display: none; } .pg-fig .stoa-dotfig { height: 40px; }
  .pg .sd-strat-col, .pg .sd-strat-col:first-child, .pg .sd-strat-col:last-child { padding: 22px 0; border-left: 0; border-top: 1px dotted var(--ui-stroke-primary); } .pg .sd-strat-col:first-child { border-top: 0; }
  .pg-calls > .pg-call:nth-child(2) { border-top-style: dotted; border-top-color: var(--ui-stroke-primary); } .pg-call { grid-template-columns: 46px minmax(0, 1fr); padding: 20px 0; } }

@media (min-width: 980px) { .pg-main { grid-template-columns: minmax(0, 1fr) 340px; } }
@media (max-width: 860px) { .pg-wrap { padding: 0 18px; } .pg-nav nav { display: none; } .pg-hero { padding-top: 40px; } .pg-lede { font-size: 16.5px; } .pg-s { margin-top: 60px; } .pg-head h2 { font-size: 24px; }
  .pg .sd-strat { grid-template-columns: minmax(0, 1fr); } .pg-reading { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .pg-key, #sm-weights { grid-template-columns: minmax(0, 1fr); } .pg-calls > *, .pg-segs > *, .pg-two > * { flex-basis: 100%; } .pg-co { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .pg-call { grid-template-columns: 40px minmax(0, 1fr); padding: 20px 18px; } .pg-tune-in { padding-left: 20px; } .pg-weight { flex-wrap: wrap; } .pg-now, .pg-who { grid-template-columns: minmax(0, 1fr); gap: 2px; } }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } .stoa-mapco, .stoa-mappop { transition: none; animation: none; } }
@media print { @page { margin: 14mm; } html, body { background: #fff; } .pg-nav, .pg-bar, .pg-tune, .stoa-maphint, .pg-btn, #sm-how { display: none !important; } .pg-wrap { max-width: none; padding: 0; }
  .pg-card, .pg .sd-strat-col { box-shadow: none; } .pg-s { margin-top: 36px; } .pg-hero { padding-top: 0; } #sm-side { height: auto !important; overflow: visible; }
  #sm-card, #sm-side, .pg-call, .pg-segcard, .pg-co, .pg-panel, .pg-key, .pg-stats { break-inside: avoid; } .pg-head, .pg-seghead, .pg-reading > span { break-after: avoid; } .pg-head { break-inside: avoid; }
  .pg-open { border: 0; } #sm-side .stoa-sub:last-child { display: none; } .pg-main { gap: 12px; } }
"""


class Changed(Exception):
    """The page script no longer has a part this page is made from."""


def _between(text: str, start: str, end: str, what: str) -> str:
    a = text.find(start)
    b = text.find(end, a + len(start)) if a >= 0 else -1
    if a < 0 or b < 0:
        raise Changed("the %s could not be found in desktop/plugin.js" % what)
    return text[a:b]


# Hermes's default theme, light and dark, in the names the Stoa page reads (Hermes's own mixes, apps/desktop/src/styles.css).
COLOURS = """
:root { color-scheme: light; --ui-base: #1b1c20; --ui-accent: #0053fd; --background: #ffffff; --ui-bg-elevated: #ffffff;
  --font-sans: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --ui-text-primary: color-mix(in srgb, var(--ui-base) 94%, transparent); --ui-text-secondary: color-mix(in srgb, var(--ui-base) 74%, transparent);
  --ui-text-tertiary: color-mix(in srgb, var(--ui-base) 58%, transparent); --ui-text-quaternary: color-mix(in srgb, var(--ui-base) 40%, transparent);
  --ui-stroke-primary: color-mix(in srgb, var(--ui-base) 14%, transparent); --ui-stroke-secondary: color-mix(in srgb, var(--ui-base) 9%, transparent);
  --ui-stroke-tertiary: color-mix(in srgb, var(--ui-base) 6%, transparent); --ui-row-hover-background: color-mix(in srgb, var(--ui-base) 4%, transparent);
  --ui-control-hover-background: color-mix(in srgb, var(--ui-base) 6%, transparent); }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --ui-base: #e8e6de; --ui-accent: #4a84fe; --background: #0a0b0d; --ui-bg-elevated: #121317;
  --ui-text-tertiary: color-mix(in srgb, var(--ui-base) 54%, transparent); --ui-text-quaternary: color-mix(in srgb, var(--ui-base) 36%, transparent);
  --ui-stroke-primary: color-mix(in srgb, var(--ui-base) 16%, transparent); --ui-stroke-secondary: color-mix(in srgb, var(--ui-base) 10%, transparent);
  --ui-stroke-tertiary: color-mix(in srgb, var(--ui-base) 7%, transparent); } }
"""


def _page_source() -> str:
    return PAGE.read_text(encoding="utf-8")


def page_css() -> str:
    """The page's look: Hermes's colours, and the GTM tab's own styles for the dial and what is said above it."""
    source = _page_source()
    gtm = _between(source, "const CSS_GTM = `", "`", "GTM tab's styles")
    dial_colours = _between(gtm, ".stoa {\n  --dial-ink", ".sd-gtm h2", "dial's colours").replace(".stoa {", ".pg {", 1)
    above = _between(gtm, "/* -- above the map", "/* -- the map as a dial", "styles for what is said above a map")
    dial = _between(gtm, "/* -- the map as a dial", "/* -- motions", "dial's styles")
    if "\\" in above + dial or "${" in above + dial:
        raise Changed("the GTM tab's styles are no longer plain CSS")
    css = "\n".join((COLOURS, "body { background: var(--background); color: var(--ui-text-primary); -webkit-font-smoothing: antialiased; }", dial_colours, above, dial, PAGE_CSS))
    return re.sub(r"/\*.*?\*/\s*", "", css, flags=re.S)       # the notes in the stylesheet are for whoever works on the plugin, not for a client


def _bare(code: str) -> str:
    """Script without its notes: a line that is only a `//` note, and a `/** ... */` block. They are for whoever works
    on the plugin, and say things in its own words that a client's page has no business carrying."""
    code = re.sub(r"^[ \t]*/\*\*.*?\*/[ \t]*\n", "", code, flags=re.S | re.M)
    return "\n".join(line for line in code.split("\n") if not line.lstrip().startswith("//"))


def script() -> str:
    """The page's script, with the GTM tab's own layout of the dial written in."""
    layout = _between(_page_source(), "const CALL = 75", "function Squares(", "dial's layout")
    own = SCRIPT.read_text(encoding="utf-8")
    if own.count("/*DIAL-LAYOUT*/") != 1 or "function dialLayout(" not in layout:
        raise Changed("the dial's layout could not be written into the page")
    return _bare(own.replace("/*DIAL-LAYOUT*/", layout))


def _squares(fit) -> str:
    n = max(1, min(5, int(round(float(fit or 0)))))
    return '<span class="stoa-fit" data-fit="%d" title="Fit %d of 5">%s<s>%s</s></span>' % (n, n, "■" * n, "■" * (5 - n))


def _link(url: str, text: str = "") -> str:
    if not url:
        return ""
    href = url if url.startswith(("http://", "https://")) else "https://" + url
    return '<a href="%s" rel="noreferrer noopener">%s</a>' % (esc(href, quote=True), esc(text or url))


def _day(stamp: Any, short: bool = False) -> str:
    """A date as a person writes it: 'November 18, 2026', or 'Feb 2026' when only the month is known."""
    text = str(stamp or "")[:10]
    try:
        if len(text) == 7:
            return dt.date.fromisoformat(text + "-01").strftime("%b %Y")
        when = dt.date.fromisoformat(text)
        return "%s %d, %d" % (when.strftime("%b" if short else "%B"), when.day, when.year)
    except ValueError:
        return ""


GONE = re.compile(r"\b(former|formerly|previous(?:ly)?|past|retired|departed|outgoing|no longer|vacant|left (?:the|in|as)|until \d{4}|ex)\b[- ]?", re.I)


def _people(people: Any) -> list:
    """The people named at a company, those still there first. The plugin calls the first one saved "the buyer"; research
    often saves whoever held a job before as well ("Former Director of Events"), and a page sent to a client should not
    put that person forward."""
    rows = [p for p in (people or []) if isinstance(p, dict) and p.get("name")]
    here = [p for p in rows if not GONE.search(str(p.get("title") or ""))]
    return here + [p for p in rows if p not in here]


def _lead(people: Any) -> str:
    """Who to ask for: the first person who is still there, with what they do. Nobody, if everyone named has gone."""
    for p in _people(people):
        if not GONE.search(str(p.get("title") or "")):
            return ", ".join(part for part in (str(p.get("name") or ""), str(p.get("title") or "")) if part)
    return ""


# A figure set in dots, five across and seven down, the way the desk sets the one at its head (the plugin's DOTS).
# Each row is its five dots read as a number, 16 the left-hand one. The letters are here for the name alone.
DOTS = {"0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14), "2": (14, 17, 1, 2, 4, 8, 31), "3": (31, 2, 4, 2, 1, 17, 14), "4": (2, 6, 10, 18, 31, 2, 2),
        "5": (31, 16, 30, 1, 1, 17, 14), "6": (6, 8, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8), "8": (14, 17, 17, 14, 17, 17, 14), "9": (14, 17, 17, 15, 1, 2, 12),
        ".": (0, 0, 0, 0, 0, 3, 3), "S": (14, 17, 16, 14, 1, 17, 14), "T": (31, 4, 4, 4, 4, 4, 4), "O": (14, 17, 17, 17, 17, 17, 14), "A": (14, 17, 17, 31, 17, 17, 17)}


def _dots(text: Any, cls: str = "") -> str:
    """`text` as a picture in dots; in type if it has a character the dots cannot spell."""
    chars = list(str(text))
    if not chars or any(ch not in DOTS for ch in chars):
        return esc(str(text))
    marks, x = [], 0
    for ch in chars:
        wide = 2 if all(row < 4 for row in DOTS[ch]) else 5
        for y, row in enumerate(DOTS[ch]):
            marks += ['<rect x="%.1f" y="%.1f" width=".8" height=".8"/>' % (x + col + 0.1, y + 0.1) for col in range(wide) if (row >> (wide - 1 - col)) & 1]
        x += wide + 1
    return '<svg class="stoa-dotfig%s" viewBox="0 0 %d 7" style="aspect-ratio:%d / 7" role="img" aria-label="%s">%s</svg>' % (
        " " + cls if cls else "", x - 1, x - 1, esc(str(text), quote=True), "".join(marks))


def _art() -> tuple:
    """The stoa itself, as the desk draws it at its head: ``(the picture as cells, the code that paints it)``, both
    taken from the plugin (its `art.json`, and the painting in its page), so it is the same building, dot for dot.
    ``("", "")`` where either has gone: the page is then without it and no worse."""
    try:
        piece = json.loads((HERE / "desktop" / "art.json").read_text(encoding="utf-8"))
        head = piece.get("head") or {}
        if piece.get("v") != 3 or not head.get("cells") or len(head["cells"]) != head["w"] * head["h"]:
            return "", ""
        painting = _between(_page_source(), "const HEAT_AT = [", "/** Keep a canvas drawn", "painting of the stoa")
        if "function paint(el, piece, cells, ink)" not in painting or "function unpack(piece)" not in painting:
            return "", ""
        return json.dumps({"w": head["w"], "h": head["h"], "cells": head["cells"]}), _bare(painting)
    except (OSError, ValueError, KeyError, Changed):
        return "", ""


ART_DRAWN = """
const el = document.getElementById('sm-artc'), piece = JSON.parse(document.getElementById('sm-art').textContent), cells = unpack(piece);
const draw = () => { try { paint(el, piece, cells, 'auto'); } catch (error) { el.hidden = true; } };
draw(); new ResizeObserver(draw).observe(el);
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => setTimeout(draw, 50));
"""


# The words a map is written in, by the kind of pipeline it is kept with (the same split as the GTM tab's words, for the
# page that is downloaded). A map on a recruiting pipeline is a talent map for a role.
SAY = {
    "": {"play": "Where we play", "win": "How we win", "needs": "What has to be true",
         "needs_lead": "For a company to be worth the call. Each is checked against the map, with the evidence.",
         "no_win": "Nothing is written yet about how to win this market.", "uses": "Who you would be replacing",
         "uses_title": "%d of %d use it today. Press to light them up on the map.", "now": "to call now",
         "bands": ["Call now", "Line up", "Watch"],
         "band_why": ["Fits, and something has just happened there", "Fits; no fresh reason to call yet", "A weaker fit, or nothing to go on yet"],
         "first": "Who to call first", "first_nav": "First calls", "first_saved": "The companies to open with, and the reason for each.",
         "first_ranked": "Ranked by how well each company fits and how fresh its reason to call is.",
         "reason": "Reason to call", "reason_stat": "With a reason to call", "has_reason": "has a reason to call",
         "named": "Buyers named", "in_pipe": "In the pipeline", "in_pipe_one": "In the pipeline", "not_in": "Not in the pipeline",
         "solid": "Solid is in the pipeline", "ring": "A ring is a reason to call", "ask_for": "Ask for",
         "uses_line": "Uses <b>%s</b> today", "uses_fact": "Uses today", "use_today": "Use today", "use_facet": "companies that use ",
         "nobody": "Nobody to call yet", "nobody_why": "A company moves in here when it fits and something has happened there: funding, a hire, a launch.",
         "map_sub": "One round face. Each slice is a group of companies, and the nearer a company sits to the centre, the sooner it is worth a call. Press any company for the evidence.",
         "how_seg": "Each slice is a segment that would get a different pitch. Nearer the centre is who to call first.",
         "how_held": "Sliced by what each company uses today: who you would be replacing. Nearer the centre is who to call first.",
         "all_sub": "Why each one fits, what makes now a good time, and which requirements it shows.", "all_sub_bare": "Why each one fits, and what makes now a good time.",
         "eyebrow": "Market map"},
    "roles": {"play": "Where to look", "win": "How to pitch the role", "needs": "What the role needs",
              "needs_lead": "What makes someone a fit. Each is checked against the companies on the map, with the evidence.",
              "no_win": "Nothing is written yet about how to pitch this role.", "uses": "What they work with",
              "uses_title": "%d of %d work with it today. Press to light them up on the map.", "now": "to approach now",
              "bands": ["Approach now", "Line up", "Watch"],
              "band_why": ["Its people fit the role, and something has just happened there", "Its people fit; no fresh reason to move yet", "A weaker fit, or nothing to go on yet"],
              "first": "Who to approach first", "first_nav": "First approaches", "first_saved": "Where to start, and the reason for each.",
              "first_ranked": "Ranked by how well each company's people fit the role and how fresh their reason to move is.",
              "reason": "Reason to move", "reason_stat": "With a reason to move", "has_reason": "has a reason to move",
              "named": "People named", "in_pipe": "Already candidates", "in_pipe_one": "Already a candidate", "not_in": "No candidate yet",
              "solid": "Solid already has a candidate", "ring": "A ring is a reason to move", "ask_for": "Approach",
              "uses_line": "Works with <b>%s</b>", "uses_fact": "Works with", "use_today": "Work with", "use_facet": "companies that work with ",
              "nobody": "Nobody to approach yet",
              "nobody_why": "A company moves in here when its people fit the role and something has happened there: a layoff, a reorganisation, a leader leaving.",
              "map_sub": "One round face. Each slice is a pool of talent, and the nearer a company sits to the centre, the sooner its people are worth approaching. Press any company for the evidence.",
              "how_seg": "Each slice is a pool of talent that would get a different pitch. Nearer the centre is who to approach first.",
              "how_held": "Sliced by what each company's people work with today. Nearer the centre is who to approach first.",
              "all_sub": "Why its people fit the role, what would make them open to a move now, and which of the role's needs they show.",
              "all_sub_bare": "Why its people fit the role, and what would make them open to a move now.",
              "eyebrow": "Talent map"},
}


# A candidate map: each row is a person. What differs from a recruiting map of companies.
SAY["people"] = dict(SAY["roles"], **{
    "needs_lead": "What makes someone a fit. Each candidate is checked against it, with the evidence.",
    "uses": "Where they work now", "uses_title": "%d of %d work there today. Press to light them up on the map.",
    "band_why": ["Fits the role, and something makes now a good time", "Fits the role; no fresh reason to move yet", "A weaker fit, or nothing to go on yet"],
    "first_saved": "Who to start with, and the reason for each.", "first_ranked": "Ranked by how well each candidate fits the role and how fresh their reason to move is.",
    "named": "Employers", "in_pipe": "In your pipeline", "in_pipe_one": "In your pipeline", "not_in": "Not in your pipeline yet",
    "solid": "Solid is already in your pipeline", "uses_line": "At <b>%s</b>", "uses_fact": "Works at", "use_today": "Work at", "use_facet": "candidates who work at ",
    "nobody_why": "A candidate moves in here when they fit the role and something makes now a good time: a layoff where they work, a leader leaving, a long stretch in one seat.",
    "map_sub": "One round face. Each dot is a candidate and each slice a pool, and the nearer a candidate sits to the centre, the sooner they are worth approaching. Press anyone for the evidence.",
    "how_seg": "Each dot is a candidate, and each slice a pool that would get a different pitch. Nearer the centre is who to approach first.",
    "how_held": "Each dot is a candidate, sliced by where they work now. Nearer the centre is who to approach first.",
    "all_sub": "Why each one fits the role, what would make them open to a move now, and which of the role's needs they show.",
    "all_sub_bare": "Why each one fits the role, and what would make them open to a move now.",
    "eyebrow": "Candidate map"})
THINGS = {"": ("company", "companies", "Companies", "Every company on the map"), "roles": ("company", "companies", "Companies", "Every company on the map"),
          "people": ("candidate", "candidates", "Candidates", "Every candidate on the map")}


def kind_of(summary: dict) -> str:
    """What the map's rows are: '' companies to sell to, 'roles' companies where candidates work, 'people' candidates.
    A map on a recruiting pipeline says which it is (``rows`` in its summary); a map on any other has no such word."""
    rows = summary.get("rows") or ""
    return "" if not rows else "people" if rows == "people" else "roles"


def _say(kind: str) -> dict:
    return dict(SAY[kind if kind in SAY else ""], kind=kind, things=THINGS[kind if kind in THINGS else ""])


def _head(eyebrow: str, title: str, sub: str = "") -> str:
    return '<div class="pg-head"><span class="stoa-eyebrow">%s</span><h2>%s</h2>%s</div>' % (esc(eyebrow), esc(title), "<p>%s</p>" % esc(sub) if sub else "")


def _bars(rows: list, label, value) -> str:
    top = max([value(r) for r in rows] + [1])
    return "".join('<div class="pg-barrow"><b>%s</b><span>%s<i style="width:%d%%"></i></span></div>'
                   % (esc(str(value(r))), esc(label(r)), max(2, round(100 * value(r) / top))) for r in rows)


def _data(summary: dict, share: bool, say: dict) -> str:
    """The companies the dial draws, as data in the page. The copy to send on names no buyer and says nothing of the pipeline."""
    segments = []
    for seg in summary["segments"]:
        rows = []
        for c in seg["companies"]:
            row = {k: c[k] for k in ("id", "company", "domain", "fit", "size", "location", "why", "signal", "signal_date", "source", "uses", "also",
                                     "headcount", "requirements") if c.get(k) not in (None, "", [])}
            if not share:
                if c.get("people"):
                    row["people"] = [{"name": p.get("name", ""), "title": p.get("title", ""), "gone": bool(GONE.search(str(p.get("title") or "")))} for p in c["people"][:4]]
                if c.get("status") == "tracked":
                    row["status"] = "tracked"
            rows.append(row)
        segments.append({"name": seg["segment"], "companies": rows})
    data = {"name": summary["market"]["name"], "today": dt.date.today().isoformat(), "requirements": summary["market"]["requirements"], "segments": segments,
            "say": {key: say[key] for key in ("bands", "band_why", "now", "has_reason", "in_pipe_one", "ask_for", "uses_fact", "use_facet", "nobody", "nobody_why", "how_seg", "how_held", "kind", "things")
                    if not (share and key in ("in_pipe_one", "ask_for"))}}        # the copy to send on has no word for what it leaves out
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")        # nothing in a company's words can end the block early


def _strategy(summary: dict, say: dict) -> str:
    """Where we play, how we win, and what has to be true: the three answers the GTM tab sets out above a map, as three cards."""
    market = summary["market"]
    segments = "".join(
        '<a class="pg-segrow" href="#seg-%d"><div class="pg-segrow-top"><b>%s</b><span class="pg-count" title="%d %s">%d</span>%s</div><em data-now="%s"></em></a>'
        % (i, esc(s["segment"]), s["count"], "company" if s["count"] == 1 else "companies", s["count"], _squares(s.get("avg_fit")), esc(s["segment"], quote=True))
        for i, s in enumerate(s for s in summary["segments"] if s.get("count")))
    points = [str(x) for x in (market.get("implications") or [])][:4]
    reading = next((par for par in str(market.get("reading") or "").split("\n") if par.strip()), "")
    win = ("<ol>%s</ol>" % "".join("<li>%s</li>" % esc(x) for x in points) if points
           else '<p class="sd-strat-lead">%s</p>' % esc(reading) if reading
           else '<p class="sd-strat-lead" data-empty="1">%s</p>' % esc(say["no_win"]))
    # Who holds the market today sits with where we play: it is part of the lie of the land, and it keeps the three cards level.
    holds = summary.get("holds") or []
    if say["kind"] == "people":             # worth a line only where two or more candidates share an employer
        holds = [h for h in holds if h.get("count", len(h.get("companies") or [])) > 1]
    if holds:
        segments += '<div class="sd-strat-sub">%s</div><div class="sd-strat-chips">%s</div>' % (esc(say["uses"]), "".join(
            '<button type="button" data-facet="uses" data-value="%s" aria-pressed="false" title="%s">%s<small>%d%%</small></button>'
            % (esc(h["name"], quote=True), esc(say["uses_title"] % (h.get("count", len(h.get("companies") or [])), summary.get("holds_of") or 0), quote=True), esc(h["name"]), h["share"])
            for h in holds[:6]))
    rates = summary.get("requirements") or []
    if rates:
        true = '<p class="sd-strat-lead">%s</p>' % esc(say["needs_lead"]) + "".join(
            '<button type="button" class="sd-strat-row sd-strat-rate" data-facet="req" data-value="%s" aria-pressed="false" title="%s">'
            '<span>%s</span><i class="sd-strat-meter"><i style="width:%d%%"></i></i><b>%s</b></button>'
            % (esc(r["label"], quote=True), esc(("%d of %d companies show it. " % (r["shown"], r["of"]) if r["of"] else "Not checked yet. ") + "Press to light them up on the map.", quote=True),
               esc(r["label"]), max(2, r["rate"] if r["of"] else 0), "%d%%" % r["rate"] if r["of"] else "—") for r in rates)
        true += '<button type="button" class="sd-strat-link" data-act="tune">Change how much each one counts  ↓</button>'
    else:
        true = '<p class="sd-strat-lead" data-empty="1">This map has no requirements yet.</p>'
    return ('<div class="sd-strat">'
            '<div class="sd-strat-col"><div class="sd-strat-n">01</div><h3>%s</h3>%s</div>'
            '<div class="sd-strat-col"><div class="sd-strat-n">02</div><h3>%s</h3>%s</div>'
            '<div class="sd-strat-col"><div class="sd-strat-n">03</div><h3>%s</h3>%s</div></div>') % (esc(say["play"]), segments, esc(say["win"]), win, esc(say["needs"]), true)


def _dial(summary: dict, share: bool, say: dict) -> str:
    """The dial's frame: the script fills it. A browser that runs no scripts sees the rest of the page, which says it all in words."""
    quick = [("all", "All"), ("best", "Best fit"), ("reason", say["reason"])] + ([] if share else [("open", say["not_in"])])
    key = lambda attrs, words: '<span><i class="sd-dial-key" %s></i>%s</span>' % (attrs, words)      # noqa: E731
    legend = "".join([key('data-tone="good"', "Good fit"), key('data-tone="okay"', "Okay"), key('data-tone="weak"', "Weak"),
                      "" if share else key('data-kind="in"', say["solid"]), key('data-kind="sig"', say["ring"])])
    weights = ('<details class="pg-tune pg-card" id="sm-what"><summary>Adjust what matters<small>Change how much each requirement counts, and the map re-scores itself.</small></summary>'
               '<div class="pg-tune-in"><p id="sm-weighnote"></p><div id="sm-weights"></div></div></details>' if summary["market"]["requirements"] else "")
    return "".join([
        '<div id="sm-live" hidden><div class="pg-bar"><span class="pg-seg" role="group" aria-label="How the dial is sliced">',
        '<button data-lens="seg">%s</button><button data-lens="held">%s</button></span>' % (("Pools", "Employer") if say["kind"] == "people" else ("Pools", "Works with") if say["kind"] == "roles" else ("Segments", "Held by")),
        '<span class="pg-quick">%s</span>' % "".join('<button data-quick="%s">%s <i></i></button>' % item for item in quick),
        '<input id="sm-find" type="search" placeholder="Find a %s" aria-label="Find a %s" autocomplete="off"></div>' % (say["things"][0], say["things"][0]),
        '<p id="sm-how"></p><div class="pg-main"><div id="sm-card" class="pg-card">',
        '<div id="sm-map" class="stoa-map sd-dial"><svg id="sm-svg" class="stoa-mapsvg" role="group" aria-label="Market map: %s"></svg>' % esc(summary["market"]["name"], quote=True),
        '<div id="sm-over" class="stoa-mapover"></div><div id="sm-tip" class="stoa-maptip" hidden></div></div>',
        '<div class="pg-legend"><div class="sd-dial-legend">%s</div></div></div>' % legend,
        '<aside id="sm-side" class="pg-card" aria-live="polite"></aside></div>%s</div>' % weights,
        '<noscript><p class="stoa-sub">The map is drawn by a script, which this browser is not running. Everything on it is in the lists below.</p></noscript>'])


def _calls(summary: dict, share: bool, say: dict) -> str:
    """Who to call first, as cards. What was written down for the map leads; a map with nothing written is ranked by the script."""
    calls = summary.get("first_calls") or []
    people = {c.get("id"): c.get("people") for seg in summary["segments"] for c in seg["companies"]}
    if not calls:
        return '<div class="pg-calls" id="sm-calls" data-auto="1"></div>'
    return '<div class="pg-calls" id="sm-calls">%s</div>' % "".join(
        '<article class="pg-call"><span class="pg-call-n">%s</span><div><div class="stoa-eyebrow">%s</div>'
        '<h3><button type="button" class="pg-open" data-act="open" data-id="%s" title="Show it on the map">%s</button></h3><p>%s</p>%s</div></article>'
        % (_dots("%02d" % (i + 1)), esc(c.get("segment", "")), esc(str(c.get("id", "")), quote=True), esc(c["company"]), esc(c.get("why", "")),
           '<span class="pg-mono">%s · %s</span>' % (esc(say["ask_for"]), esc(_lead(people.get(c.get("id"))))) if not share and _lead(people.get(c.get("id"))) else "") for i, c in enumerate(calls))


def _company(c: dict, order: dict, share: bool, say: dict) -> str:
    reqs = c.get("requirements") or []
    shown = len([r for r in reqs if r.get("shown")])
    marks = ""
    if reqs:
        marks = '<div class="pg-reqs" aria-label="%d of %d requirements shown"><small>%d of %d</small>%s</div>' % (shown, len(reqs), shown, len(reqs), "".join(
            '<span class="pg-num" data-on="%d" title="%s">%d</span>'
            % (1 if r.get("shown") else 0, esc(r["label"] + (": " + str(r.get("evidence")) if r.get("shown") and r.get("evidence") else "" if r.get("shown") else ": no evidence found"), quote=True),
               order.get(r["label"], 0)) for r in reqs if order.get(r["label"])))
    meta = [_link(c.get("domain", ""))] if c.get("domain") else []
    if say["kind"] == "people":                 # a person: their title, then where they are
        meta = ["<b>%s</b>" % esc(c["size"])] if c.get("size") else []
        place = c.get("location") or ""
    else:
        place = " · ".join(b for b in (c.get("size"), c.get("location")) if b)
    if place:
        meta.append(esc(place))
    if c.get("uses"):
        meta.append(say["uses_line"] % esc(c["uses"]))
    if c.get("also"):
        meta.append("Also fits %s" % esc(c["also"]))
    if c.get("status") == "tracked" and not share:
        meta.append("<b>%s</b>" % esc(say["in_pipe_one"]))
    now = ""
    if c.get("signal"):
        source = c.get("source", "")
        cite = _link(source, "Source") if str(source).startswith(("http://", "https://")) else esc(source)
        under = " · ".join(x for x in (_day(c.get("signal_date"), short=True), cite) if x)
        now = '<div class="pg-now"><span>Why now</span><div>%s%s</div></div>' % (esc(c["signal"]), "<small>%s</small>" % under if under else "")
    elif str(c.get("source", "")).startswith(("http://", "https://")):
        now = '<p class="pg-src">%s</p>' % _link(c["source"], "Source")
    who = ""
    if not share and c.get("people"):
        who = '<div class="pg-who"><span>People</span><div>%s</div></div>' % "<br>".join(
            "%s%s" % (esc(p.get("name", "")), " <i>· %s</i>" % esc(p["title"]) if p.get("title") else "") for p in _people(c["people"])[:4])
    return ('<article class="pg-co"><div class="pg-co-a"><h4><button type="button" class="pg-open" data-act="open" data-id="%s" title="Show it on the map">%s</button></h4>'
            '<div class="pg-co-fit">%s<span class="pg-bandtag" data-band-of="%s"></span></div><div class="pg-co-meta">%s</div></div>'
            '<div class="pg-co-b">%s%s%s%s</div></article>'
            % (esc(str(c.get("id", "")), quote=True), esc(c["company"]), _squares(c["fit"]), esc(str(c.get("id", "")), quote=True), "<br>".join(meta),
               "<p>%s</p>" % esc(c["why"]) if c.get("why") else "", now, who, marks))


def render(summary: dict, terms: dict, share: bool = False) -> str:
    say = _say(kind_of(summary))
    person = say["kind"] == "people"
    market, numbers = summary["market"], summary["numbers"]
    date = (market.get("updated_at") or "")[:10]
    segments = [s for s in summary["segments"] if s.get("count")]
    stats = [(numbers["segments"], "Pools" if say["kind"] else "Segments"), (numbers["with_signal"], say["reason_stat"]), (numbers["avg_fit"], "Average fit")]
    if not share:
        employers = len({str(c.get("uses") or "").lower() for seg in segments for c in seg["companies"] if c.get("uses")})
        stats += [(employers if person else numbers["buyers_named"], say["named"]), (numbers["in_pipeline"], say["in_pipe"])]
    links = [("summary", "Summary"), ("map", "The map"), ("calls", say["first_nav"]), ("segments", "Pools" if person else "Segments"), ("companies", say["things"][2])] if segments else []
    cells, painting = _art()
    mark = _dots("STOA", "pg-mark")
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>%s · %s</title><style>%s</style></head><body class=\"pg sd-gtm sd-maps\">" % (esc(market["name"]), esc(say["eyebrow"]), page_css()),
           '<div class="pg-nav"><div class="pg-wrap">%s<span class="stoa-eyebrow">%s</span><b>%s</b><nav aria-label="On this page">%s</nav></div></div>'
           % (mark, esc(say["eyebrow"]), esc(market["name"]), "".join('<a href="#%s">%s</a>' % item for item in links)),
           '<div class="pg-wrap"><header class="pg-hero"><div class="stoa-eyebrow">%s%s</div><h1>%s</h1>' % (esc(say["eyebrow"]), " // " + esc(_day(date)) if _day(date) else "", esc(market["name"]))]
    if market.get("brief"):
        out.append('<p class="pg-lede">%s</p>' % esc(market["brief"]))
    out.append('<div class="pg-headband"><div><div class="pg-fig">%s<span>%s mapped</span></div><div class="pg-headstats">%s</div></div>%s</div><div class="stoa-rule"></div></header>' % (
        _dots(numbers["companies"]), say["things"][0] if numbers["companies"] == 1 else say["things"][1],
        "".join("<span><small>%s</small><b>%s</b></span>" % (esc(l), esc(str(v))) for v, l in stats),
        '<div class="pg-headart"><canvas id="sm-artc" aria-hidden="true"></canvas></div>' if cells else ""))

    reading = [par for par in str(market.get("reading") or "").split("\n") if par.strip()]
    if segments:
        out.append('<section class="pg-s" id="summary" aria-label="What this map says">%s%s' % (_head("The short version", "What the map says"), _strategy(summary, say)))
        if reading:
            out.append('<div class="pg-reading"><span class="stoa-eyebrow">Reading the map</span><div>%s</div></div>' % "".join("<p>%s</p>" % esc(par) for par in reading))
        out.append("</section>")
        out.append('<section class="pg-s" id="map">%s%s</section>' % (
            _head("The candidates" if person else "The market", "The map", say["map_sub"]), _dial(summary, share, say)))
        saved = bool(summary.get("first_calls"))
        out.append('<section class="pg-s" id="calls">%s%s</section>' % (
            _head("Where to start", say["first"], say["first_saved"] if saved else say["first_ranked"]), _calls(summary, share, say)))

        cards = "".join(
            '<article class="pg-segcard pg-card"><header><h3>%s</h3><b title="%d%% of the map">%s</b></header><div class="pg-share"><i style="width:%d%%"></i></div>%s'
            "<dl><dt>Average fit</dt><dd>%s &nbsp;%s</dd>%s%s</dl><a href=\"#seg-%d\">See the %s ↓</a></article>"
            % (esc(s["segment"]), s["share"], _dots(s["count"]), max(2, s["share"]), "<p>%s</p>" % esc(s["description"]) if s.get("description") else "<p></p>",
               _squares(s.get("avg_fit")), esc(str(s.get("avg_fit", ""))),
               "<dt>Where</dt><dd>%s</dd>" % esc(", ".join(s["regions"])) if s.get("regions") else "",
               "<dt>%s</dt><dd>%s</dd>" % (esc(say["use_today"]), esc(", ".join(s["uses"]))) if s.get("uses") else "",
               i, say["things"][0] if s["count"] == 1 else "%d %s" % (s["count"], say["things"][1])) for i, s in enumerate(segments))
        out.append('<section class="pg-s" id="segments">%s<div class="pg-segs">%s</div>' % (_head("How they divide" if person else "How the market divides", "The pools" if person else "The segments", "Each would get a different pitch."), cards))
        panels = []
        if summary.get("regions"):
            panels.append('<div class="pg-panel pg-card"><h3>Where they are</h3>%s</div>' % _bars(summary["regions"], lambda r: r["name"], lambda r: r["count"]))
        if summary.get("sizes"):
            panels.append('<div class="pg-panel pg-card"><h3>How big they are</h3>%s</div>' % _bars(summary["sizes"], lambda r: r["band"] + " people", lambda r: r["count"]))
        if summary.get("bridges"):
            panels.append('<div class="pg-panel pg-card"><h3>Where the segments meet</h3><p>Companies that could sit in two segments: the natural second pool when one runs thin.</p>%s</div>' % "".join(
                '<div class="pg-bridge"><b>%s ↔ %s</b>%s</div>' % (esc(b["between"][0]), esc(b["between"][1]), esc(", ".join(b["companies"]))) for b in summary["bridges"]))
        if panels:
            out.append('<div class="pg-two">%s</div>' % "".join(panels))
        out.append("</section>")

        labels = market.get("requirements") or []
        order = {label: i + 1 for i, label in enumerate(labels)}
        out.append('<section class="pg-s" id="companies">%s' % _head("The evidence", say["things"][3],
                                                                     say["all_sub"] if labels else say["all_sub_bare"]))
        if labels:
            out.append('<ol class="pg-key pg-card" aria-label="The requirements, by number">%s</ol>' % "".join(
                '<li><span class="pg-num">%d</span><span>%s</span></li>' % (i + 1, esc(label)) for i, label in enumerate(labels)))
        for i, seg in enumerate(segments):
            out.append('<div class="pg-seghead" id="seg-%d"><h3>%s</h3><span>%d %s</span></div>%s'
                       % (i, esc(seg["segment"]), seg["count"], say["things"][0] if seg["count"] == 1 else say["things"][1], "".join(_company(c, order, share, say) for c in seg["companies"])))
        out.append("</section>")

    if segments:        # at the foot of the page, because it also fills in what comes after the map: who to call first, and each company's band
        out.append('<script type="application/json" id="sm-data">%s</script><script>%s</script>' % (_data(summary, share, say), script().replace("</script", "<\\/script")))
    if cells:           # the stoa at the head of the page, painted by the plugin's own code
        out.append('<script type="application/json" id="sm-art">%s</script><script>(() => {\n%s\n%s})();</script>' % (cells, painting.replace("</script", "<\\/script"), ART_DRAWN))
    out.append('<footer class="pg-foot">%s<p>Compiled with Stoa%s from public sources. Fit is a judgement against the brief at the top, and each company carries the evidence for it. '
               "This is a picture of a market, not a census of it; nobody on it was contacted to make it.</p></footer></div></body></html>" % (mark, " on " + esc(_day(date)) if _day(date) else ""))
    return "".join(out)
