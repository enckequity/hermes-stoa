// The dial inside a downloaded market map. One self-contained script: it reads the companies from the page itself,
// asks nothing of any server, and draws the map the way the GTM tab draws it: one round face, a slice for
// each segment, and who to call first nearest the centre.
//
// Where the marks sit is the GTM tab's own code: page.py writes `dialLayout` from desktop/plugin.js in where the line
// below says so, so the page and the tab cannot drift apart. How a company is scored and ranked follows
// mapCompanies in the plugin's page (a test holds the two together).
(() => {
'use strict';
const D = JSON.parse(document.getElementById('sm-data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const hashOf = text => { let n = 2166136261; for (let i = 0; i < text.length; i++) { n ^= text.charCodeAt(i); n = Math.imul(n, 16777619); } return n >>> 0; };
/*DIAL-LAYOUT*/
// The words come with the companies: a map kept with a recruiting pipeline is a talent map, and says so (page.py, SAY).
const SAY = D.say;
const BANDS = SAY.bands;
// A number set in dots, five across and seven down, the way the desk sets the figure at its head.
const DOTS = { 0: [14, 17, 19, 21, 25, 17, 14], 1: [4, 12, 4, 4, 4, 4, 14], 2: [14, 17, 1, 2, 4, 8, 31], 3: [31, 2, 4, 2, 1, 17, 14], 4: [2, 6, 10, 18, 31, 2, 2],
  5: [31, 16, 30, 1, 1, 17, 14], 6: [6, 8, 16, 30, 17, 17, 14], 7: [31, 1, 2, 4, 8, 8, 8], 8: [14, 17, 17, 14, 17, 17, 14], 9: [14, 17, 17, 15, 1, 2, 12] };
function dotfig(text) {
  let x = 0, marks = '';
  [...String(text)].forEach(ch => { (DOTS[ch] || []).forEach((row, y) => { for (let col = 0; col < 5; col++) if ((row >> (4 - col)) & 1) marks += '<rect x="' + (x + col + 0.1) + '" y="' + (y + 0.1) + '" width=".8" height=".8"/>'; }); x += 6; });
  return '<svg class="stoa-dotfig" viewBox="0 0 ' + (x - 1) + ' 7" style="aspect-ratio:' + (x - 1) + ' / 7" role="img" aria-label="' + esc(text) + '">' + marks + '</svg>';
}
const HOW = { seg: SAY.how_seg, held: SAY.how_held };
const S = { lens: 'seg', sel: '', quick: 'all', find: '', facet: null, weights: {} };
let LIST = [], L = null, hover = '', drawn = false;
const box = $('sm-map'), svg = $('sm-svg'), over = $('sm-over'), tip = $('sm-tip'), side = $('sm-side');
const f = v => Number(v.toFixed(1));
const squares = score => { const n = Math.max(1, Math.ceil(score / 20)); return '<span class="stoa-fit" data-fit="' + n + '" title="Fit ' + n + ' of 5">' + '■'.repeat(n) + '<s>' + '■'.repeat(5 - n) + '</s></span>'; };

function companies() {
  const own = Object.values(S.weights).some(w => w !== 2), now = new Date(D.today + 'T12:00:00').getTime(), out = [];
  D.segments.forEach(segment => segment.companies.forEach(c => {
    let score = clamp(Number(c.fit) || 3, 1, 5) * 20, weighed = false;
    const reqs = c.requirements || [];
    if (own && reqs.length) {
      let total = 0, got = 0, missed = false;
      reqs.forEach(req => { const w = S.weights[req.label] === undefined ? 2 : S.weights[req.label]; if (!w) return; total += w; if (req.shown) got += w; else if (w === 3) missed = true; });
      if (total) { score = Math.round(100 * got / total); if (missed) score = Math.min(score, 35); weighed = true; }
    }
    const buyer = (c.people || [])[0] || null;                 // what the rank counts, as the Stoa page does
    const lead = (c.people || []).find(p => !p.gone) || null;      // who to ask for: someone still there
    const days = c.signal && c.signal_date ? Math.max(0, Math.round((now - new Date(c.signal_date + 'T12:00:00').getTime()) / 86400000)) : null;
    const timing = !c.signal ? 6 + hashOf(c.id) % 14 : days === null || isNaN(days) ? 58 : clamp(Math.round(100 - days * 0.9), 30, 100);
    out.push(Object.assign({}, c, { segment: c.segment || segment.name, score, weighed, buyer, lead, reason: Boolean(c.signal), days: isNaN(days) ? null : days, timing, uses: c.uses || '',
      rank: Math.round(score * 0.65 + timing * 0.35) + (buyer ? 2 : 0) }));
  }));
  return out;
}
function shown(c) {
  if (S.find && !(c.company + ' ' + (c.location || '') + ' ' + c.segment + ' ' + c.uses).toLowerCase().includes(S.find)) return false;
  if (S.quick === 'reason' && !c.signal) return false;
  if (S.quick === 'open' && c.status === 'tracked') return false;
  if (S.quick === 'best' && c.score < 80) return false;
  if (S.facet && S.facet.kind === 'uses' && c.uses !== S.facet.value) return false;
  if (S.facet && S.facet.kind === 'req' && !(c.requirements || []).some(r => r.label === S.facet.value && r.shown)) return false;
  return true;
}

function drawMap() {
  const W = box.clientWidth;
  if (!W) return;
  L = dialLayout(LIST, S.lens, W, D.segments, 'people', 'all');
  hover = ''; tip.hidden = true;
  box.style.height = L.H + 'px'; box.dataset.narrow = L.narrow ? '1' : '0'; box.dataset.spot = '0';
  svg.setAttribute('width', L.W); svg.setAttribute('height', L.H); svg.setAttribute('viewBox', '0 0 ' + L.W + ' ' + L.H);
  const at = (r, an) => [L.cx + Math.cos(an) * r, L.cy + Math.sin(an) * r];
  const spoke = an => { const p = at(L.R * 0.1, an), q = at(L.R, an); return '<line class="sd-dial-line" x1="' + f(p[0]) + '" y1="' + f(p[1]) + '" x2="' + f(q[0]) + '" y2="' + f(q[1]) + '"/>'; };
  const centre = 'cx="' + f(L.cx) + '" cy="' + f(L.cy) + '"';
  let s = '<defs><radialGradient id="sd-dial-shade"><stop offset="0" style="stop-color:var(--dial-face-hi)"/><stop offset="0.55" style="stop-color:var(--dial-face)"/><stop offset="1" style="stop-color:var(--dial-face-lo)"/></radialGradient></defs>' +
    '<circle class="sd-dial-rim" ' + centre + ' r="' + f(L.R) + '" fill="url(#sd-dial-shade)"/><circle class="sd-dial-zone" ' + centre + ' r="' + f(L.r1) + '"/><circle class="sd-dial-line" ' + centre + ' r="' + f(L.r2) + '" fill="none"/>';
  if (L.groups.length) s += spoke(L.groups[0].a0) + L.groups.map(g => spoke(g.a1)).join('');
  s += L.ns.map((n, i) => {
    const c = n.c, picked = S.sel === n.id;
    return '<g class="stoa-mapco sd-dial-co" data-id="' + esc(n.id) + '" data-state="' + (c.status === 'tracked' ? 'in' : 'on') + '" data-sel="' + (picked ? 1 : 0) + '" data-dim="' + (shown(c) ? 0 : 1) + '" data-band="' + n.band +
      '" data-tone="' + toneOf(c.score) + '" data-hot="' + (c.reason && c.days !== null && c.days <= 16 ? 1 : 0) + '" style="transform:translate(' + n.x.toFixed(1) + 'px,' + n.y.toFixed(1) + 'px)" tabindex="0" role="button" aria-pressed="' + picked +
      '" aria-label="' + esc(c.company + ', fit ' + c.score + ' of 100, ' + BANDS[n.band - 1].toLowerCase() + (c.status === 'tracked' ? ', ' + String(SAY.in_pipe_one || '').toLowerCase() : '') + (c.reason ? ', ' + SAY.has_reason : '')) + '">' +
      '<g' + (drawn ? '' : ' class="stoa-mappop" style="animation-delay:' + Math.min(i * 12, 500) + 'ms"') + '><circle class="sd-dial-base" r="' + f(n.r + (c.reason ? 6.6 : 2.6)) + '"/>' +
      (c.reason ? '<circle class="sd-dial-sig" r="' + f(n.r + 4) + '"/>' : '') + '<circle class="sd-dial-disc" r="' + f(n.r) + '"/><circle class="sd-dial-edge" r="' + f(n.r) + '"/>' +
      '<path class="stoa-mapbrk" d="' + brackets(Math.round(n.r + (c.reason ? 9 : 5))) + '"/></g></g>';
  }).join('');
  s += L.ns.map(n => '<text class="sd-dial-lbl" data-id="' + esc(n.id) + '" text-anchor="' + n.lbl.anchor + '" data-band="' + n.band + '" data-on="' + ((n.show || S.sel === n.id) && shown(n.c) ? 1 : 0) + '" data-sel="' + (S.sel === n.id ? 1 : 0) +
    '" style="transform:translate(' + n.lbl.tx.toFixed(1) + 'px,' + n.lbl.ty.toFixed(1) + 'px)">' + esc(shortName(n.c.company)) + '</text>').join('');
  svg.innerHTML = s;

  const count = g => plural(g.m.length, SAY.things[0], SAY.things[1]) + (S.lens === 'held' ? ' · ' + Math.round(100 * g.m.length / L.ns.length) + '%' : ' · fit ' + g.avg);
  const cap = g => (g.where === 'right' ? 'left:' + f(g.px) + 'px;top:' + f(g.py) + 'px;width:' + f(Math.max(60, Math.min(L.W - g.px - 8, 200))) + 'px;transform:translateY(-50%);text-align:left'
    : g.where === 'left' ? 'right:' + f(L.W - g.px) + 'px;top:' + f(g.py) + 'px;width:' + f(Math.max(60, Math.min(g.px - 8, 200))) + 'px;transform:translateY(-50%);text-align:right'
      : 'left:' + f(g.px) + 'px;top:' + f(g.py) + 'px;width:190px;transform:translate(-50%,' + (g.where === 'top' ? '-100%' : '0') + ');text-align:center');
  let o = '<div class="stoa-maphint">Press a ' + esc(SAY.things[0]) + ' for the evidence</div>';
  if (L.ns.length) o += '<div class="stoa-mapq sd-dial-band" style="left:' + f(L.cx) + 'px;top:' + f(L.cy - L.r1 + 8) + 'px"><strong>' + esc(BANDS[0]) + '</strong></div>' +
    '<div class="stoa-mapq sd-dial-band" style="left:' + f(L.cx) + 'px;top:' + f(L.cy - L.r2 + 8) + 'px">' + esc(BANDS[1]) + '</div><div class="stoa-mapq sd-dial-band" style="left:' + f(L.cx) + 'px;top:' + f(L.cy - L.R + 9) + 'px">' + esc(BANDS[2]) + '</div>';
  else o += '<div class="stoa-mapq sd-dial-band" style="left:' + f(L.cx) + 'px;top:' + f(L.cy - 8) + 'px">No ' + esc(SAY.things[1]) + ' on this map yet</div>';
  // The centre is for a company that fits and has a reason to call. With no reasons recorded it is empty, and says why.
  if (L.ns.length && !L.ns.some(n => n.band === 1)) o += '<div class="sd-dial-none" style="left:' + f(L.cx) + 'px;top:' + f(L.cy + 6) + 'px;width:' + f(Math.max(96, 2 * L.r1 - 36)) + 'px"><b>' + esc(SAY.nobody) + '</b>' +
    (L.r1 >= 96 ? '<span>' + esc(SAY.nobody_why) + '</span>' : '') + '</div>';
  o += L.groups.map((g, index) => {
    if (L.narrow) {
      const q = at(L.R + 11, g.mid), letter = String.fromCharCode(65 + index);
      return '<div class="sd-dial-cap" style="left:' + f(q[0]) + 'px;top:' + f(q[1]) + 'px;transform:translate(-50%,-50%)"><b>' + letter + '</b></div>' +
        '<div class="sd-dial-cap sd-dial-keyline" style="left:10px;top:' + f(L.cy + L.R + 24 + index * 21) + 'px;width:' + f(L.W - 20) + 'px"><b>' + letter + ' · ' + esc(g.name) + '</b> <span>' + count(g) + '</span></div>';
    }
    return '<div class="sd-dial-cap" data-where="' + g.where + '" data-group="' + index + '" style="' + cap(g) + '"><b>' + esc(g.name) + '</b><span>' + count(g) + '</span></div>';
  }).join('');
  over.innerHTML = o;
  drawn = true;
}

/** Pointing at a mark names it, lights its slice and quiets the rest. Nothing is redrawn: only what is lit changes. */
function point(id) {
  if (!L || id === hover) return;
  hover = id;
  const n = id ? L.ns.find(x => x.id === id) : null;
  box.dataset.spot = n ? '1' : '0';
  svg.querySelectorAll('.sd-dial-co').forEach(el => { const m = n && L.ns.find(x => x.id === el.dataset.id); el.dataset.lit = !n ? '' : m === n ? 'hot' : m && m.g === n.g ? 'kin' : 'off'; });
  svg.querySelectorAll('.sd-dial-lbl').forEach(el => { const m = L.ns.find(x => x.id === el.dataset.id); el.dataset.lit = !n ? '' : m === n ? 'hot' : 'off';
    el.dataset.on = m && (m.show || S.sel === m.id || m === n) && shown(m.c) ? '1' : '0'; });
  over.querySelectorAll('.sd-dial-cap[data-group]').forEach(el => { el.dataset.lit = n && L.groups[Number(el.dataset.group)] === n.g ? '1' : '0'; });
  if (!n) { tip.hidden = true; return; }
  const c = n.c;
  tip.innerHTML = '<b>' + esc(c.company) + '</b>' + squares(c.score) + '<span class="stoa-sub">  fit ' + c.score + (c.size ? ' · ' + esc(c.size) : '') + (SAY.kind === 'people' && c.uses ? ' at ' + esc(c.uses) : '') + '</span>' +
    '<div class="sd-dial-tipband" data-band="' + n.band + '">' + BANDS[n.band - 1] + (c.status === 'tracked' ? ' · ' + esc(String(SAY.in_pipe_one || '').toLowerCase()) : '') + '</div>' +
    (c.signal ? '<div><span class="stoa-chip">' + esc(c.signal) + '</span>' + (agoWords(c.days) ? '<span class="stoa-sub">  ' + agoWords(c.days) + '</span>' : '') + '</div>' : '') +
    (c.why ? '<div class="stoa-sub">' + esc(c.why) + '</div>' : '');
  const right = Math.max(n.x + n.r + 16, n.lbl.x + n.lbl.w + 8), left = Math.min(n.x - n.r - 16, n.lbl.x - 8) - 232;
  tip.style.left = Math.max(4, right + 236 > L.W ? left : right) + 'px'; tip.style.top = Math.max(4, Math.min(L.H - 130, n.y - 24)) + 'px';
  tip.hidden = false;
}

/** The panel beside the dial. With nothing picked it says how to read the dial and who is nearest its centre, a line
    each, so it is never taller than the dial. With a company picked it is that company, and scrolls if it must. */
function drawSide() {
  const c = S.sel ? LIST.find(x => x.id === S.sel) : null;
  let h;
  if (c) {
    const band = bandOf(c.rank), site = c.domain ? (/^https?:\/\//.test(c.domain) ? c.domain : 'https://' + c.domain) : '';
    h = '<div class="stoa-eyebrow">' + esc(c.segment) + (c.also ? ' · also fits ' + esc(c.also) : '') + '</div><h3>' + esc(c.company) + '</h3>' +
      (site ? '<a href="' + esc(site) + '" rel="noreferrer noopener">' + esc(c.domain) + '</a>' : '') +
      '<div class="pg-score"><span class="stoa-eyebrow">' + (c.weighed ? 'Fit, by these weights' : 'Fit') + ' · <span class="pg-bandtag" data-band="' + band + '">' + BANDS[band - 1] + '</span></span><b>' + c.score + '</b>' + squares(c.score) + '</div>' +
      (c.why ? '<p>' + esc(c.why) + '</p>' : '') +
      (c.signal ? '<div class="pg-now" style="grid-template-columns:minmax(0,1fr);gap:2px"><span>Why now</span><div>' + esc(c.signal) + '<small>' + esc(agoWords(c.days) || 'no date on it') + '</small></div></div>' : '') +
      ((c.requirements || []).length ? '<div style="margin-top:16px">' + c.requirements.map(req => '<div class="pg-req" data-on="' + (req.shown ? 1 : 0) + '"><span>' + (req.shown ? '✓' : '–') + '</span><span><b>' + esc(req.label) + '</b><br><span class="stoa-sub">' + (req.shown ? esc(req.evidence || 'Shown') : 'No evidence found') + '</span></span></div>').join('') + '</div>' : '') +
      '<dl class="pg-facts"><dt>' + (SAY.kind === 'people' ? 'Title' : 'Size') + '</dt><dd>' + esc(c.size || 'Not known') + '</dd>' + (c.location ? '<dt>Where</dt><dd>' + esc(c.location) + '</dd>' : '') + '<dt>' + esc(SAY.uses_fact) + '</dt><dd>' + esc(c.uses || 'Not known') + '</dd>' +
      (c.status === 'tracked' ? '<dt>Pipeline</dt><dd>Already a record</dd>' : '') +
      (c.lead ? '<dt>' + esc(SAY.ask_for || '') + '</dt><dd>' + esc([c.lead.name, c.lead.title].filter(Boolean).join(', ')) + '</dd>' : '') +
      (/^https?:\/\//.test(c.source || '') ? '<dt>Source</dt><dd><a href="' + esc(c.source) + '" rel="noreferrer noopener">' + esc(c.source.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '').slice(0, 60)) + '</a></dd>' : '') + '</dl>' +
      '<button class="pg-btn" data-act="clear">← Back to the whole map</button>';
  } else {
    const seen = LIST.filter(shown), count = band => seen.filter(x => bandOf(x.rank) === band).length;
    const near = seen.slice().sort((a, b) => b.rank - a.rank).slice(0, 7);
    h = '<div class="stoa-eyebrow">How to read it</div><div class="pg-bands">' +
      BANDS.map((name, i) => [name, SAY.band_why[i]]).map((band, i) =>
        '<div class="pg-band" data-band="' + (i + 1) + '"><i></i><span><b>' + band[0] + '</b><small>' + band[1] + '</small></span><em>' + count(i + 1) + '</em></div>').join('') + '</div>' +
      '<div class="pg-near"><div class="stoa-eyebrow">Nearest the centre</div>' + (near.length ? near.map((x, i) => '<button class="pg-row" data-act="open" data-id="' + esc(x.id) + '" title="' + esc(x.company) + '"><i>' + String(i + 1).padStart(2, '0') + '</i><b>' + esc(x.company) + '</b>' + squares(x.score) + '</button>').join('')
        : '<p class="stoa-sub">No ' + esc(SAY.things[0]) + ' matches what is picked above.</p>') + '</div>' +
      '<p class="stoa-sub" style="margin:14px 0 0">Press a ' + esc(SAY.things[0]) + ', here or on the map, for the evidence.</p>';
  }
  side.innerHTML = h; side.scrollTop = 0;
}

/** Beside the dial the panel is as tall as the dial's card, so nothing is left empty under either. Under it, on a
    narrow page, it is as tall as what is in it. */
function fitSide() {
  side.style.height = '';
  if (window.matchMedia('(min-width: 980px)').matches && window.matchMedia('screen').matches) side.style.height = $('sm-card').offsetHeight + 'px';
}

/** Who to call first, on a map with nothing written down about it: the five nearest the centre, with the reason. */
function drawCalls() {
  const el = $('sm-calls');
  if (!el || el.dataset.auto !== '1') return;
  el.innerHTML = LIST.filter(c => c.status !== 'tracked').sort((a, b) => b.rank - a.rank).slice(0, 6).map((c, i) =>
    '<article class="pg-call"><span class="pg-call-n">' + dotfig(String(i + 1).padStart(2, '0')) + '</span><div><div class="stoa-eyebrow">' + esc(c.segment) + '</div>' +
    '<h3><button type="button" class="pg-open" data-act="open" data-id="' + esc(c.id) + '" title="Show it on the map">' + esc(c.company) + '</button></h3><p>' + esc(c.why || '') + '</p>' +
    (c.signal ? '<span class="pg-mono">Why now · ' + esc(c.signal) + (agoWords(c.days) ? ' · ' + agoWords(c.days) : '') + '</span>' : '') + '</div></article>').join('');
}

function drawBar() {
  document.querySelectorAll('[data-lens]').forEach(b => b.setAttribute('aria-pressed', b.dataset.lens === S.lens));
  const facet = S.facet; S.facet = null;                       // each quick filter's count is of the whole map
  document.querySelectorAll('[data-quick]').forEach(b => { b.setAttribute('aria-pressed', b.dataset.quick === S.quick); const q = S.quick; S.quick = b.dataset.quick; b.querySelector('i').textContent = LIST.filter(shown).length; S.quick = q; });
  S.facet = facet;
  document.querySelectorAll('[data-facet]').forEach(b => b.setAttribute('aria-pressed', Boolean(S.facet) && S.facet.kind === b.dataset.facet && S.facet.value === b.dataset.value));
  $('sm-how').textContent = HOW[S.lens] + (S.facet ? '  Lit up: ' + S.facet.label + '. Press it again to light them all.' : '');
  document.querySelectorAll('[data-band-of]').forEach(el => { const c = LIST.find(x => x.id === el.dataset.bandOf); const band = c ? bandOf(c.rank) : 0; el.dataset.band = band; el.textContent = band ? BANDS[band - 1] : ''; });
  document.querySelectorAll('[data-now]').forEach(el => { const n = LIST.filter(c => c.segment === el.dataset.now && c.rank >= CALL && c.status !== 'tracked').length; el.textContent = n ? n + ' ' + SAY.now : ''; });
  if (!D.requirements.length) return;
  const weighed = Object.values(S.weights).some(w => w !== 2);
  $('sm-weights').innerHTML = D.requirements.map(label => { const w = S.weights[label] === undefined ? 2 : S.weights[label]; const checked = LIST.filter(c => (c.requirements || []).some(r => r.label === label)), have = checked.filter(c => c.requirements.find(r => r.label === label).shown).length;
    return '<div class="pg-weight"><span>' + esc(label) + '<small>' + (checked.length ? have + ' of ' + checked.length + ' show it' : 'not checked') + '</small></span><span class="pg-seg">' +
      ['Off', 'Nice', 'Matters', 'Must'].map((word, i) => '<button data-act="weight" data-label="' + esc(label) + '" data-w="' + i + '" aria-pressed="' + (w === i) + '">' + word + '</button>').join('') + '</span></div>'; }).join('') +
    (weighed ? '<button class="pg-btn" data-act="reset">Back to the fit as recorded</button>' : '');
  $('sm-weighnote').textContent = weighed ? 'Fit is worked out from these weights, for every company that was checked against the requirements.' : 'Change how much a requirement counts and the map re-scores itself. Nothing is saved; reload to start again.';
}

function render() { LIST = companies(); if (S.sel && !LIST.some(c => c.id === S.sel)) S.sel = ''; drawBar(); drawMap(); drawSide(); fitSide(); drawCalls(); }

document.addEventListener('click', e => {
  const b = e.target.closest('[data-lens],[data-quick],[data-facet],[data-act]');
  if (!b) return;
  if (b.dataset.lens) S.lens = b.dataset.lens;
  else if (b.dataset.quick) S.quick = b.dataset.quick;
  else if (b.dataset.facet) { const on = S.facet && S.facet.kind === b.dataset.facet && S.facet.value === b.dataset.value; S.facet = on ? null : { kind: b.dataset.facet, value: b.dataset.value, label: (b.dataset.facet === 'uses' ? SAY.use_facet : 'companies that show: ') + b.dataset.value }; }
  else if (b.dataset.act === 'clear') S.sel = '';
  else if (b.dataset.act === 'open') S.sel = b.dataset.id;
  else if (b.dataset.act === 'tune') { const tune = $('sm-what'); if (tune) { tune.open = true; tune.scrollIntoView({ behavior: 'smooth', block: 'center' }); } return; }
  else if (b.dataset.act === 'weight') S.weights[b.dataset.label] = Number(b.dataset.w);
  else if (b.dataset.act === 'reset') S.weights = {};
  render();
  // Picked from somewhere else on the page (a first call, a company's entry, a requirement): go to the map, where it now shows.
  if ((b.dataset.facet && S.facet) || (b.dataset.act === 'open' && !b.closest('#sm-live'))) $('map').scrollIntoView({ behavior: 'smooth', block: 'start' });
});
$('sm-find').addEventListener('input', e => { S.find = e.target.value.toLowerCase().trim(); render(); });
const mark = e => (e.target.closest ? e.target.closest('.sd-dial-co') : null);
const pick = id => { S.sel = S.sel === id ? '' : id; render(); };
svg.addEventListener('click', e => { const g = mark(e); if (g) { e.stopPropagation(); pick(g.dataset.id); } });
svg.addEventListener('keydown', e => { const g = mark(e); if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); pick(g.dataset.id); } });
svg.addEventListener('pointerover', e => { const g = mark(e); if (g && e.pointerType !== 'touch') point(g.dataset.id); });
svg.addEventListener('pointerout', e => { const to = e.relatedTarget; if (!to || !to.closest || !to.closest('.sd-dial-co')) point(''); });
svg.addEventListener('focusin', e => { const g = mark(e); if (g) point(g.dataset.id); });
svg.addEventListener('focusout', () => point(''));
box.addEventListener('click', () => { if (S.sel) { S.sel = ''; render(); } });
let lastW = 0, wait = 0;
new ResizeObserver(() => { const w = box.clientWidth; if (Math.abs(w - lastW) < 2) return; lastW = w; clearTimeout(wait); wait = setTimeout(() => { drawMap(); fitSide(); }, 60); }).observe(box);
$('sm-live').hidden = false;
lastW = box.clientWidth; render();
})();
