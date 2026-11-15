(function () {
  "use strict";
  // Stoa's tab in the Hermes dashboard: set-up and status, on one page. The desk itself (the pipeline, the market
  // map, the conversation) is worked in the Hermes Desktop app or Webapp, in Slack and in chat; this tab does not
  // mirror it. It says what is set up, how to set up the rest, and what the desk holds, in totals.
  //
  // Plain React.createElement against the host's Plugin SDK: no build step, no bundled libraries, and no network
  // calls except to this plugin's own /api/plugins/stoa routes. A key is never typed here: the dashboard's own Keys
  // page holds keys, and this tab sends you to it.
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  var React = SDK.React;
  var useState = SDK.hooks.useState, useEffect = SDK.hooks.useEffect, useRef = SDK.hooks.useRef, useCallback = SDK.hooks.useCallback;
  var h = React.createElement;
  var API = "/api/plugins/stoa";
  var POLL_MS = 15000;

  function get(path) { return SDK.fetchJSON(API + path); }
  function post(path, body) {
    return SDK.fetchJSON(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }
  function why(e) { return String((e && e.message) || e || "it did not say why"); }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function usd(n) { return "$" + Number(n || 0).toFixed(2); }
  function size(bytes) {
    if (!bytes) return "empty";
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }
  function day(stamp) {
    var d = new Date(String(stamp || "").slice(0, 10) + "T12:00:00");
    return isNaN(d) ? "" : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function when(stamp) {
    var d = new Date(stamp);
    return !stamp || isNaN(d) ? "" : d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  function saveFile(name, text, type) {
    var url = URL.createObjectURL(new Blob([text], { type: type || "text/plain" }));
    var a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  // Another page of this dashboard, opened the way its own menu opens it (no reload). The tab sits at <base>/stoa,
  // so the dashboard's base path is whatever comes before that.
  function base() { return window.location.pathname.replace(/\/stoa\/?$/, ""); }
  function Go(props) {
    var href = base() + props.to;
    return h("a", { className: "stoa-btn", "data-kind": props.kind || "", href: href, title: props.title,
      onClick: function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;      // a new tab or window is the browser's to open
        e.preventDefault();
        window.history.pushState({}, "", href);
        window.dispatchEvent(new PopStateEvent("popstate"));
      } }, props.children);
  }
  function Btn(props) {
    return h("button", { type: "button", className: "stoa-btn", "data-kind": props.kind || "", disabled: props.disabled, onClick: props.onClick, title: props.title }, props.children);
  }
  function Section(props) {
    return h("section", { className: "stoa-sec" },
      h("div", { className: "stoa-h" }, h("span", null, props.title), props.note ? h("em", null, props.note) : null),
      props.children);
  }
  function Fig(props) {
    return h("div", { className: "stoa-fig" }, h("b", null, props.n), h("span", null, props.label), props.sub ? h("small", null, props.sub) : null);
  }

  // -- the set-up steps -------------------------------------------------------------------------
  // The same six steps the Stoa page shows. Each says where it stands, and opens onto how to do it from here.
  function Step(props) {
    var _o = useState(false), open = _o[0], setOpen = _o[1];
    var c = props.step;
    return h("div", { className: "stoa-step", "data-done": c.done ? "1" : "0", "data-open": open ? "1" : "0" },
      h("button", { type: "button", className: "stoa-stephead", "aria-expanded": open, onClick: function () { setOpen(!open); } },
        h("i", { "aria-hidden": "true" }, c.done ? "✓" : props.n),
        h("span", null, h("b", null, c.title), h("small", null, String(c.detail || "").replace("on its Messaging page", "on the dashboard's Channels page"))),
        h("u", { "aria-hidden": "true" }, open ? "–" : "+")),
      open ? h("div", { className: "stoa-stepbody" }, props.children) : null);
  }

  function Keys(props) {
    var _r = useState({}), results = _r[0], setResults = _r[1];
    var _b = useState(""), busy = _b[0], setBusy = _b[1];
    function test(row) {
      setBusy(row.id);
      post("/key/test", { service: row.id }).then(function (r) {
        var next = {}; for (var k in results) next[k] = results[k];
        next[row.id] = { ok: Boolean(r.ok), text: r.message || r.error || (r.ok ? "The key works." : "The service refused the key.") };
        setResults(next);
      }).catch(function (e) { props.say("Stoa could not test that key: " + why(e)); }).then(function () { setBusy(""); });
    }
    return h("div", null,
      h("p", { className: "stoa-p" }, "Stoa works without any key. Each one adds something. A key is kept by Hermes, on its Keys page; Stoa reads it when it needs it and never shows it."),
      props.keys.map(function (row) {
        var result = results[row.id];
        return h("div", { className: "stoa-key", key: row.id },
          h("div", { className: "stoa-keyhead" },
            h("b", null, row.name), h("span", { className: "stoa-tag", "data-on": row.set ? "1" : "0" }, row.set ? "Set" : "Not set"),
            h("span", { className: "stoa-grow" }),
            row.set ? h(Btn, { kind: "ghost", disabled: busy === row.id, title: row.check, onClick: function () { test(row); } }, busy === row.id ? "Testing…" : "Test") : null),
          h("p", { className: "stoa-p" }, row.unlocks + " " + row.cost),
          row.note ? h("p", { className: "stoa-p" }, row.note) : null,
          result ? h("p", { className: "stoa-p", "data-tone": result.ok ? "good" : "bad" }, result.text) : null,
          row.set ? null : h("ol", { className: "stoa-how" },
            h("li", null, row.steps, " ", h("a", { href: row.get, target: "_blank", rel: "noreferrer" }, "Open " + row.where)),
            h("li", null, "Press ", h("b", null, "Open Keys"), " below and go to ", h("b", null, "Custom Keys"), " at the foot of that page."),
            h("li", null, "Type ", h("code", null, row.env), " as the name, paste the key as its value, and add it."),
            h("li", null, "Come back here. The row shows ", h("b", null, "Set"), ", and ", h("b", null, "Test"), " has the service confirm it.")));
      }),
      h("div", { className: "stoa-btns" }, h(Go, { to: "/env", kind: "primary", title: "The dashboard's own page for keys. Stoa's are added under Custom Keys." }, "Open Keys")));
  }

  function Notes(props) {
    var notes = props.notes || {};
    var _f = useState(notes.folder || ""), folder = _f[0], setFolder = _f[1];
    var _m = useState(null), found = _m[0], setFound = _m[1];
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    function run(promise) { setBusy(true); return promise.catch(function (e) { props.say("Stoa could not do that: " + why(e)); }).then(function () { setBusy(false); }); }
    function check() { return run(post("/notes/check", { folder: folder }).then(setFound)); }
    function save(value) {
      return run(post("/notes/folder", { folder: value }).then(function (r) {
        props.say(r.ok ? (value ? "Stoa now reads call notes from that folder." : "Stoa no longer reads a notes folder.") : r.error || r.message || "Stoa could not save that.");
        if (r.ok) { setFound(null); return props.refresh(); }
      }));
    }
    function look() {
      return run(get("/notes/look").then(function (r) {
        var first = (r.found || [])[0];
        if (first) { setFolder(first.folder); setFound({ ok: true, count: first.count, message: "Found " + plural(first.count, "note", "notes") + " in " + first.folder + ". Press Save to use it." }); }
        else setFound({ ok: false, message: r.message || "No folder of notes turned up in the usual places. Paste the folder's path above." });
      }));
    }
    return h("div", null,
      h("p", { className: "stoa-p" }, "Each note is filed on the record it is about, with the next step it implies. Notes can come from a folder on the computer Hermes runs on (files ending in .md or .txt, such as an Obsidian folder), from Granola, or both."),
      h("label", { className: "stoa-label", htmlFor: "stoa-notes" }, "The folder your notes are saved in"),
      h("div", { className: "stoa-line" },
        h("input", { id: "stoa-notes", className: "stoa-input", value: folder, placeholder: "/Users/you/Documents/Meeting notes", spellCheck: false,
          onChange: function (e) { setFolder(e.target.value); setFound(null); } }),
        h(Btn, { disabled: busy || !folder.trim(), onClick: check }, "Check"),
        h(Btn, { kind: "primary", disabled: busy || !folder.trim() || notes.can_save === false, onClick: function () { save(folder.trim()); } }, "Save")),
      h("p", { className: "stoa-p" }, "On a Mac, to copy a folder's path: in Finder, right-click the folder, hold the Option key and choose Copy as Pathname."),
      found ? h("p", { className: "stoa-p", "data-tone": found.ok ? "good" : "bad" }, found.message || (found.ok ? plural(found.count || 0, "note", "notes") + " found there." : "Stoa cannot read that folder.")) : null,
      notes.folder && !found ? h("p", { className: "stoa-p", "data-tone": notes.ok ? "good" : "bad" }, notes.ok ? "Reading " + plural(notes.count || 0, "note", "notes") + " from " + notes.folder + "." : notes.message || "Stoa cannot read " + notes.folder + ".") : null,
      notes.can_save === false ? h("p", { className: "stoa-p" }, "This page cannot save the setting here. Ask Hermes: “Set Stoa's notes folder to " + (folder.trim() || "…") + "”.") : null,
      h("div", { className: "stoa-btns" },
        h(Btn, { kind: "ghost", disabled: busy, title: "Tries Documents, Obsidian and Notes, and offers any that hold notes. It counts the notes and opens none.", onClick: look }, "Look for a folder"),
        notes.folder ? h(Btn, { kind: "ghost", disabled: busy, onClick: function () { setFolder(""); save(""); } }, "Stop reading this folder") : null),
      h("p", { className: "stoa-p" }, notes.granola ? "Granola: a key is set, so your Granola notes are read too." : "Granola: add a Granola key under step 3 and its notes are read too."));
  }

  function Jobs(props) {
    var jobs = props.jobs || [], auto = props.autonomy;
    var words = { on: "On", paused: "Paused", off: "Off", unknown: "Not known from here" };
    return h("div", null,
      h("p", { className: "stoa-p" }, "Jobs Hermes can run on a schedule. To switch one on, type ", h("code", null, "/blueprint"), " in a Hermes chat and pick it; Hermes asks when it should run and where to report. Pausing, changing or removing one is done on the dashboard's Cron page."),
      h("div", { className: "stoa-rows" }, jobs.map(function (job) {
        return h("div", { className: "stoa-row", key: job.name },
          h("span", null, h("b", null, job.title), h("small", null, job.description)),
          h("span", { className: "stoa-tag", "data-on": job.state === "on" ? "1" : "0" }, words[job.state] || job.state),
          h("small", { className: "stoa-next" }, job.state === "on" && when(job.next) ? "next " + when(job.next) : ""));
      })),
      auto ? h("div", { className: "stoa-auto" },
        h("label", { className: "stoa-label", htmlFor: "stoa-auto" }, "What a scheduled job may change without asking"),
        h("select", { id: "stoa-auto", className: "stoa-input", value: auto.level, onChange: function (e) {
          post("/autonomy", { level: e.target.value }).then(function () { props.say("Saved."); return props.refresh(); }).catch(function (err) { props.say("Stoa could not save that: " + why(err)); });
        } }, auto.levels.map(function (level) { return h("option", { key: level.id, value: level.id }, level.label); })),
        h("p", { className: "stoa-p" }, (auto.levels.filter(function (level) { return level.id === auto.level; })[0] || {}).summary || "")) : null,
      h("div", { className: "stoa-btns" }, h(Go, { to: "/cron", title: "The dashboard's own page for scheduled jobs" }, "Open Cron")));
  }

  function Steps(props) {
    var data = props.data, sets = data.playbook_sets || {};
    var by = {}; data.checklist.forEach(function (c) { by[c.id] = c; });
    var left = data.checklist.filter(function (c) { return !c.done; }).length;
    return h(Section, { title: "Set up", note: left ? left + " of " + data.checklist.length + " left" : "all done" },
      h(Step, { n: 1, step: by.playbook },
        h("p", { className: "stoa-p" }, "The pipeline is on ", h("b", null, data.playbook.title), (data.playbook.shape_title ? " (" + data.playbook.shape_title.toLowerCase() + ")" : "") + ". Its stages:"),
        h("p", { className: "stoa-stages" }, (sets.stages || []).map(function (label, i) { return h("span", { key: i }, label); })),
        h("p", { className: "stoa-p" }, "To change what the pipeline is for or rename its stages, open Stoa in the Hermes Desktop app or Webapp and go to Setup → Edit stages. Or tell Hermes what your business does and it sets the stages for you.")),
      h(Step, { n: 2, step: by.records },
        h("p", { className: "stoa-p" }, "Records are added where the desk is worked, in any of these ways:"),
        h("ul", { className: "stoa-how" },
          h("li", null, h("b", null, "Tell Hermes."), " “We are starting a Staff Backend search for Northwind. Add Dana Whitfield, call her Friday.”"),
          h("li", null, h("b", null, "Drop a list on the Stoa page."), " A CSV of leads or candidates. Nothing is written until you press Apply."),
          h("li", null, h("b", null, "Add one by hand."), " + New at the top of the Stoa page."),
          h("li", null, h("b", null, "Map a market."), " “We sell a fraud prevention tool to fintechs. Who should we go after?” One press puts a company from the map in your pipeline."))),
      h(Step, { n: 3, step: by.keys }, h(Keys, { keys: data.keys || [], say: props.say })),
      h(Step, { n: 4, step: by.notes }, h(Notes, { notes: data.notes, say: props.say, refresh: props.refresh })),
      h(Step, { n: 5, step: by.slack },
        h("p", { className: "stoa-p" }, data.slack ? "Hermes is in your Slack. Records post as cards whose buttons move the pipeline, and !gtm shows the board. Try !gtm map."
          : "Slack is connected in Hermes, not in Stoa. Open Channels, choose Slack and follow its steps. Once Hermes is in your Slack there is nothing more to do: records post as cards whose buttons move the pipeline, and !gtm shows the board."),
        h("div", { className: "stoa-btns" }, h(Go, { to: "/channels", kind: data.slack ? "" : "primary", title: "The dashboard's own page for messaging platforms" }, "Open Channels"))),
      h(Step, { n: 6, step: by.sweep }, h(Jobs, { jobs: data.automations, autonomy: data.autonomy, say: props.say, refresh: props.refresh })));
  }

  // -- what the desk holds ------------------------------------------------------------------------
  function Holds(props) {
    var o = props.overview, t = o.totals, s = o.spend;
    return h(React.Fragment, null,
      h(Section, { title: "On the desk", note: o.pipelines.length > 1 ? plural(o.pipelines.length, "pipeline", "pipelines") : "" },
        h("div", { className: "stoa-figs" },
          h(Fig, { n: t.records, label: t.records === 1 ? "record" : "records", sub: t.open + " open · " + t.won + " won · " + t.lost + " lost" }),
          h(Fig, { n: t.people, label: t.people === 1 ? "person" : "people", sub: "named on those records" }),
          h(Fig, { n: t.maps, label: t.maps === 1 ? "market map" : "market maps", sub: o.tour ? "not counted while sample data is on show" : plural(t.mapped, "company", "companies") + " mapped · " + t.from_maps + " in the pipeline" }),
          h(Fig, { n: t.activities, label: "log entries", sub: t.lists_waiting ? plural(t.lists_waiting, "list", "lists") + " waiting for review" : "calls, emails, notes and moves" }))),
      h(Section, { title: "This month", note: "paid work, for " + new Date(o.month + "-15T12:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" }) },
        h("div", { className: "stoa-figs" },
          h(Fig, { n: s.usd_month === null ? "–" : usd(s.usd_month), label: "Hermes's work on maps",
            sub: s.usd_month === null ? (s.runs ? "the model in use has no price Hermes knows" : "nothing counted yet") : "Hermes's own estimate, not an invoice · " + usd(s.usd) + " in all" }),
          h(Fig, { n: s.lookups_month, label: "People Data Labs lookups", sub: s.lookup_cap ? "of the " + s.lookup_cap + " a month allowed" : "no monthly cap set" }),
          h(Fig, { n: s.searches_month, label: "Exa searches", sub: "made through Stoa for a map" }))));
  }

  function Pipelines(props) {
    var rows = props.overview.pipelines;
    return h(Section, { title: rows.length > 1 ? "Pipelines" : "Pipeline" },
      h("div", { className: "stoa-scroll" }, h("table", { className: "stoa-table" },
        h("thead", null, h("tr", null, ["Name", "Stages", "Open", "Won", "Lost", "People", "File", "Daily copies"].map(function (label, i) {
          return h("th", { key: label, "data-num": i >= 2 && i <= 5 ? "1" : "0" }, label);
        }))),
        h("tbody", null, rows.map(function (row) {
          return h("tr", { key: row.id },
            h("td", null, h("b", null, row.name), row.name !== row.playbook ? h("small", null, row.playbook) : null),
            h("td", null, h("small", null, row.stages.join(" → "))),
            h("td", { "data-num": "1" }, row.open), h("td", { "data-num": "1" }, row.won), h("td", { "data-num": "1" }, row.lost), h("td", { "data-num": "1" }, row.people),
            h("td", null, h("code", null, row.file), h("small", null, size(row.size))),
            h("td", null, row.backups.count ? h("small", null, row.backups.count + ", newest " + day(row.backups.newest)) : h("small", null, "none yet")));
        })))));
  }

  function Maps(props) {
    var o = props.overview, rows = o.maps;
    var limitWords = function (limits) {
      var out = [];
      if (limits.companies) out.push(limits.companies + " companies");
      if (limits.budget) out.push(usd(limits.budget) + " a month");
      if (limits.lookups) out.push(plural(limits.lookups, "lookup", "lookups") + " a month");
      if (limits.searches) out.push(plural(limits.searches, "search", "searches") + " a month");
      return out.join(" · ") || "none";
    };
    return h(Section, { title: "Market maps", note: rows.length ? "limits are set on the Stoa page, under Map facts" : "" },
      o.tour ? h("p", { className: "stoa-p" }, "Sample data is on show, so your own maps are not listed. Leave the tour to see them.")
        : !rows.length ? h("p", { className: "stoa-p" }, "No map yet. In a Hermes chat, or on the Stoa page's Market map tab, say what you sell and who you are after; Hermes researches it and saves the map.")
        : h("div", { className: "stoa-scroll" }, h("table", { className: "stoa-table" },
          h("thead", null, h("tr", null, ["Map", "Companies", "In pipeline", "Buyers named", "Hermes's work", "This month", "Lookups", "Searches", "Limits"].map(function (label, i) {
            return h("th", { key: label, "data-num": i >= 1 && i <= 7 ? "1" : "0" }, label);
          }))),
          h("tbody", null, rows.map(function (row) {
            return h("tr", { key: row.pipeline + ":" + row.id },
              h("td", null, h("b", null, row.name), h("small", null, (o.pipelines.length > 1 ? row.pipeline + " · " : "") + plural(row.segments, "segment", "segments") + (row.updated ? " · changed " + day(row.updated) : ""))),
              h("td", { "data-num": "1" }, row.companies), h("td", { "data-num": "1" }, row.in_pipeline), h("td", { "data-num": "1" }, row.buyers),
              h("td", { "data-num": "1", title: plural(row.runs, "turn", "turns") + " counted" }, row.runs ? (row.usd === null ? "no price" : usd(row.usd)) : "–"),
              h("td", { "data-num": "1" }, row.runs && row.usd_month !== null ? usd(row.usd_month) : "–"),
              h("td", { "data-num": "1" }, row.lookups), h("td", { "data-num": "1" }, row.searches),
              h("td", null, h("small", null, limitWords(row.limits))));
          })))));
  }

  function Rules(props) {
    return h(Section, { title: "Rules in force", note: "from the plugin's settings" },
      h("dl", { className: "stoa-dl" }, props.data.rules.map(function (rule) {
        return h(React.Fragment, { key: rule.label }, h("dt", null, rule.label), h("dd", null, rule.value));
      })));
  }

  function Data(props) {
    var data = props.data, d = data.data, o = props.overview;
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    function run(promise) { setBusy(true); return promise.catch(function (e) { props.say("Stoa could not do that: " + why(e)); }).then(function () { setBusy(false); }); }
    function tour(action) {
      return run(post("/tour", { action: action }).then(function (r) {
        props.say(action === "leave" ? "Back on your own desk." : (r.removed || 0) + " sample records removed from your desk.");
        return props.refresh();
      }));
    }
    return h(Section, { title: "Your data" },
      h("dl", { className: "stoa-dl" },
        h("dt", null, "Where it is kept"), h("dd", null, "SQLite files on the machine Hermes runs on, in ", h("code", null, o.folder), " (" + size(o.totals.size) + " in all). Nothing is kept anywhere else."),
        h("dt", null, "Daily copies"), h("dd", null, d.backups && d.backups.count ? d.backups.count + " kept in " : "None yet. They are kept in ", h("code", null, (d.backups && d.backups.folder) || ""), d.backups && d.backups.newest ? ", the newest from " + day(d.backups.newest) + "." : "."),
        h("dt", null, "Exports and saved maps"), h("dd", null, h("code", null, d.exports)),
        h("dt", null, "History"), h("dd", null, plural(d.ledger, "change", "changes") + " on the ledger, which Undo works from" + (d.archived ? "; " + plural(d.archived, "record", "records") + " taken off the desk and archived, not erased" : "") + "."),
        data.tour ? h(React.Fragment, null, h("dt", null, "Sample data"), h("dd", null, "On show: " + data.tour.title + ". It lives in a file of its own, and your desk comes back when you leave.")) : null,
        d.samples && !data.tour ? h(React.Fragment, null, h("dt", null, "Sample records"), h("dd", null, d.samples + " sample records are mixed into your own desk. An earlier version of Stoa put them there.")) : null),
      h("div", { className: "stoa-btns" },
        h(Btn, { disabled: busy, title: "Every record on the pipeline, as a CSV file saved by your browser", onClick: function () { run(get("/export").then(function (f) { saveFile(f.filename, f.csv, "text/csv"); props.say(plural(f.rows, "record", "records") + " exported."); })); } }, "Export the pipeline as CSV"),
        data.tour ? h(Btn, { kind: "primary", disabled: busy, onClick: function () { tour("leave"); } }, "Leave the tour") : null,
        d.samples && !data.tour ? h(Btn, { disabled: busy, onClick: function () { tour("tidy"); } }, "Remove the sample records") : null));
  }

  // -- the page -----------------------------------------------------------------------------------
  function StoaPage() {
    var _d = useState(null), data = _d[0], setData = _d[1];
    var _o = useState(null), overview = _o[0], setOverview = _o[1];
    var _e = useState(""), error = _e[0], setError = _e[1];
    var _t = useState(""), toast = _t[0], setToast = _t[1];
    var timer = useRef(null);
    var refresh = useCallback(function () {
      return Promise.all([get("/setup"), get("/overview")]).then(function (both) { setData(both[0]); setOverview(both[1]); setError(""); })
        .catch(function (e) { setError(why(e)); });
    }, []);
    useEffect(function () {
      refresh();
      var id = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
      return function () { clearInterval(id); clearTimeout(timer.current); };
    }, []);
    var say = useCallback(function (text) {
      setToast(text); clearTimeout(timer.current); timer.current = setTimeout(function () { setToast(""); }, 5200);
    }, []);

    var body;
    if (!data || !overview) {
      body = h("div", { className: "stoa-blank" }, h("h3", null, error ? "The desk is not answering" : "Opening…"),
        error ? h("p", { className: "stoa-p" }, error + ". If Stoa was just installed or updated, restart the gateway (hermes gateway restart) and reload this page.") : null);
    } else {
      body = h(React.Fragment, null,
        error ? h("div", { className: "stoa-note", role: "status" }, "The last check did not get through (" + error + "). What is below may be a little old.") : null,
        data.tour ? h("div", { className: "stoa-note", role: "status" }, h("span", null, h("b", null, "Sample data is on show"), " · " + data.tour.title + ". Everyone on it is made up, and your own desk is untouched."),
          h(Btn, { onClick: function () { post("/tour", { action: "leave" }).then(function () { say("Back on your own desk."); return refresh(); }).catch(function (e) { say(why(e)); }); } }, "Leave the tour")) : null,
        (data.problems || []).length ? h("div", { className: "stoa-note", "data-tone": "bad", role: "alert" },
          h("span", null, h("b", null, "Some of this could not be read."), " " + data.problems.join(" · "))) : null,
        h(Holds, { overview: overview }),
        h(Steps, { data: data, say: say, refresh: refresh }),
        h(Pipelines, { overview: overview }),
        h(Maps, { overview: overview }),
        h(Rules, { data: data }),
        h(Data, { data: data, overview: overview, say: say, refresh: refresh }));
    }
    return h("div", { className: "stoa" },      // colours and type come from the dashboard's own theme
      h("header", { className: "stoa-head" },
        h("div", null,
          h("div", { className: "stoa-eyebrow" }, "Stoa" + (data ? " · " + data.version : "")),
          h("h2", { className: "stoa-title" }, "Setup and status"),
          h("p", { className: "stoa-sub" }, "The desk is worked in the Hermes Desktop app or Webapp (Stoa in the sidebar), in Slack, and in any chat with /gtm. This tab is where you set it up and see what it holds."))),
      body,
      toast ? h("div", { className: "stoa-toast", role: "status" }, toast) : null);
  }

  window.__HERMES_PLUGINS__.register("stoa", StoaPage);
})();
