// The map inside a downloaded page. One self-contained script: it reads the companies from the page itself, asks
// nothing of any server, and draws the same picture the Stoa page does (three views, pick to open, weights, size).
// Kept in step by hand with mapCompanies and mapLayout in desktop/plugin.js.
(() => {
'use strict';
const D = JSON.parse(document.getElementById('sm-data').textContent);
const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const hashOf = text => { let n = 2166136261; for (let i = 0; i < text.length; i++) { n ^= text.charCodeAt(i); n = Math.imul(n, 16777619); } return n >>> 0; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const heatOf = score => 1 + Math.min(6, Math.floor(score / 100 * 7));
const squares = score => { const n = Math.max(1, Math.ceil(score / 20)); return '<span class="fit f' + n + '">' + '■'.repeat(n) + '<s>' + '■'.repeat(5 - n) + '</s></span>'; };
const ago = days => (days === null ? '' : days === 0 ? 'today' : days === 1 ? 'yesterday' : days < 60 ? days + ' days ago' : Math.round(days / 30) + ' months ago');
const LENSES = { seg: 'Each ring is a segment that would get a different pitch. The best fit sits in the middle.',
  pri: 'Further right is a better fit; higher up is a fresher reason to call. The top right is who to call first.',
  held: 'Grouped by what each company uses today.' };
const S = { lens: 'seg', size: D.size || 'people', sel: [], quick: 'all', find: '', weights: {} };
let LIST = [], LAY = null;
const els = new Map(), lbls = new Map();
const map = $('sm-map'), svg = $('sm-svg'), gR = $('sm-rings'), gL = $('sm-links'), gN = $('sm-nodes'), gT = $('sm-labels'), over = $('sm-over'), tip = $('sm-tip'), lasso = $('sm-lasso'), side = $('sm-side');

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
    const buyer = (c.people || [])[0] || null;
    const days = c.signal && c.signal_date ? Math.max(0, Math.round((now - new Date(c.signal_date + 'T12:00:00').getTime()) / 86400000)) : null;
    const timing = !c.signal ? 6 + hashOf(c.id) % 14 : days === null || isNaN(days) ? 58 : clamp(Math.round(100 - days * 0.9), 30, 100);
    out.push(Object.assign({}, c, { segment: c.segment || segment.name, score, weighed, buyer, days: isNaN(days) ? null : days, timing, uses: c.uses || '',
      rank: Math.round(score * 0.65 + timing * 0.35) + (buyer ? 2 : 0) }));
  }));
  return out;
}
function shown(c) {
  if (S.find && !(c.company + ' ' + (c.location || '') + ' ' + c.segment + ' ' + c.uses).toLowerCase().includes(S.find)) return false;
  if (S.quick === 'reason' && !c.signal) return false;
  if (S.quick === 'open' && c.status === 'tracked') return false;
  if (S.quick === 'best' && c.score < 80) return false;
  return true;
}
function layout() {
  const W = map.clientWidth, narrow = W < 620, cols = narrow ? 2 : 3, sc = narrow ? 0.74 : 1;
  const radius = c => (S.size === 'same' ? 12 : S.size === 'named' ? Math.min(26, 9 + (c.people || []).length * 3.4) : c.headcount ? clamp(6.5 + Math.sqrt(c.headcount) * 0.62, 8.5, 27) : 11);
  const ns = LIST.map(c => ({ c, id: c.id, r: sc * radius(c), show: false, bridge: null }));
  const out = { W, H: 0, narrow, groups: [], links: [], pri: null, ns };
  const relax = hold => {
    ns.forEach((n, i) => { n.x = n.ax + ((i * 37) % 7 - 3) * 0.4; n.y = n.ay + ((i * 53) % 7 - 3) * 0.4; });
    for (let k = 0; k < 240; k++) {
      const pull = k < 170 ? 0.07 : 0.02;
      ns.forEach(n => { n.x += (n.ax - n.x) * pull; n.y += (n.ay - n.y) * pull; });
      for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) {
        const p = ns[i], q = ns[j]; let dx = q.x - p.x, dy = q.y - p.y; const d = Math.hypot(dx, dy) || 0.01, min = p.cr + q.cr;
        if (d < min) { const o = (min - d) / 2; dx /= d; dy /= d; p.x -= dx * o; p.y -= dy * o; q.x += dx * o; q.y += dy * o; }
      }
      ns.forEach(hold);
    }
  };
  if (S.lens === 'pri') {
    const m = { l: narrow ? 30 : 46, r: 16, t: 26, b: 40 }, H = narrow ? 500 : 560;
    const x0 = m.l + 40, xw = W - m.l - m.r - 112, y0 = H - m.b - 24, yh = H - m.t - m.b - 48;      // a margin inside the axes keeps the ends of each scale in the picture
    out.H = H; out.pri = Object.assign({ mid: y0 - yh * 0.5, fx: x0 + xw * 0.6 }, m);
    ns.forEach(n => {
      n.ax = x0 + xw * n.c.score / 100 + ((hashOf(n.id + 'x') % 100) / 100 - 0.5) * W * 0.05;
      n.ay = y0 - yh * n.c.timing / 100 + (n.c.days === null ? ((hashOf(n.id) % 100) / 100 - 0.5) * yh * 0.08 : 0);
    });
    ns.slice().sort((a, b) => b.c.rank - a.c.rank).slice(0, narrow ? 5 : 9).forEach(n => { n.show = true; });
    ns.forEach(n => { n.cr = n.r + (n.show ? 11 : 3.5); });
    relax(n => { n.x = clamp(n.x, m.l + n.r + 2, W - m.r - n.r - 2); n.y = clamp(n.y, m.t + n.r + 2, H - m.b - n.r - 18); });
    return out;
  }
  let groups;
  if (S.lens === 'seg') {
    let real = D.segments.map(s => ({ key: s.name, name: s.name, m: ns.filter(n => n.c.segment === s.name) })).filter(g => g.m.length);
    const pairs = [];
    real.forEach((g, i) => g.m.forEach(n => { const j = n.c.also ? real.findIndex(q => q !== g && q.key.toLowerCase() === String(n.c.also).toLowerCase()) : -1; if (j >= 0) pairs.push([i, j]); }));
    if (pairs.length && real.length <= 7) {
      const cell = k => [k % cols, Math.floor(k / cols)];
      const cost = order => pairs.reduce((sum, pair) => { const p = cell(order.indexOf(pair[0])), q = cell(order.indexOf(pair[1])); return sum + Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]); }, 0);
      let best = real.map((_, i) => i), least = cost(best);
      const walk = (order, rest) => { if (!rest.length) { const c = cost(order); if (c < least) { best = order; least = c; } return; } rest.forEach((x, k) => walk(order.concat(x), rest.filter((_, m) => m !== k))); };
      if (least > pairs.length) walk([], real.map((_, i) => i));
      real = best.map(i => real[i]);
    }
    groups = real;
  } else {
    const by = new Map();
    ns.forEach(n => { const key = n.c.uses || 'Not known yet'; if (!by.has(key)) by.set(key, []); by.get(key).push(n); });
    groups = [...by.entries()].map(e => ({ key: e[0], name: e[0], m: e[1] })).sort((a, b) => (a.key === 'Not known yet') - (b.key === 'Not known yet') || b.m.length - a.m.length);
  }
  const rows = Math.ceil(groups.length / cols), rowH = narrow ? 256 : 304;
  out.H = rows * rowH;
  groups.forEach((g, i) => { g.colW = W / cols; g.cx = g.colW * (i % cols + 0.5); g.cy = rowH * Math.floor(i / cols) + rowH / 2 - 20; g.R = Math.min(g.colW / 2 - 10, rowH / 2 - 42); });
  groups.forEach((g, gi) => {
    g.m.sort((a, b) => b.c.rank - a.c.rank);
    g.m.forEach((n, i) => {
      const d = i ? (g.R - 24 * sc) * Math.sqrt(i / g.m.length) : 0, a = i * 2.39996 + gi * 1.1;
      n.g = g; n.ax = g.cx + Math.cos(a) * d; n.ay = g.cy + Math.sin(a) * d;
      if (i < (narrow ? 2 : 3)) n.show = true;
      const other = S.lens === 'seg' && n.c.also ? groups.find(q => q !== g && q.key.toLowerCase() === String(n.c.also).toLowerCase()) : null;
      if (other) { const ux = other.cx - g.cx, uy = other.cy - g.cy, l = Math.hypot(ux, uy) || 1; n.ax = g.cx + ux / l * (g.R - n.r - 2); n.ay = g.cy + uy / l * (g.R - n.r - 2); n.bridge = other; n.show = true; }
    });
  });
  ns.forEach(n => { n.cr = n.r + (n.show ? 11 : 3.5); });
  relax(n => { const g = n.g, dx = n.x - g.cx, dy = n.y - g.cy, d = Math.hypot(dx, dy), max = g.R - n.r - 2; if (d > max) { n.x = g.cx + dx / d * max; n.y = g.cy + dy / d * max; } });
  ns.forEach(n => { if (n.bridge) { const o = n.bridge, ux = n.x - o.cx, uy = n.y - o.cy, l = Math.hypot(ux, uy) || 1; out.links.push({ x1: n.x, y1: n.y, x2: o.cx + ux / l * o.R, y2: o.cy + uy / l * o.R }); } });
  out.groups = groups;
  return out;
}
function drawMap() {
  const L = LAY = layout(), W = L.W, H = L.H;
  map.style.height = H + 'px'; map.classList.toggle('nar', L.narrow);
  svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  tip.hidden = true;
  let r = '', o = '<div class="sm-hint">Click to open · shift-click or drag to pick several</div>';
  if (L.pri) {
    const m = L.pri, fx = m.fx;
    r += '<rect x="' + fx + '" y="' + m.t + '" width="' + (W - m.r - fx) + '" height="' + (m.mid - m.t) + '" fill="url(#sm-wash)"/>' +
      '<line class="sm-split" x1="' + fx + '" y1="' + m.t + '" x2="' + fx + '" y2="' + (H - m.b) + '"/><line class="sm-split" x1="' + m.l + '" y1="' + m.mid + '" x2="' + (W - m.r) + '" y2="' + m.mid + '"/>' +
      '<line class="sm-axis" x1="' + m.l + '" y1="' + (H - m.b) + '" x2="' + (W - m.r) + '" y2="' + (H - m.b) + '"/><line class="sm-axis" x1="' + m.l + '" y1="' + m.t + '" x2="' + m.l + '" y2="' + (H - m.b) + '"/>';
    o += '<div class="sm-q" style="left:' + (fx + 10) + 'px;top:' + (m.t + 8) + 'px"><strong>Call now</strong><em>Fits, and the reason is fresh</em></div>' +
      '<div class="sm-q" style="left:' + (fx + 10) + 'px;top:' + (m.mid + 8) + 'px">Warm up<em>Fits, no fresh reason</em></div>' +
      '<div class="sm-q" style="left:' + (m.l + 10) + 'px;top:' + (m.t + 8) + 'px">Watch<em>A reason, but a weak fit</em></div>' +
      '<div class="sm-q" style="left:' + (m.l + 10) + 'px;top:' + (m.mid + 8) + 'px">Later</div><div class="sm-q" style="left:' + m.l + 'px;bottom:12px">Fit →</div>' +
      '<div class="sm-q" style="left:10px;top:' + (m.t + 96) + 'px;transform:rotate(-90deg);transform-origin:left top">Fresher →</div>';
  }
  L.groups.forEach(g => {
    r += '<circle class="sm-ring" cx="' + g.cx + '" cy="' + g.cy + '" r="' + g.R + '"/>';
    const n = g.m.length, avg = n ? Math.round(g.m.reduce((t, x) => t + x.c.score, 0) / n) : 0;
    o += '<div class="sm-cap" style="left:' + g.cx + 'px;top:' + (g.cy + g.R + 6) + 'px;width:' + (g.colW - 8) + 'px"><b>' + esc(g.name) + '</b><span>' + n + (n === 1 ? ' company' : ' companies') +
      (S.lens === 'held' ? ' · ' + Math.round(100 * n / L.ns.length) + '%' : ' · fit ' + avg) + '</span></div>';
  });
  gR.innerHTML = r; over.innerHTML = o;
  gL.innerHTML = L.links.map(l => '<line class="sm-link" x1="' + l.x1.toFixed(1) + '" y1="' + l.y1.toFixed(1) + '" x2="' + l.x2.toFixed(1) + '" y2="' + l.y2.toFixed(1) + '"/>').join('');
  const seen = new Set();
  L.ns.forEach(n => {
    const c = n.c, k = heatOf(c.score), picked = S.sel.includes(c.id), s = n.r + 5, q = 5; seen.add(c.id);
    let g = els.get(c.id);
    if (!g) { g = document.createElementNS(NS, 'g'); g.dataset.id = c.id; g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button'); g.innerHTML = '<g class="sm-pop"></g>'; gN.appendChild(g); els.set(c.id, g); }
    g.style.transform = 'translate(' + n.x.toFixed(1) + 'px,' + n.y.toFixed(1) + 'px)';
    g.setAttribute('class', 'sm-co' + (c.status === 'tracked' ? ' in' : '') + (picked ? ' sel' : '') + (shown(c) ? '' : ' dim'));
    g.setAttribute('aria-label', c.company + ', fit ' + c.score + ' of 100' + (c.signal ? ', has a reason to call' : ''));
    g.setAttribute('aria-pressed', picked);
    g.firstChild.innerHTML = (c.signal ? '<circle class="sm-sig" r="' + (n.r + 4).toFixed(1) + '"/>' : '') +
      '<circle r="' + n.r.toFixed(1) + '" ' + (c.status === 'tracked' ? 'style="fill:var(--h' + k + ')"' : 'fill="url(#sm-st' + k + ')"') + '/>' +
      '<circle class="sm-edge" r="' + n.r.toFixed(1) + '" style="stroke:var(--h' + k + ')"/>' +
      '<path class="sm-brk" d="M' + -s + ',' + (-s + q) + 'V' + -s + 'H' + (-s + q) + 'M' + (s - q) + ',' + -s + 'H' + s + 'V' + (-s + q) + 'M' + s + ',' + (s - q) + 'V' + s + 'H' + (s - q) + 'M' + (-s + q) + ',' + s + 'H' + -s + 'V' + (s - q) + '"/>';
    let t = lbls.get(c.id);
    if (!t) { t = document.createElementNS(NS, 'text'); t.textContent = c.company.length > 22 ? c.company.slice(0, 21).trimEnd() + '…' : c.company; gT.appendChild(t); lbls.set(c.id, t); }
    t.style.transform = 'translate(' + n.x.toFixed(1) + 'px,' + (n.bridge && n.y > n.g.cy + n.g.R * 0.45 ? n.y - n.r - 7 : n.y + n.r + 15).toFixed(1) + 'px)';
    t.setAttribute('class', 'sm-lbl' + ((n.show || picked) && shown(c) ? ' on' : '') + (picked ? ' sel' : ''));
  });
  els.forEach((g, id) => { if (!seen.has(id)) { g.remove(); els.delete(id); } });
  lbls.forEach((t, id) => { if (!seen.has(id)) { t.remove(); lbls.delete(id); } });
}
function drawSide() {
  const chosen = S.sel.map(id => LIST.find(c => c.id === id)).filter(Boolean);
  let h;
  if (chosen.length === 1) {
    const c = chosen[0];
    h = '<div class="mono">' + esc(c.segment) + (c.also ? ' · also fits ' + esc(c.also) : '') + '</div><h3>' + esc(c.company) + '</h3><div class="sm-sub">' + [c.domain, c.location].filter(Boolean).map(esc).join(' · ') + '</div>' +
      (c.signal ? '<div class="sm-sub" style="margin-top:6px"><span class="tag on">' + esc(c.signal) + '</span>' + esc(ago(c.days) || 'no date on it') + '</div>' : '') +
      '<div class="sm-score"><span class="mono">' + (c.weighed ? 'Fit, by these weights' : 'Fit') + '</span><b>' + c.score + '</b>' + squares(c.score) + '</div>' +
      (c.why ? '<p>' + esc(c.why) + '</p>' : '') +
      (c.requirements || []).map(req => '<div class="sm-req' + (req.shown ? ' on' : '') + '"><span>' + (req.shown ? '[x]' : '[ ]') + '</span><span><b>' + esc(req.label) + '</b><br><span class="sm-sub">' + (req.shown ? esc(req.evidence || 'Shown') : 'No evidence found') + '</span></span></div>').join('') +
      '<dl class="sm-facts"><dt>Size</dt><dd>' + esc(c.size || 'Not known') + '</dd><dt>Uses today</dt><dd>' + esc(c.uses || 'Not known') + '</dd>' +
      (c.buyer ? '<dt>Buyer</dt><dd>' + esc([c.buyer.name, c.buyer.title].filter(Boolean).join(', ')) + '</dd>' : '') +
      (/^https?:\/\//.test(c.source || '') ? '<dt>Source</dt><dd><a href="' + esc(c.source) + '" rel="noreferrer noopener">' + esc(c.source.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '')) + '</a></dd>' : '') + '</dl>' +
      '<button class="sm-btn" data-act="clear">Close</button>';
  } else if (chosen.length > 1) {
    h = '<div class="mono">Picked</div><h3>' + chosen.length + ' companies</h3><div class="sm-sub">Average fit ' + Math.round(chosen.reduce((t, c) => t + c.score, 0) / chosen.length) + ' · ' + chosen.filter(c => c.signal).length + ' with a reason to call</div>' +
      chosen.slice().sort((a, b) => b.rank - a.rank).map(c => '<button class="sm-row" data-act="open" data-id="' + esc(c.id) + '"><b>' + esc(c.company) + '</b> ' + squares(c.score) + '<span class="sm-sub">' + esc([c.segment, c.signal, ago(c.days)].filter(Boolean).join(' · ')) + '</span></button>').join('') +
      '<button class="sm-btn" data-act="clear">Clear</button>';
  } else {
    const calls = LIST.filter(c => c.status !== 'tracked').sort((a, b) => b.rank - a.rank).slice(0, 5);
    h = '<div class="mono">Who to call first</div>' + calls.map((c, i) => '<button class="sm-row" data-act="open" data-id="' + esc(c.id) + '"><b>0' + (i + 1) + '  ' + esc(c.company) + '</b> ' + squares(c.score) +
      '<span class="sm-sub">' + esc([c.segment, c.signal, ago(c.days)].filter(Boolean).join(' · ')) + '</span>' + (c.why ? '<span>' + esc(c.why) + '</span>' : '') + '</button>').join('') +
      '<p class="sm-sub" style="margin-top:10px">Click a company on the map to open it.</p>';
  }
  side.innerHTML = h;
}
function drawBar() {
  document.querySelectorAll('[data-lens]').forEach(b => b.setAttribute('aria-pressed', b.dataset.lens === S.lens));
  document.querySelectorAll('[data-size]').forEach(b => b.setAttribute('aria-pressed', b.dataset.size === S.size));
  document.querySelectorAll('[data-quick]').forEach(b => { b.setAttribute('aria-pressed', b.dataset.quick === S.quick); const q = S.quick; S.quick = b.dataset.quick; b.querySelector('i').textContent = LIST.filter(shown).length; S.quick = q; });
  $('sm-how').textContent = LENSES[S.lens];
  const weighed = Object.values(S.weights).some(w => w !== 2);
  $('sm-weights').innerHTML = D.requirements.map(label => { const w = S.weights[label] === undefined ? 2 : S.weights[label]; const checked = LIST.filter(c => (c.requirements || []).some(r => r.label === label)), have = checked.filter(c => c.requirements.find(r => r.label === label).shown).length;
    return '<div class="sm-weight"><span>' + esc(label) + '<small>' + (checked.length ? have + ' of ' + checked.length + ' show it' : 'not checked') + '</small></span><span class="sm-seg">' +
      ['Off', 'Nice', 'Matters', 'Must'].map((word, i) => '<button data-act="weight" data-label="' + esc(label) + '" data-w="' + i + '" aria-pressed="' + (w === i) + '">' + word + '</button>').join('') + '</span></div>'; }).join('') +
    (weighed ? '<button class="sm-btn" data-act="reset" style="margin-top:8px">Back to the fit as recorded</button>' : '');
  $('sm-weighnote').textContent = weighed ? 'Fit is worked out from these weights, for every company that was checked against the requirements.' : 'Change how much a requirement counts and the map re-scores itself. Nothing is saved; reload to start again.';
}
function render() { LIST = companies(); S.sel = S.sel.filter(id => LIST.some(c => c.id === id)); drawBar(); drawMap(); drawSide(); }
function choose(id, many) {
  if (many) S.sel = S.sel.includes(id) ? S.sel.filter(x => x !== id) : S.sel.concat(id);
  else S.sel = S.sel.length === 1 && S.sel[0] === id ? [] : [id];
  render();
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-lens],[data-size],[data-quick],[data-act]'); if (!b) return;
  if (b.dataset.lens) S.lens = b.dataset.lens;
  else if (b.dataset.size) S.size = b.dataset.size;
  else if (b.dataset.quick) S.quick = b.dataset.quick;
  else if (b.dataset.act === 'clear') S.sel = [];
  else if (b.dataset.act === 'open') S.sel = [b.dataset.id];
  else if (b.dataset.act === 'weight') S.weights[b.dataset.label] = Number(b.dataset.w);
  else if (b.dataset.act === 'reset') S.weights = {};
  render();
});
$('sm-find').addEventListener('input', e => { S.find = e.target.value.toLowerCase().trim(); render(); });
gN.addEventListener('click', e => { const g = e.target.closest('.sm-co'); if (g) choose(g.dataset.id, e.shiftKey || e.metaKey || e.ctrlKey); });
gN.addEventListener('keydown', e => { const g = e.target.closest('.sm-co'); if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); choose(g.dataset.id, e.shiftKey); } });
gN.addEventListener('pointerover', e => {
  const g = e.target.closest('.sm-co'); if (!g || e.pointerType === 'touch' || !LAY) return;
  const n = LAY.ns.find(x => x.id === g.dataset.id); if (!n) return; const c = n.c;
  tip.innerHTML = '<b>' + esc(c.company) + '</b>' + squares(c.score) + ' <span class="sm-sub">fit ' + c.score + (c.size ? ' · ' + esc(c.size) : '') + '</span>' + (c.signal ? '<br><span class="tag on">' + esc(c.signal) + '</span><span class="sm-sub">' + esc(ago(c.days)) + '</span>' : '') + (c.why ? '<br><span class="sm-sub">' + esc(c.why) + '</span>' : '');
  tip.hidden = false;
  let x = n.x + n.r + 14; if (x + 236 > LAY.W) x = n.x - n.r - 14 - 232;
  tip.style.left = Math.max(4, x) + 'px'; tip.style.top = clamp(n.y - 24, 4, LAY.H - 120) + 'px';
});
gN.addEventListener('pointerout', e => { const t = e.relatedTarget; if (!t || !t.closest || !t.closest('.sm-co')) tip.hidden = true; });
let drag = null, lassoed = false;
map.addEventListener('pointerdown', e => { if (e.pointerType !== 'mouse' || e.button !== 0 || e.target.closest('.sm-co')) return; const b = map.getBoundingClientRect(); drag = { x: e.clientX - b.left, y: e.clientY - b.top, b, add: e.shiftKey, moved: false, r: null }; });
window.addEventListener('pointermove', e => {
  if (!drag) return; const x = e.clientX - drag.b.left, y = e.clientY - drag.b.top;
  if (Math.abs(x - drag.x) + Math.abs(y - drag.y) > 6) drag.moved = true; if (!drag.moved) return;
  const r = drag.r = { l: Math.min(x, drag.x), t: Math.min(y, drag.y), w: Math.abs(x - drag.x), h: Math.abs(y - drag.y) };
  lasso.style.left = r.l + 'px'; lasso.style.top = r.t + 'px'; lasso.style.width = r.w + 'px'; lasso.style.height = r.h + 'px'; lasso.hidden = false;
});
window.addEventListener('pointerup', () => {
  if (!drag) return; const d = drag; drag = null; lasso.hidden = true;
  if (!d.moved || !d.r || !LAY) return;
  const ids = LAY.ns.filter(n => shown(n.c) && n.x >= d.r.l && n.x <= d.r.l + d.r.w && n.y >= d.r.t && n.y <= d.r.t + d.r.h).map(n => n.id);
  S.sel = d.add ? [...new Set(S.sel.concat(ids))] : ids;
  lassoed = true; setTimeout(() => { lassoed = false; }, 0); render();
});
map.addEventListener('click', e => { if (lassoed || e.target.closest('.sm-co')) return; if (S.sel.length) { S.sel = []; render(); } });
let lastW = 0, wait = 0;
new ResizeObserver(() => { const w = map.clientWidth; if (Math.abs(w - lastW) < 2) return; lastW = w; clearTimeout(wait); wait = setTimeout(drawMap, 60); }).observe(map);
$('sm-live').hidden = false;
if ($('sm-static')) $('sm-static').hidden = true;      // the still picture is there for a browser that runs no scripts
lastW = map.clientWidth; render();
})();
