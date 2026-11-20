/**
 * Stoa in the Hermes Desktop app and the Webapp.
 *
 * One file, no build step. It adds a Stoa page (Today, the pipeline, the market map, the activity
 * log and Setup), a live pane that sits beside the conversation, and five components the agent
 * can draw inside a reply: a record card, a market map, the review of a list someone sent in, the
 * day (Today's lists, with their buttons) and one report. Each names its pipeline when the desk has
 * more than one, so it shows that pipeline whatever the page is showing.
 *
 * Everything here talks to the plugin's own backend (/api/plugins/stoa, the same routes the web
 * dashboard tab uses) through ctx.rest. A button or a form saves at once, with no model involved;
 * when a click calls for writing, one line is sent into the open chat (host.composer.submit, the
 * same as typing it) and the agent writes the draft there. The page, which has no chat beside it,
 * keeps a conversation of its own for its talk box (see "talking to the desk" below).
 */
import * as SDK from '@hermes/plugin-sdk'
import { atom, host, queryClient, useQuery, useValue } from '@hermes/plugin-sdk'
import { Component, createContext, createElement as h, useContext, useEffect, useMemo, useRef, useState } from 'react'

let rest = null // ctx.rest, set in register()
let storage = null // ctx.storage, set in register()
// The talk box is a thin shell over Hermes's own parts wherever the app hands them to a plugin: the model menu the
// real chat box uses, the app's message renderer, and the gateway's own way of attaching a file. Each is looked up
// rather than assumed, so on a Hermes without one the page falls back to its own plain version instead of breaking.
const { ModelCatalogMenu, ModelMenuCloseContext, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, MessageTextContent } = SDK
const APP_MENU = Boolean(ModelCatalogMenu && ModelMenuCloseContext && DropdownMenu && DropdownMenuContent && DropdownMenuItem && DropdownMenuTrigger)

/** Draws `children`; if they throw while drawing, draws `fallback` instead and says so once in the console. The app's
    parts are the app's to change, and a change there must cost the page a nicer menu, never the page. */
class Guard extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error) { console.warn('[stoa] ' + (this.props.what || 'a part of the app') + ' could not be drawn; using the plain one.', error) }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

// What the pane and the page are narrowed to. One store, so both show the same thing. `stage` is
// not a filter: it is the stage picked out on the board, where every column keeps its cards.
const NO_FILTER = { text: '', owner: '', group: '', stage: '', due: false, quiet: false }
const $filter = atom({ ...NO_FILTER })
function setFilter(patch) {
  const next = { ...$filter.get(), ...patch }
  $filter.set(next)
  // Only whose records and which group are remembered between visits. Anything that hides records
  // for a moment (a search, due, quiet, a picked-out stage) starts clear, so nothing looks missing.
  if (storage) storage.set('filter', { owner: next.owner, group: next.group })
}
const filtering = f => Boolean(f.text || f.owner || f.group || f.due || f.quiet)
function matches(deal, f) {
  if (f.owner && deal.owner !== f.owner) return false
  if (f.group && deal.group !== f.group) return false
  if ((f.due || f.quiet) && !((f.due && deal.due) || (f.quiet && deal.stale && !deal.due))) return false
  if (!f.text) return true
  return [deal.name, deal.company, deal.group, deal.next_step, deal.owner, deal.stage_label].join(' ').toLowerCase().includes(f.text.toLowerCase())
}
const STATE = ['stoa', 'state']
// The pipeline a part drawn in a chat belongs to ('main', 'p2', ...), handed down to the record, map and report inside it so
// every read and every button names it. Null on the page and in the pane: they follow the page's own pipeline ($pipe).
const InPipe = createContext(null)
const inPipe = pipe => (pipe ? { pipeline: pipe } : {})
const refresh = () => queryClient.invalidateQueries({ queryKey: ['stoa'] })
const $tab = atom('today') // the page's tab: today | pipeline | database | cash | market | reports | activity | setup
const $draft = atom('') // words put in the dock for the person to finish and send
let onPage = false // the Stoa page is what is on screen: a click that calls for writing goes to its dock
const $onPage = atom(false) // the same, for the pane to watch: beside the open desk it shows other things
const $want = atom('') // a record picked in the pane while the desk is open: the page opens it
const $view = atom('board') // the pipeline tab: board | companies | groups
const $pipe = atom('') // which pipeline the page is showing ('' = the first): an id from state.pipelines
const $last = atom(null) // what was just done, while it can be taken back: { deal, name, text, op }
let lastTimer = 0

/** Offer to take back what was just done. The offer goes away by itself. */
function offerUndo(last) {
  if (!last || !onPage) return
  $last.set(last)
  clearTimeout(lastTimer)
  lastTimer = setTimeout(() => { if ($last.get() === last) $last.set(null) }, 14000)
}

/** A record taken off the desk (its add was undone, or someone deleted it) comes back, log and all. */
async function putBack(dealId, pipe) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/restore', { method: 'POST', body: {}, ...inPipe(pipe) })
    refresh()
    host.notify({ kind: 'success', message: result.name + ' is back on the desk.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not put that back')
  }
}

async function undo(dealId, op, pipe) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/undo', { method: 'POST', body: { op: op || '' }, ...inPipe(pipe) })
    $last.set(null)
    refresh()
    host.notify({ kind: 'info', message: 'Taken back: ' + result.undid + (result.removed ? '. ' + result.name + ' is off the desk.' : '') })
    return result
  } catch (error) {
    $last.set(null)
    refresh()
    host.notifyError(error, 'Not taken back')     // nothing left to undo, or it would overwrite a later change (the message says which)
    return null
  }
}

// -- small helpers -------------------------------------------------------------------------------
const SYMBOL = { USD: '$', EUR: '€', GBP: '£' }

function money(value, currency) {
  if (!value) return ''
  const sign = SYMBOL[currency] ?? ''
  const text = value >= 1e6 ? (value / 1e6).toFixed(1) + 'M' : value >= 1e4 ? Math.round(value / 1e3) + 'k' : Math.round(value).toLocaleString()
  return sign ? sign + text : text + ' ' + (currency || '')
}

function today() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
}

function ago(stamp) {
  const then = Date.parse(stamp || '')
  if (!then) return ''
  const hours = (Date.now() - then) / 36e5
  if (hours < 1) return 'just now'
  if (hours < 24) return Math.floor(hours) + 'h ago'
  if (hours < 48) return 'yesterday'
  return Math.floor(hours / 24) + 'd ago'
}

function dueLabel(deal) {
  if (!deal.next_due) return ''
  if (deal.next_due < today()) return 'overdue'
  if (deal.next_due === today()) return 'today'
  const d = new Date(deal.next_due + 'T12:00:00')
  return isNaN(d) ? deal.next_due : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const dueTone = deal => (deal.due ? (deal.next_due < today() ? 'bad' : 'warn') : '')
const cap = text => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '')
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many)

/** The desk as the page shows it; with `pipe` (an id from state.pipelines), as that pipeline has it, whatever the page is showing. */
function useDesk(pipe) {
  const own = typeof pipe === 'string' && pipe !== ''
  return useQuery({ queryKey: own ? ['stoa', 'state', 'in', pipe] : STATE, queryFn: () => rest('/state', own ? { pipeline: pipe } : undefined),
    refetchInterval: 5000, staleTime: 1500 })
}

/** Send one line into the chat, as if typed: the conversation the card is in, else the one in focus. */
function toChat(text, sessionId, lane) {
  if (onPage) {
    if ($talk.get().busy) {
      host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Ask again when it has answered.' })
      return false
    }
    say(text, null, lane === 'maps' ? 'maps' : 'desk')
    return true
  }
  const composer = host.composer
  let sent = false
  try {
    const focused = host.state.focusedSessionId ? host.state.focusedSessionId.get() : null
    for (const address of [sessionId, focused, null, 'new']) {
      if (sent || address === undefined || !composer) continue
      sent = composer.submit(address, text) === true
    }
  } catch {
    sent = false
  }
  if (!sent) host.notify({ kind: 'info', message: 'Saved. The draft is waiting on the record: open a chat and ask for it.' })
  return sent
}

async function act(dealId, body, sessionId, pipe) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/act', { method: 'POST', body, ...inPipe(pipe) })
    refresh()
    if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    if (result && result.ask) toChat(result.ask, sessionId)
    if (result && result.last) offerUndo(result.last)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

async function track(target, sessionId, pipe) {
  try {
    const result = await rest('/target/' + encodeURIComponent(target.id) + '/track', { method: 'POST', body: {}, ...inPipe(pipe) })
    refresh()
    if (result && result.ask) toChat(result.ask, sessionId)
    else if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    else host.notify({ kind: 'success', message: target.company + ' is in the pipeline.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not add that company')
  }
}

// -- a command typed on the page ----------------------------------------------------------------------
// A line that starts with "/" is a Hermes command (/blueprint, /gtm today). The app's own chat box runs those. The
// desk's talk box cannot: it hands every line to the agent as words, where "/blueprint" means nothing. So a command
// is taken to a new chat, typed there and left for the person to send.
const isCommand = line => /^\/[a-z][\w:-]*(\s|$)/i.test(String(line || '').trim())      // "/gtm today" is one; "/Users/me/list.csv" is a path

async function typeInChat(line) {
  const composer = host.composer
  const pause = ms => new Promise(done => setTimeout(done, ms))
  try {
    if (composer && typeof composer.setDraft === 'function' && typeof host.newChat === 'function') {
      host.newChat()
      for (let tries = 0; tries < 24; tries++) {
        await pause(125)
        if (await composer.setDraft('new', line)) {
          if (typeof composer.focus === 'function') composer.focus('new')
          host.notify({ kind: 'info', message: 'Commands run in a chat, so here is one with ' + line + ' typed. Press Enter to run it.' })
          return true
        }
      }
    }
  } catch { /* an app without these: say how instead */ }
  let copied = false
  try { await navigator.clipboard.writeText(line); copied = true } catch { /* no clipboard here */ }
  host.notify({ kind: 'info', message: copied ? 'Copied ' + line + '. Commands run in a chat: open a new one and paste it there.'
    : 'Commands run in a chat: open a new one and type ' + line + ' there.' })
  return false
}

// -- talking to the desk from its own page -------------------------------------------------------
// The page has no conversation beside it, so it keeps one of its own: an ordinary session named
// "Stoa desk", made and resumed through the gateway the way the app makes any session, and listed
// in the sidebar like any other. What is typed (or dictated) into the dock at the foot of the page
// is submitted to it, and the reply is read off the gateway's event stream (see register) and
// shown above the dock.
const $talk = atom({ busy: false, open: false, turns: [] })
const $attached = atom([]) // files waiting to go with the next message: { name, text } for a small text file, { name, file, image } for anything else
// The page keeps two conversations of its own. The desk's is the talk box: what you say, and what the buttons on a
// record ask for. The map's is research: starting a map, growing it, filling it in. They are kept apart so each can
// run on the model that suits it (a small one is plenty for "what needs me today"; finding and checking companies
// wants the strongest) and so neither has to re-read the other's history at every step.
// `key` is where each is kept in the page's storage; `left` is the conversation New conversation walked away from,
// until the next one exists.
const LANES = {
  desk: { key: 'talk', title: 'Stoa desk', name: 'The desk\'s conversation', where: 'under the talk box' },
  maps: { key: 'research', title: 'Stoa map research', name: 'Map research', where: 'beside Map facts' }
}
const sessions = { desk: { runtime: '', stored: '', primed: false, left: '' }, maps: { runtime: '', stored: '', primed: false, left: '' } }
const talking = { turn: 0, lane: 'desk' }      // the turn in flight, and which conversation it went to
const setTalk = patch => $talk.set({ ...$talk.get(), ...patch })
const kept = lane => (storage && storage.get(LANES[lane].key, {})) || {}
const keep = (lane, patch) => { if (storage) storage.set(LANES[lane].key, { ...kept(lane), ...patch }) }
// The model each conversation runs on. A conversation in Hermes keeps the model it began with, so the page keeps the
// person's pick on this computer and gives it to the conversation: the one it has, and any it starts afterwards.
// A pick is { model, provider }, or null for whatever Hermes's own default is; the map's research does not follow the
// desk's, so a cheap model picked for the talk box never does the market research. `asking` holds a pick Hermes
// wants a second yes for, with Hermes's own words for why.
const $model = atom({ desk: null, maps: null, asking: null })
const setModel = patch => $model.set({ ...$model.get(), ...patch })

const newId = () => (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID()
  : 'xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx'.replace(/x/g, () => Math.floor(Math.random() * 16).toString(16)))

/** Whether the page has that conversation in hand: open now, or kept from an earlier visit. */
const hasTalk = (lane = 'desk') => Boolean(sessions[lane].runtime || sessions[lane].stored || kept(lane).stored)

/** Put a conversation on a model, the way the app's own picker does (`config.set model "<model> --provider <id> --session"`,
    the one form the app turns into a change to that conversation alone; a bare "reset" is refused, seen on a real run). */
async function modelTo(sessionId, pick, sure) {
  const result = await host.request('config.set', { key: 'model', session_id: sessionId, value: pick.model + ' --provider ' + pick.provider + ' --session', ...(sure ? { confirm_expensive_model: true } : {}) })
  if (pick.effort && !(result && result.confirm_required)) await effortTo(sessionId, pick.effort)
  return result || {}
}

/** How hard a conversation thinks, set the way the app's own menu sets it (`config.set reasoning <level>` for that
    conversation). A level Hermes will not take for the model is said, and the model pick stands. */
async function effortTo(sessionId, effort) {
  try { await host.request('config.set', { key: 'reasoning', session_id: sessionId, value: effort }) } catch (error) {
    host.notify({ kind: 'warning', message: 'Hermes did not set thinking to ' + effort + ': ' + why(error) })
  }
}

/** The person changed the thinking level on a row of the model menu. On the model in use it changes only that; on
    another row it is a pick of that model at that level. */
async function pickEffort(row, effort, lane = 'desk') {
  const now = $model.get()[lane]
  if (!row.isActive || !now || now.model !== row.model || now.provider !== row.provider) return pickModel({ model: row.model, provider: row.provider, effort }, false, null, lane)
  if ($talk.get().busy) { host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Change it when it has answered.' }); return false }
  const pick = { ...now, effort }
  if (hasTalk(lane)) await effortTo(await talkOpen(true, lane), effort)
  keep(lane, { model: pick })
  setModel({ [lane]: pick })
  return true
}

/** The person picked a model for one of the page's conversations. Kept for later ones; the one in hand is switched
    now. `pick` null is "Hermes's default": nothing is kept, and the conversation in hand goes to `usual`, the
    profile's own model. */
async function pickModel(pick, sure, usual, lane = 'desk') {
  if ($talk.get().busy) { host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Change the model when it has answered.' }); return false }
  const target = pick || usual
  try {
    if (hasTalk(lane) && target && target.model && target.provider) {
      const result = await modelTo(await talkOpen(true, lane), target, sure)
      if (result.confirm_required) { setModel({ asking: { lane, pick, usual, message: String(result.confirm_message || 'Hermes asks you to confirm this model.') } }); return true }
      if (result.warning) host.notify({ kind: 'warning', message: String(result.warning) })
    }
    keep(lane, { model: pick })
    setModel({ [lane]: pick, asking: null })
    host.notify({ kind: 'success', message: LANES[lane].name + (pick ? ' now runs on ' + pick.model + '.' : ' is back on Hermes\'s default model.') })
    return true
  } catch (error) {
    host.notifyError(error, 'Hermes could not change the model')
    return false
  }
}

/** Leave a conversation and begin another with the next thing sent to it. The old one stays in the sidebar. */
function freshTalk(lane = 'desk') {
  if ($talk.get().busy) return host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Start a new conversation when it has answered.' })
  const session = sessions[lane]
  const left = session.stored || kept(lane).stored || session.left || ''
  Object.assign(session, { runtime: '', stored: '', primed: false, left })
  keep(lane, { stored: '', left })
  if (lane === 'desk') $talk.set({ busy: false, open: false, turns: [] })
  host.notify({ kind: 'success', message: lane === 'desk' ? 'The next thing you say starts a new conversation. The old one is still in the sidebar.'
    : 'The next thing you ask of a map starts a new research conversation. The old one is still in the sidebar.' })
}
const PRIME = 'This conversation is the talk box on the Stoa desk page. Act on the Stoa desk with the gtm tools, and answer in a few '
  + 'short lines: the page already shows the pipeline. What is said here is for the desk: do not also write it to another system '
  + '(a spreadsheet, another CRM, email) unless asked to.\n\n'
const PRIME_MAPS = 'This conversation is the Stoa page\'s research on market maps. Each request names the map it is about. Do the research with '
  + 'your web tools and gtm_market, record what you find on that map with its source, and answer in a few short lines: the page '
  + 'already shows the map. Never guess a fact; leave out what no source says.\n\n'

/** The name of the pipeline the page is showing, when the desk has more than one ('' otherwise). */
function onScreen() {
  const state = queryClient.getQueryData(STATE)
  const pipes = (state && state.pipelines) || []
  if (pipes.length < 2) return ''
  const found = pipes.find(item => item.id === state.pipeline)
  return found ? found.name : ''
}

/** Change the turn being answered, which is always the last one. */
function setTurn(patch) {
  const talk = $talk.get()
  if (!talk.turns.length) return
  const turns = talk.turns.slice()
  const last = turns[turns.length - 1]
  turns[turns.length - 1] = { ...last, ...(typeof patch === 'function' ? patch(last) : patch) }
  $talk.set({ ...talk, turns })
}

async function talkOpen(bare, lane = 'desk') {
  const session = sessions[lane]
  if (session.runtime) return session.runtime
  const stored = session.stored || kept(lane).stored || ''
  if (!session.left) session.left = kept(lane).left || ''      // a reload between leaving one and starting the next
  // A new conversation begins on the pick. If Hermes will not take it (the model is gone, the key was removed) the
  // conversation still opens, on Hermes's default, and says so. `bare` is the picker opening the conversation to
  // set a model on it itself.
  const refused = (pick, error) => host.notify({ kind: 'warning', message: 'Hermes could not use ' + pick.model + ' (' + why(error) + '), so this is on its default model. Pick another ' + LANES[lane].where + '.' })
  if (stored) {
    try {
      const resumed = await host.request('session.resume', { session_id: stored, omit_messages: true })
      if (resumed && resumed.session_id) {
        // Nothing is set here. Hermes keeps a conversation's model with the conversation (it survives a restart), and
        // setting it again on every visit would push a long conversation onto another model without the warning
        // Hermes gives for that. A pick reaches a conversation when it is made, and when one is created.
        Object.assign(session, { runtime: resumed.session_id, stored, primed: true })
        return session.runtime
      }
    } catch { /* that conversation is gone: start another */ }
  }
  // A title is a conversation's identity in Hermes: asking for a new one called "Stoa desk" while one of that name
  // exists hands back the one that exists (seen on a real run). So the first carries the plain title and one begun
  // with New conversation carries the day and time, to the second if the minute is taken. The pick rides on the
  // request, so the conversation begins on that model; if Hermes will not have it, it begins on the default.
  const pick = bare ? null : $model.get()[lane]
  const left = session.left
  const stamp = seconds => new Date().toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) })
  const create = async seconds => {
    const ask = { title: left ? LANES[lane].title + ' · ' + stamp(seconds) : LANES[lane].title, request_id: newId() }
    try {
      return await host.request('session.create', pick ? { ...ask, model: pick.model, provider: pick.provider, ...(pick.effort ? { reasoning_effort: pick.effort } : {}) } : ask)
    } catch (error) {
      if (!pick) throw error
      refused(pick, error)
      return host.request('session.create', ask)
    }
  }
  const same = made => Boolean(left) && Boolean(made) && [made.stored_session_id, made.session_id].includes(left)
  let made = await create(false)
  if (same(made)) made = await create(true)
  if (same(made)) throw new Error('Hermes handed back the conversation you just left instead of a new one')
  const runtime = (made && made.session_id) || ''
  if (!runtime) throw new Error('Hermes did not open a conversation')
  Object.assign(session, { runtime, stored: made.stored_session_id || runtime, primed: false, left: '' })
  keep(lane, { stored: session.stored, left: '' })
  return runtime
}

const readAsDataUrl = file => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(reader.error || new Error('the browser could not read ' + file.name))
  reader.readAsDataURL(file)
})

/** Hand files to Hermes the way the app's own chat box does, before the message that goes with them: an image is
    staged with `image.attach_bytes`, anything else is put in the conversation's workspace (`file.attach`) and comes
    back as a reference that goes at the head of the message, where Hermes's own file tools read it. Returns those
    references, and the pictures to name on the message itself: the gateway the Webapp and Desktop app talk to only
    stages a picture and answers with its path and type, and the message must then carry them (seen on a real run:
    without that the model is told nothing of the picture). An older gateway queues the picture itself and answers
    with no type, and then there is nothing to carry. */
// Hermes keeps its own copy of a file it is handed, and nothing of Hermes's clears it. The page tells the backend
// the path Hermes gave back, so the copy is on Stoa's list and goes after the days set (Setup, Your data).
const noteHanded = (path, name) => { if (path) rest('/files/handed', { method: 'POST', body: { path: String(path), name, lane: talking.lane } }).catch(() => {}) }

async function handOver(sessionId, files) {
  const refs = [], pictures = []
  for (const item of files) {
    setTurn({ note: 'Handing ' + item.name + ' to Hermes…' })
    const url = await readAsDataUrl(item.file)
    if (item.image) {
      const got = await host.request('image.attach_bytes', { session_id: sessionId, content_base64: url.replace(/^data:[^,]*,/, ''), filename: item.name }, 120000)
      if (!got || got.attached === false) throw new Error((got && got.message) || 'Hermes did not take ' + item.name)
      if (got.path && got.mime) pictures.push({ path: String(got.path), mime: String(got.mime) })
      noteHanded(got.path, item.name)
    } else {
      const got = await host.request('file.attach', { session_id: sessionId, data_url: url, name: item.name }, 120000)
      if (!got || !got.ref_text) throw new Error((got && got.message) || 'Hermes did not take ' + item.name)
      refs.push(got.ref_text)
      noteHanded(got.path, item.name)
    }
  }
  return { refs, pictures }
}

/** Send a line to one of the page's conversations: the desk's, or (lane 'maps') the map's research. `file` is one
    file or several: a small text file rides in the message itself, anything else is handed to Hermes first. */
async function say(text, file, lane = 'desk') {
  const line = String(text || '').trim()
  const files = [].concat(file || []).filter(Boolean)
  if ((!line && !files.length) || $talk.get().busy) return
  const turn = ++talking.turn
  talking.lane = lane
  const session = sessions[lane]
  const shown = line || (files.length === 1 ? 'Here is ' + files[0].name + '.' : 'Here are ' + files.length + ' files.')
  const typed = files.filter(item => typeof item.text === 'string').reduce((all, item) => all + '\n\nAttached file "' + item.name + '":\n```\n' + item.text + '\n```', shown)
  const whole = files.filter(item => item.file)
  const here = onScreen()
  const sent = here ? '(On the Stoa page I am looking at the pipeline “' + here + '”.)\n\n' + typed : typed
  $talk.set({ busy: true, open: true,
    turns: $talk.get().turns.slice(-7).concat([{ you: shown, file: files.map(item => item.name).join(', '), lane, reply: '', note: 'Sending…', error: '' }]) })
  let reached = false      // whether Hermes took the message: until it has, a failure gives the words and the files back
  const submit = async () => {
    const id = await talkOpen(false, lane)
    const { refs, pictures } = whole.length ? await handOver(id, whole) : { refs: [], pictures: [] }
    const body = [refs.join('\n'), sent].filter(Boolean).join('\n\n')      // the same order the app's chat box uses: references, then the words
    await host.request('prompt.submit', { session_id: id, text: session.primed ? body : (lane === 'maps' ? PRIME_MAPS : PRIME) + body, ...(pictures.length ? { attachments: pictures } : {}) })
    session.primed = true
    reached = true
  }
  try {
    try {
      await submit()
    } catch (first) {
      if (!session.runtime) throw first
      session.runtime = '' // the gateway let the idle session go: resume it (or start another) and send once more
      await submit()
    }
    if (talking.turn === turn && $talk.get().busy) setTurn(last => (last.reply ? {} : { note: lane === 'maps' ? 'Hermes is researching…' : 'Hermes is working on it…' }))
    setTimeout(() => {
      if (talking.turn !== turn || !$talk.get().busy) return
      setTalk({ busy: false })
      setTurn({ note: 'Still working. Open the conversation to follow it; the desk updates when it is done.' })
    }, 180000)
  } catch (error) {
    setTalk({ busy: false })
    const back = !reached && lane === 'desk' && files.length > 0
    const said = String((error && error.message) || error).trim()
    setTurn({ note: '', error: 'Hermes could not take that: ' + said + (/[.!?]$/.test(said) ? '' : '.') + (back ? ' Your words and files are back in the box.' : '') })
    if (back) {      // typed words and picked files are a person's work: a failed hand-over does not throw them away
      if (!$attached.get().length) $attached.set(files)
      if (line) $draft.set(line)
    }
  }
}

/** Open, in the app's own chat, the conversation the last thing on the sheet went to. */
function openTalk() {
  const turns = $talk.get().turns
  const lane = (turns.length && turns[turns.length - 1].lane) || 'desk'
  const stored = sessions[lane].stored || kept(lane).stored
  if (stored) Promise.resolve(host.openSession(stored)).catch(() => {})
  else host.notify({ kind: 'info', message: 'Say something to the desk first; the conversation starts then.' })
}

// A file handed to the desk. A CSV is read by the desk itself and comes back as a review, with no
// model in between. A small text file rides along in the next message. Anything else (a PDF, a Word
// file, a spreadsheet, a picture) waits by the box and is handed to Hermes with the next message, the
// way the app's own chat box hands one over, so Hermes reads it with its own tools.
const LIST_FILE = /\.(csv|tsv)$/i
const TEXT_FILE = /\.(txt|md|markdown|json|vcf|log|ya?ml)$/i
const IMAGE_FILE = /\.(png|jpe?g|gif|webp)$/i      // what Hermes takes as a picture to look at; any other picture goes as a file
const MAX_FILES = 6
const MAX_FILE_BYTES = 25 * 1024 * 1024               // Hermes's own limit for a picture sent as bytes; kept for every file, so the box stays quick

async function takeFile(file) {
  if (!file) return
  try {
    if (LIST_FILE.test(file.name)) {
      if (file.size > 2e6) return host.notify({ kind: 'warning', message: 'That list is over 2 MB. Send it in parts.' })
      const text = await file.text()
      const result = await rest('/intake', { method: 'POST', body: { name: file.name, text } })
      refresh()
      if (result.unread) { // the desk could not tell which column is the name: Hermes reads it, and asks if it cannot tell either
        if (text.length > 60000) return host.notify({ kind: 'info', message: 'Stoa could not tell which column of ' + file.name + ' holds the name. It is too long for the box here: press "Open the conversation" and drop it there.' })
        host.notify({ kind: 'info', message: result.headline })
        return say('Stoa could not tell which column of this list holds the name. Read it, work out which column is which, and put it on the desk as a list for my review. If you cannot tell, ask me.', { name: file.name, text })
      }
      host.notify({ kind: result.waiting ? 'success' : 'info', message: result.headline })
      const page = document.querySelector('.stoa-page')
      if (result.waiting) $tab.set('today') // where everything waiting for a yes or a no is
      if (result.waiting && page) page.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    const waiting = $attached.get()
    if (waiting.length >= MAX_FILES) return host.notify({ kind: 'info', message: 'That is ' + MAX_FILES + ' files already. Send these, then add more.' })
    if (waiting.some(item => item.name === file.name)) return host.notify({ kind: 'info', message: 'A file called ' + file.name + ' is already waiting by the box.' })
    if ((TEXT_FILE.test(file.name) || String(file.type || '').startsWith('text/')) && file.size <= 60000) {
      $attached.set(waiting.concat([{ name: file.name, text: await file.text() }]))
      return
    }
    if (!file.size) return host.notify({ kind: 'info', message: file.name + ' is empty.' })
    if (file.size > MAX_FILE_BYTES) return host.notify({ kind: 'warning', message: file.name + ' is over 25 MB, which is more than the box here hands over. Press "Open the conversation" and drop it there.' })
    $attached.set(waiting.concat([{ name: file.name, file, image: IMAGE_FILE.test(file.name) && String(file.type || '').startsWith('image/') }]))
  } catch (error) {
    host.notifyError(error, 'Stoa could not read that file')
  }
}

// -- look ------------------------------------------------------------------------------------------
// -- recruiting: candidates on the map, searches, and how a pipeline is paid for ----------------------------------
// On a recruiting playbook (shape "roles") a market map is a map for one role and each row on it is a CANDIDATE: a
// person, with their own fit, their own checks against the role and their own reason to move. Rows are kept in a
// map's company fields: `company` is the person, `size` their title, `uses` where they work now. What a map's rows
// are is written on the map and comes with it as `rows` ("people" or "companies"); it is never worked out from what
// is on the map. A recruiting map of companies (the older kind, people named under each) is read as that.
const MAP_COMPANIES = { people: false, thing: 'company', things: 'companies', thingCap: 'Company', find: 'Find a company', gone: 'Hermes does not propose a dismissed company again.' }
const MAP_SALES = { ...MAP_COMPANIES, recruiting: false, reason: 'a reason to call', inPipe: ' in the pipeline' }
const MAP_ROLES = {
  ...MAP_COMPANIES, recruiting: true, reason: 'a reason to move', inPipe: ' already candidates',
  lens: { seg: 'Pools', pri: 'Priority', held: 'Works with' },
  how: { seg: 'Each ring is a pool of talent that would get a different pitch. The best fit sits in the middle.',
    pri: 'Further right is a better fit; higher up is a fresher reason to move. The top right is who to approach first.',
    held: 'Grouped by what each company’s people work with today.' },
  suggestAsk: here => 'Suggest two neighbouring pools of talent for' + here + ': who is in them, and why they would be worth mapping for this role. Do not add anything yet; I will say which.'
}
const CANDIDATE_ROW = 'Every row on this map is one candidate: `company` = their full name, `size` = their current title, `uses` = the company they work at now by name alone, '
  + '`location`, `segment` = their pool, `fit` 1 to 5 for the role, `why` = the evidence, `requirements` checked for that person, `signal` and `signal_date` = why they would move now, '
  + 'and `source` = the public page you found them on. No `people`, `domain` or headcount. Only people a source shows.'
const MAP_PEOPLE = {
  ...MAP_ROLES, people: true, thing: 'candidate', things: 'candidates', thingCap: 'Candidate', find: 'Find a candidate', gone: 'Hermes does not propose a dismissed candidate again.',
  inPipe: ' in your pipeline',
  lens: { seg: 'Pools', pri: 'Priority', held: 'Employer' },
  how: { seg: 'Each dot is a candidate, and each ring a pool that would get a different pitch. The best fit sits in the middle.',
    pri: 'Each dot is a candidate. Further right is a better fit; higher up is a fresher reason to move. The top right is who to approach first.',
    held: 'Each dot is a candidate, grouped by where they work now.' },
  // What the buttons on a candidate ask for. The page's own requests speak of companies and their buyers.
  draftAsk: (here, names) => (names.length === 1
    ? 'Draft a first approach to ' + names[0] + ', a candidate on' + here + ', about the role. Lead with why they in particular fit it, from the evidence on the map, and with their reason to move if the map has one. Show me the draft. Do not send anything.'
    : 'Draft a first approach about the role to each of these candidates on' + here + ', each leading with why that person fits and their own reason to move: ' + names.join(', ') + '. Show me the drafts. Do not send anything.'),
  moreAsk: (here, room, names) => 'Find up to ' + room + ' more ' + (room === 'one' ? 'candidate' : 'candidates') + ' like ' + names + ' for' + here + '. ' + CANDIDATE_ROW
    + ' Keep to what the role needs and check each person against it with the evidence. Add them to the map, then tell me who you added and why.',
  growAsk: (here, room, pool) => 'Find up to ' + room + ' more ' + (room === 'one' ? 'candidate' : 'candidates') + ' for the “' + pool + '” pool of' + here + '. ' + CANDIDATE_ROW
    + ' Keep to what the role needs and check each person against it with the evidence. Add them to the map, then tell me who you added and why.',
  requireAsk: (here, text) => 'Add this to what the role needs on' + here + ' and check every candidate on it against it, with the evidence: ' + text
}
/** What a map's rows are, as it says itself: 'people', 'companies', or '' on a pipeline that is not recruiting. */
const rowsOf = market => String((market && (market.rows || (market.numbers && market.numbers.rows))) || '')
const mapWords = (shape, market) => (shape !== 'roles' ? MAP_SALES : rowsOf(market) === 'people' ? MAP_PEOPLE : MAP_ROLES)

/** What Hermes is asked for when a new map is started on a recruiting pipeline: the candidates for the role described.
    `search`: the search the map is for, if the person chose one. `web` false: only the people named in the request. */
const roleMapAsk = (said, search, web) => 'Map the candidates for a role I am recruiting for, and keep it on the desk as a map of its own: ' + said
  + (search ? ' It is for my search “' + search + '”: pass search="' + search.replace(/"/g, '') + '" to gtm_market(action="define") so its candidates join that search, name the map after it, and save the role'
    : ' Name the map after the role, and save the role')
  + ' and who fits it as the map\'s brief, written so the hiring manager could read it. Set the requirements from what the role needs: '
  + 'what someone must have done, the level, the place, and anything that rules a person out. Split the candidates into pools that would each get a different pitch. '
  + (web === false ? 'Put on the map ONLY the people I have named above, from what I have said about them: do not search the web, and do not add anyone else. '
    : 'Then find real people who could fill it, from public sources. ') + CANDIDATE_ROW + ' '
  + (web === false ? 'Give `source` as "my notes", and use only what I have told you about each person. ' : 'Never invent a person, a title or an employer. ') + 'Tell me when it is on the desk.'

// How the pipeline the page is showing is paid for, as the desk's state last said (a recruiting pipeline is
// "contingent", a fee for each placement, or "contract", by the hour and billed each month; any other has none).
const $paidHow = atom({})                   // by pipeline id
const pipeKey = pipe => (pipe === '' || pipe === null || pipe === undefined || pipe === 'main' ? 'main' : String(pipe))
const billingNow = () => $paidHow.get()[pipeKey($pipe.get())] || {}
/** On a contract pipeline no fee is shown or asked for anywhere but the money tab. `from`: the desk's state where the
    caller has it (a card in a chat is about its own pipeline, which need not be the one the page is showing). */
const noFees = from => ((from && from.billing) || billingNow()).type === 'contract'
const FEE_FACTS = ['fee_type', 'fee_pct', 'target_base', 'rate', 'hours', 'openings']        // what the recruiting playbook keeps on a search about money
const groupName = text => String(text || '').trim().toLowerCase().replace(/\s+/g, ' ')
/** How many seats a search is hiring for, and how many it still has to fill (none once it is done or closed). */
const openingsOf = group => Math.max(1, Math.round(Number(((group || {}).fields || {}).openings) || 1))
const seatsLeft = group => (group.status === 'done' || group.status === 'closed' ? 0 : Math.max(0, openingsOf(group) - (group.won || 0)))
const SEATS = {}                            // by pipeline: the seats left in each search, wherever that is not one
/** What a list of open records has in play. On a contingent recruiting pipeline a search's fee counts once for each
    seat still to fill, however many candidates are in it (the desk adds its own totals up the same way: `store.once`).
    This is for the sums the page makes itself: the board's columns and Today's stages. */
function inPlay(deals, from) {
  const list = deals || []
  if (((from && from.billing) || billingNow()).type !== 'contingent') return list.reduce((sum, deal) => sum + (deal.value || 0), 0)
  const fees = new Map(), seats = SEATS[pipeKey(from && from.pipeline !== undefined ? from.pipeline : $pipe.get())] || {}
  let alone = 0
  for (const deal of list) {
    const search = groupName(deal.group)
    if (!search || (deal.status && deal.status !== 'open')) alone += deal.value || 0       // placed or passed: each was paid for, a plain sum
    else fees.set(search, (fees.get(search) || []).concat([deal.value || 0]))
  }
  for (const [search, mine] of fees) alone += mine.sort((a, b) => b - a).slice(0, search in seats ? seats[search] : 1).reduce((sum, fee) => sum + fee, 0)
  return alone
}
const AMOUNT = /(^|_)value$/
function withoutFees(node, depth) {
  if (Array.isArray(node)) return node.map(item => withoutFees(item, depth + 1))
  if (!node || typeof node !== 'object' || depth > 5) return node
  const out = {}
  for (const key of Object.keys(node)) out[key] = AMOUNT.test(key) && typeof node[key] === 'number' ? 0 : withoutFees(node[key], depth + 1)
  return out
}
const MONEY_SIDE = /^\/(client|cash|production|pipeline\/billing)(\/|\?|$)/        // a client's billing and the money tab's own requests
/** Every answer the page is given passes through here. It notes how the pipeline is paid for and the seats its
    searches have left, and for a contract pipeline leaves every fee out of what the page sees (a fee that is on a
    record from before the pipeline was contract work then shows nowhere: not on the board, the record or the data
    table). What the money tab reads (`cash`) and a client's own billing are left as they are. */
function seenAnswer(path, pipe, answer) {
  if (!answer || typeof answer !== 'object') return answer
  const key = pipeKey(pipe), state = /^\/state(\?|$)/.test(path)
  if (state) {
    $paidHow.set({ ...$paidHow.get(), [key]: answer.billing || {} })
    if (Array.isArray(answer.groups)) SEATS[key] = Object.fromEntries(answer.groups.map(group => [groupName(group.name), seatsLeft(group)]).filter(pair => pair[1] !== 1))
  }
  if (($paidHow.get()[key] || {}).type !== 'contract' || MONEY_SIDE.test(path)) return answer
  if (!state) return withoutFees(answer, 0)
  const out = { ...answer }
  for (const side of ['deals', 'totals', 'flow', 'groups', 'companies', 'clients']) if (answer[side]) out[side] = withoutFees(answer[side], 0)
  // A record is not billed itself on contract work. One placed before the pipeline was contract work may be: where any
  // record here has an invoice, the page keeps the place they are shown and paid.
  if (answer.playbook && !(answer.billing || {}).record_invoices) out.playbook = { ...answer.playbook, billing: 'none' }
  return out
}

/** In the form that starts a new map on a recruiting pipeline: which search it is for, and whether to look for more people. */
function MapFor({ value, onChange, web, onWeb }) {
  const state = useDesk().data
  const rows = ((state && state.groups) || []).filter(row => row.status === 'open' || row.status === 'hold')
  const known = rows.some(row => row.name === value)
  return h('div', { className: 'stoa-mapfor' },
    rows.length ? h('label', null, h('span', { className: 'stoa-sub' }, 'Which search is it for?'),
      h('select', { className: 'stoa-select', value: known ? value : '', onChange: event => onChange(event.target.value) },
        h('option', { value: '' }, 'A new search, named after the role'),
        rows.map(row => h('option', { key: row.name, value: row.name }, row.name + (row.client && !row.name.toLowerCase().includes(row.client.toLowerCase()) ? '  ·  ' + row.client : ''))))) : null,
    h('label', { className: 'stoa-mapweb' }, h('input', { type: 'checkbox', checked: web !== false, onChange: event => onWeb(event.target.checked) }),
      h('span', null, 'Look for more candidates'), h('span', { className: 'stoa-sub' }, web !== false ? 'Each one found comes with the public page they were found on.' : 'Hermes is asked to put only the people you name above on the map.')))
}

/** At the head of a candidate map: the search its candidates join when they are added to the pipeline, and a way to
    point the map at another. Left alone, it is the search named after the map, which the first one added opens. */
function MapSearch({ market }) {
  const pipe = useContext(InPipe)
  const state = useDesk(pipe).data
  const [busy, setBusy] = useState(false)
  if (rowsOf(market) !== 'people' || !state) return null
  const groups = state.groups || [], goes = market.search || market.name
  const now = groups.find(group => groupName(group.name) === groupName(goes))
  const own = groupName(goes) === groupName(market.name)
  const set = search => {
    setBusy(true)
    rest('/market/' + encodeURIComponent(market.id) + '/search', { method: 'POST', body: { search }, ...inPipe(pipe) })
      .then(() => refresh()).catch(error => host.notifyError(error, 'Not changed')).then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-mapsearch' },
    h('span', { className: 'stoa-eyebrow' }, cap(state.playbook.terms.group)),
    h('span', null, 'Candidates added from this map join'),
    h('select', { className: 'stoa-select', value: own ? '' : goes, disabled: busy, 'aria-label': 'The search this map’s candidates join', onChange: event => set(event.target.value) },
      h('option', { value: '' }, '“' + market.name + '”' + (own && !now ? '  ·  opened by the first one added' : '')),
      groups.filter(group => groupName(group.name) !== groupName(market.name)).map(group => h('option', { key: group.name, value: group.name },
        group.name + (group.client && !group.name.toLowerCase().includes(group.client.toLowerCase()) ? '  ·  ' + group.client : '') + (group.status === 'open' ? '' : '  ·  ' + group.status_label.toLowerCase())))),
    now ? h('span', { className: 'stoa-sub' }, now.client ? 'for ' + now.client : 'no client on it yet') : null)
}

/** The panel beside a candidate map for the one candidate picked. The panel for a company offers to look up a buyer
    there (a paid lookup, which on a person's name finds nothing and costs a credit) and has no place for a title or
    an employer. `actions` are the map's own buttons for the row. */
function MapPerson({ c, market, busy, actions, open, move, close }) {
  const fact = (label, value, link) => (value ? h('div', { key: label, style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, label),
    link ? h('a', { className: 'stoa-alink', href: value, target: '_blank', rel: 'noreferrer', style: { display: 'block', overflowWrap: 'anywhere' } }, bareLink(value))
      : h('span', { style: { overflowWrap: 'anywhere' } }, value)) : null)
  const head = text => h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px' } }, h('span', { className: 'stoa-eyebrow' }, text))
  return h('div', { className: 'stoa-mapperson' },
    h('div', { className: 'stoa-eyebrow' }, c.segment + (c.also ? ' · also fits ' + c.also : '')),
    h('div', { className: 'stoa-maph' }, c.company),
    h('div', { className: 'stoa-sub' }, [c.size, c.uses ? 'at ' + c.uses : ''].filter(Boolean).join(' ') || 'Title and employer not known'),
    c.location ? h('div', { className: 'stoa-sub' }, c.location) : null,
    h('div', { className: 'stoa-chips' },
      c.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in your pipeline') : c.fresh ? h(Chip, { tone: 'warn' }, 'new') : h(Chip, null, 'on the map'),
      c.signal ? h(Chip, { tone: 'warn', wrap: true }, c.signal) : null,
      c.signal ? h('span', { className: 'stoa-sub' }, agoWords(c.days) || 'no date on it') : null),
    h('div', { className: 'stoa-mapscore' }, h('span', { className: 'stoa-eyebrow' }, c.weighed ? 'Fit for the role, by your weights' : 'Fit for the role'), h('b', null, c.score), h(Fit, { value: squaresOf(c.score) })),
    c.why ? h('p', { style: { margin: '10px 0' } }, c.why) : null,
    h('div', { className: 'stoa-btns' }, open ? h(Btn, { kind: 'primary', onClick: open }, 'Open the record') : null, actions),
    (c.requirements || []).length ? [head('Against the role'), c.requirements.map(req => h('div', { key: req.label, className: 'stoa-mapreq', 'data-on': req.shown ? '1' : '0' },
      h('span', null, req.shown ? '[x]' : '[ ]'), h('span', null, h('b', null, req.label), h('span', { className: 'stoa-sub', style: { display: 'block' } }, req.shown ? (req.evidence || 'Shown; the source is on the map') : 'No evidence found'))))] : null,
    head('About them'),
    h('div', { className: 'stoa-facts', style: { marginTop: 0, paddingTop: 0, borderTop: 0 } },
      fact('Title', c.size || 'Not known'), fact('Works at', c.uses || 'Not known'), fact('Based', c.location),
      /^https?:\/\//.test(c.source || '') ? fact('Found on', c.source, true) : fact('Found on', c.source)),
    c.status !== 'tracked' && market.segments.length > 1 ? [head('Adjust'), h('select', { key: 'move', className: 'stoa-select', value: c.segment, disabled: busy, 'aria-label': 'Move to another pool',
      onChange: event => move(event.target.value) }, market.segments.map(segment => h('option', { key: segment.name, value: segment.name }, (segment.name === c.segment ? 'In ' : 'Move to ') + segment.name)))] : null,
    h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'quiet', onClick: close }, 'Close')))
}

const BILLING_KINDS = [
  ['contingent', 'Contingent', 'A fee for each placement, a share of the salary. The pipeline counts in money: what is in play, what was placed.'],
  ['contract', 'Contract', 'Paid by the hour by one company, on any number of searches, and billed each month. The pipeline counts candidates; the money is on the Cash tab only.']]

/** At the head of the Pipeline tab on a recruiting pipeline: one line on how it is paid for, and the form that changes
    it. A contract needs the company, the hourly rate and the hours a week: the Cash tab bills from them. */
function Billing({ state }) {
  const billing = state.billing || {}
  const [open, setOpen] = useState(false)
  const [v, setV] = useState(null)
  const [busy, setBusy] = useState(false)
  if (!billing.type) return null
  const hourly = billing.type === 'contract', pb = state.playbook
  const start = () => { setV({ type: billing.type, client: billing.client || '', rate: billing.rate ? String(billing.rate) : '', hours: billing.hours_week ? String(billing.hours_week) : '',
    pct: billing.fee_pct ? String(billing.fee_pct) : '', from: billing.start || '', moved: false, clear: false }); setOpen(true) }
  const line = h('div', { className: 'stoa-billing' },
    h('span', { className: 'stoa-eyebrow' }, 'Billing'), h('b', null, hourly ? 'Contract' : 'Contingent'),
    hourly ? (billing.client ? h('span', null, 'with ' + billing.client) : null) : billing.fee_pct ? h('span', null, billing.fee_pct + '% of salary') : null,
    h('span', { className: 'stoa-sub' }, hourly ? 'Paid by the hour and billed each month. This side counts ' + pb.terms.deals + '; the hours and the bills are on the Cash tab.'
      : 'A fee for each placement. What is in play counts each ' + pb.terms.group + ' once, for each opening left.'),
    billing.fixed || open ? null : h(Btn, { kind: 'quiet', onClick: start }, 'Change'))
  if (!open || !v) return line
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const contract = v.type === 'contract'
  const ready = contract ? v.client.trim() && Number(v.rate) > 0 && Number(v.hours) > 0 : true
  const carried = billing.fees_kept || 0          // fees in the file, which a contract pipeline neither counts nor shows
  const save = () => {
    setBusy(true)
    rest('/pipeline/billing', { method: 'POST', body: { type: v.type, client: v.client.trim(), rate: Number(v.rate) || 0, hours_week: Number(v.hours) || 0, ...(v.moved && v.from ? { start: v.from } : {}), fee_pct: Number(v.pct) || 0, clear_fees: contract && v.clear } })
      .then(() => { setOpen(false); refresh() }).catch(error => host.notifyError(error, 'Not saved')).then(() => setBusy(false))
  }
  return h('div', null, line, h('div', { className: 'stoa-box stoa-payform' },
    h('div', { className: 'stoa-eyebrow' }, 'How this pipeline is paid for'),
    h('div', { className: 'stoa-billkinds' }, BILLING_KINDS.map(([id, title, words]) => h('button', { type: 'button', key: id, className: 'stoa-billkind', 'aria-pressed': v.type === id, 'data-on': v.type === id ? '1' : '0', onClick: () => setV({ ...v, type: id }) },
      h('b', null, title), h('span', { className: 'stoa-sub' }, words)))),
    contract ? h('div', { className: 'stoa-form' },
      h(Field, { label: 'The company the contract is with', value: v.client, onChange: set('client') }),
      h(Field, { label: 'Hourly rate, ' + (pb.currency || 'USD'), type: 'number', min: 0, value: v.rate, onChange: set('rate') }),
      h(Field, { label: 'Hours a week', type: 'number', min: 1, max: 80, value: v.hours, onChange: set('hours') }),
      h(Field, { label: 'Billed from the month of', type: 'month', value: v.from, onChange: event => setV({ ...v, from: event.target.value, moved: true }) }))
      : h('div', { className: 'stoa-form' }, h(Field, { label: 'Your usual fee, % of salary (optional)', type: 'number', min: 0, max: 100, value: v.pct, onChange: set('pct') })),
    h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, contract
      ? 'Each month that ends is put on the Cash tab to bill: the rate times that month’s hours (the hours a week, over its working days), which you can correct before you record the invoice. A month that is not over shows there as the month in progress. Leave the month empty to start with this one; a company you already bill by the hour keeps the month it started. No fee is asked for or shown on a ' + pb.terms.deal + ' here.'
      : 'Give a ' + pb.terms.group + ' the fee each ' + pb.terms.deal + ' in it would bring, and how many openings it has; what is in play counts that fee once for each opening left.'),
    contract && carried ? h('label', { className: 'stoa-billclear' }, h('input', { type: 'checkbox', checked: v.clear, onChange: event => setV({ ...v, clear: event.target.checked }) }),
      h('span', null, 'Some ' + pb.terms.deals + ' or ' + pb.terms.groups + ' here carry fees from before. On contract work they are not counted or shown anywhere. Tick this to remove them for good; left alone, they are back if this pipeline is made contingent again.')) : null,
    h('div', { className: 'stoa-btns', style: { marginTop: 10 } }, h(Btn, { kind: 'primary', disabled: busy || !ready, onClick: save }, busy ? 'Saving…' : 'Save'), h(Btn, { kind: 'quiet', onClick: () => setOpen(false) }, 'Cancel'))))
}

/** At the head of the Cash tab: every pipeline at once (a contract's month of hours beside the others' fees), then, on
    a contract pipeline, the contract itself and the month in progress. A month is put on the tab to bill only once it
    has ended, so without that the tab would have nothing to show for a contract until the first of next month. */
function ProductionHead({ state, openClient, showPipe }) {
  const all = useQuery({ queryKey: ['stoa', 'production'], queryFn: () => rest('/production'), staleTime: 3000, refetchInterval: 30000 }).data
  const rows = (all && all.pipelines) || [], billing = state.billing || {}, pb = state.playbook
  const cols = [['this_month', (all && all.month) || 'This month', ''], ['collected', 'Collected', 'good'], ['owed', 'Owed', ''], ['overdue', 'Overdue', 'bad'], ['to_bill', 'To bill', 'warn'], ['likely', 'Likely to come', '']]
  const cell = (row, key, tone, currency) => h('td', { key, 'data-num': '1', 'data-tone': row[key] && tone ? tone : '' }, row[key] ? money(row[key], row.currency || currency) : '—')
  const across = rows.length > 1 ? h('div', { key: 'all', className: 'stoa-prod' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Across your ' + rows.length + ' pipelines'), h('span', { className: 'stoa-sub' }, 'Press one for its own')),
    h('div', { style: { overflowX: 'auto' } }, h('table', { className: 'stoa-prodtable' },
      h('thead', null, h('tr', null, h('th', null, 'Pipeline'), cols.map(col => h('th', { key: col[0], 'data-num': '1' }, col[1])))),
      h('tbody', null, rows.map(row => h('tr', { key: row.id, 'data-on': row.id === state.pipeline ? '1' : '0' },
        h('td', null, h('button', { type: 'button', className: 'stoa-prodname', disabled: row.id === state.pipeline, onClick: () => showPipe(row.id) }, row.name),
          h('small', null, [row.billing === 'contract' ? 'Contract' + (row.client ? ' · ' + row.client : '') : row.billing === 'contingent' ? 'Contingent' : '', row.this_month_note].filter(Boolean).join(' · '))),
        cols.map(col => cell(row, col[0], col[2], all.currency))))),
      all.totals ? h('tfoot', null, h('tr', null, h('td', null, 'All of them'), cols.map(col => cell(all.totals, col[0], col[2], all.currency)))) : null)),
    h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, ((all && all.month) || 'This month') + ' is what was won since the first, and for contract work the month in progress at the hours agreed. “Likely to come” weighs what is open by how far along it is.'
      + (all.totals ? '' : ' No totals: the pipelines are in different currencies.'))) : null
  const month = billing.month
  const contract = billing.type === 'contract' ? h('div', { key: 'contract', className: 'stoa-prod' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Contract' + (billing.client ? ' · ' + billing.client : '')),
      billing.client && openClient ? h(Btn, { kind: 'quiet', onClick: () => openClient(billing.client) }, 'Its terms and invoices') : null),
    month ? h('div', { className: 'stoa-strip' },
      h('div', { className: 'stoa-tile' }, h('span', { className: 'stoa-eyebrow' }, 'Hourly rate'), h('b', null, money(billing.rate, pb.currency)), h('span', { className: 'stoa-sub' }, billing.hours_week + ' hours a week')),
      h('div', { className: 'stoa-tile' }, h('span', { className: 'stoa-eyebrow' }, 'This month'), h('b', null, month.running ? money(month.amount, pb.currency) : '—'),
        h('span', { className: 'stoa-sub' }, month.running ? month.label + ' · about ' + month.hours + ' hours' : 'outside the contract’s months')),
      h('div', { className: 'stoa-tile', 'data-tone': billing.to_bill ? 'warn' : '' }, h('span', { className: 'stoa-eyebrow' }, 'Waiting to be billed'), h('b', null, billing.to_bill ? String(billing.to_bill) : '0'),
        h('span', { className: 'stoa-sub' }, billing.to_bill ? (billing.to_bill === 1 ? 'month' : 'months') + ' ended, below' : 'no month has ended yet')))
      : h('p', { className: 'stoa-sub' }, 'Say who the contract is with, the hourly rate and the hours a week on the Pipeline tab, under Billing, and each month’s bill is worked out here.'),
    month ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'A month is put here to bill once it has ended, and you can correct the hours before you record the invoice. '
      + (month.running ? month.label + ' can be billed from ' + month.bills_from + '. ' : '') + 'Billing runs from ' + (billing.start_label || 'this month') + '.') : null) : null
  return [across, contract]
}

const CSS = `
/* The look. Surfaces, words and lines are the app's theme (--ui-*), so every theme carries it.
   Labels, figures and anything you press are set in the app's mono face; sentences stay in its text face.
   Corners are square and lines are hairlines. A quantity is drawn as dots, never as a smooth bar.
   Heat (--stoa-h1 to h7) is the one set of colours Stoa brings: the ramp its artwork is drawn in, cold to hot. */
.stoa { font-size: 13px; line-height: 1.45; color: var(--ui-text-primary);
  --stoa-mono: var(--font-mono, "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace);
  --stoa-accent: var(--ui-accent);
  --stoa-on-accent: var(--dt-midground-foreground, #fff);
  --stoa-accent-text: color-mix(in oklab, var(--ui-accent) 58%, var(--ui-text-primary));
  --stoa-accent-soft: color-mix(in srgb, var(--ui-accent) 16%, transparent);
  --stoa-accent-line: color-mix(in srgb, var(--ui-accent) 58%, transparent);
  --stoa-h1: #2150cc; --stoa-h2: #2c86d9; --stoa-h3: #30b4c4; --stoa-h4: #48c78c; --stoa-h5: #f2b24c; --stoa-h6: #f4967e; --stoa-h7: #f3a9c0;
  --stoa-heat: linear-gradient(90deg, var(--stoa-h1), var(--stoa-h2), var(--stoa-h3), var(--stoa-h4), var(--stoa-h5), var(--stoa-h6), var(--stoa-h7));
  --stoa-bar: linear-gradient(90deg, var(--stoa-h1), var(--stoa-h2) 46%, var(--ui-text-primary));      /* the quiet one: blue to the colour of the words */
  --stoa-r: 2px;
  --stoa-page: var(--background, #0d1015);
  /* Two sets of stripes that cross: what is left is a grid of 2px dots, 4px apart. Used as a mask. */
  --stoa-grid: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px), repeating-linear-gradient(0deg, #000 0 2px, transparent 2px 4px); }
.stoa *, .stoa *::before, .stoa *::after { box-sizing: border-box; }
/* The app turns text selection off for the whole window and back on for its own messages. The desk's text (a name, an
   email, a note, an answer) is the person's to select and copy. A card that is dragged, and a button, stay as they were. */
.stoa { -webkit-user-select: text; user-select: text; }
.stoa [draggable="true"], .stoa [draggable="true"] *, .stoa button, .stoa [role="button"], .stoa summary { -webkit-user-select: none; user-select: none; }
.stoa button.stoa-proplink, .stoa input, .stoa textarea { -webkit-user-select: text; user-select: text; }
.stoa-pane { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.stoa-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 12px 18px; }
.stoa-page { height: 100%; overflow: auto; padding: 24px 34px 0; }
.stoa-wrap { max-width: 1320px; margin: 0 auto; container-type: inline-size; }
.stoa-eyebrow { font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-figure { display: flex; align-items: flex-end; gap: 10px; margin-top: 6px; font-family: var(--stoa-mono); font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-figure small { font-size: 11.5px; font-weight: 400; letter-spacing: .02em; color: var(--ui-text-tertiary); padding-bottom: 3px; }
.stoa-page .stoa-figure { font-size: 44px; }
.stoa-page .stoa-figure small { padding-bottom: 1px; }
/* The figure that matters, set in dots. */
.stoa-dotfig { display: block; height: 28px; width: auto; overflow: visible; }
.stoa-page .stoa-dotfig { height: 42px; }
.stoa-dotfig rect { fill: var(--ui-text-primary); }
/* The line under the header: one row of dots, blue running to white. */
.stoa-rule { height: 2px; margin: 14px 0 4px; background: var(--stoa-bar); -webkit-mask-image: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); mask-image: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-sec { margin-top: 16px; }
.stoa-sechead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
/* A section opens with three shades of block, light to dark: the same dots, at the size of a letter. */
.stoa-sechead > .stoa-eyebrow:first-child::before, .stoa-rpsechead h3::before { content: "░▒▓"; margin-right: 8px; letter-spacing: 0; font-family: var(--stoa-mono); font-weight: 400;
  background: var(--stoa-bar); -webkit-background-clip: text; background-clip: text; color: transparent; }
.stoa-row { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 6px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-row:hover { background: var(--ui-row-hover-background); }
.stoa-row[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-dot { flex: none; width: 6px; height: 6px; margin-top: 6px; background: var(--ui-text-quaternary); }
.stoa-dot[data-tone="bad"] { background: var(--ui-red); }
.stoa-dot[data-tone="warn"] { background: var(--ui-orange); }
.stoa-dot[data-tone="good"] { background: var(--stoa-accent); }
.stoa-grow { flex: 1; min-width: 0; }
.stoa-name { font-weight: 550; }
.stoa-num { flex: none; font-family: var(--stoa-mono); font-size: 11.5px; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); white-space: nowrap; }
.stoa-num[data-tone="bad"] { color: var(--ui-red); }
.stoa-num[data-tone="warn"] { color: var(--ui-orange); }
.stoa-box { padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-detail { margin: 2px 0 10px; }
.stoa-inchat { max-width: 460px; margin: 6px 0; }
.stoa-chatwide { max-width: 640px; margin: 6px 0; container-type: inline-size; }
.stoa-chathead { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; margin-bottom: 8px; }
.stoa-chatlist { margin-top: 10px; }
.stoa-chatlisthead { display: flex; gap: 8px; align-items: baseline; padding: 0 0 3px; }
.stoa-chatday .stoa-trow { grid-template-columns: minmax(0, 1fr) auto; }
.stoa-chatday .stoa-trow > .stoa-tacts { grid-column: 1 / -1; justify-content: flex-start; flex-wrap: wrap; padding: 0 0 8px 23px; }
.stoa-chatrec { grid-column: 1 / -1; padding: 0 0 10px 8px; }
.stoa-chatfoot { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; justify-content: space-between; margin-top: 12px; padding-top: 10px; border-top: 1px dotted var(--ui-stroke-primary); }
/* Every bar is a row or two of dots: lit as far as it is filled. */
.stoa-meter, .stoa-weight, .stoa-rate u, .stoa-fc u, .stoa-billbar, .stoa-path > *::after { -webkit-mask-image: var(--stoa-grid); mask-image: var(--stoa-grid); -webkit-mask-composite: source-in; mask-composite: intersect; }
.stoa-meter { display: flex; margin: 8px 0 4px; }
.stoa-meter i { flex: 1; height: 4px; background: var(--ui-stroke-primary); }
/* Lit dots take their colour from the heat ramp: --pos is where along it this piece sits, --size how many pieces share it. */
.stoa-meter i[data-on="1"], .stoa-weight i, .stoa-path > [data-state="done"]::after, .stoa-path > [data-state="now"]::after, .stoa-path > [data-state="won"]::after { background: var(--stoa-heat); background-size: var(--size, 100%) 100%; background-position: var(--pos, 0) 0; }
.stoa-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 14px 0 2px; padding-top: 12px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-facts span { display: block; overflow-wrap: anywhere; }
.stoa-next { margin-top: 10px; }
.stoa-log { margin-top: 10px; padding-top: 8px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-log p { margin: 0 0 5px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.stoa-btn, .stoa-select { height: 28px; padding: 0 11px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-btn, .stoa-select { transition: background .15s ease, opacity .15s ease; }
.stoa-btn:hover, .stoa-select:hover { background: var(--ui-control-hover-background); }
.stoa-btn[data-kind="primary"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-btn[data-kind="primary"] { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); font-weight: 600; }
.stoa-btn[data-kind="quiet"] { border-color: transparent; color: var(--ui-text-secondary); }
.stoa-btn:disabled, .stoa-select:disabled { opacity: .5; cursor: default; }
/* A tag is a word in square brackets, with no box round it. */
.stoa-chip { display: inline-block; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-chip::before { content: "["; margin-right: 2px; opacity: .55; }
.stoa-chip::after { content: "]"; margin-left: 1px; opacity: .55; }
.stoa-chip[data-tone="bad"] { color: var(--ui-red); }
.stoa-chip[data-tone="warn"] { color: var(--ui-orange); }
.stoa-chip[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-chip[data-wrap="1"] { white-space: normal; overflow-wrap: anywhere; }
.stoa-chips { display: flex; flex-wrap: wrap; gap: 5px 9px; margin-top: 7px; }
.stoa-chips > * { min-width: 0; }
.stoa-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-top: 1px dotted var(--ui-stroke-primary); }
/* Tabs: a strip of cells. Where you are is shown the way a terminal shows it, light on dark turned round. */
.stoa-tabs { display: inline-flex; margin: 18px 0 18px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); }
.stoa-tab { display: inline-flex; align-items: center; gap: 8px; height: 30px; padding: 0 14px; border: 0; border-radius: 0; background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 12px; cursor: pointer; outline: none; transition: background .15s ease, color .15s ease; }
.stoa-tab + .stoa-tab { border-left: 1px solid var(--ui-stroke-tertiary); }
.stoa-tab:hover { color: var(--ui-text-primary); background: var(--ui-control-hover-background); }
.stoa-tab[data-on="1"], .stoa-tab[data-on="1"]:hover { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; }
.stoa-tab small { font-size: 10px; opacity: .6; }
.stoa-tab[data-on="1"] small { opacity: .75; }
.stoa button:focus-visible, .stoa select:focus-visible, .stoa input:focus-visible { outline: 2px solid var(--stoa-accent-line); outline-offset: 2px; }
.stoa-split { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px 36px; align-items: start; }
.stoa-split[data-side="1"] { grid-template-columns: minmax(0, 1fr) 340px; }
.stoa-split > .stoa-box { position: sticky; top: 0; max-height: calc(100vh - 235px); overflow-y: auto; }   /* it ends above the dock */
.stoa-board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(176px, 1fr); gap: 12px; overflow-x: auto; padding-bottom: 8px; }
.stoa-colhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 2px 8px; }
.stoa-card { display: block; width: 100%; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-card { transition: border-color .15s ease, transform .15s ease; }
.stoa-card:hover, .stoa-card[data-open="1"] { border-color: var(--ui-stroke-primary); }
.stoa-card:hover { transform: translateY(-1px); }
.stoa-card[data-open="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-cardfoot { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-top: 6px; font-size: 12px; }
.stoa-cardfoot em { flex: none; font-style: normal; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-tertiary); }
.stoa-card h5 { display: flex; justify-content: space-between; gap: 8px; margin: 0; font-size: 13px; font-weight: 600; }
.stoa-card[data-tone="bad"] { box-shadow: inset 2px 0 0 var(--ui-red); }
.stoa-card[data-tone="warn"] { box-shadow: inset 2px 0 0 var(--ui-orange); }
.stoa-cardwho { margin-top: 1px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-cardwho[data-none="1"] { color: var(--ui-text-quaternary); }
.stoa-cardnext { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; margin-top: 7px; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.stoa-cardnext[data-none="1"] { color: var(--ui-text-quaternary); }
.stoa-cardmeta { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-top: 8px; padding-top: 7px; border-top: 1px dotted var(--ui-stroke-secondary); }
.stoa-cardmeta em { flex: none; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-style: normal; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-tertiary); }
.stoa-cardfacts { display: flex; flex-wrap: wrap; gap: 2px 10px; min-width: 0; }
.stoa-cardfact { font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .04em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-cardfact[data-tone="bad"] { color: var(--ui-red); }
.stoa-cardfact[data-tone="warn"] { color: var(--ui-orange); }
.stoa-cardfact[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-boardwrap { position: relative; }
.stoa-peek { position: absolute; z-index: 40; padding: 12px 14px 10px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--stoa-page); box-shadow: 0 10px 34px rgba(0, 0, 0, .22); font-size: 12.5px; pointer-events: none; animation: stoa-peekin .14s ease both; }
@keyframes stoa-peekin { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
.stoa-peekhead b { display: block; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.stoa-peekhead .stoa-sub { display: block; margin-top: 2px; font-family: var(--stoa-mono); font-size: 10.5px; }
.stoa-peeksec { margin-top: 10px; padding-top: 9px; border-top: 1px dotted var(--ui-stroke-secondary); }
.stoa-peeksec > small { display: block; margin-bottom: 3px; }
.stoa-peektext { overflow-wrap: anywhere; }
.stoa-peeklog { display: grid; grid-template-columns: 62px minmax(0, 1fr) auto; gap: 8px; align-items: baseline; margin-top: 4px; }
.stoa-peeklog:first-of-type { margin-top: 0; }
.stoa-peekkind { font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-peeklog .stoa-peektext { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.stoa-peekfoot { margin-top: 10px; padding-top: 8px; border-top: 1px dotted var(--ui-stroke-secondary); font-size: 11px; }
@media (prefers-reduced-motion: reduce) { .stoa-peek { animation: none; } }
.stoa-empty { padding: 10px 2px; color: var(--ui-text-quaternary); }
.stoa-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
/* A column is as wide as its share and no wider: whatever is in it folds, so one long word or tag cannot push a card past its edge. */
.stoa-cols > * { min-width: 0; }
.stoa-company { padding: 10px 0; border-top: 1px dotted var(--ui-stroke-primary); overflow-wrap: anywhere; }
.stoa-company:first-of-type { border-top: 0; }
/* Fit is heat: a better fit is a hotter mark. */
.stoa-fit { font-family: var(--stoa-mono); font-size: 9px; letter-spacing: 2px; color: var(--stoa-h4); white-space: nowrap; }
.stoa-fit[data-fit="1"] { color: var(--stoa-h2); } .stoa-fit[data-fit="2"] { color: var(--stoa-h3); } .stoa-fit[data-fit="4"] { color: var(--stoa-h5); } .stoa-fit[data-fit="5"] { color: var(--stoa-h6); }
.stoa-fit s { text-decoration: none; color: var(--ui-text-quaternary); opacity: .6; }
.stoa-rate { display: grid; grid-template-columns: minmax(0, 1fr) 92px 44px; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-rate u { display: block; height: 4px; background: var(--ui-stroke-primary); overflow: hidden; }
.stoa-rate u i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-reading { max-width: 70ch; margin: 8px 0 0; color: var(--ui-text-secondary); white-space: pre-line; }
.stoa-h2 { margin: 26px 0 8px; font-size: 15px; font-weight: 600; }
.stoa-filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.stoa-filters .stoa-input { flex: 1 1 120px; min-width: 0; height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-filters .stoa-input:focus { border-color: var(--ui-stroke-primary); }
.stoa-filters .stoa-input::placeholder { color: var(--ui-text-quaternary); }
.stoa-filters .stoa-select { max-width: 150px; }
.stoa-toggle { height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-toggle[data-on="1"] { border-color: var(--stoa-accent-line); color: var(--ui-text-primary); background: var(--stoa-accent-soft); }
/* The funnel: each stage a block of dots, cold where records come in and hot where they are won. */
.stoa-funnel { margin: 0 0 20px; padding: 16px 18px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-funnel svg { display: block; width: 100%; height: auto; overflow: visible; }
.stoa-funnel text { font-family: var(--stoa-mono); }
.stoa-fn-block { fill: var(--stoa-h3); }
.stoa-fn-step[data-heat="1"] .stoa-fn-block { fill: var(--stoa-h1); } .stoa-fn-step[data-heat="2"] .stoa-fn-block { fill: var(--stoa-h2); }
.stoa-fn-step[data-heat="4"] .stoa-fn-block { fill: var(--stoa-h4); } .stoa-fn-step[data-heat="5"] .stoa-fn-block { fill: var(--stoa-h5); }
.stoa-fn-step[data-heat="6"] .stoa-fn-block { fill: var(--stoa-h6); }
/* Where a record is won is not one more colour on the ramp: it is white, and it glows. */
.stoa-fn-won { fill: var(--ui-text-primary); filter: drop-shadow(0 0 5px color-mix(in srgb, var(--ui-text-primary) 70%, transparent)) drop-shadow(0 0 16px color-mix(in srgb, var(--stoa-h7) 60%, transparent)); }
.stoa-fn-block, .stoa-fn-won { transform-box: fill-box; transform-origin: center; animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both; transition: filter .15s ease; }
.stoa-fn-won { animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both, stoa-glow 3.6s ease-in-out 1s infinite; }
.stoa-fn-step:last-child .stoa-fn-label, .stoa-fn-step:last-child .stoa-fn-num { fill: var(--ui-text-primary); }
.stoa-fn-unit { font-family: var(--stoa-mono); font-size: 9.5px; font-weight: 400; letter-spacing: .08em; text-transform: uppercase; fill: var(--ui-text-tertiary); }
.stoa-fn-link { fill: var(--ui-text-primary); opacity: .17; }
.stoa-fn-pill { fill: var(--stoa-page); stroke: var(--ui-stroke-secondary); }
.stoa-fn-hit { fill: transparent; }
.stoa-fn-step[data-press="1"] { cursor: pointer; }
.stoa-fn-step[data-press="1"]:hover .stoa-fn-block { filter: brightness(1.25); }
.stoa-fn-step[data-on="1"] .stoa-fn-label { fill: var(--ui-text-primary); font-weight: 700; }
.stoa-fn-step[data-dim="1"] .stoa-fn-block { opacity: .35; }
.stoa-fn-label { font-size: 9.5px; letter-spacing: .12em; fill: var(--ui-text-tertiary); }
.stoa-fn-num { font-size: 15px; font-weight: 600; fill: var(--ui-text-primary); }
.stoa-fn-pct { font-size: 10.5px; fill: var(--ui-text-primary); }
.stoa-fn-sub { font-size: 9px; letter-spacing: .06em; fill: var(--ui-text-tertiary); }
.stoa-funnel-foot { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 6px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-funnel-foot b { font-family: var(--stoa-mono); color: var(--ui-text-primary); font-weight: 600; }
.stoa-list { max-height: 340px; overflow-y: auto; margin-top: 10px; }
.stoa-pick { display: flex; gap: 9px; align-items: flex-start; padding: 7px 0; border-top: 1px dotted var(--ui-stroke-primary); cursor: pointer; }
.stoa-pick:first-child { border-top: 0; }
.stoa-pick input { flex: none; margin-top: 3px; accent-color: var(--stoa-accent); }
.stoa-pick[data-off="1"] .stoa-grow { opacity: .5; }
.stoa-rest { margin-top: 10px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-rest summary { cursor: pointer; }
.stoa-rest p { margin: 4px 0 0 14px; }
.stoa-reply { margin-top: 10px; overflow-wrap: anywhere; }
.stoa-reply p { margin: 0 0 8px; }
.stoa-reply p:last-child { margin-bottom: 0; }
.stoa-reply-app { font-size: 13.5px; line-height: 1.55; }
.stoa-reply-app table { display: block; max-width: 100%; overflow-x: auto; }
/* The dock floats at the foot of the page; the board fades out beneath it instead of running into it. */
.stoa-dockzone { position: sticky; bottom: 0; z-index: 6; margin: 26px -34px 0; padding: 54px 34px 16px; background: linear-gradient(to top, var(--stoa-page) 46%, transparent); pointer-events: none; }
.stoa-dock { width: min(780px, 100%); margin: 0 auto; pointer-events: auto; }
.stoa-glass { border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 88%, transparent); -webkit-backdrop-filter: blur(18px) saturate(1.2); backdrop-filter: blur(18px) saturate(1.2); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 2px 8px rgba(0, 0, 0, .22); }
.stoa-sheet { margin-bottom: 8px; padding: 10px 14px 14px; max-height: min(46vh, 430px); overflow-y: auto; animation: stoa-lift .2s ease both; }
.stoa-sheethead { position: sticky; top: -10px; z-index: 2; display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: -10px -14px 8px; padding: 6px 8px 6px 14px; background: var(--ui-bg-elevated, var(--stoa-page)); border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-sheethead .stoa-btn { display: inline-flex; align-items: center; gap: 5px; }
.stoa-ask { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; padding: 10px 12px; }
.stoa-askwords { flex: 1 1 100%; overflow-wrap: anywhere; }
.stoa-composerfoot { display: flex; align-items: center; gap: 6px; margin-top: 2px; padding: 3px 2px 0; border-top: 1px dotted var(--ui-stroke-secondary, var(--ui-stroke-primary)); }
.stoa-footbtn { max-width: 100%; height: 24px; padding: 0 7px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
.stoa-footbtn:hover, .stoa-footbtn[aria-expanded="true"] { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-footbtn:focus-visible { outline: 2px solid var(--stoa-accent); outline-offset: -2px; }
.stoa-footbtn:disabled { opacity: .45; cursor: default; }
.stoa-menu[data-right="1"] .stoa-menulist { left: auto; right: 0; }
.stoa-menu[data-up="1"] .stoa-menulist { top: auto; bottom: calc(100% + 6px); left: 0; max-height: min(420px, 56vh); }
.stoa-footbtn .stoa-menucaret { font-size: 11px; }
.stoa-appmenurow { display: block; }
.stoa-appmenurow small { display: block; font-size: 11.5px; opacity: .7; }
.stoa-turn + .stoa-turn { margin-top: 14px; padding-top: 14px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-youline { display: flex; justify-content: flex-end; }
.stoa-you { max-width: 86%; padding: 6px 12px; border-radius: var(--stoa-r); background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--stoa-accent) 24%, transparent); overflow-wrap: anywhere; white-space: pre-wrap; }
.stoa-you small { display: block; margin-top: 2px; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-working { display: inline-flex; align-items: center; gap: 8px; }
.stoa-working::before { content: ""; width: 7px; height: 13px; background: var(--stoa-accent); animation: stoa-blink 1s steps(1) infinite; }
.stoa-composer { padding: 7px; transition: border-color .15s ease, box-shadow .15s ease; }
.stoa-composer:focus-within { border-color: var(--stoa-accent-line); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 0 0 1px var(--stoa-accent-line); }
.stoa-composerrow { display: flex; align-items: flex-end; gap: 3px; }
/* The talk box is a prompt: a mark, then what you type, in the mono face. */
.stoa-prompt { flex: none; align-self: flex-start; padding: 6px 0 0 5px; font-family: var(--stoa-mono); font-size: 15px; font-weight: 700; line-height: 22px; color: var(--stoa-accent-text); user-select: none; }
.stoa-composer textarea { flex: 1; min-width: 0; min-height: 34px; max-height: 132px; padding: 8px 6px 6px; border: 0; background: transparent; color: var(--ui-text-primary); font-family: var(--stoa-mono); font-size: 13px; line-height: 1.5; resize: none; }
.stoa .stoa-composer textarea, .stoa .stoa-composer textarea:focus-visible { outline: none; }
.stoa-composer textarea::placeholder { color: var(--ui-text-quaternary); }
.stoa-icon { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; padding: 0; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); cursor: pointer; transition: background .15s ease, color .15s ease, opacity .15s ease; }
.stoa-icon:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-icon:disabled { opacity: .4; cursor: default; }
.stoa-icon[data-kind="send"] { margin-left: 3px; background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-icon[data-kind="send"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-icon[data-kind="send"]:disabled { opacity: .32; }
.stoa-icon[data-on="1"] { color: var(--ui-red); background: color-mix(in srgb, var(--ui-red) 14%, transparent); animation: stoa-pulse 1.4s ease-in-out infinite; }
.stoa-turnhint { margin-top: 10px; padding: 8px 10px; border: 1px dotted var(--ui-stroke-primary); border-radius: var(--stoa-r); color: var(--ui-text-primary); font-size: 13px; }
.stoa-filechips { display: flex; flex-wrap: wrap; gap: 0 2px; }
.stoa-filechip { display: inline-flex; align-items: center; gap: 6px; margin: 1px 0 6px 4px; padding: 2px 3px 2px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); font-family: var(--stoa-mono); font-size: 11.5px; color: var(--ui-text-secondary); }
.stoa-hints { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin-bottom: 8px; }
.stoa-hint { height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 86%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; transition: color .15s ease, border-color .15s ease; }
.stoa-hint:hover { color: var(--ui-text-primary); border-color: var(--ui-stroke-primary); }
.stoa-hint:disabled { opacity: .5; cursor: default; }
.stoa-dropnote { position: sticky; top: 0; z-index: 7; width: fit-content; margin: 0 auto 12px; padding: 8px 18px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); font-size: 13px; }
.stoa-page[data-drop="1"] { box-shadow: inset 0 0 0 2px var(--stoa-accent-line); }
/* The head of the page is two columns. The figure and its counts take the room they need; the picture (the stoa,
   or your name in its place) has what is left, so it is cropped or made smaller and never runs under the figures.
   Where too little is left, the picture goes: a name then sits above the figures instead. */
.stoa-head { position: relative; display: grid; grid-template-columns: minmax(0, max-content) minmax(0, 1fr); gap: 14px 44px; align-items: end; min-height: 124px; }
.stoa-head > * { position: relative; }
.stoa-headmain { grid-column: 1; grid-row: 1; min-width: 0; }
.stoa-headstats { grid-column: 1; grid-row: 2; }
.stoa-headart { grid-column: 2; grid-row: 1 / span 2; align-self: stretch; min-width: 0; pointer-events: none; -webkit-mask-image: linear-gradient(90deg, transparent, #000 90px); mask-image: linear-gradient(90deg, transparent, #000 90px); }
.stoa-headart > canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
.stoa-art { display: block; width: 100%; height: 100%; }
.stoa-nameart { font-family: var(--stoa-mono); }
.stoa-headname { height: 56px; width: min(100%, 700px); margin: 0 0 12px -3px; }
.stoa-headname[data-narrow="1"] { display: none; }
.stoa-head[data-sign="art"] > .stoa-headart { -webkit-mask-image: none; mask-image: none; }
.stoa-head[data-sign="art"] > .stoa-headart > canvas { top: 4px; height: calc(100% - 8px); }
@container (max-width: 980px) {
  .stoa-head[data-sign="art"] > .stoa-headart { display: none; }
  .stoa-headname[data-narrow="1"] { display: block; }
}
@container (max-width: 860px) { .stoa-headart { display: none; } .stoa-head { min-height: 0; } }
.stoa-headstats { display: flex; flex-wrap: wrap; gap: 10px 32px; }
.stoa-headstats b { display: block; margin-top: 3px; font-family: var(--stoa-mono); font-size: 18px; font-weight: 600; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tabsrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
.stoa-col { min-width: 0; border-radius: var(--stoa-r); transition: opacity .2s ease, background .15s ease; }
.stoa-col[data-dim="1"] { opacity: .45; }
.stoa-col[data-dim="1"]:hover { opacity: .8; }
.stoa-col[data-spot="1"] .stoa-colhead .stoa-eyebrow { color: var(--ui-text-primary); }
.stoa-col[data-over="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }   /* inset: the board clips anything drawn outside a column */
.stoa-weight { height: 4px; margin: 0 2px 9px; background: var(--ui-stroke-primary); }
.stoa-weight i { display: block; height: 100%; transition: width .4s ease; }
.stoa-weight[data-won="1"] i { background: var(--ui-text-primary); }
.stoa-col:has(.stoa-weight[data-won="1"]) .stoa-colhead .stoa-eyebrow { color: var(--ui-text-primary); }
.stoa-card[draggable="true"] { cursor: grab; }
.stoa-card[draggable="true"]:active { cursor: grabbing; }
.stoa-split > .stoa-box { animation: stoa-slide .22s ease both; }
@keyframes stoa-rise { from { transform: scaleY(.12); opacity: 0; } }
@keyframes stoa-lift { from { transform: translateY(8px); opacity: 0; } }
@keyframes stoa-slide { from { transform: translateX(10px); opacity: 0; } }
@keyframes stoa-blink { 50% { opacity: 0; } }
@keyframes stoa-glow { 50% { filter: drop-shadow(0 0 9px color-mix(in srgb, var(--ui-text-primary) 92%, transparent)) drop-shadow(0 0 28px color-mix(in srgb, var(--stoa-h7) 80%, transparent)); } }
@keyframes stoa-pulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--ui-red) 16%, transparent); } }
@media (prefers-reduced-motion: reduce) { .stoa *, .stoa *::before { animation: none !important; transition: none !important; } }
.stoa-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; margin-top: 12px; }
.stoa-form label { display: block; min-width: 0; }
.stoa-form label[data-wide="1"] { grid-column: 1 / -1; }
.stoa-field { display: block; width: 100%; height: 28px; margin-top: 3px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-field:focus { border-color: var(--ui-stroke-primary); }
.stoa-field::placeholder { color: var(--ui-text-quaternary); }
.stoa-edit { margin-top: 12px; padding-top: 2px; border-top: 1px dotted var(--ui-stroke-primary); }
/* Sample data on show: said plainly, on every surface, with the way back beside it. */
.stoa-tour { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 16px; margin: -6px 0 18px; padding: 8px 10px 8px 14px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); }
.stoa-tourline { margin-bottom: 10px; padding: 4px 8px; border: 1px dashed var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); font-size: 11px; }
.stoa-stat { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: default; }
.stoa-stat[data-press="1"] { cursor: pointer; }
.stoa-stat[data-press="1"]:hover b { color: var(--stoa-accent-text); }
/* Today: a greeting, then each kind of thing that wants you as a group that opens. Closed, a group is one calm line. */
.stoa-today { min-width: 0; }
.stoa-todayhead { margin: 6px 0 20px; }
.stoa-hello { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -.015em; line-height: 1.2; }
.stoa-todayhead .stoa-sub { margin: 6px 0 0; font-size: 13px; }
.stoa-folds { border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-folds:empty { display: none; }
.stoa-fold { border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-foldhead { display: flex; align-items: center; gap: 10px; }
.stoa-foldmain { flex: 1; min-width: 0; display: grid; grid-template-columns: auto 28px 196px minmax(0, 1fr); gap: 0 12px; align-items: baseline; padding: 16px 8px 15px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; transition: background .15s ease; }
.stoa-foldmain:hover { background: var(--ui-row-hover-background); }
.stoa-foldmark { font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-foldmain:hover .stoa-foldmark, .stoa-fold[data-open="1"] .stoa-foldmark { color: var(--ui-text-primary); }
.stoa-foldcount { font-family: var(--stoa-mono); font-size: 15px; font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); }
.stoa-foldcount[data-tone="bad"] { color: var(--ui-red); }
.stoa-foldcount[data-tone="warn"] { color: var(--ui-orange); }
.stoa-foldcount[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-foldtitle { font-size: 14px; font-weight: 600; white-space: nowrap; }
.stoa-foldsum { padding-left: 6px; font-size: 12.5px; color: var(--ui-text-tertiary); }
.stoa-foldaside { flex: none; padding-right: 6px; }
.stoa-foldbody { padding: 0 0 18px 44px; animation: stoa-lift .16s ease both; }
.stoa-foldbody > .stoa-trow:first-child, .stoa-foldbody > .stoa-irow:first-child { border-top: 0; }
.stoa-foldbody > .stoa-proposal:last-child { margin-bottom: 0; }
@container (max-width: 760px) { .stoa-foldmain { grid-template-columns: auto 28px minmax(0, 1fr); } .stoa-foldsum { display: none; } .stoa-foldbody { padding-left: 8px; } }
.stoa-trow { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 2px 12px; align-items: center; padding: 2px 6px 2px 0; border-top: 1px dotted var(--ui-stroke-primary); border-radius: 0; transition: background .15s ease; }
.stoa-trow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-tmain { display: flex; gap: 9px; align-items: flex-start; min-width: 0; padding: 7px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-tmain:hover { background: var(--ui-row-hover-background); }
.stoa-tacts { display: flex; gap: 6px; align-items: center; justify-content: flex-end; }
.stoa-tacts:empty { display: none; }
.stoa-nextform { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1fr) 150px auto auto; gap: 6px 8px; align-items: center; padding: 2px 8px 10px 23px; animation: stoa-lift .18s ease both; }
.stoa-nextform .stoa-field, .stoa-pickdate .stoa-field { margin-top: 0; }
.stoa-box .stoa-nextform { grid-template-columns: minmax(0, 1fr) 132px; padding: 12px 0 0; }
.stoa-box .stoa-nextform .stoa-btn { justify-self: start; }
.stoa-pickdate { display: inline-flex; gap: 4px; align-items: center; }
.stoa-pickdate .stoa-field { width: 138px; }
.stoa-pace { margin: -4px 2px 9px; font-family: var(--stoa-mono); font-size: 10px; color: var(--ui-text-quaternary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Cash: the strip, the forecast, what has been billed, and invoices */
.stoa-cash { min-width: 0; }
.stoa-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; }
.stoa-tile { padding: 12px 14px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-tile b { display: block; margin: 5px 0 1px; font-family: var(--stoa-mono); font-size: 22px; font-weight: 600; letter-spacing: -.015em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tile[data-tone="bad"] b { color: var(--ui-red); }
.stoa-tile[data-tone="warn"] b { color: var(--ui-orange); }
.stoa-tile[data-tone="good"] b { color: var(--stoa-accent-text); }
.stoa-fc { display: grid; grid-template-columns: 120px minmax(0, 1fr) 200px; gap: 12px; align-items: center; padding: 5px 0; }
.stoa-fc u { position: relative; display: block; height: 8px; background: var(--ui-stroke-primary); text-decoration: none; overflow: hidden; }
.stoa-fc u i, .stoa-fc u b { position: absolute; top: 0; bottom: 0; left: 0; transition: width .4s ease; }
.stoa-fc u i { background: color-mix(in srgb, var(--ui-text-primary) 46%, transparent); }
.stoa-fc u b { background: var(--stoa-accent); }
.stoa-fc[data-key="past"] > span:first-child { color: var(--ui-orange); }
.stoa-fc[data-key="none"] u b { background: color-mix(in srgb, var(--stoa-accent) 55%, transparent); }
.stoa-fc .stoa-num { text-align: right; }
.stoa-billbar { position: relative; height: 4px; margin: 8px 0 2px; background: var(--ui-stroke-primary); overflow: hidden; }
.stoa-billbar i { position: absolute; top: 0; bottom: 0; left: 0; transition: width .4s ease; }
.stoa-billbar i[data-kind="invoiced"] { background: color-mix(in srgb, var(--ui-text-primary) 46%, transparent); }
.stoa-billbar i[data-kind="paid"] { background: var(--stoa-accent); }
.stoa-irow { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-irow > .stoa-grow { flex: 1 1 200px; }      /* in a narrow panel the moves drop to a second line; the words are never squeezed */
.stoa-irow > .stoa-tacts { margin-left: auto; }
.stoa-irow[data-state="void"] { opacity: .5; }
.stoa-log > .stoa-irow:first-of-type { border-top: 0; }
/* Approvals, undo, people and companies */
.stoa-proposal { margin-bottom: 10px; padding: 11px 14px 12px; border: 1px solid var(--ui-stroke-secondary); border-left: 2px solid var(--stoa-accent); border-radius: var(--stoa-r); background: var(--ui-bg-card); animation: stoa-lift .2s ease both; }
.stoa-prophead { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.stoa-proplink { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-proplink.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-proplink.stoa-name { font-weight: 600; }
.stoa-proplink:hover { color: var(--stoa-accent-text); }
.stoa-propchange { margin-top: 5px; font-size: 14px; font-weight: 550; overflow-wrap: anywhere; }
.stoa-evidence { margin: 9px 0 0; padding: 2px 0 2px 11px; border-left: 2px dotted var(--ui-text-tertiary); color: var(--ui-text-secondary); }
.stoa-evidence small { display: block; margin-bottom: 2px; }
.stoa-evidence span { display: block; overflow-wrap: anywhere; }
.stoa-undobar { display: flex; align-items: center; gap: 10px; width: fit-content; max-width: 100%; margin: 0 auto 8px; padding: 4px 5px 4px 13px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 92%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); box-shadow: 0 12px 34px -14px rgba(0, 0, 0, .65); font-size: 12.5px; animation: stoa-lift .2s ease both; }
.stoa-undobar b { font-weight: 600; }
.stoa-undobar .stoa-btn { height: 25px; border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 600; }
.stoa-person { display: flex; gap: 6px; align-items: center; padding: 5px 0; }
.stoa-notes { height: auto; min-height: 62px; margin-top: 5px; padding: 7px 8px; line-height: 1.45; resize: vertical; }
.stoa-waiting { display: block; width: fit-content; margin: -6px 0 16px; padding: 6px 13px; border: 1px solid var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font: inherit; font-size: 12.5px; cursor: pointer; }
.stoa-waiting b { font-family: var(--stoa-mono); font-weight: 600; }
.stoa-viewrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin: 0 0 14px; }
/* -- the database: accounts, deals and people as a table to look through ------------------------------ */
.stoa-dbtop { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 14px; margin: 0 0 14px; }
.stoa-dbtop .stoa-tabs { margin: 0; }
.stoa-dbviews { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 12px; }
.stoa-dbview { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 10px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; transition: background .15s ease, color .15s ease; }
.stoa-dbview:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-dbview[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font-weight: 600; }
.stoa-dbview[data-own="1"]::before { content: "★"; font-size: 9px; color: var(--stoa-accent-text); }
.stoa-dbview small { font-size: 10px; opacity: .6; font-weight: 400; }
.stoa-dbfacets { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
.stoa-dbfacet { flex: 0 1 132px; min-width: 96px; padding: 7px 10px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; text-align: left; cursor: pointer; transition: background .15s ease, border-color .15s ease; }
.stoa-dbfacet:hover { background: var(--ui-control-hover-background); }
.stoa-dbfacet[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-dbfacet span { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; }
.stoa-dbfacet span b { font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-dbfacet i { display: block; height: 6px; min-width: 6px; margin-top: 7px; background: conic-gradient(from 90deg at 2px 2px, var(--stoa-accent) 90deg, #0000 0) 0 0 / 6px 6px round; }
.stoa-dbbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 10px; }
.stoa-dbbar .stoa-input { flex: 0 1 240px; min-width: 120px; height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-dbbar .stoa-input:focus { border-color: var(--ui-stroke-primary); }
.stoa-dbbar .stoa-input::placeholder { color: var(--ui-text-quaternary); }
.stoa-dbbar .stoa-menulist { min-width: 220px; }
.stoa-dbfilters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 10px; }
.stoa-dbfilter { display: inline-flex; align-items: center; gap: 4px; height: 28px; padding: 0 2px 0 9px; border: 1px solid var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); font-size: 12px; }
.stoa-dbfilter b { font-weight: 550; white-space: nowrap; }
.stoa-dbfilter select, .stoa-dbfilter input { height: 22px; max-width: 170px; padding: 0 4px; border: 0; border-bottom: 1px dotted var(--ui-stroke-primary); border-radius: 0; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-dbfilter input[type="number"] { width: 64px; }
.stoa-dbfilter .stoa-icon { width: 22px; height: 22px; }
.stoa-dbcheck { display: flex; align-items: center; gap: 8px; padding: 6px 10px; font-size: 12.5px; cursor: pointer; border-radius: var(--stoa-r); }
.stoa-dbcheck:hover { background: var(--ui-row-hover-background); }
.stoa-dbcheck input { accent-color: var(--stoa-accent); }
.stoa-dbwrap { overflow: auto; max-height: max(360px, calc(100vh - 330px)); border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--stoa-dbbg); --stoa-dbbg: var(--stoa-page); }
.stoa-db { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12.5px; }
.stoa-db th { position: sticky; top: 0; z-index: 2; padding: 0; border-bottom: 1px solid var(--ui-stroke-primary); background: color-mix(in srgb, var(--ui-text-primary) 4%, var(--stoa-dbbg)); text-align: left; white-space: nowrap; font-weight: 400; }
.stoa-db th button { display: flex; align-items: center; gap: 6px; width: 100%; padding: 9px 12px 8px; border: 0; background: transparent; color: var(--ui-text-tertiary); font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; cursor: pointer; }
.stoa-db th button:hover, .stoa-db th[data-sorted="1"] button { color: var(--ui-text-primary); }
.stoa-db th[data-num="1"] button { justify-content: flex-end; }
.stoa-db td { max-width: 300px; padding: 7px 12px; border-bottom: 1px dotted var(--ui-stroke-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; background: var(--stoa-dbbg); }
.stoa-db tbody tr:last-child td { border-bottom: 0; }
.stoa-db tbody tr:hover td { background: color-mix(in srgb, var(--ui-text-primary) 5%, var(--stoa-dbbg)); }
.stoa-db td[data-num="1"] { text-align: right; font-family: var(--stoa-mono); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.stoa-db td[data-tone="bad"] { color: var(--ui-red); }
.stoa-db td[data-tone="warn"] { color: var(--ui-orange); }
.stoa-db td[data-empty="1"] { color: var(--ui-text-quaternary); }
.stoa-db th:first-child, .stoa-db td:first-child { position: sticky; left: 0; }
.stoa-db th:nth-child(2), .stoa-db td:nth-child(2) { position: sticky; left: 36px; box-shadow: inset -1px 0 0 var(--ui-stroke-tertiary); }
.stoa-db th:first-child, .stoa-db th:nth-child(2) { z-index: 3; }
.stoa-db td:first-child, .stoa-db td:nth-child(2) { z-index: 1; }
.stoa-db td:nth-child(2) { max-width: 260px; }
.stoa-db .stoa-dbtick { width: 36px; min-width: 36px; max-width: 36px; padding: 0 0 0 12px; text-overflow: clip; }
.stoa-db .stoa-dbtick input { display: block; margin: 0; accent-color: var(--stoa-accent); cursor: pointer; }
.stoa-db tbody tr[data-on="1"] td { background: color-mix(in srgb, var(--stoa-accent) 9%, var(--stoa-dbbg)); }
.stoa-db td[data-edit="1"] { cursor: text; }
.stoa-db td[data-edit="1"]:hover { box-shadow: inset 0 0 0 1px var(--ui-stroke-primary); }
.stoa-db td[data-editing="1"] { padding: 2px 4px; overflow: visible; box-shadow: inset 0 0 0 2px var(--stoa-accent-line); }
.stoa-dbedit { display: block; width: 100%; min-width: 120px; height: 28px; padding: 0 7px; border: 0; border-radius: 0; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12.5px; outline: none; }
.stoa-dbname { display: flex; align-items: center; gap: 4px; min-width: 0; }
.stoa-dbname > :first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.stoa-dbrename { flex: none; width: 20px; height: 20px; font-size: 12px; opacity: 0; }
.stoa-db tr:hover .stoa-dbrename, .stoa-dbrename:focus-visible { opacity: .7; }
.stoa-dbbulk { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 0 0 10px; padding: 8px 10px; border: 1px solid var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); }
.stoa-dbbulk > b { margin-right: 6px; font-family: var(--stoa-mono); font-size: 11.5px; }
.stoa-dbbulkform { display: inline-flex; gap: 6px; align-items: center; }
.stoa-dbbulkform .stoa-field { width: 230px; margin: 0; background: var(--stoa-page); }
.stoa-db tr.stoa-dbgroup td { position: sticky; left: 0; padding: 8px 12px; border-bottom: 1px solid var(--ui-stroke-secondary); background: color-mix(in srgb, var(--ui-text-primary) 6%, var(--stoa-dbbg)); cursor: pointer; }
.stoa-db tr.stoa-dbgroup td > span { display: inline-flex; align-items: center; gap: 10px; }
.stoa-dbfoot { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin: 8px 2px 0; }
.stoa-dbsave { display: inline-flex; gap: 6px; align-items: center; }
.stoa-dbsave input { width: 190px; margin: 0; }
.stoa-companies { min-width: 0; }
.stoa-crow { display: grid; grid-template-columns: minmax(0, 2.2fr) minmax(0, 1.1fr) minmax(0, 1.1fr) 64px 84px; gap: 12px; align-items: center; width: 100%; padding: 9px 10px; border: 0; border-top: 1px dotted var(--ui-stroke-primary); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-crow[data-cols="7"] { grid-template-columns: minmax(0, 2fr) minmax(0, 1.2fr) 76px minmax(0, 1fr) minmax(0, 1fr) 52px 80px; }
.stoa-crow:hover { background: var(--ui-row-hover-background); }
.stoa-crow[data-dim="1"] > * { opacity: .55; }
.stoa-newgroup { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; margin: 0 0 12px; max-width: 760px; }
.stoa-pipes { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.stoa-pipe { display: inline-flex; align-items: center; gap: 8px; height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-pipe:hover { border-color: var(--ui-stroke-primary); color: var(--ui-text-primary); }
.stoa-pipe[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font-weight: 600; }
.stoa-pipe small { font-size: 10.5px; opacity: .7; font-variant-numeric: tabular-nums; }
.stoa-piperow { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-sechead + .stoa-piperow { border-top: 0; padding-top: 0; }
.stoa-newgroup > label { flex: 1 1 200px; }
.stoa-kinds { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.stoa-proposal[data-follow="1"] { margin-top: -6px; margin-left: 14px; }      /* another suggestion about the record above it */
.stoa-crow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-crow .stoa-num { text-align: left; }
.stoa-chead, .stoa-chead:hover { border-top: 0; background: transparent; cursor: default; padding-bottom: 6px; }
.stoa-level { display: flex; gap: 10px; align-items: flex-start; width: 100%; margin-bottom: 6px; padding: 9px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; transition: border-color .15s ease, background .15s ease; }
.stoa-level:hover { border-color: var(--ui-stroke-primary); }
.stoa-level[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); cursor: default; }
.stoa-btn[data-kind="go"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 600; }
.stoa-btn[data-kind="go"]:hover { background: color-mix(in srgb, var(--ui-accent) 30%, transparent); }
.stoa-tacts .stoa-btn:not([data-kind="go"]), .stoa-tacts .stoa-select { border-color: var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.stoa-tacts .stoa-btn:not([data-kind="go"]):hover, .stoa-tacts .stoa-select:hover { color: var(--ui-text-primary); }
.stoa-tacts .stoa-select { max-width: 112px; }
.stoa-ahead .stoa-row { align-items: center; }
.stoa-aheadrow { display: flex; align-items: center; gap: 2px; margin: 0 -6px; }
.stoa-aheadrow .stoa-row { flex: 1; min-width: 0; }
.stoa-weekline { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 5px; color: var(--ui-text-secondary); }
.stoa-weekline b { font-family: var(--stoa-mono); font-size: 15px; font-weight: 600; color: var(--ui-text-primary); font-variant-numeric: tabular-nums; }
/* The way in, and Setup */
.stoa-welcome { max-width: 1040px; }
.stoa-hero { position: relative; display: flex; flex-direction: column; justify-content: flex-end; min-height: 300px; margin: 2px 0 18px; padding: 0 0 6px; }
.stoa-hero > * { position: relative; }
.stoa-heroart { position: absolute; inset: 0; pointer-events: none; -webkit-mask-image: linear-gradient(90deg, transparent 34%, #000 58%); mask-image: linear-gradient(90deg, transparent 34%, #000 58%); }
@container (max-width: 760px) { .stoa-heroart { display: none; } .stoa-hero { min-height: 0; } }
.stoa-herotitle { max-width: 15ch; margin: 8px 0 8px; font-size: 34px; font-weight: 600; letter-spacing: -.02em; line-height: 1.08; }
.stoa-hero .stoa-reading { max-width: 40ch; }
.stoa-modal { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; padding: 28px 20px; background: color-mix(in srgb, var(--background, #0d1015) 62%, transparent); -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px); animation: stoa-lift .16s ease both; }
.stoa-modalpanel { width: min(980px, 100%); max-height: calc(100vh - 56px); overflow-y: auto; padding: 20px 22px 0; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--ui-bg-elevated, var(--background, #14161b)); box-shadow: 0 30px 80px -20px rgba(0, 0, 0, .55); }
.stoa-modalhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.stoa-modaltitle { margin-top: 4px; font-size: 19px; font-weight: 600; letter-spacing: -.01em; }
.stoa-modalsec { margin-top: 18px; }
.stoa-modalfoot { position: sticky; bottom: 0; display: flex; gap: 8px; align-items: center; margin: 18px -22px 0; padding: 12px 22px; border-top: 1px dotted var(--ui-stroke-primary); background: var(--ui-bg-elevated, var(--background, #14161b)); }
.stoa-describe summary { cursor: pointer; color: var(--ui-text-secondary); }
.stoa-describe[open] summary { margin-bottom: 8px; }
.stoa-warn { margin: 8px 0; padding: 8px 10px; border: 1px dashed var(--ui-orange); border-radius: var(--stoa-r); color: var(--ui-text-primary); font-size: 12.5px; }
.stoa-shapes { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.stoa-shape { display: flex; flex-direction: column; gap: 2px; padding: 14px 16px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; transition: border-color .15s ease, transform .15s ease, background .15s ease; }
.stoa-shape:hover { border-color: var(--stoa-accent-line); transform: translateY(-1px); }
.stoa-shape[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-shapehead { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.stoa-shapetitle { font-size: 16px; font-weight: 600; letter-spacing: -.005em; }
.stoa-uses { margin: 8px 0 6px; padding-left: 0; list-style: none; color: var(--ui-text-secondary); font-size: 12.5px; line-height: 1.6; }
.stoa-uses li::before { content: "·"; display: inline-block; width: 14px; font-family: var(--stoa-mono); font-weight: 700; color: var(--ui-text-tertiary); }
.stoa-stagelist { list-style: none; margin: 0; padding: 0; max-width: 560px; }
.stoa-stagelist li { display: flex; gap: 4px; align-items: center; margin-bottom: 6px; }
.stoa-stagelist .stoa-field { margin-top: 0; height: 32px; font-size: 13px; }
.stoa-stagelist .stoa-icon { width: 30px; height: 30px; }
.stoa-stagenum { flex: none; width: 22px; font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-tertiary); text-align: right; margin-right: 4px; }
.stoa-doors { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-top: 14px; }
.stoa-door { display: flex; flex-direction: column; gap: 3px; transition: border-color .15s ease, transform .15s ease; }
.stoa-door:hover { border-color: var(--ui-stroke-primary); transform: translateY(-1px); }
.stoa-door[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-door .stoa-btns { margin-top: auto; padding-top: 10px; }
.stoa-chain { margin: 8px 0 6px; font-family: var(--stoa-mono); font-size: 10.5px; letter-spacing: .02em; line-height: 1.7; color: var(--ui-text-secondary); }
.stoa-setup { columns: 2 380px; column-gap: 14px; max-width: 1120px; }
/* Setup as one short list: a step is a line that says where it stands, and opens onto the way to do it right there.
   Titles are in the page's strongest ink at a size that reads on a light theme as well as a dark one. */
.stoa-setup2 { max-width: 880px; }
.stoa-setintro h2 { margin: 0 0 4px; font-size: 21px; font-weight: 600; letter-spacing: -.015em; color: var(--ui-text-primary); }
.stoa-setintro p { margin: 0; font-size: 13.5px; color: var(--ui-text-secondary); }
.stoa-setgroup { display: flex; align-items: baseline; gap: 10px; margin: 22px 0 8px; font-family: var(--stoa-mono); font-size: 11px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-primary); }
.stoa-setgroup::before { content: "░▒▓"; letter-spacing: 0; font-weight: 400; background: var(--stoa-bar); -webkit-background-clip: text; background-clip: text; color: transparent; }
.stoa-setgroup span { font-weight: 400; letter-spacing: .04em; text-transform: none; color: var(--ui-text-secondary); }
.stoa-steps { border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-step + .stoa-step { border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-stepmain { display: flex; gap: 12px; align-items: flex-start; width: 100%; padding: 13px 16px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-stepmain:hover { background: var(--ui-row-hover-background); }
.stoa-stepmain:focus-visible { outline: 2px solid var(--stoa-accent); outline-offset: -2px; }
.stoa-stepmain .stoa-tick { margin-top: 2px; }
.stoa-step[data-done="1"] .stoa-tick { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-stepmain .stoa-foldmark { margin-top: 3px; }
.stoa-steptitle { display: block; font-size: 14px; font-weight: 600; color: var(--ui-text-primary); }
.stoa-stepopt { margin-left: 8px; font-family: var(--stoa-mono); font-size: 10px; font-weight: 400; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-secondary); }
.stoa-stepopt::before { content: "["; opacity: .6; } .stoa-stepopt::after { content: "]"; opacity: .6; }
.stoa-stepstatus { display: block; margin-top: 2px; font-size: 12.5px; color: var(--ui-text-secondary); overflow-wrap: anywhere; }
.stoa-stepbody { padding: 2px 16px 18px 44px; }
.stoa-stepbody[data-plain="1"] { padding-left: 16px; }
.stoa-steplabel { margin: 16px 0 8px; padding-top: 12px; border-top: 1px dotted var(--ui-stroke-primary); font-family: var(--stoa-mono); font-size: 10.5px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-primary); }
.stoa-setup2 .stoa-sub { color: var(--ui-text-secondary); }
.stoa-setup2 .stoa-eyebrow { color: var(--ui-text-secondary); }
.stoa-setup2 .stoa-stepbody .stoa-setbox { padding-bottom: 0; }
.stoa-setup2 .stoa-stepbody .stoa-setbox > .stoa-box { padding: 0; border: 0; background: transparent; }
.stoa-setup2 .stoa-stepbody .stoa-setbox .stoa-sechead { display: none; }
.stoa-stepbody .stoa-setrow:first-child { border-top: 0; padding-top: 4px; }
.stoa-ways { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px 24px; }
.stoa-pathrow { display: flex; gap: 6px; align-items: center; }
.stoa-notepick { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 2px; }
.stoa-notepick .stoa-btn[data-on="1"] { background: var(--ui-text-primary); border-color: var(--ui-text-primary); color: var(--stoa-page); }
.stoa-noteway { margin-top: 10px; padding: 12px 14px; border: 1px dotted var(--ui-stroke-primary); border-radius: var(--stoa-r); }
.stoa-notesteps { margin: 0 0 10px; padding-left: 22px; list-style: decimal outside; } .stoa-notesteps li { margin: 4px 0; padding-left: 2px; }
.stoa-noteconn { margin-top: 14px; padding-top: 12px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-linkbtn { padding: 0; border: 0; background: none; color: inherit; font: inherit; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
.stoa-linkbtn:disabled { opacity: .5; cursor: default; }
.stoa-notewait { margin-top: 16px; padding-top: 12px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-noterow { display: grid; grid-template-columns: 54px minmax(0, 1fr) minmax(0, 220px); gap: 10px; align-items: start; padding: 7px 0; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-notematch { text-align: right; min-width: 0; } .stoa-notematch b { display: block; font-weight: 550; overflow-wrap: anywhere; }
.stoa-notematch small { display: block; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-notematch[data-on="1"] b::before { content: "→ "; color: var(--stoa-accent); }
@container (max-width: 620px) { .stoa-noterow { grid-template-columns: 54px minmax(0, 1fr); } .stoa-notematch { grid-column: 2; text-align: left; } }
.stoa-pathrow .stoa-field { flex: 1; min-width: 0; margin-top: 0; font-family: var(--stoa-mono); }
.stoa-code { padding: 1px 5px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); font-family: var(--stoa-mono); font-size: 11.5px; white-space: nowrap; }
.stoa-linkbtn { padding: 0; border-width: 0 0 1px; background: transparent; font: inherit; cursor: pointer; }
/* The market map. A mark is a disc of dots: colour is fit (the heat ramp, leaned toward the page's ink so it reads on a
   light theme too), size is what "Size by" says. Solid is in the pipeline, a dashed edge arrived since you last looked, a ring is a reason to call. */
.stoa-mapwrap { --stoa-m1: color-mix(in oklab, var(--stoa-h1) 84%, var(--ui-text-primary)); --stoa-m2: color-mix(in oklab, var(--stoa-h2) 84%, var(--ui-text-primary));
  --stoa-m3: color-mix(in oklab, var(--stoa-h3) 84%, var(--ui-text-primary)); --stoa-m4: color-mix(in oklab, var(--stoa-h4) 84%, var(--ui-text-primary));
  --stoa-m5: color-mix(in oklab, var(--stoa-h5) 84%, var(--ui-text-primary)); --stoa-m6: color-mix(in oklab, var(--stoa-h6) 84%, var(--ui-text-primary));
  --stoa-m7: color-mix(in oklab, var(--stoa-h7) 84%, var(--ui-text-primary)); }
.stoa-maptop { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: flex-end; margin-bottom: 14px; }
.stoa-menu { position: relative; }
.stoa-menutitle { display: inline-block; max-width: 100%; margin: 0 0 2px -8px; padding: 3px 8px; overflow-wrap: anywhere; text-wrap: balance; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary);
  font: inherit; font-size: 20px; font-weight: 600; letter-spacing: -.015em; line-height: 1.25; text-align: left; cursor: pointer; }
.stoa-menutitle:hover, .stoa-menutitle[aria-expanded="true"] { background: var(--ui-row-hover-background); }
.stoa-menutitle:focus-visible, .stoa-menuitem:focus-visible { outline: 2px solid var(--stoa-accent); outline-offset: -2px; }
.stoa-menucaret { font-size: 15px; font-weight: 400; color: var(--ui-text-secondary); }
.stoa-btn .stoa-menucaret { margin-left: 2px; }
.stoa-menulist { position: absolute; z-index: 30; top: calc(100% + 4px); left: -8px; width: max-content; min-width: 300px; max-width: min(460px, 86vw); max-height: 420px; overflow-y: auto; padding: 5px;
  border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--stoa-page); box-shadow: 0 10px 30px rgba(0, 0, 0, .28); }
.stoa-menuitem { display: block; width: 100%; padding: 8px 10px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-primary); font: inherit; text-align: left; cursor: pointer; }
.stoa-menuitem:hover { background: var(--ui-row-hover-background); }
.stoa-menuitem[data-on="1"] { box-shadow: inset 2px 0 0 var(--stoa-accent); background: var(--stoa-accent-soft); }
.stoa-menuitem[data-danger="1"] { color: var(--ui-red); }
.stoa-menuitem > span { display: block; font-weight: 550; overflow-wrap: anywhere; }
.stoa-menuitem small { display: block; margin-top: 1px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-menurule { height: 0; margin: 5px 4px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-mapform { max-width: 720px; margin-bottom: 14px; }
/* A window over the page. It is a real dialog, so it sits above everything, takes the keyboard, and Escape closes it. */
.stoa-dialog { position: fixed; inset: 0; margin: auto; width: min(900px, 94vw); height: fit-content; max-height: 90vh; padding: 0; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--stoa-page); color: var(--ui-text-primary); font: inherit; overflow: hidden; }
.stoa-dialog::backdrop { background: rgba(0, 0, 0, .5); }
.stoa-dialogin { max-height: 90vh; overflow-y: auto; padding: 20px 24px 22px; }
.stoa-dialoghead { position: sticky; top: -20px; z-index: 1; display: flex; gap: 12px; align-items: flex-start; margin: -20px -24px 16px; padding: 20px 24px 14px; background: var(--stoa-page); border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-dialog .stoa-sub, .stoa-dialog .stoa-eyebrow { color: var(--ui-text-secondary); }
.stoa-factgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px 32px; }
.stoa-factgrid > section { min-width: 0; }
.stoa-factfigs { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-bottom: 12px; }
.stoa-factfigs b, .stoa-factcost b { display: block; font-family: var(--stoa-mono); font-size: 24px; font-weight: 600; letter-spacing: -.02em; line-height: 1.15; font-variant-numeric: tabular-nums; }
.stoa-factfigs small, .stoa-factcost small { display: block; font-size: 11.5px; color: var(--ui-text-secondary); }
.stoa-factrow { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 12px; align-items: baseline; padding: 7px 0; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-factrow .stoa-mapmeter { grid-column: 1 / -1; margin-top: 3px; }
.stoa-factcost { margin-bottom: 8px; } .stoa-factcost p { margin: 6px 0 0; }
.stoa-factlimits { margin-top: 22px; padding-top: 16px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-factlimit { display: grid; grid-template-columns: minmax(0, 1fr) 120px 130px; gap: 12px; align-items: center; padding: 8px 0; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-factlimit b { display: block; font-weight: 550; } .stoa-factlimit small { display: block; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-factlimit .stoa-field { margin-top: 0; font-family: var(--stoa-mono); text-align: right; }
.stoa-factnow { font-family: var(--stoa-mono); text-align: right; white-space: nowrap; }
@media (max-width: 620px) { .stoa-factlimit { grid-template-columns: minmax(0, 1fr) 100px; } .stoa-factnow { grid-column: 1 / -1; text-align: left; } }
.stoa-mapform .stoa-btns { margin-top: 10px; }
.stoa-maptext { height: auto; min-height: 76px; padding: 8px 10px; font-size: 13px; line-height: 1.45; resize: vertical; }
.stoa-btn[data-kind="danger"] { border-color: var(--ui-red); color: var(--ui-red); font-weight: 600; }
.stoa-btn[data-kind="danger"]:hover { background: color-mix(in srgb, var(--ui-red) 12%, transparent); }
.stoa-maph { font-size: 20px; font-weight: 600; letter-spacing: -.015em; line-height: 1.2; margin: 4px 0 2px; }
.stoa-mapbar { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin-bottom: 8px; }
.stoa-mapquick { display: flex; flex-wrap: wrap; gap: 2px 4px; flex: 1 1 auto; min-width: 0; }
.stoa-mapquick .stoa-btn[data-on="1"] { border-color: var(--ui-stroke-primary); color: var(--ui-text-primary); }
.stoa-mapfind { flex: 0 0 200px; width: 200px; max-width: 100%; margin-top: 0; }
/* The picture, then the company panel, then the breakdowns and the table. On a wide page the panel is a rail beside all
   of it that stays in view as you scroll, so a company picked from the table opens where you can see it. */
.stoa-mapbody { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; }
.stoa-mapa, .stoa-mapb { min-width: 0; }
.stoa-mapside { min-width: 0; overflow-wrap: anywhere; }
@container (min-width: 1040px) {
  .stoa-mapbody { grid-template-columns: minmax(0, 1fr) 348px; grid-template-areas: "a side" "b side"; column-gap: 18px; }
  .stoa-mapa { grid-area: a; } .stoa-mapb { grid-area: b; }
  .stoa-mapside { grid-area: side; position: sticky; top: 12px; max-height: calc(100vh - 232px); overflow-y: auto; }
  .stoa-mapabar { display: none !important; }
}
.stoa-maphow { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; justify-content: space-between; margin: 0 0 8px; }
.stoa-maphow p { flex: 1 1 320px; min-width: 0; margin: 0; }
.stoa-mapnote { margin: 0 0 8px; min-height: 54px; }
.stoa-mapside .stoa-select { width: 100%; max-width: 100%; text-overflow: ellipsis; }      /* a list of long segment names is as wide as the panel, not as its longest name */
.stoa-mapside .stoa-sechead { margin-top: 14px; padding-top: 10px !important; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-mapside > div > .stoa-sechead:first-child { margin-top: 0; padding-top: 0 !important; border-top: 0; }
.stoa-mapside .stoa-btns { margin-top: 10px; }
.stoa-mapscore { display: flex; align-items: baseline; gap: 8px; margin-top: 12px; }
.stoa-mapscore b { font-family: var(--stoa-mono); font-size: 24px; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.stoa-mapscore .stoa-eyebrow { flex: 1; }
.stoa-mapreq { display: grid; grid-template-columns: 26px minmax(0, 1fr); padding: 6px 0; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-mapreq > span:first-child { font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-quaternary); padding-top: 1px; }
.stoa-mapreq[data-on="1"] > span:first-child { color: var(--stoa-m4); }
.stoa-mapreq b { font-weight: 500; } .stoa-mapreq[data-on="0"] b { font-weight: 400; color: var(--ui-text-secondary); }
.stoa-maprow { display: block; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-maprow b { font-weight: 600; } .stoa-maprow:hover b { text-decoration: underline dotted; text-underline-offset: 3px; }
.stoa-map { position: relative; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background-color: var(--ui-bg-card);
  background-image: radial-gradient(var(--ui-stroke-secondary) 1px, transparent 1.2px); background-size: 16px 16px; overflow: hidden; user-select: none; -webkit-user-select: none; }
.stoa-mapsvg { position: absolute; inset: 0; display: block; }
.stoa-mapring { fill: none; stroke: var(--ui-text-tertiary); stroke-width: 1; stroke-dasharray: 1.2 4.6; stroke-linecap: square; animation: stoa-mapfade .7s both; }
.stoa-mapring[data-ghost="1"] { stroke: var(--ui-text-quaternary); }
.stoa-maplink { stroke: var(--ui-text-secondary); stroke-width: 1; stroke-dasharray: 1.2 3.6; stroke-linecap: square; animation: stoa-mapfade .9s both; }
.stoa-mapsplit { stroke: var(--ui-text-quaternary); stroke-width: 1; stroke-dasharray: 1.2 4.6; stroke-linecap: square; }
.stoa-mapaxis { stroke: var(--ui-text-tertiary); stroke-width: 1; }
@keyframes stoa-mapfade { from { opacity: 0; } to { opacity: 1; } }
@keyframes stoa-mappop { from { transform: scale(0); opacity: 0; } to { transform: scale(1); opacity: 1; } }
@keyframes stoa-mappulse { 50% { opacity: .2; } }
.stoa-mapco { cursor: pointer; outline: none; transition: transform .75s cubic-bezier(.2,.75,.2,1), opacity .3s; }
.stoa-mapco[data-dim="1"] { opacity: .12; }
.stoa-mappop { animation: stoa-mappop .5s cubic-bezier(.2,1.25,.4,1) both; }
.stoa-mapedge { fill: none; stroke-width: 1; opacity: .55; }
.stoa-mapco[data-state="in"] .stoa-mapedge { opacity: 0; }
.stoa-mapco[data-state="fresh"] .stoa-mapedge { stroke: var(--ui-text-primary); stroke-dasharray: 3 3; opacity: 1; }
.stoa-mapsig { fill: none; stroke: var(--ui-text-primary); stroke-width: 1; opacity: .75; }
.stoa-mapbrk { fill: none; stroke: var(--ui-text-primary); stroke-width: 1.5; opacity: 0; }
.stoa-mapco[data-sel="1"] .stoa-mapbrk { opacity: 1; }
.stoa-mapco:focus-visible .stoa-mapbrk { opacity: 1; stroke: var(--stoa-accent); }
.stoa-mapco:hover .stoa-mapdisc { filter: brightness(1.12); }
.stoa-maplbl { font-size: 11px; font-weight: 500; fill: var(--ui-text-secondary); text-anchor: middle; opacity: 0; pointer-events: none; transition: transform .75s cubic-bezier(.2,.75,.2,1), opacity .3s;
  paint-order: stroke; stroke: var(--ui-bg-card); stroke-width: 4px; stroke-linejoin: round; }
.stoa-maplbl[data-on="1"] { opacity: 1; } .stoa-maplbl[data-sel="1"] { fill: var(--ui-text-primary); font-weight: 600; }
.stoa-mapover { position: absolute; inset: 0; pointer-events: none; }
.stoa-mapover > * { position: absolute; pointer-events: auto; animation: stoa-mapfade .7s both; }
.stoa-mapcap { transform: translateX(-50%); text-align: center; }
.stoa-mapcap b { display: block; font-family: var(--stoa-mono); font-size: 10.5px; font-weight: 600; letter-spacing: .12em; line-height: 1.35; text-transform: uppercase; color: var(--ui-text-primary); text-wrap: balance; }
.stoa-mapcap > span { font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-secondary); }
.stoa-mapcap .stoa-btn { height: 22px; padding: 0 6px; font-size: 10.5px; margin-left: 4px; }
.stoa-mapghost { transform: translate(-50%, -50%); display: flex; flex-direction: column; align-items: center; gap: 5px; text-align: center; }
.stoa-mapghost b { font-family: var(--stoa-mono); font-size: 10.5px; letter-spacing: .12em; text-transform: uppercase; }
.stoa-mapghost p { margin: 0; font-size: 12px; line-height: 1.4; color: var(--ui-text-secondary); }
.stoa-map[data-narrow="1"] .stoa-mapghost p, .stoa-map[data-narrow="1"] .stoa-maphint { display: none; }
.stoa-mapq { font-family: var(--stoa-mono); font-size: 10.5px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-secondary); pointer-events: none !important; }
.stoa-mapq strong { color: var(--ui-text-primary); font-weight: 600; }
.stoa-mapq em { display: block; font-family: var(--font-sans, inherit); font-size: 11.5px; font-style: normal; letter-spacing: 0; text-transform: none; }
.stoa-maphint { right: 10px; top: 8px; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); pointer-events: none !important; }
.stoa-maptip { z-index: 5; width: 232px; padding: 9px 11px; border: 1px solid var(--ui-stroke-primary); border-radius: var(--stoa-r); background: var(--stoa-page); font-size: 12px; line-height: 1.4; pointer-events: none !important; animation: none !important; }
.stoa-maptip b { display: block; font-size: 13px; }
.stoa-maptip { overflow-wrap: anywhere; }
.stoa-maplasso { z-index: 4; border: 1px dashed var(--ui-text-primary); background: color-mix(in srgb, var(--ui-text-primary) 7%, transparent); pointer-events: none !important; animation: none !important; }
.stoa-maplegend { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 20px; align-items: center; padding: 9px 2px 0; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-secondary); }
.stoa-maplegend > div { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; }
.stoa-maplegend > div > span { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.stoa-mapheat { display: inline-block; width: 60px; height: 6px; background: linear-gradient(90deg, var(--stoa-m1), var(--stoa-m2), var(--stoa-m3), var(--stoa-m4), var(--stoa-m5), var(--stoa-m6), var(--stoa-m7));
  -webkit-mask-image: var(--stoa-grid); mask-image: var(--stoa-grid); -webkit-mask-composite: source-in; mask-composite: intersect; }
.stoa-mapkey { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
.stoa-mapkey[data-kind="on"] { background: var(--stoa-m4); -webkit-mask-image: var(--stoa-grid); mask-image: var(--stoa-grid); -webkit-mask-composite: source-in; mask-composite: intersect; }
.stoa-mapkey[data-kind="in"] { background: var(--stoa-m4); }
.stoa-mapkey[data-kind="fresh"] { border: 1px dashed var(--ui-text-primary); }
.stoa-mapkey[data-kind="sig"] { width: 8px; height: 8px; margin: 0 3px; background: var(--ui-text-tertiary); box-shadow: 0 0 0 2px var(--ui-bg-card), 0 0 0 3px var(--ui-text-primary); }
.stoa-mapcols { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px 32px; margin-top: 22px; align-items: start; }
.stoa-mapcols > div { min-width: 0; }
/* A requirement: its wording on a line of its own, then how much it counts and how common it is, always in the same places. */
.stoa-mapweight { padding: 9px 0; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-mapwlabel { overflow-wrap: anywhere; }
.stoa-mapwrow { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; margin-top: 6px; }
.stoa-mapwrow > .stoa-tabs { flex: none; }
.stoa-mapwrow .stoa-mapfacet { flex: 1 1 170px; width: auto; padding: 3px 0; border-bottom: 0; grid-template-columns: 38px minmax(0, 1fr) auto; }
.stoa-mapwrow .stoa-mapfacet .stoa-mapmeter { margin-top: 0; }
.stoa-mapfacet { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 10px; align-items: center; width: 100%; padding: 7px 0; border: 0; border-bottom: 1px dotted var(--ui-stroke-primary);
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-mapfacet:hover { background: var(--ui-row-hover-background); }
.stoa-mapfacet[aria-pressed="true"] { box-shadow: inset 2px 0 0 var(--ui-text-primary); padding-left: 8px; }
.stoa-mapfacet > span { min-width: 0; overflow-wrap: anywhere; }
.stoa-mapfacet b { font-family: var(--stoa-mono); font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stoa-mapfacet small { font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-secondary); }
.stoa-mapmeter { display: block; height: 4px; margin-top: 5px; background: var(--ui-stroke-primary); text-decoration: none; -webkit-mask-image: var(--stoa-grid); mask-image: var(--stoa-grid); -webkit-mask-composite: source-in; mask-composite: intersect; }
.stoa-mapmeter i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-maptablewrap { overflow-x: auto; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); }
.stoa-maptable { width: 100%; min-width: 900px; border-collapse: collapse; table-layout: fixed; }
.stoa-maptable th { padding: 0; text-align: left; border-bottom: 1px dotted var(--ui-text-tertiary); white-space: nowrap; }
.stoa-maptable th button { width: 100%; padding: 9px 10px; border: 0; background: none; text-align: left; font-family: var(--stoa-mono); font-size: 10.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-secondary); cursor: pointer; }
.stoa-maptable th button:hover, .stoa-maptable th button[aria-pressed="true"] { color: var(--ui-text-primary); }
.stoa-maptable td { padding: 9px 10px; border-bottom: 1px dotted var(--ui-stroke-primary); vertical-align: top; overflow-wrap: break-word; }
.stoa-maptable th[data-num="1"] button, .stoa-maptable td[data-num="1"] { text-align: right; }
.stoa-maptable td:first-child, .stoa-maptable th:first-child { padding-right: 0; }
.stoa-maptable tbody tr:last-child td { border-bottom: 0; }
.stoa-maptable td .stoa-btn { height: 24px; padding: 0 9px; }
.stoa-maptable th button { overflow: hidden; text-overflow: ellipsis; }
/* Long words in a cell fold to two lines and stop, so every row is about as tall as the next. The whole of it is in the title and on the panel. */
.stoa-clamp { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.stoa-maptable td b { font-weight: 600; }
.stoa-maptable tbody tr { cursor: pointer; } .stoa-maptable tbody tr:hover { background: var(--ui-row-hover-background); }
.stoa-maptable tbody tr[data-sel="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-maptable tbody tr[data-dim="1"] { opacity: .4; }
.stoa-mapabar { position: sticky; bottom: 76px; z-index: 6; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 14px; padding: 10px 14px; border-radius: var(--stoa-r);
  background: var(--ui-text-primary); color: var(--stoa-page); }
.stoa-mapabar > b { font-family: var(--stoa-mono); font-size: 12px; margin-right: 6px; }
.stoa-mapabar .stoa-btn { color: var(--stoa-page); border-color: color-mix(in srgb, var(--stoa-page) 40%, transparent); }
.stoa-mapabar .stoa-btn:hover { background: color-mix(in srgb, var(--stoa-page) 14%, transparent); }
.stoa-mapabar .stoa-btn[data-kind="primary"] { background: var(--stoa-page); border-color: var(--stoa-page); color: var(--ui-text-primary); }
@media (prefers-reduced-motion: reduce) { .stoa-mapco, .stoa-maplbl, .stoa-mappop, .stoa-mapring, .stoa-maplink, .stoa-mapover > * { transition: none !important; animation: none !important; } }
.stoa-setbox { break-inside: avoid; padding-bottom: 14px; }      /* padding, not a margin: a margin at the foot of a column is dropped, and boxes touched */
.stoa-setbox[data-wide="1"] { column-span: all; }
.stoa-check, .stoa-setrow { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-sechead + .stoa-check, .stoa-sechead + .stoa-setrow { border-top: 0; padding-top: 0; }
.stoa-check:last-child, .stoa-setrow:last-child { padding-bottom: 0; }
/* A step done or not: a box with a mark in it, the way a checklist is typed. */
.stoa-tick { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-top: 2px; border: 1px solid var(--ui-stroke-primary); font-family: var(--stoa-mono); font-style: normal; font-size: 11px; font-weight: 700; line-height: 1; }
.stoa-check[data-done="1"] .stoa-tick { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-check[data-done="1"] .stoa-name { font-weight: 400; color: var(--ui-text-secondary); }
.stoa-lamp { flex: none; width: 8px; height: 8px; margin-top: 5px; border: 1px solid var(--ui-text-quaternary); }
.stoa-lamp[data-on="1"] { border-color: var(--stoa-accent); background: var(--stoa-accent); box-shadow: 0 0 0 3px var(--stoa-accent-soft); }
.stoa-sayline { display: block; width: 100%; margin: 0 0 3px; padding: 5px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font: inherit; text-align: left; cursor: pointer; }
.stoa-sayline::before { content: "›"; margin-right: 8px; font-family: var(--stoa-mono); font-weight: 700; color: var(--stoa-accent-text); }
.stoa-sayline:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.stoa-tip { margin: 0 0 7px; color: var(--ui-text-secondary); }
.stoa-tip code { padding: 1px 5px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-primary); white-space: nowrap; }
.stoa-tip b { font-weight: 550; color: var(--ui-text-primary); }
/* A record, a group or a company on a page of its own: a header, the stages, the next step, then details | activity | related */
.stoa-rp { container-type: inline-size; padding-bottom: 8px; animation: stoa-lift .18s ease both; }
.stoa-backrow { margin: -4px 0 12px; }
.stoa-back { display: inline-flex; align-items: center; gap: 4px; height: 26px; padding: 0 10px 0 6px; border: 0; border-radius: var(--stoa-r); background: transparent; color: var(--ui-text-secondary); font-family: var(--stoa-mono); font-size: 11.5px; cursor: pointer; }
.stoa-back:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-back span { font-size: 15px; line-height: 1; }
.stoa-rphead { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 12px 32px; }
.stoa-rptitle { margin: 3px 0 0; font-size: 26px; font-weight: 600; letter-spacing: -.015em; line-height: 1.15; overflow-wrap: anywhere; }
.stoa-rpwhere { margin-top: 5px; font-size: 13px; color: var(--ui-text-secondary); }
.stoa-rpwhere .stoa-proplink { color: var(--ui-text-primary); font-weight: 500; }
.stoa-rpsep { margin: 0 8px; color: var(--ui-text-quaternary); }
.stoa-rpwhere .stoa-proplink:hover { color: var(--stoa-accent-text); }
.stoa-rpfig { text-align: right; }
.stoa-rpfig b { display: block; font-family: var(--stoa-mono); font-size: 24px; font-weight: 600; letter-spacing: -.015em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-rpstats { display: flex; flex-wrap: wrap; gap: 10px 30px; padding-bottom: 3px; }
.stoa-rpstats b { display: block; margin-top: 3px; font-family: var(--stoa-mono); font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
/* The stages: a strip of cells like the tabs, with a line of dots beneath that heats up as the record moves along. */
.stoa-path { display: flex; margin: 16px 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); }
.stoa-path > * { position: relative; flex: 1 1 0; min-width: 0; height: 30px; padding: 0 10px; border: 0; background: transparent; color: var(--ui-text-tertiary); font-family: var(--stoa-mono); font-size: 11px; display: flex; align-items: center; justify-content: center; }
.stoa-path > * + * { border-left: 1px solid var(--ui-stroke-tertiary); }
.stoa-path > * > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-path button { cursor: pointer; transition: background .15s ease, color .15s ease; }
.stoa-path button:not(:disabled):hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-path button:disabled { cursor: default; }
.stoa-path [data-state="done"] { color: var(--ui-text-secondary); }
.stoa-path > *::after { content: ""; position: absolute; left: 0; right: 0; bottom: -7px; height: 4px; background: var(--ui-stroke-primary); }
.stoa-path [data-state="now"] { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; }
.stoa-path [data-state="won"] { background: var(--ui-text-primary); color: var(--stoa-page); font-weight: 600; box-shadow: 0 0 14px color-mix(in srgb, var(--ui-text-primary) 50%, transparent), 0 0 30px color-mix(in srgb, var(--stoa-h7) 40%, transparent); }
.stoa-path > .stoa-pathend[data-state="won"]::after { background: var(--ui-text-primary); }
.stoa-path [data-state="lost"] { background: color-mix(in srgb, var(--ui-red) 22%, transparent); color: var(--ui-text-primary); font-weight: 550; }
.stoa-pathend { flex: 0 1 auto !important; min-width: 84px !important; }
.stoa-rpbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px 20px; margin-top: 12px; padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-rpbar[data-plain="1"] { margin-top: 14px; padding: 0; border: 0; background: transparent; }
.stoa-rpnext { min-width: 0; flex: 1 1 260px; }
.stoa-rpnextline { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-top: 3px; font-size: 14px; }
.stoa-rpgrid { display: grid; grid-template-columns: minmax(250px, 300px) minmax(0, 1fr) minmax(260px, 330px); gap: 14px; align-items: start; margin-top: 14px; }
@container (max-width: 1080px) { .stoa-rpgrid { grid-template-columns: minmax(240px, 290px) minmax(0, 1fr); } .stoa-rpgrid > :nth-child(3) { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; align-items: start; } }
@container (max-width: 700px) { .stoa-rpgrid { grid-template-columns: minmax(0, 1fr); } .stoa-rpgrid > :nth-child(3) { display: flex; } }
.stoa-rpcol { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.stoa-rpsec { min-width: 0; }
.stoa-rpsec > .stoa-log:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
.stoa-rpsec > .stoa-log:first-child > .stoa-sechead { margin: -2px 0 10px; padding: 0; }
.stoa-rpsechead { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: -2px 0 10px; }
.stoa-rpsechead h3 { margin: 0; font-family: var(--stoa-mono); font-size: 10px; font-weight: 400; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-rpsechead .stoa-btn { height: 24px; }
.stoa-dl { display: grid; grid-template-columns: minmax(0, 1fr); margin: 0; }
.stoa-dl dt { margin-top: 9px; font-family: var(--stoa-mono); font-size: 9.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-dl dt:first-child { margin-top: 0; }
.stoa-dl dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.stoa-dl dd[data-empty="1"] { color: var(--ui-text-quaternary); }
.stoa-alink { color: var(--ui-text-primary); text-decoration: none; border-bottom: 1px dotted var(--ui-text-tertiary); }
.stoa-alink:hover { color: var(--stoa-accent-text); border-bottom-color: var(--stoa-accent-line); }
.stoa-form[data-one="1"] { grid-template-columns: minmax(0, 1fr); }
.stoa-logbox { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 6px; align-items: center; margin-bottom: 6px; }
.stoa-logbox .stoa-field { margin-top: 0; }
.stoa-timeline { list-style: none; margin: 0; padding: 0; }
.stoa-tlday { margin: 14px 0 4px; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-tl { display: flex; gap: 10px; align-items: flex-start; padding: 7px 0; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-tlday + .stoa-tl { border-top: 0; }
.stoa-tlkind { flex: none; width: 66px; padding: 2px 0; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--ui-text-secondary); }
.stoa-tl[data-kind="stage"] .stoa-tlkind, .stoa-tl[data-kind="created"] .stoa-tlkind { color: var(--stoa-accent-text); }
.stoa-tl[data-kind="updated"] .stoa-tlkind { color: var(--ui-text-tertiary); }
.stoa-tl[data-undone="1"] .stoa-tltext { text-decoration: line-through; opacity: .55; }
.stoa-tltext { overflow-wrap: anywhere; white-space: pre-line; }
.stoa-rlist { display: flex; flex-direction: column; margin: 4px -8px 0; }
.stoa-rrow { display: flex; gap: 8px; align-items: center; width: 100%; padding: 6px 8px; border: 0; border-radius: var(--stoa-r); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-rrow:hover { background: var(--ui-row-hover-background); }
.stoa-rrow[data-closed="1"] { opacity: .6; }
.stoa-stagegroup + .stoa-stagegroup { margin-top: 12px; }
.stoa-billform { flex-basis: 100%; display: grid; grid-template-columns: minmax(110px, 150px) minmax(110px, 150px) auto auto; gap: 8px 12px; align-items: end; padding: 6px 0 4px; animation: stoa-lift .18s ease both; }
.stoa-billform label .stoa-field { margin-top: 3px; }
.stoa-placed { margin-top: 10px; padding-top: 8px; border-top: 1px dotted var(--ui-stroke-primary); }
.stoa-placerow { display: flex; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-placerow .stoa-select { flex: none; width: 140px; }
.stoa-billtotal b { display: block; font-family: var(--stoa-mono); font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stoa-crow[data-cols="6"] { grid-template-columns: minmax(0, 1.6fr) 84px minmax(0, 1fr) 70px minmax(0, 1.3fr) 96px; }
.stoa-stagehead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding-bottom: 2px; border-bottom: 1px dotted var(--ui-stroke-primary); }
/* reports: every quantity is drawn in dots, like the funnel. A colour is set once, as --c, by what a mark stands for. */
.stoa-rep { --c: var(--stoa-h2); }
.stoa-rep [data-heat="0"] { --c: var(--stoa-h1); } .stoa-rep [data-heat="1"] { --c: var(--stoa-h2); } .stoa-rep [data-heat="2"] { --c: var(--stoa-h3); }
.stoa-rep [data-heat="3"] { --c: var(--stoa-h4); } .stoa-rep [data-heat="4"] { --c: var(--stoa-h5); } .stoa-rep [data-heat="5"] { --c: var(--stoa-h6); } .stoa-rep [data-heat="6"] { --c: var(--stoa-h7); }
.stoa-rep [data-tone="bad"] { --c: var(--stoa-h6); }
.stoa-rep [data-k="won"] { --c: var(--ui-text-primary); } .stoa-rep [data-k="late"] { --c: var(--stoa-h4); }
.stoa-rep [data-k="early"], .stoa-rep [data-k="open"], .stoa-rep [data-k="approved"] { --c: var(--stoa-h2); }
.stoa-rep [data-k="lost"], .stoa-rep [data-k="declined"] { --c: var(--ui-text-quaternary); } .stoa-rep [data-k="waiting"] { --c: var(--stoa-h5); }
.stoa-repbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin: 0 0 14px; }
.stoa-repbar > div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.stoa-repbar .stoa-tabs { margin: 0; }
.stoa-repbar .stoa-tab { height: 26px; padding: 0 11px; font-size: 12px; }
.stoa-repgrid { display: flex; flex-wrap: wrap; gap: 14px; }
.stoa-repcard { flex: 1 1 320px; min-width: 0; display: flex; flex-direction: column; gap: 12px; padding: 14px 16px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); }
.stoa-repcard[data-wide="1"] { flex: 2 1 500px; }
.stoa-rephead { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
.stoa-rephead h3 { margin: 0; color: var(--ui-text-secondary); font-weight: 600; letter-spacing: .12em; }
.stoa-rephead h3::before { content: "░▒▓"; margin-right: 8px; letter-spacing: 0; font-weight: 400; color: var(--stoa-accent-text); }
.stoa-repsays { margin: auto 0 0; padding-top: 10px; border-top: 1px dotted var(--ui-stroke-primary); font-size: 12.5px; line-height: 1.5; color: var(--ui-text-secondary); }
.stoa-repsays b { color: var(--ui-text-primary); font-weight: 600; }
.stoa-reprows { display: grid; grid-template-columns: fit-content(44%) minmax(0, 1fr) auto; gap: 9px 12px; }
.stoa-reprow { display: grid; grid-template-columns: subgrid; grid-column: 1 / -1; row-gap: 4px; align-items: center; }
.stoa-reprows[data-big="1"] { row-gap: 0; column-gap: 16px; }
.stoa-reprows[data-big="1"] .stoa-reprow { padding: 11px 10px; margin: 0 -10px; border: 1px solid transparent; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-reprows[data-big="1"] .stoa-reprow:last-child { border-bottom: 0; }
.stoa-reprows[data-big="1"] .stoa-reprow[data-on="1"] { border: 1px solid var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-replabel { min-width: 0; padding: 0; border: 0; background: none; text-align: left; font-family: var(--stoa-mono); font-size: 11px; color: var(--ui-text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stoa-reprows[data-big="1"] .stoa-replabel { font-family: inherit; font-size: 13px; color: var(--ui-text-primary); }
button.stoa-replabel { cursor: pointer; } button.stoa-replabel:hover { text-decoration: underline; text-underline-offset: 3px; }
.stoa-reprow[data-on="1"] .stoa-replabel { font-weight: 600; }
.stoa-reptrack { position: relative; display: flex; min-width: 0; height: 12px; }
.stoa-reprows[data-big="1"] .stoa-reptrack { height: 18px; }
.stoa-reptrack > i, .stoa-repsq { display: block; flex: none; height: 100%; background: conic-gradient(from 90deg at 2px 2px, var(--c) 90deg, #0000 0) 0 0 / 6px 6px round; }
.stoa-reptrack > i.stoa-repfill { min-width: 6px; animation: stoa-repgrow .5s ease both; }
.stoa-reptrack > i.stoa-represt { flex: 1 1 0; min-width: 0; --c: color-mix(in srgb, var(--ui-text-primary) 10%, transparent); }
.stoa-repmark { position: absolute; top: -4px; bottom: -4px; width: 2px; background: var(--ui-text-primary); }
.stoa-repsqs { display: flex; min-width: 0; overflow: hidden; }
.stoa-repsq { height: 10px; background: conic-gradient(from 90deg at 3px 3px, var(--c) 90deg, #0000 0) 0 0 / 10px 10px; }
.stoa-reprows[data-big="1"] .stoa-repsq, .stoa-repstrip .stoa-repsq { height: 14px; background: conic-gradient(from 90deg at 4px 4px, var(--c) 90deg, #0000 0) 0 0 / 14px 14px; }
.stoa-repnum { font-family: var(--stoa-mono); font-size: 11.5px; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; color: var(--ui-text-secondary); }
.stoa-repnum b { font-size: 13px; font-weight: 600; color: var(--ui-text-primary); }
.stoa-reprow[data-tone="bad"] .stoa-repnum b { color: var(--stoa-h6); }
.stoa-repwas { grid-column: 2 / -1; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-tertiary); }
.stoa-replegend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-family: var(--stoa-mono); font-size: 10.5px; color: var(--ui-text-secondary); }
.stoa-replegend span { display: inline-flex; align-items: center; gap: 6px; }
.stoa-replegend i { display: block; width: 9px; height: 9px; background: var(--c); }
.stoa-replegend i[data-k="lost"] { background: none; box-shadow: inset 0 0 0 1px var(--ui-text-tertiary); }
.stoa-replegend u { display: block; width: 2px; height: 12px; background: var(--ui-text-primary); }
.stoa-repfig { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 6px 14px; }
.stoa-repbig { font-family: var(--stoa-mono); font-size: 44px; font-weight: 600; letter-spacing: -.03em; line-height: 1; font-variant-numeric: tabular-nums; }
.stoa-repfig span { display: flex; flex-direction: column; gap: 2px; padding-bottom: 3px; font-family: var(--stoa-mono); font-size: 11.5px; color: var(--ui-text-secondary); }
.stoa-repfig small[data-tone="good"] { color: var(--stoa-h4); } .stoa-repfig small[data-tone="bad"] { color: var(--stoa-h6); }
.stoa-repmonths { display: flex; flex-wrap: wrap; gap: 12px 22px; margin-top: 14px; }
.stoa-repmonths > div { display: flex; flex-direction: column; gap: 6px; }
.stoa-repdots { display: flex; flex-wrap: wrap; gap: 4px; max-width: 180px; }
.stoa-repdots i { display: block; width: 14px; height: 14px; background: var(--c); }
.stoa-repdots i[data-k="lost"] { background: none; box-shadow: inset 0 0 0 1px var(--ui-text-tertiary); }
.stoa-reptiles { display: flex; flex-wrap: wrap; gap: 10px 26px; }
.stoa-reptiles b { display: block; font-family: var(--stoa-mono); font-size: 24px; font-weight: 600; letter-spacing: -.02em; line-height: 1.15; font-variant-numeric: tabular-nums; }
.stoa-reptiles div[data-tone="mute"] b { color: var(--ui-text-tertiary); } .stoa-reptiles div[data-tone="warn"] b { color: var(--stoa-h5); }
.stoa-reptiles small { font-size: 12px; color: var(--ui-text-secondary); }
.stoa-repstrip { display: flex; flex-wrap: wrap; margin-top: 12px; }
.stoa-repask { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; padding: 14px 16px; border: 1px dotted var(--ui-stroke-primary); border-radius: var(--stoa-r); }
.stoa-repask form { display: flex; flex-wrap: wrap; gap: 8px; }
.stoa-repask .stoa-field { flex: 1 1 280px; width: auto; height: 32px; margin-top: 0; font-family: var(--stoa-mono); font-size: 12.5px; }
.stoa-repask .stoa-btn { height: 32px; }
.stoa-repchips { display: flex; flex-wrap: wrap; gap: 8px; }
.stoa-repchips .stoa-btn { height: 24px; padding: 0 9px; font-size: 11px; border-color: var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.stoa-replist { border-top: 1px solid var(--ui-stroke-secondary); }
.stoa-repitem { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 14px; padding: 11px 2px; border-bottom: 1px dotted var(--ui-stroke-primary); }
.stoa-repitem > div:first-child { flex: 1 1 320px; min-width: 0; }
.stoa-repitem > div:last-child { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.stoa-repitem .stoa-sub { display: block; font-family: var(--stoa-mono); font-size: 11px; }
.stoa-repopen { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
.stoa-repopen > div:first-child { flex: 999 1 520px; min-width: 0; display: flex; flex-direction: column; gap: 14px; }
.stoa-repopen > div:last-child { flex: 1 1 280px; min-width: 0; display: flex; flex-direction: column; gap: 14px; }
.stoa-reptitle { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px; margin: 4px 0 16px; }
.stoa-reptitle h2 { margin: 4px 0 4px; font-size: 24px; font-weight: 600; letter-spacing: -.01em; line-height: 1.2; }
.stoa-reptitle p { margin: 0; font-family: var(--stoa-mono); font-size: 11.5px; color: var(--ui-text-secondary); }
.stoa-reptitle > div:last-child { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.stoa-repmade { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 8px 12px; margin: 0; font-size: 13px; }
.stoa-repmade dt { padding-top: 2px; font-family: var(--stoa-mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-repmade dd { margin: 0; overflow-wrap: anywhere; }
.stoa-repform { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px 12px; }
.stoa-repform label[data-wide="1"] { grid-column: 1 / -1; }
.stoa-repform .stoa-select { display: block; width: 100%; max-width: 100%; margin-top: 3px; }
.stoa-reptable { width: 100%; min-width: 520px; border-collapse: collapse; font-size: 13px; }
.stoa-reptable th { padding: 6px 12px 6px 0; border-bottom: 1px solid var(--ui-stroke-secondary); text-align: left; font-family: var(--stoa-mono); font-size: 10px; font-weight: 400; letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-reptable td { padding: 8px 12px 8px 0; border-bottom: 1px dotted var(--ui-stroke-primary); vertical-align: top; }
.stoa-reptable tr:last-child td { border-bottom: 0; }
.stoa-reptable [data-num="1"] { text-align: right; font-family: var(--stoa-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
.stoa-reptable td small { display: block; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-reptable td i { display: inline-block; width: 8px; height: 8px; margin-right: 8px; background: var(--c); }
.stoa-reptable td i[data-k=""] { display: none; }
.stoa-repscroll { overflow-x: auto; }
.stoa-repdraft { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px; padding: 10px 14px; border: 1px dotted var(--stoa-accent-line); border-radius: var(--stoa-r); background: var(--stoa-accent-soft); }
.stoa-repdraft > div { display: flex; flex-wrap: wrap; gap: 8px; }
@keyframes stoa-repgrow { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }
@media (prefers-reduced-motion: reduce) { .stoa-reptrack > i.stoa-repfill { animation: none; } }
`

const CSS_RECRUITING = `
.stoa-mapsearch { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin: 0 0 14px; padding: 8px 0 10px; border-bottom: 1px dotted var(--ui-stroke-secondary); font-size: 12.5px; color: var(--ui-text-secondary); }
.stoa-mapsearch .stoa-select, .stoa-mapfor .stoa-select { width: auto; max-width: min(440px, 100%); }
.stoa-mapfor { display: grid; gap: 8px; margin: 8px 0 2px; }
.stoa-mapfor label { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
.stoa-billing { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin: 0 0 12px; font-size: 12.5px; color: var(--ui-text-secondary); }
.stoa-billing b { color: var(--ui-text-primary); font-weight: 600; }
.stoa-payform { margin: 0 0 14px; }
.stoa-payform .stoa-form { max-width: 620px; }
.stoa-billkinds { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 10px 0; }
.stoa-billkind { display: grid; gap: 4px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: var(--stoa-r); background: var(--ui-bg-card); color: var(--ui-text-primary); text-align: left; cursor: pointer; }
.stoa-billkind[data-on="1"] { border-color: var(--stoa-accent); background: var(--stoa-accent-soft); }
.stoa-billkind b { font-weight: 600; }
.stoa-billclear { display: flex; align-items: flex-start; gap: 8px; margin: 10px 0 0; font-size: 12px; line-height: 1.45; color: var(--ui-text-secondary); }
.stoa-prod { margin: 0 0 20px; padding-bottom: 14px; border-bottom: 1px dotted var(--ui-stroke-secondary); }
.stoa-prodtable { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.stoa-prodtable th { padding: 6px 10px 6px 0; border-bottom: 1px solid var(--ui-stroke-secondary); font: 10.5px var(--stoa-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--ui-text-tertiary); text-align: left; white-space: nowrap; }
.stoa-prodtable td { padding: 8px 10px 8px 0; border-bottom: 1px dotted var(--ui-stroke-secondary); vertical-align: top; }
.stoa-prodtable [data-num="1"] { text-align: right; font-family: var(--stoa-mono); font-variant-numeric: tabular-nums; white-space: nowrap; padding: 8px 0 8px 14px; }
.stoa-prodtable td[data-tone="good"] { color: var(--stoa-accent-text); }
.stoa-prodtable td[data-tone="warn"] { color: var(--ui-orange, #c8642d); }
.stoa-prodtable td[data-tone="bad"] { color: var(--ui-red, #b83a4b); }
.stoa-prodtable small { display: block; margin-top: 2px; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-prodname { padding: 0; border: 0; background: none; color: var(--ui-text-primary); font: inherit; font-weight: 600; text-align: left; cursor: pointer; text-decoration: underline dotted; text-underline-offset: 3px; }
.stoa-prodname:disabled { cursor: default; text-decoration: none; }
.stoa-prodtable tfoot td { border-bottom: 0; border-top: 1px solid var(--ui-stroke-secondary); font-weight: 600; }
`
const Style = () => h('style', null, CSS + CSS_RECRUITING)
// A tag is one short word or two and never breaks. `wrap` is for a tag that carries someone's own sentence (a signal), which must fold inside its box.
const Chip = ({ tone, wrap, children }) => h('span', { className: 'stoa-chip', 'data-tone': tone || '', 'data-wrap': wrap ? '1' : '0' }, children)
const Btn = ({ kind, children, ...props }) => h('button', { type: 'button', className: 'stoa-btn', 'data-kind': kind || '', ...props }, children)

/** Delete, beside a record's Edit. Two presses. The record is taken off the desk, not destroyed: everything on it is
 *  kept, and it is put back from the Activity tab or from Your data in Setup, where it can also be deleted for good. */
function RemoveRecord({ deal, after }) {
  const pipe = useContext(InPipe)
  const [sure, setSure] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (!sure) return undefined; const timer = setTimeout(() => setSure(false), 6000); return () => clearTimeout(timer) }, [sure])
  const remove = async () => {
    setBusy(true)
    try {
      await rest('/deal/' + encodeURIComponent(deal.id) + '/remove', { method: 'POST', body: {}, ...inPipe(pipe) })
      refresh()
      host.notify({ kind: 'success', message: deal.name + ' was taken off the desk. Nothing on it was lost: put it back from the Activity tab, or from Your data in Setup.' })
      if (after) after()
    } catch (error) {
      host.notifyError(error, 'Stoa could not take that off the desk')
    }
    setBusy(false)
  }
  if (!sure) return h(Btn, { kind: 'quiet', title: 'Take this record off the desk. It can be put back.', onClick: () => setSure(true) }, 'Delete')
  return h('span', { className: 'stoa-sub', style: { display: 'inline-flex', alignItems: 'center', gap: 8 } }, 'Delete ' + deal.name + '?',
    h(Btn, { kind: 'danger', disabled: busy, onClick: remove }, busy ? 'Deleting…' : 'Delete it'),
    h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setSure(false) }, 'Keep'))
}

/** In Setup, under Your data: the records taken off the desk, each to be put back or deleted for good. */
function TakenOff() {
  const query = useQuery({ queryKey: ['stoa', 'removed'], queryFn: () => rest('/removed'), staleTime: 3000 })
  const [sure, setSure] = useState('')
  useEffect(() => { if (!sure) return undefined; const timer = setTimeout(() => setSure(''), 5000); return () => clearTimeout(timer) }, [sure])
  const rows = (query.data && query.data.records) || []
  if (!rows.length) return null
  const destroy = async row => {
    if (sure !== row.id) return setSure(row.id)
    try {
      await rest('/deal/' + encodeURIComponent(row.id) + '/destroy', { method: 'POST', body: {} })
      refresh()
    } catch (error) {
      host.notifyError(error, 'Stoa could not delete that')
    }
    setSure('')
  }
  return h('div', { style: { marginTop: 14 } },
    h('div', { className: 'stoa-name' }, 'Taken off the desk'),
    h('div', { className: 'stoa-sub', style: { marginBottom: 4 } }, 'Each is kept with everything on it until you delete it for good. Deleting for good cannot be undone.'),
    rows.map(row => h('div', { key: row.id, className: 'stoa-row', style: { cursor: 'default', alignItems: 'center' } },
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, row.name),
        h('span', { className: 'stoa-sub' }, '  ' + [row.company && row.company !== row.name ? row.company : '', row.stage, 'taken off ' + ago(row.removed_at)].filter(Boolean).join(' · '))),
      h(Btn, { kind: 'quiet', onClick: () => putBack(row.id) }, 'Put back'),
      h(Btn, { kind: sure === row.id ? 'danger' : 'quiet', title: 'The record, its log and its people\'s links are destroyed', onClick: () => destroy(row) }, sure === row.id ? 'Delete for good?' : 'Delete for good'))))
}

function Fit({ value }) {
  const n = Math.max(0, Math.min(5, Number(value) || 0))
  return h('span', { className: 'stoa-fit', 'data-fit': n, title: 'Fit ' + n + ' of 5' }, '■'.repeat(n), h('s', null, '■'.repeat(5 - n)))
}

// -- drawn in dots ---------------------------------------------------------------------------------
/** Where a piece sits on the heat ramp: place `index` of `count`, cold first. Read by the style block as --pos and --size. */
const heatAt = (index, count) => ({ '--size': Math.max(1, count) * 100 + '%', '--pos': (count > 1 ? (100 * index) / (count - 1) : 0) + '%' })

// The figure at the head of the desk is set in dots, five across and seven down, the way a sign board spells it.
// Each row is the five dots read as a number (16 is the left-hand dot); a narrow mark has two dots a row.
const DOTS = {
  0: [14, 17, 19, 21, 25, 17, 14], 1: [4, 12, 4, 4, 4, 4, 14], 2: [14, 17, 1, 2, 4, 8, 31], 3: [31, 2, 4, 2, 1, 17, 14], 4: [2, 6, 10, 18, 31, 2, 2],
  5: [31, 16, 30, 1, 1, 17, 14], 6: [6, 8, 16, 30, 17, 17, 14], 7: [31, 1, 2, 4, 8, 8, 8], 8: [14, 17, 17, 14, 17, 17, 14], 9: [14, 17, 17, 15, 1, 2, 12],
  $: [4, 15, 20, 14, 5, 30, 4], '€': [6, 9, 28, 8, 28, 9, 6], '£': [6, 9, 8, 28, 8, 8, 31], k: [16, 16, 18, 20, 24, 20, 18], M: [17, 27, 21, 21, 17, 17, 17],
  '.': [0, 0, 0, 0, 0, 3, 3], ',': [0, 0, 0, 0, 3, 1, 2], ' ': null
}

function DotFigure({ text }) {
  const chars = [...String(text)]
  if (!chars.length || chars.some(ch => !(ch in DOTS))) return String(text)      // what it cannot spell is set in type
  const marks = []
  let x = 0
  chars.forEach((ch, at) => {
    const wide = DOTS[ch] && DOTS[ch].every(row => row < 4) ? 2 : 5
    ;(DOTS[ch] || []).forEach((row, y) => {
      for (let col = 0; col < wide; col++) {
        if ((row >> (wide - 1 - col)) & 1) marks.push(h('rect', { key: at + ':' + y + ':' + col, x: x + col + 0.1, y: y + 0.1, width: 0.8, height: 0.8 }))
      }
    })
    x += (DOTS[ch] ? wide : 2) + 1
  })
  return h('svg', { className: 'stoa-dotfig', viewBox: '0 0 ' + (x - 1) + ' 7', style: { aspectRatio: (x - 1) + ' / 7' }, role: 'img', 'aria-label': String(text) }, marks)
}

// The stoa itself. The plugin carries it as a grid of cells, one to a CSS pixel, and it is drawn here dot by dot
// on the screen's own pixels, so it is sharp at any size. A cell says how bright the stone is there, and whether
// it holds a dot on a dark theme and on a light one (see GET /art). On a dark theme the picture glows: a dot where
// the stone is bright. On a light theme the same stone is inked instead and the shade is left as paper, over a
// wash of its own colours. Either can be drawn in the heat colours or in one ink, the colour of the words. Left
// alone (`auto`) it is in colour on a dark theme and in one ink on a light one; the desk can fix it either way
// (Setup), and its name follows it.
const HEAT_AT = [0.33, 0.47, 0.59, 0.675, 0.78, 0.85, 0.925]      // the brightness at which each heat colour sits
const PAPER_RICH = 1.5, PAPER_DEEP = 0.66                         // on a light theme: how much richer, and how much deeper
const PAPER_WASH = 0.42, STONE_WASH = 0.3                         // and how strong the wash is under a name, and under the stoa
// One character is one cell, a number from 0 to 51: brightness * 4 + (a dot on a dark theme ? 2 : 0) + (a dot on a
// light theme ? 1 : 0). Brightness runs 1 to 11; 12 is an outline; 0 is nothing. The characters are the digits and
// the consonants and nothing else, so that no run of them can look like a word or a key to a scanner.
const CELL_OF = (() => {
  const of = new Uint8Array(128)
  ;[...'0123456789bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ'].forEach((letter, value) => { of[letter.charCodeAt(0)] = value })
  return of
})()
const CELL_KINDS = 52, CELL_BRIGHTEST = 11, CELL_LINE = 12, CELL_WASH_FROM = 3

function unpack(piece) {
  const cells = new Uint8Array(piece.w * piece.h), text = piece.cells
  for (let i = 0; i < cells.length; i++) cells[i] = CELL_OF[text.charCodeAt(i) & 127]
  return cells
}

/** The theme as the pictures need it: whether the page is light, the colour of its words, and the heat colour for
    any brightness from 0 to 1 (as red, green and blue). */
function themeOf(el) {
  const style = getComputedStyle(el)
  // Any colour the theme can name, as red, green and blue: painted on one pixel and read back.
  const probe = document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  probe.canvas.width = probe.canvas.height = 1
  const see = colour => { probe.clearRect(0, 0, 1, 1); probe.fillStyle = colour; probe.fillRect(0, 0, 1, 1); return [...probe.getImageData(0, 0, 1, 1).data].slice(0, 3) }
  const mix = (from, to, k) => [0, 1, 2].map(i => from[i] + (to[i] - from[i]) * k)
  const stops = [1, 2, 3, 4, 5, 6, 7].map(n => see(style.getPropertyValue('--stoa-h' + n).trim()))
  const words = see(style.color)
  const paper = words[0] + words[1] + words[2] < 384        // dark words mean a light theme
  const heat = (t, inked) => {
    let at = 0
    while (at < 5 && t >= HEAT_AT[at + 1]) at++
    let colour = t <= HEAT_AT[0] ? mix([0, 0, 0], stops[0], 0.62 + (0.38 * t) / HEAT_AT[0])
      : t >= HEAT_AT[6] ? mix(stops[6], words, (0.7 * (t - HEAT_AT[6])) / (1 - HEAT_AT[6]))
        : mix(stops[at], stops[at + 1], (t - HEAT_AT[at]) / (HEAT_AT[at + 1] - HEAT_AT[at]))
    if (paper) {
      // On paper a dot has to be darker than the page to be seen at all, so each colour is made richer and
      // deeper; where a line is wanted (`inked`), the brightest are turned to ink as well.
      const grey = (colour[0] + colour[1] + colour[2]) / 3
      colour = colour.map(part => Math.min(255, Math.max(0, grey + (part - grey) * PAPER_RICH)) * PAPER_DEEP)
      if (inked && t >= HEAT_AT[5]) colour = mix(colour, words, 0.5)
    }
    return colour.map(Math.round)
  }
  return { paper, words, heat }
}

/** Whether to draw in one ink: the desk's choice, or by the theme when it has made none. */
const oneInk = (ink, paper) => ink === 'mono' || (ink !== 'color' && paper)

/** How strongly one ink shows for a brightness on a dark theme: the words' own colour, dimmer in shade. */
const glow = t => 0.34 + 0.66 * t

/** The fifteen brightnesses a name is drawn in, as colours. `inks[0]` is unused: 0 is no dot. */
function inksOf(el, ink) {
  const theme = themeOf(el), inks = [''], mono = oneInk(ink, theme.paper)
  for (let level = 1; level <= 15; level++) {
    inks.push(!mono ? 'rgb(' + theme.heat(level / 15, true).join(',') + ')'
      : 'rgba(' + theme.words.join(',') + ',' + (theme.paper ? 1 : glow(level / 15).toFixed(3)) + ')')
  }
  inks.paper = theme.paper
  inks.mono = mono
  return inks
}

/** Fit a canvas to its box on the screen's own pixels, cleared. Returns its size, or null while it has none. */
function fitted(el) {
  const ratio = window.devicePixelRatio || 1, box = el.getBoundingClientRect()
  const W = Math.round(box.width * ratio), H = Math.round(box.height * ratio)
  if (!W || !H) return null
  if (el.width !== W || el.height !== H) { el.width = W; el.height = H }
  const g = el.getContext('2d')
  g.clearRect(0, 0, W, H)
  return { g, W, H, ratio }
}

function paint(el, piece, cells, ink) {
  const fit = fitted(el)
  if (!fit) return
  const { g, W, H, ratio } = fit
  const theme = themeOf(el), mono = oneInk(ink, theme.paper)
  // What each kind of cell looks like here: red, green, blue and how solid. Nothing where it stays 0.
  const look = new Uint8ClampedArray(CELL_KINDS * 4)
  for (let cell = 1; cell < CELL_KINDS; cell++) {
    const level = cell >> 2, dot = cell & (theme.paper ? 1 : 2), line = level === CELL_LINE, bright = level / CELL_BRIGHTEST
    let colour = null, solid = 1
    if (dot && mono) { colour = theme.words; solid = theme.paper || line ? 1 : glow(bright) }
    else if (dot) colour = line ? (theme.paper ? theme.words : theme.heat(0.93)) : theme.heat(bright)
    else if (theme.paper && !mono && level >= CELL_WASH_FROM && !line) { colour = theme.heat(bright); solid = STONE_WASH }
    if (colour) look.set([colour[0], colour[1], colour[2], 255 * solid], cell * 4)
  }
  const pitch = Math.max(1, Math.round(ratio))                      // a cell is one CSS pixel
  const left = W - piece.w * pitch, top = H - piece.h * pitch       // it sits in the right-hand foot of its box
  const image = g.createImageData(W, H), pixels = image.data
  for (let y = Math.max(0, Math.ceil(-top / pitch)); y < piece.h; y++) {
    for (let x = Math.max(0, Math.ceil(-left / pitch)); x < piece.w; x++) {
      const at = cells[y * piece.w + x] * 4
      if (!look[at + 3]) continue
      for (let j = 0; j < pitch; j++) {
        let to = ((top + y * pitch + j) * W + left + x * pitch) * 4
        for (let i = 0; i < pitch; i++, to += 4) { pixels[to] = look[at]; pixels[to + 1] = look[at + 1]; pixels[to + 2] = look[at + 2]; pixels[to + 3] = look[at + 3] }
      }
    }
  }
  g.putImageData(image, 0, 0)
}

/** Keep a canvas drawn: when it first shows, when its box changes size, when the fonts arrive, and when the theme
    changes. A theme change is seen from the canvas itself (the colour of its own text is looked at once a second),
    so nothing outside the plugin's own markup is watched. */
function useDrawn(canvas, draw, deps) {
  useEffect(() => {
    const el = canvas.current
    if (!el) return undefined
    let frame = 0
    const again = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => draw(el)) }
    again()
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(again)
    const size = new ResizeObserver(again)
    size.observe(el)
    let words = getComputedStyle(el).color
    const watch = setInterval(() => {
      if (document.hidden) return
      const now = getComputedStyle(el).color
      if (now !== words) { words = now; again() }
    }, 1000)
    return () => { cancelAnimationFrame(frame); size.disconnect(); clearInterval(watch) }
  }, deps)
}

function Art({ name, ink }) {
  const query = useQuery({ queryKey: ['stoa', 'art'], queryFn: () => rest('/art'), staleTime: Infinity, retry: false })
  const canvas = useRef(null)
  const piece = query.data && query.data.v === 3 && query.data[name]
  const cells = useRef(null)
  useDrawn(canvas, el => {
    if (!piece || !piece.cells) return
    if (!cells.current || cells.current.of !== piece) cells.current = { of: piece, grid: unpack(piece) }
    paint(el, piece, cells.current.grid, ink)
  }, [piece, ink])
  return h('canvas', { ref: canvas, className: 'stoa-art', 'aria-hidden': true })
}

// -- your own name, drawn the way the colonnade is ---------------------------------------------------
// Any name is set in the app's mono capitals off screen, read back as a grid of dots, and lit like the building:
// the light comes from the upper left, so each stroke is shaded as a column is, bright where an edge catches the
// light, sparse and blue in shadow, with one warm patch part-way along. The name stays whole: only the stoa
// itself comes apart at its far end.
// The name and where it sits are kept with the desk (POST /sign), so everyone who opens it sees the same thing.
const $typing = atom(null) // a name being typed on Setup, shown at the head of the page before it is saved

function paintName(el, text, side, ink) {
  const fit = fitted(el)
  if (!fit) return
  const { g, W, H, ratio } = fit
  const inks = inksOf(el, ink)
  const pitch = Math.max(2, Math.round(1.5 * ratio))
  const cols = Math.floor(W / pitch), rows = Math.floor(H / pitch)
  if (cols < 24 || rows < 10) return
  // 1. The name as a shape: three samples a dot each way.
  const S = 3, mw = cols * S, mh = rows * S
  const off = document.createElement('canvas')
  off.width = mw
  off.height = mh
  const o = off.getContext('2d', { willReadFrequently: true })
  const label = [...text.toUpperCase()], family = getComputedStyle(el).fontFamily
  const face = px => '700 ' + px + 'px ' + family
  o.font = face(100)
  const step = (o.measureText('M').width / 100) * 0.94    // a letter's width in ems: the mono face, set a little tight
  const room = mw - 4 * S
  let size = mh * 1.02                                    // capitals about three quarters of the box high
  // A long name is narrowed a little before it is made smaller.
  const squeeze = Math.min(1, Math.max(0.8, room / (label.length * step * size)))
  if (label.length * step * size * squeeze > room) size = room / (label.length * step * squeeze)
  const wide = label.length * step * size * squeeze, cap = size * 0.73
  const start = side === 'right' ? mw - wide - 2 * S : 2 * S          // against the right of its box, or the left
  o.font = face(size)
  o.textAlign = 'center'
  o.lineJoin = 'round'
  o.lineWidth = size * 0.05                               // a little weight added, so a stroke has room for its shading
  label.forEach((letter, index) => {
    o.setTransform(squeeze, 0, 0, 1, start + (index + 0.5) * step * size * squeeze, 0)
    o.fillText(letter, 0, (mh + cap) / 2)
    o.strokeText(letter, 0, (mh + cap) / 2)
  })
  o.setTransform(1, 0, 0, 1, 0, 0)
  const alpha = o.getImageData(0, 0, mw, mh).data
  const cover = new Float32Array(cols * rows)
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      let sum = 0
      for (let j = 0; j < S; j++) for (let i = 0; i < S; i++) sum += alpha[((y * S + j) * mw + x * S + i) * 4 + 3]
      cover[y * cols + x] = sum / (255 * S * S)
    }
  }
  // 2. A soft copy of the shape: its slope at any dot says which way the nearest edge faces.
  const reach = Math.max(2, Math.round((size * 0.09) / S))
  const blur = (from, across) => {
    const to = new Float32Array(from.length), n = across ? cols : rows, m = across ? rows : cols
    for (let line = 0; line < m; line++) {
      for (let k = 0; k < n; k++) {
        let sum = 0
        for (let d = -reach; d <= reach; d++) {
          const q = Math.min(n - 1, Math.max(0, k + d))
          sum += from[across ? line * cols + q : q * cols + line]
        }
        to[across ? line * cols + k : k * cols + line] = sum / (2 * reach + 1)
      }
    }
    return to
  }
  const soft = blur(blur(cover, true), false)
  const at = (field, x, y) => field[Math.min(rows - 1, Math.max(0, y)) * cols + Math.min(cols - 1, Math.max(0, x))]
  const inside = (x, y) => x >= 0 && y >= 0 && x < cols && y < rows && cover[y * cols + x] >= 0.5
  const dither = (x, y) => { const v = 52.9829189 * ((0.06711056 * x + 0.00583715 * y) % 1); return v - Math.floor(v) }
  const chance = (x, y, salt) => { const v = Math.sin(x * 12.9898 + y * 78.233 + salt * 37.719) * 43758.5453; return v - Math.floor(v) }
  const x0 = Math.floor(start / S), x1 = Math.ceil((start + wide) / S)        // where the name starts and ends, in dots
  const along = x => (x - x0) / Math.max(1, x1 - x0)
  const dots = Array.from({ length: 16 }, () => []), ground = Array.from({ length: 16 }, () => []), rim = Array.from({ length: 16 }, () => [])
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      if (!inside(x, y)) continue
      const gx = at(soft, x + 1, y) - at(soft, x - 1, y), gy = at(soft, x, y + 1) - at(soft, x, y - 1)
      const steep = Math.hypot(gx, gy)
      const lit = steep > 1e-4 ? ((0.8 * gx + 0.6 * gy) / steep) * Math.min(1, steep * reach * 1.7) : 0
      const warm = Math.exp(-((along(x) - 0.26) ** 2) / 0.022)
      const edge = !inside(x - 1, y) || !inside(x + 1, y) || !inside(x, y - 1) || !inside(x, y + 1)
      let t = 0.5 + 0.27 * lit + 0.24 * warm + 0.06 * (chance(x, y, 1) - 0.5)
      let odds = Math.min(1, Math.max(0, (t - 0.02) / 0.58)) ** 1.15
      if (edge) {
        t = lit > 0.1 ? 0.9 + 0.1 * warm : 0.36
        odds = lit > 0.1 ? 1 : 0.74
      } else if (inks.paper) t = Math.min(t, 0.83)           // on paper only an edge is ink: the lit patch stays warm
      const level = Math.min(15, Math.max(1, Math.round(t * 15)))
      if (edge && inks.paper) rim[level].push(x, y)          // on paper every edge is a whole line, lit or not
      else {
        ground[level].push(x, y)
        if (dither(x, y) < odds) dots[level].push(x, y)
      }
    }
  }
  // On paper the gaps between dots are the page showing through, which thins a letter out. So each letter is
  // first laid in as a wash of its own colours, and the dots are printed over it.
  if (inks.paper) {
    g.globalAlpha = inks.mono ? 0.1 : PAPER_WASH             // in one ink the wash is only a breath of it
    for (let level = 1; level <= 15; level++) {
      g.fillStyle = inks[level]
      const list = ground[level]
      for (let i = 0; i < list.length; i += 2) g.fillRect(list[i] * pitch, list[i + 1] * pitch, pitch, pitch)
    }
    g.globalAlpha = 1
    for (let level = 1; level <= 15; level++) {
      g.fillStyle = inks[level]
      const list = rim[level]
      for (let i = 0; i < list.length; i += 2) g.fillRect(list[i] * pitch, list[i + 1] * pitch, pitch, pitch)
    }
  }
  for (let level = 1; level <= 15; level++) {
    g.fillStyle = inks[level]
    const dot = level > 12 && pitch > 2 ? pitch : pitch - 1
    const list = dots[level]
    for (let i = 0; i < list.length; i += 2) g.fillRect(list[i] * pitch, list[i + 1] * pitch, dot, dot)
  }
}

function NameArt({ text, side, ink }) {
  const canvas = useRef(null)
  useDrawn(canvas, el => paintName(el, text, side, ink), [text, side, ink])
  return h('canvas', { ref: canvas, className: 'stoa-art stoa-nameart', role: 'img', 'aria-label': text })
}

// (Setup and Welcome say why with `why(error)`.)
function Unavailable({ error, loading }) {
  return h('div', { className: 'stoa stoa-scroll' }, h(Style), h('div', { className: 'stoa-eyebrow' }, 'Stoa'),
    h('p', { className: 'stoa-sub', style: { marginTop: 8 } }, loading ? 'Opening the desk…'
      : 'The desk is not answering. Enable the stoa plugin for this profile (hermes plugins enable stoa), restart the gateway, and it appears here.'),
    !loading && error ? h('p', { className: 'stoa-sub' }, String(error.message || error)) : null)
}

// -- a form: one labelled input per fact ----------------------------------------------------------
function Field({ label, wide, children, ...props }) {
  return h('label', { 'data-wide': wide ? '1' : '0' }, h('small', { className: 'stoa-eyebrow' }, label),
    children || h('input', { className: 'stoa-field', ...props }))
}

const uniq = values => [...new Set(values.filter(Boolean))].sort()

/** Change a record by hand: its next step, its stage, its facts, and a line for the log. */
function Edit({ deal, state, done }) {
  const pipe = useContext(InPipe)
  const pb = state.playbook, terms = pb.terms
  const start = { next_step: deal.next_step || '', next_due: deal.next_due || '', value: deal.value ? String(deal.value) : '', owner: deal.owner || '',
    company: deal.company || '', group: deal.group || '', email: deal.email || '', close_on: deal.close_on || '' }
  const [v, setV] = useState(start)
  const [note, setNote] = useState('')
  const [kind, setKind] = useState('note')
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => { setBusy(true); return act(deal.id, body, undefined, pipe).then(result => { setBusy(false); return result }) }
  const save = () => {
    const body = { verb: 'update' }
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now !== start[key]) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (Object.keys(body).length === 1) return done()
    send(body).then(result => { if (result) done() })
  }
  const log = () => { if (note.trim()) send({ verb: 'log', kind, text: note.trim() }).then(result => { if (result) setNote('') }) }
  const open = deal.status === 'open', active = open || Boolean(deal.in_delivery)      // a win still in delivery keeps its next step
  return h('div', { className: 'stoa-edit' },
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') }),
      open ? h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: deal.stage, disabled: busy,
        onChange: event => send({ verb: 'move', stage: event.target.value }) },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))) : h('span'),
      noFees(state) ? null : h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), value: v.owner, onChange: set('owner') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), value: v.group, onChange: set('group') }),
      open ? h(Field, { label: 'Expected close', type: 'date', value: v.close_on, title: 'The forecast puts it in this month', onChange: set('close_on') }) : h('span'),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') })),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: done }, 'Cancel')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Log what happened', wide: true, value: note, placeholder: 'Called her; she wants to see the comp range', onChange: event => setNote(event.target.value),
        onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) log() } })),
    h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
      h('select', { className: 'stoa-select', value: kind, 'aria-label': 'What it was', onChange: event => setKind(event.target.value) },
        ['note', 'call', 'email', 'meeting'].map(name => h('option', { key: name, value: name }, cap(name)))),
      h(Btn, { disabled: busy || !note.trim(), onClick: log }, 'Add to log')))
}

/** Put someone or something new on the desk by hand. */
// The sources people have typed, for a box to suggest: not the marks Stoa puts there itself (a map, a list, an import, a sample).
const typedSources = deals => uniq(deals.map(deal => String(deal.source || '').trim()).filter(source => source && source !== 'stoa-demo' && !/^(market map|intake:|import$)/i.test(source)))

function NewRecord({ state, close, opened, preset }) {
  const pb = state.playbook, terms = pb.terms
  const [v, setV] = useState({ name: '', company: '', group: '', stage: pb.stages[0] ? pb.stages[0].id : '', value: '', owner: '', email: '', phone: '', source: '', next_step: '', next_due: '', ...(preset || {}) })
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = () => {
    const body = {}
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (!body.name || busy) return
    setBusy(true)
    rest('/deal', { method: 'POST', body })
      .then(result => {
        refresh()
        host.notify({ kind: result && result.exists ? 'info' : 'success', message: result && result.exists ? body.name + ' is already on the desk.' : body.name + ' is on the desk.' })
        if (result && result.last) offerUndo(result.last)
        opened(result && result.deal ? result.deal.id : '')
      })
      .catch(error => host.notifyError(error, 'Stoa could not add that'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
      h('div', { className: 'stoa-name', style: { fontSize: 14 } }, 'New ' + terms.deal),
      h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Name', wide: true, autoFocus: true, value: v.name, onChange: set('name') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), list: 'stoa-new-groups', value: v.group, onChange: set('group') }),
      h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: v.stage, onChange: set('stage') },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))),
      noFees(state) ? null : h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), list: 'stoa-new-owners', value: v.owner, onChange: set('owner') }),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') }),
      h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone') }),
      h(Field, { label: 'Source', wide: true, list: 'stoa-new-sources', placeholder: 'Where they came from: a referral, LinkedIn, an event…', value: v.source, onChange: set('source') }),
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') })),
    h('datalist', { id: 'stoa-new-groups' }, uniq(state.deals.map(deal => deal.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-new-owners' }, uniq(state.deals.map(deal => deal.owner)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-new-sources' }, typedSources(state.deals).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Add to the desk'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel')))
}

// -- one record: what it is, what is next, and the buttons ---------------------------------------
function Record({ id, state: shown, sessionId, close, onCompany, onGroup }) {
  const pipe = useContext(InPipe)
  const query = useQuery({ queryKey: pipe ? ['stoa', 'deal', id, 'in', pipe] : ['stoa', 'deal', id],
    queryFn: () => rest('/deal/' + encodeURIComponent(id), pipe ? inPipe(pipe) : undefined), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const deal = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.')
  if (!deal) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const elsewhere = Boolean(deal.pipeline && shown.pipeline && deal.pipeline.id !== shown.pipeline)     // from a pipeline the page is not showing
  const state = deal.playbook ? { ...shown, playbook: deal.playbook } : shown
  const pb = state.playbook
  const open = deal.status === 'open', active = open || Boolean(deal.in_delivery)      // a win still in delivery keeps its next step
  const run = body => { setBusy(true); act(id, body, sessionId, pipe).then(() => setBusy(false)) }
  const facts = [[cap(pb.terms.group), deal.group], ...(noFees(shown) ? [] : [[pb.terms.value, money(deal.value, pb.currency)]]), [cap(pb.terms.owner), deal.owner],
    ['Expected close', open && deal.close_on ? shortDate(deal.close_on) + (deal.close_on < today() ? ' (past)' : '') : '']]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Email', deal.email], ['Phone', deal.phone]]).filter(pair => pair[1])
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        elsewhere ? h('div', { className: 'stoa-eyebrow', style: { marginBottom: 3 } }, deal.pipeline.name) : null,
        h('div', { className: 'stoa-name', style: { fontSize: 14 } }, deal.name),
        deal.company && onCompany ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'Everything at ' + deal.company, onClick: () => onCompany(deal.company) }, deal.company + '  ›'))
          : deal.company && deal.company !== deal.name ? h('div', { className: 'stoa-sub' }, deal.company) : null,
        deal.group && onGroup ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'The ' + pb.terms.group + ' this is in: its client, its terms and who is on their side',
          onClick: () => onGroup(deal.group) }, deal.group + (deal.client ? ' · for ' + deal.client : '') + (deal.group_status ? ' · ' + deal.group_status.toLowerCase() : '') + '  ›')) : null),
      close ? h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×') : null),
    h('div', { className: 'stoa-meter' }, pb.stages.map((stage, index) =>
      h('i', { key: stage.id, style: heatAt(index, pb.stages.length), 'data-on': deal.status === 'won' || (open && index <= deal.position) ? '1' : '0' }))),
    h('div', { className: 'stoa-sub', style: { display: 'flex', justifyContent: 'space-between' } },
      h('span', null, deal.stage_label + (deal.lost_reason ? ' · ' + deal.lost_reason : '')),
      h('span', null, open && deal.position >= 0 ? deal.position + 1 + ' of ' + pb.stages.length : ago(deal.stage_at))),
    deal.due || deal.stale || deal.pending_task || asleep(deal) ? h('div', { className: 'stoa-chips' },
      deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
      deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
      asleep(deal) ? h(Chip, null, 'snoozed to ' + shortDate(deal.snoozed_until)) : null,
      deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null,
    deal.next_step ? h('div', { className: 'stoa-next' }, h('small', { className: 'stoa-eyebrow' }, 'Next'),
      h('div', null, deal.next_step, deal.next_due ? h('span', { className: 'stoa-sub' }, '  ' + dueLabel(deal)) : null)) : null,
    h('div', { className: 'stoa-btns' },
      open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null,
      active && deal.next_step ? h(Btn, { disabled: busy, title: 'The next step is done: log it and say what comes next', onClick: () => setFinishing(!finishing) }, 'Done') : null,
      active ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
      active ? h(Snooze, { deal, busy, run }) : null,
      open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
        onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) =>
          h('option', { key: index, value: String(index) }, reason)))) : null,
      deal.status === 'won' && (deal.delivery_stages || []).length ? h('select', { className: 'stoa-select', value: deal.delivery || '', disabled: busy,
        'aria-label': 'Where the work stands', title: 'Where the work stands', onChange: event => run({ verb: 'update', delivery: event.target.value }) },
        deal.delivery_stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))) : null,
      !open ? h(Btn, { kind: deal.status === 'won' ? '' : 'primary', disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
      h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close editing' : 'Edit'),
      h(RemoveRecord, { deal, after: close })),
    finishing && active && deal.next_step ? h(NextStep, { deal, mode: 'done', close: () => setFinishing(false) }) : null,
    editing ? h(Edit, { key: deal.stage, deal, state, done: () => setEditing(false) }) : null,
    facts.length ? h('div', { className: 'stoa-facts' }, facts.map(pair =>
      h('div', { key: pair[0], style: pair[0] === 'Email' || pair[0] === 'Phone' ? { gridColumn: '1 / -1' } : undefined }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))) : null,
    pb.billing !== 'none' && (deal.status === 'won' || (deal.invoices || []).length) ? h(Money, { deal, pb }) : null,
    close ? h(People, { deal, term: pb.terms.deal }) : (deal.people || []).length ? h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'Also on this ' + pb.terms.deal),
      deal.people.map(person => h('p', { key: person.id }, person.name, h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · '))))) : null,
    h(RecordNotes, { key: 'notes-' + deal.id, deal }),
    h(RecordActivity, { key: 'log-' + deal.id, deal, sessionId, full: Boolean(close) }),
    deal.undo ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Put the record back as it was before: ' + deal.undo.text,
      onClick: () => { setBusy(true); undo(deal.id, deal.undo.op, pipe).then(() => setBusy(false)) } },
    h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Take back the last change')) : null)
}

/** A few lines kept on the record itself: what to know about it. Edited in place; saved like any other change, so it can be taken back. */
function RecordNotes({ deal, bare }) {
  const pipe = useContext(InPipe)
  const [text, setText] = useState(null) // null: showing what is saved
  const [busy, setBusy] = useState(false)
  const saved = deal.notes || ''
  const changed = text !== null && text.trim() !== saved.trim()
  const save = () => {
    setBusy(true)
    act(deal.id, { verb: 'update', notes: text }, undefined, pipe).then(result => { setBusy(false); if (result) setText(null) })
  }
  return h('div', { className: bare ? '' : 'stoa-log' },
    bare ? null : h('small', { className: 'stoa-eyebrow' }, 'Notes'),
    h('textarea', { className: 'stoa-field stoa-notes', style: bare ? { marginTop: 0 } : undefined, rows: saved ? Math.min(8, saved.split('\n').length + 1) : 2, maxLength: 4000,
      value: text === null ? saved : text, placeholder: 'What to know about them: how they found you, what they care about, what to avoid…',
      onChange: event => setText(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && changed) { event.preventDefault(); save() } } }),
    changed ? h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save notes'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setText(null) }, 'Cancel')) : null)
}

const LOG_KINDS = { call: 'Call', email: 'Email', meeting: 'Meeting', note: 'Note', slack: 'Slack', linkedin: 'LinkedIn', stage: 'Stage',
  created: 'Added', updated: 'Edited', task: 'Task', enriched: 'Lookup', invoice: 'Invoice', payment: 'Payment' }

/** What happened on the record, newest first, with a line to add to it. */
function RecordActivity({ deal, sessionId, full }) {
  const pipe = useContext(InPipe)
  const [all, setAll] = useState(false)
  const [line, setLine] = useState('')
  const [busy, setBusy] = useState(false)
  const entries = deal.activity || []
  const shown = all ? entries : entries.slice(0, full ? 6 : 4)
  const add = () => {
    const text = line.trim()
    if (!text) return
    setBusy(true)
    act(deal.id, { verb: 'log', kind: 'note', text }, sessionId, pipe).then(result => { setBusy(false); if (result) setLine('') })
  }
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Activity'),
      entries.length > shown.length || all ? h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => setAll(!all) },
        all ? 'Fewer' : 'All ' + entries.length) : null),
    full ? h('div', { style: { display: 'flex', gap: 6, margin: '6px 0 8px' } },
      h('input', { className: 'stoa-field stoa-grow', value: line, disabled: busy, placeholder: 'Log a call, an email, a thought…', 'aria-label': 'Add to the log',
        onChange: event => setLine(event.target.value), onKeyDown: event => { if (event.key === 'Enter') { event.preventDefault(); add() } } }),
      line.trim() ? h(Btn, { disabled: busy, onClick: add }, 'Add') : null) : null,
    shown.length ? shown.map((entry, index) =>
      h('p', { key: index, style: entry.undone ? { textDecoration: 'line-through', opacity: 0.55 } : undefined },
        LOG_KINDS[entry.kind] && !['created', 'updated', 'stage'].includes(entry.kind) ? h('b', { style: { fontWeight: 550 } }, LOG_KINDS[entry.kind] + '  ') : null,
        entry.text,
        h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at) + (entry.by ? ' · ' + entry.by : ''))))
      : h('p', { className: 'stoa-sub' }, 'Nothing logged yet.'))
}

// -- a list sent in: tick what to keep, then Apply ------------------------------------------------
function changeText(change) {
  if (change.field === 'Person') return 'add ' + change.to
  if (change.field === 'Due') return change.from ? 'Due: ' + shortDate(change.from) + ' → ' + shortDate(change.to) : 'Due: ' + shortDate(change.to)
  if (change.field === 'Note') return 'note: ' + change.to
  return change.from ? change.field + ': ' + change.from + ' → ' + change.to : change.field + ': ' + change.to
}

function Review({ id }) {
  const query = useQuery({ queryKey: ['stoa', 'intake', id], queryFn: () => rest('/intake/' + encodeURIComponent(id)), staleTime: 1500 })
  const [off, setOff] = useState({})
  const [placed, setPlaced] = useState({}) // what to do with each column that has no place: notes (the default), field or skip
  const [busy, setBusy] = useState(false)
  const intake = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That list is no longer here.')
  if (!intake) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const open = intake.status === 'open'
  const usable = intake.rows.filter(row => (row.kind === 'new' || row.kind === 'update') && (!open || !row.done))
  const rest_ = intake.rows.filter(row => row.kind !== 'new' && row.kind !== 'update')
  const chosen = usable.filter(row => !off[row.n])
  const counts = intake.counts, result = intake.result || {}
  const send = verb => {
    setBusy(true)
    rest('/intake/' + encodeURIComponent(id) + '/' + verb, { method: 'POST', body: verb === 'apply' ? { rows: chosen.map(row => row.n), columns: placed } : {} })
      .then(done => {
        refresh()
        if (verb === 'apply' && done.applied) host.notify({ kind: 'success', message: done.added + ' added, ' + done.updated + ' updated' })
        else if (done.note) host.notify({ kind: 'info', message: done.note })
        if (done.problems && done.problems.length) host.notify({ kind: 'warning', message: done.problems[0] })
      })
      .catch(error => host.notifyError(error, 'Stoa could not apply that list'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-eyebrow' }, (intake.origin === 'proposal' ? 'Proposed · ' : 'Intake · ') + intake.source),
      open ? null : h(Chip, { tone: intake.status === 'applied' ? 'good' : '' }, intake.status)),
    h('div', { className: 'stoa-name', style: { marginTop: 4 } },
      [counts.new ? counts.new + ' new' : '', counts.update ? counts.update + ' to update' : '', counts.same ? counts.same + ' already there' : '',
        counts.unclear ? counts.unclear + ' unclear' : '', counts.problem ? counts.problem + ' unusable' : ''].filter(Boolean).join(' · ') || 'Nothing to add'),
    usable.length ? h('div', { className: 'stoa-list' }, usable.map(row => h('label', { key: row.n, className: 'stoa-pick', 'data-off': open && off[row.n] ? '1' : '0' },
      open ? h('input', { type: 'checkbox', checked: !off[row.n], disabled: busy, onChange: () => setOff({ ...off, [row.n]: !off[row.n] }) })
        : h('i', { className: 'stoa-dot', 'data-tone': row.applied ? 'good' : '', title: row.applied ? 'applied' : row.done === 'declined' ? 'declined' : 'left out' }),
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, row.name),
        h('span', { className: 'stoa-sub' }, '  ' + [row.company !== row.name ? row.company : '', row.group].filter(Boolean).join(' · ')),
        h('span', { className: 'stoa-sub', style: { display: 'block' } },
          row.kind === 'new' ? 'New' + (row.stage ? ' at ' + row.stage : '') : row.changes.map(changeText).join(' · ')),
        row.case ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, row.case) : null,
        (row.warnings || []).map((warning, index) => h('span', { key: index, className: 'stoa-sub', style: { display: 'block' } }, warning))),
      h(Chip, { tone: row.kind === 'new' ? 'good' : '' }, row.kind === 'new' ? 'new' : 'update')))) : null,
    open && (intake.columns || []).length ? h('div', { className: 'stoa-placed' },
      h('div', { className: 'stoa-sub', style: { marginBottom: 4 } }, 'These columns have no place on the desk. What should happen to them?'),
      intake.columns.map(column => h('div', { key: column.name, className: 'stoa-placerow' },
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, column.name),
          h('span', { className: 'stoa-sub' }, '  e.g. ' + column.example)),
        h('select', { className: 'stoa-select', value: placed[column.name] || 'notes', disabled: busy, 'aria-label': 'What to do with ' + column.name,
          onChange: event => setPlaced({ ...placed, [column.name]: event.target.value }) },
        h('option', { value: 'notes' }, 'Keep in notes'), h('option', { value: 'field' }, 'Add as a field'), h('option', { value: 'skip' }, 'Leave out'))))) : null,
    rest_.length ? h('details', { className: 'stoa-rest' },
      h('summary', null, plural(rest_.length, 'row', 'rows') + ' left out'),
      rest_.map(row => h('p', { key: row.n }, (row.name || 'Row ' + row.n) + ': ' + row.why))) : null,
    open ? h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !chosen.length, onClick: () => send('apply') }, 'Apply ' + chosen.length),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => send('discard') }, 'Discard'),
      h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Nothing is written until you apply.'))
      : intake.status === 'applied' ? h('div', { className: 'stoa-sub', style: { marginTop: 10 } },
        (result.added || 0) + ' added · ' + (result.updated || 0) + ' updated' + (result.left ? ' · ' + result.left + ' left out' : '')) : null)
}

// -- narrowing what is shown ----------------------------------------------------------------------
function Filters({ state }) {
  const f = useValue($filter)
  const open = state.deals.filter(deal => deal.status === 'open')
  const owners = [...new Set(open.map(deal => deal.owner).filter(Boolean))].sort()
  const groups = [...new Set(open.map(deal => deal.group).filter(Boolean))].sort()
  const terms = state.playbook.terms
  return h('div', { className: 'stoa-filters' },
    h('input', { className: 'stoa-input', value: f.text, placeholder: 'Filter…', 'aria-label': 'Filter by text', onChange: event => setFilter({ text: event.target.value }) }),
    owners.length > 1 ? h('select', { className: 'stoa-select', value: f.owner, 'aria-label': 'Owner', onChange: event => setFilter({ owner: event.target.value }) },
      [h('option', { key: '', value: '' }, 'Everyone')].concat(owners.map(owner => h('option', { key: owner, value: owner }, owner)))) : null,
    groups.length > 1 ? h('select', { className: 'stoa-select', value: f.group, 'aria-label': cap(terms.group), onChange: event => setFilter({ group: event.target.value }) },
      [h('option', { key: '', value: '' }, 'All ' + terms.groups)].concat(groups.map(group => h('option', { key: group, value: group }, group)))) : null,
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.due ? '1' : '0', onClick: () => setFilter({ due: !f.due }) }, 'Due'),
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.quiet ? '1' : '0', onClick: () => setFilter({ quiet: !f.quiet }) }, 'Quiet'),
    f.stage && state.playbook.stages.some(stage => stage.id === f.stage) ? h('button', { type: 'button', className: 'stoa-toggle', 'data-on': '1',
      title: 'This stage is picked out on the board. Press to let go.', onClick: () => setFilter({ stage: '' }) },
    state.playbook.stages.find(stage => stage.id === f.stage).label + '  ×') : null,
    filtering(f) || f.stage ? h('button', { type: 'button', className: 'stoa-toggle', onClick: () => setFilter({ ...NO_FILTER }) }, 'Clear') : null)
}

// -- the funnel: how many reach each stage, and what share moves on ------------------------------
// Press a stage to pick out its column on the board (the others stay, dimmed); press it again to let go.
function Funnel({ state }) {
  const flow = state.flow, pb = state.playbook
  const filter = useValue($filter)
  if (!flow || !flow.total) return null
  const steps = flow.stages.map(stage => ({ id: stage.id, label: stage.label, reached: stage.reached, here: stage.here, lost: stage.lost, value: stage.value }))
    .concat([{ id: '', label: flow.won_label, reached: flow.won, won: true, value: flow.won_value }])
  const W = 1000, H = 196, mid = 102, tall = 100, col = W / steps.length, block = col * 0.5
  const height = step => Math.max(3, (tall * step.reached) / flow.total)
  const closed = flow.won + flow.lost
  const links = [], groups = []
  steps.forEach((step, index) => {
    const x = index * col, tallness = height(step), next = steps[index + 1]
    if (next) {
      const after = height(next), right = (index + 1) * col, centre = x + block + (col - block) / 2
      links.push(h('polygon', { key: 'l' + index, className: 'stoa-fn-link', mask: 'url(#stoa-fn-mask)',
        points: [x + block, mid - tallness / 2, right, mid - after / 2, right, mid + after / 2, x + block, mid + tallness / 2].join(' ') }))
      links.push(h('rect', { key: 'q' + index, className: 'stoa-fn-pill', x: centre - 19, y: mid - 10, width: 38, height: 20, rx: 2 }))
      links.push(h('text', { key: 'p' + index, className: 'stoa-fn-pct', x: centre, y: mid + 4, textAnchor: 'middle' },
        step.reached ? Math.round((100 * next.reached) / step.reached) + '%' : '–'))
    }
    const on = Boolean(step.id) && filter.stage === step.id
    groups.push(h('g', { key: 'g' + index, className: 'stoa-fn-step', 'data-on': on ? '1' : '0', 'data-press': step.id ? '1' : '0',
      'data-heat': Math.min(6, 1 + Math.round((5 * index) / Math.max(1, steps.length - 2))),
      onClick: step.id ? () => setFilter({ stage: on ? '' : step.id }) : undefined },
      h('title', null, step.id ? plural(step.reached, pb.terms.deal + ' has', pb.terms.deals + ' have') + ' got as far as ' + step.label + ', counting those now further on or ' + pb.won.label.toLowerCase()
        + '. ' + step.here + ' ' + (step.here === 1 ? 'is' : 'are') + ' in it now. ' + (on ? 'Press to let go of it.' : 'Press to pick out its column on the board.') : step.label),
      h('rect', { className: 'stoa-fn-hit', x: x - 4, y: 0, width: col - 2, height: H }),
      h('rect', { className: step.won ? 'stoa-fn-won' : 'stoa-fn-block', mask: step.won ? undefined : 'url(#stoa-fn-mask)', x, y: mid - tallness / 2, width: block, height: tallness,
        style: { animationDelay: index * 55 + 'ms' } }),
      h('text', { className: 'stoa-fn-label', x, y: 11 }, step.label.toUpperCase().slice(0, Math.max(6, Math.floor(col / 7.6)))),
      h('text', { className: 'stoa-fn-num', x, y: 31 }, String(step.reached), step.won ? null : h('tspan', { className: 'stoa-fn-unit', dx: 7 }, 'got this far')),
      h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 22 },
        step.won ? (step.value ? money(step.value, pb.currency) : '') : step.here + ' here now' + (step.value ? ' · ' + money(step.value, pb.currency) : '')),
      step.lost ? h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 35 }, step.lost + ' lost here') : null))
  })
  return h('div', { className: 'stoa-funnel' },
    h('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'How many records reach each stage, and what share moves on' },
      h('defs', null,       // a mask of dots: whatever is drawn through it comes out stippled
        h('pattern', { id: 'stoa-fn-dots', width: 4, height: 4, patternUnits: 'userSpaceOnUse' }, h('rect', { width: 2.4, height: 2.4, fill: '#fff' })),
        h('mask', { id: 'stoa-fn-mask', maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: W, height: H }, h('rect', { width: W, height: H, fill: 'url(#stoa-fn-dots)' }))),
      links, groups),
    h('div', { className: 'stoa-funnel-foot' },
      h('span', null, h('b', null, closed ? Math.round((100 * flow.won) / closed) + '%' : '–'), ' win rate' + (closed ? ' (' + flow.won + ' of ' + closed + ' closed)' : '')),
      h('span', null, h('b', null, Math.round((100 * flow.won) / flow.total) + '%'), ' of everything that entered has been ' + flow.won_label.toLowerCase()),
      h('span', null, 'The large number is every ' + pb.terms.deal + ' that got as far as that stage, those now further on included; “here now” is what its column holds. Percentages are the share that reached the next stage.')))
}

// -- the pane beside the conversation ------------------------------------------------------------
function Row({ deal, sub, right, tone, open, onClick }) {
  return h('button', { type: 'button', className: 'stoa-row', 'data-open': open ? '1' : '0', onClick },
    h('i', { className: 'stoa-dot', 'data-tone': tone || '' }),
    h('span', { className: 'stoa-grow' },
      h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
      sub ? h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub) : null),
    right ? h('span', { className: 'stoa-num', 'data-tone': tone || '' }, right) : null)
}

/** The pane while the desk page is open beside it: not the same lists twice, but what the page is not showing. */
function Beside({ state }) {
  const tab = useValue($tab)
  const day = state.today || {}
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const coming = tab === 'today' ? [] : (day.coming || []).filter(row => byId.has(row.id))
  const lately = tab === 'activity' ? [] : (state.activity || []).slice(0, 30)
  return h('div', { className: 'stoa stoa-pane' }, h(Style),
    h('div', { className: 'stoa-scroll' },
      h('div', { style: { padding: '0 8px' } },
        state.tour ? h('div', { className: 'stoa-tourline' }, 'Sample data · nothing here is real') : null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · beside the desk'),
        h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'The desk is open, so this shows what the page is not showing. Press a name to open it there.'),
        h('div', { className: 'stoa-rule' })),
      coming.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Coming up this week'), h('span', { className: 'stoa-num' }, String(coming.length))),
        coming.slice(0, 7).map(row => h(Row, { key: row.id, deal: byId.get(row.id), sub: byId.get(row.id).next_step || byId.get(row.id).stage_label,
          right: shortDate(row.due), onClick: () => $want.set(row.id) }))) : null,
      lately.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Lately on the desk')),
        lately.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default' } },
          h('i', { className: 'stoa-dot', 'data-tone': entry.kind === 'stage' && !entry.undone ? 'good' : '' }),
          h('span', { className: 'stoa-grow', style: entry.undone ? { opacity: 0.55 } : undefined },
            byId.has(entry.deal) ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', title: 'Open the record on the desk', onClick: () => $want.set(entry.deal) }, entry.name)
              : h('span', { className: 'stoa-name' }, entry.name),
            h('span', { style: { display: 'block', overflowWrap: 'anywhere', textDecoration: entry.undone ? 'line-through' : undefined } }, entry.text),
            h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + (entry.undone ? 'taken back' : entry.kind) + (entry.by ? ' · ' + entry.by : '')))))) : null,
      !coming.length && !lately.length ? h('p', { className: 'stoa-sub', style: { padding: 8 } }, 'Nothing has happened on the desk yet.') : null))
}

function Pane() {
  const query = useDesk()
  const filter = useValue($filter)
  const beside = useValue($onPage)
  const [openId, setOpenId] = useState('')
  if (!query.data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  if (beside) return h(Beside, { state: query.data })
  const state = query.data, pb = state.playbook, totals = state.totals
  const everyone = state.deals.filter(deal => deal.status === 'open')
  const open = everyone.filter(deal => matches(deal, filter))
  // due: open records, and wins still in delivery, whose next step counts until the work is through
  const due = state.deals.filter(deal => deal.due && (deal.status === 'open' || deal.in_delivery) && matches(deal, filter)).sort((a, b) => (a.next_due < b.next_due ? -1 : 1))
  const quiet = open.filter(deal => deal.stale && !deal.due)
  const toggle = id => setOpenId(openId === id ? '' : id)
  const listed = (deal, key, sub, right, tone) => [
    h(Row, { key, deal, sub, right, tone, open: openId === key, onClick: () => toggle(key) }),
    openId === key ? h('div', { key: key + ':detail', className: 'stoa-detail' }, h(Record, { id: deal.id, state })) : null]
  const calls = (state.markets[0] ? state.markets[0].first_calls : []).filter(call => call.status === 'new').slice(0, 3)
  return h('div', { className: 'stoa stoa-pane' }, h(Style),
    h('div', { className: 'stoa-scroll' },
      h('div', { style: { padding: '0 8px' } },
        state.tour ? h('div', { className: 'stoa-tourline' }, 'Sample data · nothing here is real') : null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, h(DotFigure, { text: money(totals.open_value, pb.currency) || String(totals.open) }), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open')),
        h('div', { className: 'stoa-sub' }, [plural(totals.open, 'open', 'open'), totals.due ? totals.due + ' due' : '', totals.stale ? totals.stale + ' quiet' : '']
          .filter(Boolean).join(' · ')),
        h('div', { className: 'stoa-rule' }),
        state.deals.length ? h('div', { style: { marginTop: 10 } }, h(Filters, { state })) : null,
        filtering(filter) ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Showing ' + open.length + ' of ' + everyone.length) : null),
      (state.intakes || []).length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Waiting for your approval'), h('span', { className: 'stoa-num' }, state.intakes.length)),
        state.intakes.map(item => h('div', { key: item.id, style: { marginBottom: 8 } }, h(Review, { id: item.id })))) : null,
      !state.deals.length ? h('p', { className: 'stoa-sub', style: { padding: '8px' } },
        'Nothing on the desk yet. Tell Hermes about a ' + pb.terms.deal + ', send it a list, or ask it to map a market.') : null,
      due.length || quiet.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Needs you'), h('span', { className: 'stoa-num' }, due.length + quiet.length)),
        due.map(deal => listed(deal, 'due:' + deal.id, deal.next_step || deal.stage_label, dueLabel(deal), dueTone(deal))),
        quiet.map(deal => listed(deal, 'quiet:' + deal.id, deal.stage_label, 'quiet ' + ago(deal.last_touch || deal.updated_at).replace(' ago', '')))) : null,
      pb.stages.map(stage => {
        const here = open.filter(deal => deal.stage === stage.id)
        if (!here.length) return null
        const value = inPlay(here, state)
        return h('div', { className: 'stoa-sec', key: stage.id },
          h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, stage.label),
            h('span', { className: 'stoa-num' }, here.length + (value ? ' · ' + money(value, pb.currency) : ''))),
          here.map(deal => listed(deal, stage.id + ':' + deal.id, [deal.company, deal.next_step].filter(Boolean).join(' · '), money(deal.value, pb.currency))))
      }),
      calls.length && !filtering(filter) ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'First calls'), h('span', { className: 'stoa-num' }, state.markets[0].name)),
        calls.map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default' } },
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, call.company),
            h('span', { className: 'stoa-sub', style: { display: 'block' } }, call.why)),
          h(Btn, { onClick: () => track(call) }, 'Add')))) : null),
    h('div', { className: 'stoa-foot' },
      h(Btn, { onClick: () => host.navigate('/stoa') }, 'Open the desk'),
      h(Btn, { kind: 'quiet', onClick: () => toChat("What's on my plate today on the Stoa desk?") }, "What's on my plate?")))
}

// -- the dock at the foot of the page: say it, dictate it, or hand it a file ------------------------
const ICON = {
  clip: 'M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5',
  mic: 'M12 2.5a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0v-6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v4',
  up: 'M12 19.5v-15M5.5 11L12 4.5 18.5 11',
  down: 'M6 9.5l6 6 6-6',
  upchev: 'M6 14.5l6-6 6 6',
  x: 'M6.5 6.5l11 11M17.5 6.5l-11 11',
  back: 'M9 14.5L4 9.5l5-5M4 9.5h10.5a5.5 5.5 0 0 1 0 11H11'
}
const Icon = ({ name, size }) => h('svg', { width: size || 17, height: size || 17, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }, h('path', { d: ICON[name] }))

/** A reply, set plainly: paragraphs, bullets and bold. Lines that only draw a card in a chat are left out. */
function Words({ text }) {
  const clean = String(text || '').replace(/^[ \t]*::stoa-[a-z]+\{[^}\n]*\}[ \t]*$/gm, '').trim()
  const bold = line => line.split(/(\*\*[^*]+\*\*)/g).map((part, index) => (/^\*\*[^*]+\*\*$/.test(part) ? h('b', { key: index }, part.slice(2, -2)) : part))
  const plain = h('div', { className: 'stoa-reply' }, clean.split(/\n{2,}/).map((block, index) => h('p', { key: index },
    block.split('\n').map((line, at) => h('span', { key: at, style: { display: 'block' } }, bold(line.replace(/^\s*[-*]\s+/, '•  ').replace(/^#+\s+/, '')))))))
  // Hermes's own renderer where the app gives it: tables, lists, code and links then look as they do in a chat.
  if (!MessageTextContent) return plain
  return h(Guard, { what: 'the app\'s message renderer', fallback: plain }, h('div', { className: 'stoa-reply stoa-reply-app' }, h(MessageTextContent, { text: clean })))
}

// Dictation is the browser's own speech recognition, where it has one (Chrome, Edge and Safari do;
// the Desktop app does not, and there the microphone in the full chat is the one to use).
const Recognition = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null

/** Which model one of the page's conversations runs on: the desk's (under the talk box) or the map's research (beside
    Map facts). The menu is Hermes's own, the one the real chat box opens: search, providers, favourites, a custom
    model by name, and how hard to think on each row. What a pick MEANS is ours: it is kept for that conversation
    and set on it (see pickModel and pickEffort). */
function ModelPick({ lane = 'desk' }) {
  return APP_MENU ? h(Guard, { what: 'the app\'s model menu', fallback: h(ModelPickPlain, { lane }) }, h(ModelPickApp, { lane })) : h(ModelPickPlain, { lane })
}

function ModelPickApp({ lane }) {
  const picked = useValue($model)[lane]
  const talk = useValue($talk)
  const [open, setOpen] = useState(false)
  const query = useQuery({ queryKey: ['stoa', 'models'], queryFn: () => host.request('model.options', {}), staleTime: 60000, retry: false })      // only for the name of Hermes's default
  const data = query.data || {}
  const usual = data.model && data.provider ? { model: String(data.model), provider: String(data.provider) } : null
  const short = name => String(name || '').replace(/^.*\//, '')
  const now = picked || usual || { model: '', provider: '' }
  const maps = lane === 'maps'
  const controller = {
    allowSpeed: false,
    current: { model: now.model, provider: now.provider, effort: (picked && picked.effort) || '', fast: false },
    presetFor: () => ({}),      // what the chat box last used for a model is the chat box's; a pick here starts on Hermes's default depth
    applyPreset: () => {},
    // Thinking depth, from the row's own submenu. Speed is not offered (allowSpeed): there is no way to carry it here yet.
    setOptions: (patch, row) => { if (patch.effort !== undefined) pickEffort(row, patch.effort, lane) },
    select: (model, provider) => pickModel({ model, provider }, false, null, lane)
  }
  const row = (key, label, sub, onSelect) => h(DropdownMenuItem, { key, className: 'stoa-appmenurow', onSelect }, h('span', null, label), sub ? h('small', null, sub) : null)
  return h(DropdownMenu, { open, onOpenChange: setOpen },
    h(DropdownMenuTrigger, { asChild: true },
      h('button', { type: 'button', className: maps ? 'stoa-btn' : 'stoa-footbtn', 'aria-label': maps ? 'The model for map research' : 'The model for the desk\'s conversation',
        title: maps ? 'The model Hermes researches your maps with: starting a map, growing it, filling in what is missing. It is kept apart from the talk box\'s model, and has a conversation of its own.'
          : 'The model the desk\'s conversation runs on. Map research has its own, beside Map facts on the Market map tab.' },
        (maps ? 'Research: ' : '') + (short(now.model) || 'Model'), '\u00a0', h('span', { className: 'stoa-menucaret', 'aria-hidden': true }, '▾'))),
    h(DropdownMenuContent, { align: maps ? 'end' : 'start', side: maps ? 'bottom' : 'top', className: 'w-72 p-0' },
      h(ModelMenuCloseContext.Provider, { value: () => setOpen(false) },
        h(ModelCatalogMenu, { controller, request: (method, params) => host.request(method, params || {}),
          footer: [
            picked && usual ? row('usual', 'Use Hermes\'s default', short(usual.model), () => pickModel(null, false, usual, lane)) : null,
            maps && hasTalk('maps') ? row('fresh', 'New research conversation', 'The next request starts afresh; the maps keep everything', () => freshTalk('maps')) : null,
            talk.busy ? h('div', { key: 'busy', className: 'stoa-sub', style: { padding: '6px 10px' } }, 'Hermes is answering; a change waits until it is done.') : null] }))))
}

/** The same choice on a Hermes whose app does not hand a plugin its model menu: a plain list of every model Hermes names. */
function ModelPickPlain({ lane = 'desk' }) {
  const picked = useValue($model)[lane]
  const talk = useValue($talk)
  const query = useQuery({ queryKey: ['stoa', 'models'], queryFn: () => host.request('model.options', {}), staleTime: 60000, retry: false })      // no conversation named: the profile's own default, which is what "Hermes's default" means here
  const data = query.data || {}
  const usual = data.model ? String(data.model) : ''
  const short = name => String(name || '').replace(/^.*\//, '')
  const price = (row, model) => {
    const cost = row.pricing && row.pricing[model]
    if (!cost) return ''
    if (cost.free) return 'free'
    return cost.input && cost.output ? cost.input + ' in · ' + cost.output + ' out, per million tokens' : ''
  }
  const rows = (data.providers || []).filter(row => row.authenticated !== false && (row.models || []).length)
  const name = picked ? short(picked.model) : usual ? short(usual) : 'Model'
  const maps = lane === 'maps'
  return h(Menu, { kind: maps ? '' : 'foot', up: !maps, right: maps, label: maps ? 'Research: ' + name : name,
    title: maps ? 'The model Hermes researches your maps with: starting a map, growing it, filling in what is missing. It is kept apart from the talk box\'s model, and has a conversation of its own.'
      : 'The model the desk\'s conversation runs on. Map research has its own, beside Map facts on the Market map tab.' },
    h(MenuItem, { title: 'Hermes\'s default', sub: picked ? 'Whatever this profile is set to use' : (usual ? short(usual) + ' · in use' : 'In use'), on: !picked,
      onClick: () => { if (picked) pickModel(null, false, data.model && data.provider ? { model: String(data.model), provider: String(data.provider) } : null, lane) } }),
    query.isLoading ? h('div', { className: 'stoa-sub', style: { padding: '8px 10px' } }, 'Asking Hermes for its models…') : null,
    query.error ? h('div', { className: 'stoa-sub', style: { padding: '8px 10px' } }, 'Hermes did not give its list of models: ' + why(query.error)) : null,
    rows.map(row => [h('div', { key: row.slug + ':rule', className: 'stoa-menurule' }),
      h('div', { key: row.slug + ':name', className: 'stoa-eyebrow', style: { padding: '4px 10px 2px' } }, row.name || row.slug),
      (row.models || []).map(model => h(MenuItem, { key: row.slug + ':' + model, title: short(model), sub: price(row, model), on: Boolean(picked) && picked.model === model && picked.provider === row.slug,
        onClick: () => pickModel({ model, provider: row.slug }, false, null, lane) }))]),
    maps && hasTalk('maps') ? [h('div', { key: 'rule', className: 'stoa-menurule' }),
      h(MenuItem, { key: 'fresh', title: 'New research conversation', sub: 'The next request starts afresh; the maps keep everything', onClick: () => freshTalk('maps') })] : null,
    talk.busy ? h('div', { className: 'stoa-sub', style: { padding: '6px 10px' } }, 'Hermes is answering; a change waits until it is done.') : null)
}

function Dock({ state }) {
  const talk = useValue($talk)
  const files = useValue($attached)
  const { asking } = useValue($model)
  const [text, setText] = useState('')
  const [listening, setListening] = useState(false)
  const area = useRef(null), picker = useRef(null), sheet = useRef(null), ear = useRef(null), before = useRef('')
  const draft = useValue($draft)
  useEffect(() => {
    if (!draft) return
    setText(draft)
    $draft.set('')
    const el = area.current
    if (el) { el.focus(); setTimeout(() => el.setSelectionRange(draft.length, draft.length), 0) }
  }, [draft])
  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 132) + 'px'
  }, [text])
  useEffect(() => {
    if (sheet.current) sheet.current.scrollTop = sheet.current.scrollHeight
  }, [talk.turns, talk.open])
  useEffect(() => () => { try { if (ear.current) ear.current.abort() } catch { /* already stopped */ } }, [])
  const hush = () => { try { if (ear.current) ear.current.stop() } catch { /* already stopped */ } }
  const send = line => {
    const words = typeof line === 'string' ? line : text
    if (isCommand(words) && !files.length) {          // a command is for a chat, not for the agent (see typeInChat)
      hush()
      setText('')
      typeInChat(words.trim())
      return
    }
    if ((!words.trim() && !files.length) || talk.busy) return
    hush()
    say(words, files)
    setText('')
    $attached.set([])
  }
  const dictate = () => {
    if (listening) return hush()
    try {
      const hearing = new Recognition()
      hearing.lang = navigator.language || 'en-US'
      hearing.interimResults = true
      hearing.continuous = true
      before.current = text.trim() ? text.replace(/\s*$/, ' ') : ''
      hearing.onresult = event => {
        let said = ''
        for (let index = 0; index < event.results.length; index++) said += event.results[index][0].transcript
        setText(before.current + said)
      }
      hearing.onerror = event => {
        setListening(false)
        if (event.error === 'no-speech' || event.error === 'aborted') return
        host.notify({ kind: 'warning', message: event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'The browser did not allow the microphone.' : 'Dictation is not available here. The microphone in the full chat works everywhere.' })
      }
      hearing.onend = () => setListening(false)
      hearing.start()
      ear.current = hearing
      setListening(true)
    } catch {
      setListening(false)
    }
  }
  const hints = ['What needs me today?', 'Who has gone quiet?', 'What moved this week?']
  return h('div', { className: 'stoa-dockzone' }, h('div', { className: 'stoa-dock' },
    h(UndoBar),
    talk.turns.length && talk.open ? h('div', { className: 'stoa-glass stoa-sheet', ref: sheet },
      h('div', { className: 'stoa-sheethead' },
        h('span', { className: 'stoa-eyebrow' }, 'Stoa desk'),
        h('span', { style: { display: 'flex', gap: 2 } },
          h(Btn, { kind: 'quiet', onClick: openTalk }, 'Open the conversation'),
          h(Btn, { kind: 'quiet', 'aria-label': 'Hide the conversation', title: 'Hide the conversation (Esc). It carries on, and the desk updates when Hermes is done.', onClick: () => setTalk({ open: false }) }, 'Hide', h(Icon, { name: 'down', size: 13 })))),
      talk.turns.map((turn, index) => h('div', { key: index, className: 'stoa-turn' },
        h('div', { className: 'stoa-youline' }, h('span', { className: 'stoa-you' }, turn.you, turn.file ? h('small', null, turn.file) : null, turn.lane === 'maps' ? h('small', null, 'Map research') : null)),
        turn.reply ? h(Words, { text: turn.reply }) : null,
        turn.note ? h('div', { className: 'stoa-sub stoa-working', style: { marginTop: 8 } }, turn.note) : null,
        turn.error ? h('div', { style: { marginTop: 8, color: 'var(--ui-red)' } }, turn.error) : null,
        turn.hint ? h('div', { className: 'stoa-turnhint', role: 'note' }, turn.hint) : null)))
      : talk.turns.length ? h('div', { className: 'stoa-hints' },
        h('button', { type: 'button', className: 'stoa-hint', onClick: () => setTalk({ open: true }) }, 'Show the conversation'))
        : !text && !files.length ? h('div', { className: 'stoa-hints' }, hints.map(hint =>
          h('button', { type: 'button', key: hint, className: 'stoa-hint', disabled: talk.busy, onClick: () => send(hint) }, hint))) : null,
    asking ? h('div', { className: 'stoa-glass stoa-ask', role: 'alertdialog', 'aria-label': 'Confirm the model' },
      h('span', { className: 'stoa-askwords' }, asking.message.replace(/^[!\s]+[A-Z ]+[!\s]+/, '')),
      h(Btn, { kind: 'primary', title: 'Leaves this conversation as it is and begins a new one on the model you picked, so nothing long is re-read. The desk keeps everything.',
        onClick: () => { const lane = asking.lane || 'desk'; keep(lane, { model: asking.pick }); setModel({ [lane]: asking.pick, asking: null }); freshTalk(lane) } }, 'Start a new conversation on it'),
      h(Btn, { onClick: () => pickModel(asking.pick, true, asking.usual, asking.lane || 'desk') }, 'Switch this conversation'),
      h(Btn, { kind: 'quiet', onClick: () => setModel({ asking: null }) }, 'Cancel')) : null,
    h('div', { className: 'stoa-glass stoa-composer' },
      files.length ? h('div', { className: 'stoa-filechips' }, files.map(item => h('div', { key: item.name, className: 'stoa-filechip', title: typeof item.text === 'string' ? 'Goes in the message as text' : item.image ? 'Hermes is shown this picture with your message' : 'Handed to Hermes with your message, to read with its own tools' },
        h(Icon, { name: 'clip', size: 13 }), item.name,
        h('button', { type: 'button', className: 'stoa-icon', style: { width: 22, height: 22 }, 'aria-label': 'Remove ' + item.name, onClick: () => $attached.set($attached.get().filter(other => other !== item)) }, h(Icon, { name: 'x', size: 13 }))))) : null,
      h('div', { className: 'stoa-composerrow' },
        h('button', { type: 'button', className: 'stoa-icon', title: 'Hand it a file. A CSV becomes a review to approve. Anything else (a PDF, a Word file, a spreadsheet, a picture, a text file) goes to Hermes with your next message.', 'aria-label': 'Attach a file',
          onClick: () => picker.current && picker.current.click() }, h(Icon, { name: 'clip' })),
        h('input', { ref: picker, type: 'file', hidden: true, multiple: true,
          onChange: async event => { const picked = [...event.target.files]; event.target.value = ''; for (const one of picked) await takeFile(one) } }),
        h('span', { className: 'stoa-prompt', 'aria-hidden': true }, '›'),
        h('textarea', { ref: area, rows: 1, value: text, disabled: talk.busy, 'aria-label': 'Talk to the desk',
          placeholder: listening ? 'Listening…' : 'Tell Stoa what happened, ask it anything, or drop a list on the page…',
          onChange: event => setText(event.target.value),
          onKeyDown: event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() }
            else if (event.key === 'Escape' && talk.open) { event.preventDefault(); setTalk({ open: false }) }
          } }),
        Recognition ? h('button', { type: 'button', className: 'stoa-icon', 'data-on': listening ? '1' : '0', disabled: talk.busy,
          title: listening ? 'Stop dictating' : 'Dictate (your browser\'s speech recognition)', 'aria-label': listening ? 'Stop dictating' : 'Dictate', onClick: dictate },
          h(Icon, { name: 'mic' })) : null,
        h('button', { type: 'button', className: 'stoa-icon', 'data-kind': 'send', disabled: talk.busy || (!text.trim() && !files.length), title: 'Send', 'aria-label': 'Send',
          onClick: () => send() }, h(Icon, { name: 'up' }))),
      h('div', { className: 'stoa-composerfoot' },
        h(ModelPick, { lane: 'desk' }),
        h('span', { className: 'stoa-grow' }),
        talk.turns.length || hasTalk() ? h('button', { type: 'button', className: 'stoa-footbtn', disabled: talk.busy,
          title: 'Begin a new conversation with the next thing you say. Hermes re-reads the whole conversation on every step, so a long one costs more each time; the desk itself keeps everything, so nothing is lost by starting again.',
          onClick: () => freshTalk('desk') }, 'New conversation') : null))))
}

// -- Today: what wants you, most pressing first, one move per row ---------------------------------
const tomorrow = () => { const d = new Date(Date.now() + 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }
const shortDate = iso => { const d = new Date(iso + 'T12:00:00'); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
const asleep = deal => Boolean(deal.snoozed_until) && deal.snoozed_until > today()

/** What comes after a step is done, or the first step for a record that has none. Nothing is saved until the button. */
function NextStep({ deal, mode, close, lead }) {
  const pipe = useContext(InPipe)
  const [step, setStep] = useState('')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const save = () => {
    if (busy || (mode === 'plan' && !step.trim())) return
    setBusy(true)
    act(deal.id, { verb: mode === 'done' ? 'done' : 'update', next_step: step.trim(), next_due: due }, undefined, pipe).then(result => { setBusy(false); if (result) close() })
  }
  return h('div', { className: 'stoa-nextform' },
    mode === 'done' ? h('div', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, 'Done: ' + deal.next_step + '. What comes next?') : null,
    mode !== 'done' && lead ? h('div', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, lead) : null,
    h('input', { className: 'stoa-field', autoFocus: true, value: step, 'aria-label': 'The next step',
      placeholder: mode === 'done' ? 'The next step (leave empty if nothing is planned)' : 'The next step',
      onChange: event => setStep(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save(); if (event.key === 'Escape') close() } }),
    h('input', { className: 'stoa-field', type: 'date', value: due, min: today(), 'aria-label': 'Due', onChange: event => setDue(event.target.value) }),
    h(Btn, { kind: 'primary', disabled: busy || (mode === 'plan' && !step.trim()), onClick: save }, mode === 'done' ? 'Mark done' : 'Save'),
    h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel'))
}

/** Put a record aside for a while, or until a date. Until then it is on no list that asks for attention. */
function Snooze({ deal, busy, run }) {
  const [picking, setPicking] = useState(false)
  const [date, setDate] = useState('')
  if (picking) {
    return h('span', { className: 'stoa-pickdate' },
      h('input', { className: 'stoa-field', type: 'date', autoFocus: true, min: tomorrow(), value: date, 'aria-label': 'Snooze until', onChange: event => setDate(event.target.value),
        onKeyDown: event => { if (event.key === 'Escape') setPicking(false) } }),
      h(Btn, { disabled: busy || !date || date <= today(), onClick: () => { run({ verb: 'snooze', until: date }); setPicking(false) } }, 'Snooze'),
      h(Btn, { kind: 'quiet', onClick: () => setPicking(false), 'aria-label': 'Cancel' }, '×'))
  }
  const off = asleep(deal)
  return h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'Snooze',
    title: 'Put it aside: it leaves Today, the due list and the quiet list until then',
    onChange: event => {
      const pick = event.target.value
      if (pick === 'date') setPicking(true)
      else if (pick === 'wake') run({ verb: 'snooze', days: 0 })
      else if (pick) run({ verb: 'snooze', days: Number(pick) })
    } },
  [h('option', { key: 'none', value: '' }, off ? 'Snoozed to ' + shortDate(deal.snoozed_until) : 'Snooze…')]
    .concat(off ? [h('option', { key: 'wake', value: 'wake' }, 'Wake it now')] : [])
    .concat([['1', 'Until tomorrow'], ['3', '3 days'], ['7', 'A week'], ['14', 'Two weeks'], ['30', 'A month']].map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))
    .concat([h('option', { key: 'date', value: 'date' }, 'Pick a date…')]))
}

/** Hand a record's next step to Hermes from the page: it writes in the dock's conversation. */
function draftFor(deal, quiet, sessionId, pipe, where) {
  if (quiet || !deal.next_step) return act(deal.id, { verb: 'draft' }, sessionId, pipe)
  toChat('On the Stoa desk' + (where || '') + ', ' + deal.name + ' (record ' + deal.id + ') has this next step due: “' + deal.next_step + '”. Draft what it needs and show me.', sessionId)
  return Promise.resolve(null)
}

// Each kind of thing that wants you is a group that opens. Closed, it is one line: how many, what, and who.
const $folds = atom({}) // which groups on Today are open, kept between visits: { overdue: true, … }
function setFold(id, open) {
  const next = { ...$folds.get(), [id]: open }
  $folds.set(next)
  if (storage) storage.set('folds', next)
}

/** The first few names and how many more: enough to know a group without opening it. */
function names(list, limit = 3) {
  const all = [...new Set(list.filter(Boolean))]
  if (all.length > limit) return all.slice(0, limit).join(', ') + ' and ' + (all.length - limit) + ' more'
  return all.length > 1 ? all.slice(0, -1).join(', ') + ' and ' + all[all.length - 1] : all.join('')
}

function Fold({ id, count, tone, title, help, summary, aside, children }) {
  const open = Boolean(useValue($folds)[id])
  return h('section', { className: 'stoa-fold', 'data-open': open ? '1' : '0' },
    h('div', { className: 'stoa-foldhead' },
      h('button', { type: 'button', className: 'stoa-foldmain', 'aria-expanded': open, title: help, onClick: () => setFold(id, !open) },
        h('span', { className: 'stoa-foldmark', 'aria-hidden': true }, open ? '[−]' : '[+]'),
        h('b', { className: 'stoa-foldcount', 'data-tone': tone || '' }, String(count)),
        h('span', { className: 'stoa-foldtitle' }, title),
        h('span', { className: 'stoa-foldsum stoa-clip' }, open ? '' : summary)),
      open && aside ? h('span', { className: 'stoa-foldaside' }, aside) : null),
    open ? h('div', { className: 'stoa-foldbody' }, children) : null)
}

const TODAY_LISTS = [
  { key: 'overdue', title: 'Overdue', tone: 'bad', help: 'The next step had a date, and the date has passed.' },
  { key: 'due_today', title: 'Due today', tone: 'warn', help: 'The next step is dated today.' },
  { key: 'drafts', title: 'Writing waiting for Hermes', tone: 'good', help: 'A button asked for a draft and nobody has picked it up yet.' },
  { key: 'invoices_overdue', title: 'Invoices overdue', tone: 'bad', help: 'Sent, not paid in full, and past the due date.' },
  { key: 'to_invoice', title: 'Won, not invoiced', tone: 'warn', help: 'Won in the last two months with no invoice on the record. Shown once the desk keeps invoices at all.' },
  { key: 'quiet', title: 'Gone quiet', tone: '', help: 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage.' },
  { key: 'new', title: 'New, nothing planned', tone: '', help: 'Added in the last week, with no next step yet.' },
  { key: 'unplanned', title: 'Won, no next step yet', tone: 'warn', help: 'Won, and the work after the win has begun (onboarding, say), but nothing is planned for it. Plan its first step.' }
]

/** What a row on Today says under its name, and the figure on its right, for each list. */
const dayFacts = pb => ({
  overdue: (deal, row) => [deal.next_step || deal.stage_label, row.days + 'd overdue'],
  due_today: deal => [deal.next_step || deal.stage_label, 'today'],
  drafts: deal => [deal.stage_label + ' · a draft is waiting to be written', ''],
  invoices_overdue: (deal, row) => ['Invoice' + (row.number ? ' #' + row.number : '') + ' · ' + exact(row.owed, pb.currency) + ' owed', row.days + 'd late'],
  to_invoice: (deal, row) => [exact(row.amount, pb.currency) + ' · ' + pb.won.label.toLowerCase() + (row.days ? ' ' + row.days + 'd ago' : ' today'), ''],
  quiet: (deal, row) => [deal.stage_label + ' · nothing for ' + row.days + 'd', 'nudge due after ' + row.expected + 'd'],
  new: deal => [[deal.company, 'added ' + ago(deal.created_at)].filter(Boolean).join(' · '), ''],
  unplanned: (deal, row) => ['In ' + (row.delivery || 'delivery').toLowerCase() + ' · nothing planned', row.days ? row.days + 'd' : 'today']
})

function Today({ state, openId, setOpenId, openClient }) {
  const pb = state.playbook, day = state.today
  const [form, setForm] = useState(null) // the row with its "what comes next" form open: { id, mode }
  const [busy, setBusy] = useState('')
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const run = (id, body) => { setBusy(id); return act(id, body).then(() => setBusy('')) }
  const facts = dayFacts(pb)
  const moves = {
    overdue: deal => [h(Btn, { key: 'done', kind: 'go', title: 'Log it as done and say what comes next', onClick: () => setForm({ id: deal.id, mode: 'done' }) }, '✓  Done'),
      h(Btn, { key: 'draft', title: 'Hermes drafts what this step needs', onClick: () => draftFor(deal) }, 'Draft it'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    invoices_overdue: (deal, row) => [
      h(Btn, { key: 'paid', kind: 'go', title: 'Everything still owed came in', onClick: () => bill(deal.id, { verb: 'pay', invoice: String(row.invoice) }) }, 'Paid'),
      h(Btn, { key: 'remind', title: 'Hermes drafts a payment reminder', onClick: () => toChat('On the Stoa desk, ' + deal.name + ' (record ' + deal.id + ') has an invoice'
        + (row.number ? ' #' + row.number : '') + ' with ' + exact(row.owed, pb.currency) + ' still owed, ' + row.days + ' days past due. Draft a short, polite payment reminder and show me.') }, 'Draft a reminder'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    to_invoice: deal => [
      h(Btn, { key: 'bill', kind: 'go', onClick: () => { $billing.set(deal.id); setOpenId(deal.id) } }, 'Add invoice'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    drafts: deal => [h(Btn, { key: 'write', kind: 'go', onClick: () => toChat('Do the writing task waiting on ' + deal.name + ' on the Stoa desk (record ' + deal.id + ').') }, 'Write it now')],
    quiet: deal => [h(Btn, { key: 'draft', kind: 'go', title: 'Hermes drafts a follow-up from the record and its log', onClick: () => draftFor(deal, true) }, 'Draft a nudge'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    new: deal => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the next step')],
    unplanned: (deal, row) => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the ' + (row.delivery || 'next').toLowerCase() + ' step')],
  }
  moves.due_today = moves.overdue
  const rows = (key, tone) => (day[key] || []).map(row => {
    const deal = byId.get(row.id)
    if (!deal) return null
    const [sub, right] = facts[key](deal, row)
    const open = Boolean(form) && form.id === deal.id
    return h('div', { key: key + deal.id + ':' + (row.invoice || ''), className: 'stoa-trow', 'data-open': openId === deal.id ? '1' : '0' },
      h('button', { type: 'button', className: 'stoa-tmain', title: 'Open the record', onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('i', { className: 'stoa-dot', 'data-tone': tone }),
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name,
            deal.value ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ' + money(deal.value, pb.currency)) : null),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub))),
      right ? h('span', { className: 'stoa-num', 'data-tone': tone }, right) : null,
      h('span', { className: 'stoa-tacts' }, moves[key](deal, row)),
      open ? h(NextStep, { deal, mode: form.mode, close: () => setForm(null) }) : null)
  })
  const counts = day.counts
  // What a closed group says about itself: who is in it, and the one figure that matters most.
  const brief = key => {
    const list = day[key] || []
    const most = Math.max(0, ...list.map(row => row.days || 0))
    const sum = field => exact(list.reduce((total, row) => total + (row[field] || 0), 0), pb.currency)
    const tail = key === 'overdue' && most ? 'oldest ' + most + 'd' : key === 'quiet' && most ? 'longest ' + most + 'd'
      : key === 'invoices_overdue' ? sum('owed') + ' owed' : key === 'to_invoice' ? sum('amount') : ''
    return [names(list.map(row => (byId.get(row.id) || {}).name)), tail].filter(Boolean).join(' · ')
  }
  const hour = new Date().getHours()
  const hello = hour >= 5 && hour < 12 ? 'Good morning' : hour >= 12 && hour < 18 ? 'Good afternoon' : 'Good evening'
  return h('div', { className: 'stoa-today' },
    h('div', { className: 'stoa-todayhead' },
      h('h2', { className: 'stoa-hello' }, hello + '. ' + (day.needs ? plural(day.needs, 'thing needs you', 'things need you') : 'Nothing needs you today') + '.'),
      h('p', { className: 'stoa-sub' }, day.needs ? 'Most pressing first. Open a group to see what is in it and deal with it.'
        : counts.coming ? 'What is coming up is on the right.' : state.deals.length ? 'No next step is due and nobody has gone quiet.' : '')),
    h('div', { className: 'stoa-folds' },
      h(Approvals, { state, setOpenId }),
      (day.to_bill || []).length ? h(Fold, { id: 'to_bill', count: day.to_bill.length, tone: 'warn', title: 'To bill',
        help: 'Months that have ended for clients billed by the month, not billed yet', summary: names(day.to_bill.map(row => row.client)) },
      day.to_bill.map(row => h(BillMonth, { key: row.client + row.period, row, pb, showClient: true, openClient }))) : null,
      (day.client_overdue || []).length ? h(Fold, { id: 'client_overdue', count: day.client_overdue.length, tone: 'bad', title: 'Client invoices overdue',
        help: 'Sent to a client, not paid in full, and past the due date.',
        summary: [names(day.client_overdue.map(row => row.client)), exact(day.client_overdue.reduce((total, row) => total + (row.owed || 0), 0), pb.currency) + ' owed'].join(' · ') },
      day.client_overdue.map(row => h('div', { key: row.invoice, className: 'stoa-irow' },
        h('span', { className: 'stoa-grow' },
          h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openClient(row.client) }, row.client),
          h('span', { className: 'stoa-sub', style: { display: 'block' } }, [row.period ? monthLabel(row.period) : '', row.number ? '#' + row.number : '', exact(row.owed, pb.currency) + ' owed'].filter(Boolean).join(' · '))),
        h('span', { className: 'stoa-num', 'data-tone': 'bad' }, row.days + 'd late'),
        h('span', { className: 'stoa-tacts' }, h(Btn, { kind: 'go', onClick: () => billClient(row.client, { verb: 'pay', invoice: String(row.invoice) }) }, 'Paid'))))) : null,
      TODAY_LISTS.map(list => (day[list.key] || []).length ? h(Fold, { key: list.key, id: list.key, count: counts[list.key], tone: list.tone,
        title: list.key === 'to_invoice' ? pb.won.label + ', not invoiced' : list.title, help: list.help, summary: brief(list.key) },
      rows(list.key, list.tone)) : null)))
}

/** Beside Today while no record is open: what is coming, how the week is going, and what is put aside. */
function Ahead({ state, setOpenId }) {
  const day = state.today, totals = state.totals, pb = state.playbook
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const line = (row, right, extra) => {
    const deal = byId.get(row.id)
    return deal ? h('div', { key: row.id, className: 'stoa-aheadrow' },
      h('button', { type: 'button', className: 'stoa-row', style: { padding: '5px 6px' }, title: 'Open the record', onClick: () => setOpenId(deal.id) },
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, deal.next_step || deal.stage_label)),
        h('span', { className: 'stoa-num' }, right)), extra || null) : null
  }
  const coming = day.coming || [], snoozed = day.snoozed || []
  return h('div', { className: 'stoa-box stoa-ahead' },
    h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow' }, 'Coming up this week'), h('span', { className: 'stoa-num' }, String(coming.length))),
    coming.length ? coming.slice(0, 8).map(row => line(row, shortDate(row.due)))
      : h('div', { className: 'stoa-sub', style: { padding: '6px 0 2px' } }, 'No next step is dated in the next seven days.'),
    coming.length > 8 ? h('div', { className: 'stoa-sub', style: { padding: '4px 6px 0' } }, 'and ' + (coming.length - 8) + ' more') : null,
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-eyebrow' }, 'The last seven days'),
      h('div', { className: 'stoa-weekline' }, [[totals.new_week, 'new'], [totals.moves_week, totals.moves_week === 1 ? 'stage move' : 'stage moves']].map(pair =>
        h('span', { key: pair[1] }, h('b', null, String(pair[0])), ' ' + pair[1])))),
    snoozed.length ? h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow', title: 'Put aside until a date: on no list until then' }, 'Snoozed'),
        h('span', { className: 'stoa-num' }, String(snoozed.length))),
      snoozed.slice(0, 8).map(row => line(row, 'to ' + shortDate(row.until),
        h(Btn, { kind: 'quiet', title: 'Bring it back now', onClick: () => act(row.id, { verb: 'snooze', days: 0 }) }, 'Wake')))) : null)
}

// -- the way in: a desk nobody has set up yet --------------------------------------------------------
async function tour(action, example) {
  try {
    const result = await rest('/tour', { method: 'POST', body: { action, example: example || '' } })
    refresh()
    if (action === 'start') host.notify({ kind: 'info', message: 'Sample data: ' + result.title + '. Your own desk is untouched.' })
    else if (action === 'leave') host.notify({ kind: 'info', message: 'Back on your own desk.' })
    else host.notify({ kind: 'success', message: plural(result.removed || 0, 'sample record', 'sample records') + ' removed from your desk.' })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

// -- setting a pipeline up: pick its shape, then its stages. One window, always in view ------------------
const $setupFlow = atom(null) // open: { mode: 'first' | 'add' | 'edit', step: 'shape' | 'stages', shape }

function ShapeCard({ shape, picked, current, onPick }) {
  return h('button', { type: 'button', className: 'stoa-shape', 'data-on': picked ? '1' : '0', onClick: () => onPick(shape) },
    h('div', { className: 'stoa-shapehead' }, h('span', { className: 'stoa-shapetitle' }, shape.title), current ? h(Chip, { tone: 'good' }, 'now') : null),
    h('div', { className: 'stoa-sub', style: { marginTop: 3 } }, shape.motion),
    h('ul', { className: 'stoa-uses' }, shape.uses.map(use => h('li', { key: use }, use))),
    h('div', { className: 'stoa-chain', style: { marginBottom: 0 } }, shape.stages.map(stage => stage.label).concat([shape.won]).join('  →  ')))
}

/** The stages, in order, as a list to rename, reorder, add to and take from. */
function StageEditor({ stages, setStages, most, record }) {
  const move = (index, by) => {
    const next = stages.slice()
    const [item] = next.splice(index, 1)
    next.splice(index + by, 0, item)
    setStages(next)
  }
  return h('div', null,
    h('ol', { className: 'stoa-stagelist' }, stages.map((stage, index) => h('li', { key: stage.key },
      h('span', { className: 'stoa-stagenum' }, String(index + 1)),
      h('input', { className: 'stoa-field', value: stage.label, maxLength: 40, 'aria-label': 'Stage ' + (index + 1), placeholder: 'Stage name',
        onChange: event => setStages(stages.map((item, at) => (at === index ? { ...item, label: event.target.value } : item))) }),
      h('button', { type: 'button', className: 'stoa-icon', disabled: index === 0, title: 'Earlier', 'aria-label': 'Move earlier', onClick: () => move(index, -1) }, h(Icon, { name: 'upchev', size: 14 })),
      h('button', { type: 'button', className: 'stoa-icon', disabled: index === stages.length - 1, title: 'Later', 'aria-label': 'Move later', onClick: () => move(index, 1) }, h(Icon, { name: 'down', size: 14 })),
      h('button', { type: 'button', className: 'stoa-icon', disabled: stages.length <= 2, title: 'Remove this stage', 'aria-label': 'Remove', onClick: () => setStages(stages.filter((item, at) => at !== index)) },
        h(Icon, { name: 'x', size: 13 }))))),
    h('div', { className: 'stoa-btns', style: { marginTop: 6 } },
      h(Btn, { disabled: stages.length >= most, onClick: () => setStages(stages.concat([{ key: 'n' + Date.now(), id: '', label: '' }])) },
        stages.length >= most ? 'Seven stages is the most' : '+  Add a stage'),
      h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Every ' + record + ' moves through these, in order, until it is won or lost.')))
}

function SetupFlow() {
  const flow = useValue($setupFlow)
  const query = useQuery({ queryKey: ['stoa', 'shapes', $pipe.get()], queryFn: () => rest('/shapes'), staleTime: 2000, enabled: Boolean(flow) })
  const [step, setStep] = useState('shape')
  const [shape, setShape] = useState(null)
  const [name, setName] = useState('')
  const [stages, setStages] = useState([])
  const [won, setWon] = useState('')
  const [lost, setLost] = useState('')
  const [describe, setDescribe] = useState('')
  const [busy, setBusy] = useState(false)
  const panel = useRef(null)
  const data = query.data
  const close = () => { if (!busy) $setupFlow.set(null) }
  const begin = (chosen, current) => {
    setShape(chosen)
    const same = current && current.shape === chosen.id
    const list = same ? current.stages : chosen.stages
    setStages(list.map((stage, index) => ({ key: 's' + index + stage.id, id: stage.id, label: stage.label })))
    setWon(same ? current.won : chosen.won)
    setLost(same ? current.lost : chosen.lost)
    setStep('stages')
  }
  // open where it was asked to: at the shapes, or straight at the stages of a shape
  useEffect(() => {
    if (!flow || !data) return
    setName(flow.mode === 'first' ? '' : flow.mode === 'add' ? '' : data.current.pipeline)
    setDescribe('')
    const wanted = data.shapes.find(item => item.id === (flow.shape || (flow.mode === 'edit' && flow.step === 'stages' ? data.current.shape : '')))
    if (flow.step === 'stages' && wanted) begin(wanted, flow.mode === 'edit' ? data.current : null)
    else { setShape(null); setStep('shape') }
  }, [flow, Boolean(data)])
  useEffect(() => {
    if (!flow) return undefined
    const key = event => { if (event.key === 'Escape') close() }
    document.addEventListener('keydown', key)
    return () => document.removeEventListener('keydown', key)
  }, [flow, busy])
  useEffect(() => { if (panel.current) panel.current.scrollTop = 0 }, [step])
  if (!flow) return null
  const most = data ? data.most_stages : 7
  const words = shape ? shape.record : 'record'
  const named = stages.map(stage => stage.label.trim()).filter(Boolean)
  const repeated = new Set(named.map(label => label.toLowerCase())).size !== named.length
  const ready = shape && named.length === stages.length && named.length >= 2 && named.length <= most && !repeated && (flow.mode !== 'add' || name.trim())
  const verb = flow.mode
  const submit = async ask => {
    if (!ready || busy) return
    setBusy(true)
    try {
      const result = await rest('/pipeline/setup', { method: 'POST', body: { verb, shape: shape.id, name: name.trim(), won: won.trim(), lost: lost.trim(),
        stages: stages.map(stage => ({ id: stage.id, label: stage.label.trim() })) } })
      if (result.ask) toChat(result.ask)
      if (result.added) { $pipe.set(result.added.id); if (storage) storage.set('pipe', result.added.id) }
      refresh()
      $setupFlow.set(null)
      host.notify({ kind: 'success', message: verb === 'add' ? (name.trim() + ' is open: ' + shape.title.toLowerCase() + ', ' + named.length + ' stages.')
        : verb === 'first' ? 'Your pipeline is set up. Add your first ' + words + ', or drop a list on the page.' : 'Saved: ' + named.join(' → ') + '.' })
      if (ask) say('Set up the stages of my ' + (name.trim() || (data && data.current.pipeline) || 'Stoa') + ' pipeline (' + shape.title.toLowerCase()
        + ') to fit how I work, at most ' + most + ' before it is won or lost. Ask me anything you need, then set them with gtm_playbook action stages. About my business: ' + ask)
    } catch (error) {
      host.notifyError(error, 'Stoa could not set that up')
    }
    setBusy(false)
  }
  const title = step === 'shape'
    ? (verb === 'add' ? 'A new pipeline: what kind of work is it for?' : verb === 'edit' ? 'Change what this pipeline is for' : 'What kind of work is your pipeline for?')
    : (verb === 'edit' ? 'The stages of ' + ((data && data.current.pipeline) || 'this pipeline') : 'Name it, and set its stages')
  return h('div', { className: 'stoa stoa-modal', role: 'dialog', 'aria-modal': true, 'aria-label': title, onMouseDown: event => { if (event.target === event.currentTarget) close() } },
    h('div', { className: 'stoa-modalpanel', ref: panel },
      h('div', { className: 'stoa-modalhead' },
        h('div', null,
          h('div', { className: 'stoa-eyebrow' }, step === 'shape' ? 'Step 1 of 2' : (flow.step === 'stages' && flow.mode === 'edit' ? shape && shape.title : 'Step 2 of 2 · ' + (shape ? shape.title : ''))),
          h('div', { className: 'stoa-modaltitle' }, title)),
        h('button', { type: 'button', className: 'stoa-icon', 'aria-label': 'Close', onClick: close }, h(Icon, { name: 'x' }))),
      !data ? h('p', { className: 'stoa-sub' }, query.error ? 'Stoa is not answering (' + why(query.error) + ').' : 'Loading…')
        : step === 'shape' ? h('div', { style: { paddingBottom: 22 } },
          h('p', { className: 'stoa-sub', style: { margin: '0 0 12px' } }, 'Pick the one closest to how the work moves. You set the stages next, in your own words.'),
          verb === 'edit' && data.current.open ? h('p', { className: 'stoa-warn' }, 'This pipeline has ' + plural(data.current.open, 'open record', 'open records') + '. A different shape only works if they are moved or closed first; for another kind of work, add a new pipeline instead.') : null,
          h('div', { className: 'stoa-shapes' }, data.shapes.map(item => h(ShapeCard, { key: item.id, shape: item, picked: shape && shape.id === item.id,
            current: verb === 'edit' && data.current.shape === item.id, onPick: chosen => begin(chosen, verb === 'edit' ? data.current : null) }))))
          : h('div', null,
            verb !== 'edit' ? h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0, maxWidth: 420 } },
              h(Field, { label: verb === 'add' ? 'Name of the new pipeline' : 'Name your pipeline (you can change it later)', autoFocus: true, value: name,
                placeholder: shape && shape.id === 'roles' ? 'Contract searches' : shape && shape.id === 'leads' ? 'Client campaigns' : 'Sales',
                onChange: event => setName(event.target.value) })) : null,
            h('div', { className: 'stoa-modalsec' }, h('div', { className: 'stoa-name' }, 'Stages'),
              h('div', { className: 'stoa-sub', style: { marginBottom: 8 } }, 'Rename them to the words you use, put them in your order, add or take away. Two to ' + most + '.'),
              h(StageEditor, { stages, setStages, most, record: words }),
              repeated ? h('p', { className: 'stoa-warn' }, 'Two stages have the same name.') : null),
            h('div', { className: 'stoa-modalsec' },
              h('div', { className: 'stoa-form', style: { marginTop: 0, maxWidth: 520 } },
                h(Field, { label: 'When it is won, it is', value: won, placeholder: shape && shape.won, onChange: event => setWon(event.target.value) }),
                h(Field, { label: 'When it is lost, it is', value: lost, placeholder: shape && shape.lost, onChange: event => setLost(event.target.value) }))),
            h('details', { className: 'stoa-modalsec stoa-describe' },
              h('summary', null, 'Not sure? Describe your business and Hermes sets the stages'),
              h('textarea', { className: 'stoa-field stoa-notes', rows: 3, value: describe, placeholder: 'I am a contract recruiter for a VC fund; I place engineers and product people at its portfolio companies…',
                onChange: event => setDescribe(event.target.value) }),
              h('div', { className: 'stoa-btns' }, h(Btn, { disabled: !ready || busy || !describe.trim(), onClick: () => submit(describe.trim()) },
                verb === 'add' ? 'Open it, then ask Hermes' : 'Save these, then ask Hermes'),
              h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Hermes answers in the box at the foot of the page.')))),
      data && step === 'stages' ? h('div', { className: 'stoa-modalfoot' },
        flow.step === 'stages' && flow.mode === 'edit' ? h('span') : h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setStep('shape') }, '‹  Back'),
        h('span', { className: 'stoa-grow' }),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel'),
        h(Btn, { kind: 'primary', disabled: !ready || busy, onClick: () => submit('') }, verb === 'add' ? 'Open the pipeline' : verb === 'first' ? 'Set it up' : 'Save stages')) : null))
}

function Welcome({ skip, ink }) {
  const query = useQuery({ queryKey: ['stoa', 'shapes', 'welcome'], queryFn: () => rest('/shapes'), staleTime: 5000 })
  const setupQuery = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000 })
  const [busy, setBusy] = useState(false)
  const data = query.data, setup = setupQuery.data
  if (!data) return h('p', { className: 'stoa-sub' }, query.error ? 'The desk is not answering yet (' + why(query.error) + '). Reload in a moment.' : 'Opening the desk…')
  return h('div', { className: 'stoa-welcome' },
    h('div', { className: 'stoa-hero' },
      h('div', { className: 'stoa-heroart' }, h(Art, { name: 'welcome', ink })),
      h('div', { className: 'stoa-eyebrow' }, 'Welcome to Stoa'),
      h('div', { className: 'stoa-herotitle' }, 'The CRM Hermes runs for you.'),
      h('p', { className: 'stoa-reading', style: { marginTop: 0 } },
        'You say what happened, in your own words. Hermes keeps the pipeline, logs your email and calls, drafts the follow-ups, and asks before anything changes. First: what kind of work is your pipeline for?')),
    h('div', { className: 'stoa-shapes', style: { marginTop: 14 } }, data.shapes.map(item => h(ShapeCard, { key: item.id, shape: item,
      onPick: chosen => $setupFlow.set({ mode: 'first', step: 'stages', shape: chosen.id }) }))),
    h('div', { className: 'stoa-sub', style: { marginTop: 10 } }, 'Two kinds of work? Set up one now; add the other as a second pipeline under Setup.'),
    h('div', { className: 'stoa-box', style: { marginTop: 16 } },
      h('div', { className: 'stoa-name' }, 'Or look around first'),
      h('div', { className: 'stoa-sub' }, 'A full pipeline of made-up people and companies, kept in a file of its own. Your desk stays empty, and one press brings you back.'),
      h('div', { className: 'stoa-btns' }, (setup ? setup.examples : []).map(example =>
        h(Btn, { key: example.id, disabled: busy, onClick: () => { setBusy(true); tour('start', example.id).then(() => setBusy(false)) } }, cap(example.title))),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: skip }, 'Skip: I will set it up later'))))
}

// -- Setup: what is connected, the rules in force, and how to work the desk ------------------------
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 4000)
}

const SAY = ['Add Priya Raman from Lumen Health, met at the conference, call her Friday', 'I spoke to Dana: she wants the proposal by Thursday',
  'Add their CFO, Lena Ortiz, to the Kestrel deal', 'Who do we know at Kestrel?', 'Undo that',
  'I invoiced Kestrel 12k today, net 30', 'Kestrel paid half of invoice 1042', 'How is cash looking, and what is likely to close this month?',
  'Move Marcus to the next stage and draft the email', 'Who has gone quiet, and what should I send each of them?', 'Here are my leads (then drop a CSV on the page)',
  'Map the market for us and add the three best fits', 'Write me a playbook for selling to dental practices']
const BY_HAND = ['Drag a card to another column to move it; drop it on the last column to close it as won.', 'Changed your mind? Undo appears above the box at the foot of the page, and on the record.',
  'Press a stage in the funnel to pick out its column on the board; press it again to let go.', 'Press a company name on a record, or Companies on the pipeline, for everything at that company.',
  'Press a card or a row to open the record: advance it, mark the step done, snooze it, or edit any fact.', 'Drop a CSV anywhere on the page. It comes back as a review; nothing is written until you apply.',
  'Use the microphone in the box at the foot of the page to dictate.']
const TYPED = [['/gtm', 'the board, in any Hermes chat'], ['/gtm today', 'what needs you, most pressing first'],
  ['/gtm cash', 'won, invoiced, collected, overdue, and the forecast'], ['/gtm brief', 'the morning brief'], ['/gtm <name>', 'one record as a card'],
  ['/gtm map', 'your market map'], ['!gtm …', 'the same in Slack, which keeps the slash for itself'], ['/blueprint', 'switch on an automation'], ['hermes stoa status', 'in a terminal: what is connected']]
const ALWAYS = ['Keeps one ledger: every change to a record or a person, what it replaced and what it left. Lines are never altered.',
  'Keeps each person once, however many records they are on.', 'Gives each record one next step and a date, and tells you when the date comes.',
  'Calls a record quiet when nothing has happened for longer than its stage allows.', 'Holds any list, and anything Hermes suggests, for your yes or no, with what it rests on.',
  'Remembers a no, and does not ask again.', 'Lets you take a change back, whoever made it, unless that would overwrite a later one.',
  'Takes a record off the desk without destroying it, so it can be put back.',
  'Logs what it finds in your email, calls and Slack on the right record, whoever on it sent it.', 'Posts cards in Slack whose buttons act at once, and only for the people allowed to press them.',
  'Keeps the invoices on a record in whole cents, and tells you what is late and what was won but never billed.',
  'Lets you snooze anything until a date, and export everything at any time.']

/** An error from the desk's backend, as a line a person can pass on: its message, and the status if there is one. */
function why(error) {
  if (!error) return ''
  const parts = [error.status || (error.response && error.response.status) || '', error.detail || (error.body && error.body.detail) || '', error.message || String(error)]
  return [...new Set(parts.map(part => String(part || '').trim()).filter(Boolean))].join(' · ').slice(0, 400) || 'No reason was given.'
}

function Lamp({ on }) {
  return h('i', { className: 'stoa-lamp', 'data-on': on === true ? '1' : on === false ? '0' : '', 'aria-label': on === true ? 'on' : on === false ? 'off' : 'not checked' })
}

/** The pipelines on this desk: each has its own records and its own playbook. Add one, rename one, remove an empty one. */
function Pipelines({ state, setup }) {
  const pipes = state.pipelines || []
  const [renaming, setRenaming] = useState('')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const send = (body, said) => {
    setBusy(true)
    return rest('/pipeline', { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not do that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const rename = id => {
    if (!title.trim() || busy) return
    send({ verb: 'rename', id, name: title.trim() }).then(result => { if (result) setRenaming('') })
  }
  return h('div', { className: 'stoa-setbox', 'data-wide': '0' }, h('div', { className: 'stoa-box' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Pipelines'),
      h('span', { className: 'stoa-sub' }, pipes.length > 1 ? pipes.length + ' side by side' : 'one')),
    pipes.map(item => h('div', { key: item.id, className: 'stoa-piperow' },
      renaming === item.id
        ? h('input', { className: 'stoa-field stoa-grow', autoFocus: true, value: title, 'aria-label': 'Name', onChange: event => setTitle(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') rename(item.id); if (event.key === 'Escape') setRenaming('') } })
        : h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.name),
          h('span', { className: 'stoa-sub' }, item.title + ' · ' + item.open + ' open' + (item.id === state.pipeline ? ' · on screen' : ''))),
      renaming === item.id
        ? h(Btn, { kind: 'primary', disabled: busy || !title.trim(), onClick: () => rename(item.id) }, 'Save')
        : h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setRenaming(item.id); setTitle(item.name) } }, 'Rename'),
      !item.first && !item.open && renaming !== item.id ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Only a pipeline with nothing in it can be removed',
        onClick: () => send({ verb: 'remove', id: item.id }, item.name + ' was removed.') }, 'Remove') : null)),
    h('p', { className: 'stoa-sub', style: { margin: '10px 0 0' } }, 'Each pipeline has its own records, its own playbook and its own autonomy setting: your own sales beside the work you do for clients, say. In a chat, name the pipeline; left out, Hermes means the first.'),
    pipes.length < 6 ? h('div', { className: 'stoa-btns', style: { marginTop: 10 } }, h(Btn, { onClick: () => $setupFlow.set({ mode: 'add', step: 'shape' }) }, '+  Pipeline')) : null))
}

/** The desk's own name: typed here, drawn at the head of the page as it is typed, and saved a moment after. */
function NameBox({ state }) {
  const sign = state.sign || { text: '', place: 'art', ink: 'auto' }, ink = ['color', 'mono'].includes(sign.ink) ? sign.ink : 'auto'
  const [text, setText] = useState(sign.text || '')
  const save = body => rest('/sign', { method: 'POST', body }).then(refresh).catch(error => host.notifyError(error, 'Stoa could not save the name'))
  useEffect(() => {
    if (text.trim() === (sign.text || '')) { $typing.set(null); return undefined }
    $typing.set(text)
    const timer = setTimeout(() => save({ text }), 700)
    return () => clearTimeout(timer)
  }, [text, sign.text])
  useEffect(() => () => $typing.set(null), [])
  return h('div', null,
    h('p', { className: 'stoa-sub', style: { margin: '0 0 10px' } }, 'Type your business name and it is drawn in dots at the head of the page, lit the way the stoa is. Leave it empty and the stoa stays. On Auto both are in color on a dark theme and in one ink on a light one; or keep them one way. Everyone who opens this desk sees it.'),
    h('div', { className: 'stoa-newgroup', style: { margin: 0, maxWidth: 'none', alignItems: 'flex-end' } },
      h('label', null, h('span', { className: 'stoa-eyebrow' }, 'Name'),
        h('input', { className: 'stoa-field', maxLength: 26, value: text, placeholder: 'Your business name', onChange: event => setText(event.target.value) })),
      h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'Where the name sits' },
        [['art', 'In place of the stoa'], ['beside', 'Beside the stoa']].map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 28 },
          'data-on': (sign.place === 'beside' ? 'beside' : 'art') === item[0] ? '1' : '0', 'aria-pressed': (sign.place === 'beside' ? 'beside' : 'art') === item[0],
          onClick: () => save({ place: item[0] }) }, item[1]))),
      h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'What the stoa and the name are drawn in' },
        [['auto', 'Auto'], ['color', 'Color'], ['mono', 'One ink']].map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 28 },
          title: item[0] === 'auto' ? 'Color on a dark theme, one ink on a light one' : undefined, 'data-on': ink === item[0] ? '1' : '0', 'aria-pressed': ink === item[0],
          onClick: () => save({ ink: item[0] }) }, item[1])))))
}

/** Where Hermes keeps this plugin's settings, its keys among them: its own form, which is where a key is saved. */
const openSettings = () => host.navigate('/settings?tab=plugins&agent=stoa')

/** The keys a person may add, each optional: what it is for, how to get one, whether it is set, and a test.
    Hermes keeps a key and Stoa only reads it, so adding one happens on Hermes's settings page, one press away. */
function Keys({ setup, only }) {
  const [tests, setTests] = useState({})
  const test = key => {
    setTests(now => ({ ...now, [key.id]: { busy: true } }))
    rest('/key/test', { method: 'POST', body: { service: key.id } })
      .then(result => setTests(now => ({ ...now, [key.id]: result })))
      .catch(error => setTests(now => ({ ...now, [key.id]: { ok: false, state: 'unreachable', message: 'Stoa could not run the test: ' + why(error) } })))
  }
  return h('div', null,
    (setup.keys || []).filter(key => !only || only.includes(key.id)).map(key => {
      const result = tests[key.id] || {}
      return h('div', { key: key.id, className: 'stoa-setrow' },
        h(Lamp, { on: key.set }),
        h('div', { className: 'stoa-grow' },
          h('div', null, h('span', { className: 'stoa-name' }, key.name), h('span', { className: 'stoa-sub' }, '  ' + key.kind)),
          h('div', null, key.unlocks),
          h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, key.set ? [key.note, key.cost].filter(Boolean).join(' ') : key.without + ' ' + key.cost),
          key.set ? null : h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, h('b', { style: { fontWeight: 500, color: 'var(--ui-text-secondary)' } }, 'To get one: '), key.steps),
          result.message ? h('div', { role: 'status', style: { marginTop: 6 } }, h(Chip, { tone: result.ok ? 'good' : result.state === 'unreachable' ? 'warn' : 'bad' }, result.ok ? 'works' : result.state === 'unreachable' ? 'no answer' : 'not working'), '  ' + result.message) : null,
          h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
            key.set ? h(Btn, { disabled: Boolean(result.busy), title: key.check, onClick: () => test(key) }, result.busy ? 'Testing…' : 'Test') : null,
            h(Btn, { kind: key.set ? undefined : 'primary', title: 'Opens Hermes\'s settings for Stoa, where the key is saved', onClick: openSettings }, key.set ? 'Change key' : 'Add key'),
            h('a', { className: 'stoa-alink', style: { alignSelf: 'center', marginLeft: 4 }, href: key.get, target: '_blank', rel: 'noreferrer' }, key.set ? 'Open ' + key.where : 'Get a key'))))
    }))
}

/** One step of setting up, or one part of the desk's own settings: what it is and where it stands on one line,
    and, opened, the way to do it right here. Open or closed is remembered, like the groups on Today. */
function Step({ id, done, optional, plain, title, status, open: openFirst, children }) {
  const folds = useValue($folds), key = 'setup:' + id
  const open = key in folds ? Boolean(folds[key]) : Boolean(openFirst)
  return h('section', { className: 'stoa-step', 'data-done': done ? '1' : '0', 'data-open': open ? '1' : '0' },
    h('button', { type: 'button', className: 'stoa-stepmain', 'aria-expanded': open, onClick: () => setFold(key, !open) },
      plain ? null : h('i', { className: 'stoa-tick', role: 'img', 'aria-label': done ? 'Done' : 'Not done yet' }, done ? '✓' : ''),
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-steptitle' }, title, optional && !done ? h('span', { className: 'stoa-stepopt' }, 'optional') : null),
        status ? h('span', { className: 'stoa-stepstatus' }, status) : null),
      h('span', { className: 'stoa-foldmark', 'aria-hidden': true }, open ? '[−]' : '[+]')),
    open ? h('div', { className: 'stoa-stepbody', 'data-plain': plain ? '1' : '0' }, children) : null)
}

/** Where call notes come from, on Setup. Pick where your notes live and it shows the way in for that tool; the folders,
    keys and connections in use are listed under it; and "what Stoa would pick up" shows the notes waiting and the record
    each looks to be about, before any sweep has run. Nothing here involves a model except the button that hands the
    waiting notes to Hermes. */
function NotesSources({ setup }) {
  const notes = setup.notes || {}
  const tools = notes.tools || []
  const folders = notes.folders || []
  const connections = notes.connections || []
  const anything = folders.length || notes.granola || notes.fireflies || connections.length
  const [pick, setPick] = useState('')
  const [path, setPath] = useState('')
  const [result, setResult] = useState(null)
  const [found, setFound] = useState(null)
  const [busy, setBusy] = useState('')
  const [looked, setLooked] = useState(false)
  const preview = useQuery({ queryKey: ['stoa', 'notes-preview'], queryFn: () => rest('/notes/preview'), enabled: looked, staleTime: 5000 })
  const tool = tools.find(item => item.id === pick)
  const call = (what, promise) => {
    setBusy(what)
    return promise.catch(error => ({ ok: false, message: 'Stoa could not do that: ' + why(error) })).then(answer => { setBusy(''); return answer })
  }
  const again = () => { refresh(); queryClient.invalidateQueries({ queryKey: ['stoa', 'notes-preview'] }) }
  const check = () => call('check', rest('/notes/check', { method: 'POST', body: { folder: path } })).then(setResult)
  const add = folder => call('save', rest('/notes/folder', { method: 'POST', body: { folder, verb: 'add' } })).then(answer => {
    setResult(answer)
    if (answer.ok) { setPath(''); setFound(null); setLooked(true); again() }      // and straight away: what is in it, and who it matches
  })
  const drop = folder => call('save', rest('/notes/folder', { method: 'POST', body: { folder, verb: 'remove' } })).then(answer => { setResult(answer); if (answer.ok) again() })
  const connect = names => call('connect', rest('/notes/connections', { method: 'POST', body: { connections: names } })).then(answer => { setResult(answer); if (answer.ok) again() })
  const look = () => call('look', rest('/notes/look')).then(answer => { setResult(null); setFound(answer.found || []) })
  const fileNow = () => {
    if (toChat('Go through the notes inbox on the Stoa desk. Read each waiting note, file it on the record it is about with a short summary and the next step it implies, and skip any that is not about the pipeline. Then tell me what you filed, and any stage change you would recommend. Do not change a stage yourself.'))
      host.notify({ kind: 'info', message: 'Hermes is going through your notes. The conversation above the talk box shows what it files.' })
  }
  const typed = path.trim()
  const day = stamp => { const d = new Date(String(stamp || '').slice(0, 10) + 'T12:00:00'); return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
  const where = { folder: 'a folder', granola: 'Granola', fireflies: 'Fireflies' }
  const addRow = hint => h('div', null,
    h('div', { className: 'stoa-pathrow' },
      h('input', { className: 'stoa-field', value: path, placeholder: hint || '~/Documents/Meeting notes', 'aria-label': 'The folder your notes are saved in', spellCheck: false,
        onChange: event => { setPath(event.target.value); setResult(null) }, onKeyDown: event => { if (event.key === 'Enter' && typed) check() } }),
      h(Btn, { disabled: !typed || Boolean(busy), title: 'Says what is in the folder without changing anything', onClick: check }, busy === 'check' ? 'Checking…' : 'Check'),
      h(Btn, { kind: 'primary', disabled: !typed || Boolean(busy), onClick: () => add(path) }, busy === 'save' ? 'Adding…' : 'Add folder')),
    h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'Paste the folder\'s path. On a Mac: in Finder, right-click the folder, hold the Option key, and choose Copy as Pathname. ',
      h('button', { type: 'button', className: 'stoa-linkbtn', disabled: Boolean(busy), title: 'Looks in a few usual places on the computer Hermes runs on, such as Documents, Plaud and Obsidian. It counts the notes there and opens none.', onClick: look },
        busy === 'look' ? 'Looking…' : 'Or have Stoa look for a folder.')))
  const ways = tool ? h('div', { className: 'stoa-noteway' },
    h('p', { style: { margin: '0 0 6px' } }, tool.about),
    tool.route === 'key' ? h(Keys, { setup, only: [tool.key] })
      : [h('ol', { key: 'steps', className: 'stoa-notesteps' }, (tool.steps || []).map((step, index) => h('li', { key: index }, step))),
        h('div', { key: 'add' }, addRow(tool.folder)),
        tool.connection ? h('div', { key: 'conn', className: 'stoa-noteconn' },
          h('span', { className: 'stoa-eyebrow' }, 'Or with nothing to export'),
          h('p', { style: { margin: '4px 0 6px' } }, tool.connection.about + ' In Hermes, open Capabilities, then Connectors, add a connector with this address, and sign in to ' + tool.name + ' when it asks: ',
            h('code', { className: 'stoa-code' }, tool.connection.address)),
          h('p', { className: 'stoa-sub', style: { margin: '0 0 6px' } }, 'Then tell Stoa to have Hermes look there on each notes sweep. This way is new: Hermes reads the notes with the connector\'s own tools, so how well it works depends on that connector.'),
          h('div', { className: 'stoa-btns', style: { margin: 0 } },
            h(Btn, { onClick: () => host.navigate('/capabilities') }, 'Open Capabilities'),
            connections.includes(tool.connection.name)
              ? h(Btn, { kind: 'quiet', disabled: Boolean(busy), onClick: () => connect(connections.filter(name => name !== tool.connection.name)) }, 'Stop looking in ' + tool.name)
              : h(Btn, { disabled: Boolean(busy) || notes.can_save === false, onClick: () => connect(connections.concat([tool.connection.name])) }, 'Have Hermes look in ' + tool.name))) : null]) : null
  const waiting = preview.data
  return h('div', { className: 'stoa-notes' },
    h('p', { className: 'stoa-sub', style: { margin: '0 0 10px' } }, 'Each note is filed on the record it is about, with the next step it implies. Stoa reads what your recorder wrote (the summary, the transcript); it does not record or transcribe anything itself.'),
    h('div', { className: 'stoa-eyebrow' }, 'Where do your notes live?'),
    h('div', { className: 'stoa-notepick', role: 'group', 'aria-label': 'Where your notes live' }, tools.map(item =>
      h('button', { key: item.id, type: 'button', className: 'stoa-btn', 'data-on': pick === item.id ? '1' : '0', 'aria-pressed': pick === item.id,
        onClick: () => { setPick(pick === item.id ? '' : item.id); setResult(null); setFound(null); setPath('') } }, item.name))),
    ways,
    !tool && !anything ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'Pick one and it shows the steps. More than one is fine.') : null,
    result ? h('div', { role: 'status', style: { marginTop: 10 } }, h(Chip, { tone: result.ok ? 'good' : 'bad' }, result.ok ? (result.saved ? 'added' : result.folder ? 'found' : 'done') : 'not yet'), '  ' + (result.message || ''),
      result.elsewhere ? h(Btn, { style: { marginLeft: 8 }, onClick: openSettings }, 'Open Stoa\'s settings') : null) : null,
    found ? (found.length
      ? h('div', { style: { marginTop: 8 } }, found.map(row => h('div', { key: row.folder, className: 'stoa-setrow' },
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block', overflowWrap: 'anywhere' } }, row.folder),
          h('span', { className: 'stoa-sub' }, plural(row.count, 'note', 'notes') + ', the newest from ' + row.newest)),
        h(Btn, { disabled: Boolean(busy), onClick: () => add(row.folder) }, 'Use this'))))
      : h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'Nothing in the usual places (Documents, Plaud, Obsidian, Notes). Paste the folder\'s path instead.')) : null,

    anything ? [h('div', { key: 'eyebrow', className: 'stoa-eyebrow', style: { marginTop: 16 } }, 'What Stoa reads now'),
      folders.map(row => h('div', { key: row.folder, className: 'stoa-setrow' },
        h(Lamp, { on: row.ok && row.count > 0 ? true : row.ok ? null : false }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block', overflowWrap: 'anywhere' } }, row.folder),
          h('span', { className: 'stoa-sub' }, row.message)),
        h(Btn, { kind: 'quiet', disabled: Boolean(busy), onClick: () => drop(row.folder) }, 'Remove'))),
      notes.granola || notes.fireflies ? h(Keys, { key: 'keys', setup, only: [notes.granola ? 'granola' : '', notes.fireflies ? 'fireflies' : ''].filter(Boolean) }) : null,
      connections.map(name => h('div', { key: 'c:' + name, className: 'stoa-setrow' },
        h(Lamp, { on: null }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, cap(name) + ', through Hermes'),
          h('span', { className: 'stoa-sub' }, 'On a notes sweep Hermes looks there with that connector\'s own tools. Stoa cannot see into it, so it shows no count.')),
        h(Btn, { kind: 'quiet', disabled: Boolean(busy), onClick: () => connect(connections.filter(other => other !== name)) }, 'Remove'))),
      !tool && folders.length < 8 ? h('div', { key: 'more', style: { marginTop: 8 } }, addRow()) : null,

      h('div', { key: 'waiting', className: 'stoa-notewait' },
        h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px' } }, h('span', { className: 'stoa-eyebrow' }, 'What Stoa would pick up'),
          h('span', { className: 'stoa-sub' }, 'the last ' + plural(notes.days || 7, 'day', 'days') + ', not yet filed')),
        !looked ? h('div', { className: 'stoa-btns', style: { margin: 0 } }, h(Btn, { onClick: () => setLooked(true) }, 'See what is waiting'))
          : !waiting ? h('p', { className: 'stoa-sub', style: { margin: 0 } }, preview.error ? 'The notes could not be listed: ' + why(preview.error) : 'Looking…')
            : h('div', null,
              (waiting.problems || []).map(problem => h('p', { key: problem, style: { margin: '0 0 6px', color: 'var(--ui-red)' } }, problem)),
              waiting.notes.length ? [
                h('p', { key: 'sum', style: { margin: '0 0 6px' } }, plural(waiting.waiting, 'note', 'notes') + ' waiting, ' + (waiting.matched === waiting.notes.length && waiting.waiting === waiting.notes.length ? 'each' : waiting.matched) + ' matched to a record'
                  + (waiting.waiting > waiting.notes.length ? ' among the newest ' + waiting.notes.length : '') + '.'),
                waiting.notes.map(note => h('div', { key: note.id, className: 'stoa-noterow' },
                  h('span', { className: 'stoa-num' }, day(note.when)),
                  h('span', { className: 'stoa-grow', style: { minWidth: 0 } }, h('span', { className: 'stoa-name', style: { display: 'block', overflowWrap: 'anywhere' } }, note.title),
                    h('span', { className: 'stoa-sub' }, [where[note.source] || note.source, note.parts > 1 ? note.parts + ' files, read as one' : '', note.handed ? 'a PDF: Hermes reads it' : ''].filter(Boolean).join(' · '))),
                  h('span', { className: 'stoa-notematch', 'data-on': note.suggested.length ? '1' : '0' }, note.suggested.length
                    ? [h('b', { key: 'n' }, note.suggested[0].name), h('small', { key: 'w' }, note.suggested[0].why)]
                    : h('small', null, 'no record matched yet')))),
                h('p', { key: 'unmatched', className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'A note with no match is still read: Hermes works out who it is about from what it says, and asks before adding anyone new.'),
                h('div', { key: 'go', className: 'stoa-btns' },
                  h(Btn, { kind: 'primary', title: 'Sends Hermes a line asking it to read and file the waiting notes. It proposes stage changes; it does not make them.', onClick: fileNow }, 'Have Hermes file them now'),
                  h(Btn, { kind: 'quiet', onClick: () => queryClient.invalidateQueries({ queryKey: ['stoa', 'notes-preview'] }) }, 'Look again'))]
                : h('p', { className: 'stoa-sub', style: { margin: 0 } }, 'Nothing is waiting. A new note shows here as soon as it is saved, and is gone once it is filed or skipped.'),
              h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'To have this happen without asking, switch on File meeting notes in the next step but one, “Let Hermes keep the desk current”.')))] : null)
}

/** A schedule in words where it is one of the plain kinds ("30 8 * * 1-5" is weekdays at 8:30); otherwise as Hermes shows it. */
const fileSize = bytes => (bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : bytes >= 1024 ? Math.round(bytes / 1024) + ' KB' : bytes + ' bytes')

/** The copies Hermes keeps of files dropped in the talk box: which are still there, when each goes, and removing them now. */
function HandedFiles() {
  const KEY = ['stoa', 'files']
  const query = useQuery({ queryKey: KEY, queryFn: () => rest('/files'), staleTime: 3000 })
  const [busy, setBusy] = useState(false)
  const [sure, setSure] = useState(false)
  const data = query.data
  if (!data) return null
  const send = async (path, body, done) => {
    setBusy(true)
    try {
      const out = await rest(path, { method: 'POST', body })
      if (out && out.ok === false) host.notify({ kind: 'warning', message: out.error || 'That could not be done.' })
      else { queryClient.setQueryData(KEY, out); if (done) done(out) }
    } catch (error) {
      host.notifyError(error, 'Stoa could not do that')
    }
    setBusy(false)
    setSure(false)
  }
  const days = data.keep_days
  const choices = [[1, 'a day'], [7, '7 days'], [30, '30 days'], [0, 'never, I will remove them']].concat([1, 7, 30, 0].includes(days) ? [] : [[days, days + ' days']])
  const all = data.folders.reduce((sum, folder) => sum + folder.files, 0), others = all - data.files.length
  const where = data.folders.length ? data.folders[0].path : ''
  const removed = out => host.notify({ kind: out.left && out.left.length ? 'warning' : 'success',
    message: plural(out.removed_now, 'file', 'files') + ' removed.' + (out.left && out.left.length ? ' Left: ' + out.left.map(item => item.name + ' (' + item.why + ')').join('; ') + '.' : '') })
  return h('div', { style: { marginTop: 14, paddingTop: 12, borderTop: '1px dotted var(--ui-stroke-primary)' } },
    h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginBottom: 6 } }, 'Files dropped in the talk box'),
    h('div', { className: 'stoa-sub', style: { overflowWrap: 'anywhere' } },
      'Hermes keeps its own copy of a file you drop here, on the computer it runs on' + (where ? ' (' + where + ')' : '') + ', and nothing of Hermes\'s clears it. Stoa removes the copies its talk box caused after ',
      h('select', { className: 'stoa-select', style: { height: 24, padding: '0 6px', margin: '0 2px' }, 'aria-label': 'Remove dropped files after', value: String(days), disabled: busy || !data.can_save,
        onChange: event => send('/files/keep', { days: Number(event.target.value) }) }, choices.map(item => h('option', { key: item[0], value: String(item[0]) }, item[1]))),
      data.can_save ? '.' : '. Change it in Stoa\'s settings.', ' Your own file, where you had it, is never touched.'),
    data.files.length
      ? h('div', { style: { marginTop: 8 } }, data.files.slice(0, 8).map(file => h('div', { key: file.id, className: 'stoa-setrow', style: { alignItems: 'baseline', justifyContent: 'space-between' } },
        h('span', { style: { overflowWrap: 'anywhere' } }, file.name),
        h('span', { className: 'stoa-sub', style: { whiteSpace: 'nowrap' } }, fileSize(file.bytes) + ' · ' + ago(file.at) + (file.changed ? ' · changed since, left alone'
          : file.days_left === null ? '' : file.days_left <= 0 ? ' · goes today' : ' · goes in ' + plural(file.days_left, 'day', 'days'))))),
      data.files.length > 8 ? h('div', { className: 'stoa-sub', style: { marginTop: 4 } }, 'and ' + (data.files.length - 8) + ' more') : null,
      h('div', { className: 'stoa-btns' },
        sure ? h(Btn, { disabled: busy, onClick: () => send('/files/remove', {}, removed) }, 'Yes, remove ' + (data.files.length === 1 ? 'it' : 'all ' + data.files.length))
          : h(Btn, { disabled: busy, onClick: () => setSure(true) }, data.files.length === 1 ? 'Remove it now' : 'Remove them now'),
        sure ? h(Btn, { kind: 'quiet', onClick: () => setSure(false) }, 'Keep them') : null),
      sure ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Hermes will not be able to read ' + (data.files.length === 1 ? 'it' : 'them') + ' again in the conversation ' + (data.files.length === 1 ? 'it was' : 'they were') + ' dropped in.') : null)
      : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'None are there now.' + (data.removed ? ' ' + plural(data.removed, 'copy has', 'copies have') + ' been removed so far.' : '')),
    others > 0 ? h('div', { className: 'stoa-sub', style: { marginTop: 6, overflowWrap: 'anywhere' } },
      plural(others, 'other file is', 'other files are') + ' in Hermes\'s folders (' + data.folders.filter(folder => folder.files).map(folder => folder.files + ' in ' + folder.name + ', ' + fileSize(folder.bytes)).join('; ')
      + ' in all): from other chats, or dropped here before Stoa kept a list. Stoa does not touch those. Clear them yourself if they should not stay.') : null,
    data.elsewhere ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } },
      plural(data.elsewhere, 'file', 'files') + ' went where Stoa does not reach (this profile keeps attachments in each conversation\'s workspace), so ' + (data.elsewhere === 1 ? 'it is' : 'they are') + ' not on the list.') : null)
}

function scheduleWords(text) {
  const parts = String(text || '').trim().split(/\s+/)
  if (parts.length !== 5 || parts[2] !== '*' || parts[3] !== '*' || !/^\d+$/.test(parts[0]) || !/^\d+(,\d+)*$/.test(parts[1])) return String(text || '')
  const days = { '*': 'every day', '1-5': 'weekdays', '0': 'Sundays', '1': 'Mondays', '2': 'Tuesdays', '3': 'Wednesdays', '4': 'Thursdays', '5': 'Fridays', '6': 'Saturdays', '7': 'Sundays' }[parts[4]]
  if (!days) return String(text || '')
  const times = parts[1].split(',').map(hour => Number(hour) + ':' + parts[0].padStart(2, '0'))
  return days + ' at ' + (times.length > 1 ? times.slice(0, -1).join(', ') + ' and ' + times[times.length - 1] : times[0])
}

/** The jobs Hermes can run on a schedule for the desk, each with whether it is on, and what one may change by itself. */
function Automations({ setup }) {
  const next = stamp => { const d = new Date(stamp); return isNaN(d) ? '' : d.toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' }) }
  return h('div', null,
    h('p', { className: 'stoa-sub', style: { margin: '0 0 4px' } }, 'Switch on… opens a chat with the command typed. Press Enter, and Hermes asks when it should run and where to report. That is all there is to it.'),
    (setup.automations || []).map(item => {
      const made = item.state === 'on' || item.state === 'paused'
      return h('div', { key: item.name, className: 'stoa-setrow' },
        h(Lamp, { on: item.state === 'on' ? true : item.state === 'unknown' ? null : false }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.title), h('span', { className: 'stoa-sub' }, item.description),
          made ? h('span', { className: 'stoa-stepstatus' }, [item.state === 'on' ? 'On' : 'Paused in Hermes', scheduleWords(item.schedule), item.state === 'on' && next(item.next) ? 'next ' + next(item.next) : ''].filter(Boolean).join(' · ')) : null),
        made ? h(Btn, { kind: 'quiet', title: 'Opens Hermes\'s scheduled jobs, where it is paused, changed or removed', onClick: () => host.navigate('/cron') }, 'Change…')
          : h(Btn, { title: 'Opens a chat with /blueprint stoa:' + item.name + ' typed. Nothing is switched on until you run it there.', onClick: () => typeInChat('/blueprint stoa:' + item.name) }, 'Switch on…'))
    }),
    h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'quiet', onClick: () => host.navigate('/cron') }, 'See all of Hermes\'s scheduled jobs')),
    h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'The sweep of email, calls and Slack reads them through the tools Hermes itself is connected to. If it finds nothing, connect them under Capabilities.  ',
      h('button', { type: 'button', className: 'stoa-alink stoa-linkbtn', onClick: () => host.navigate('/capabilities') }, 'Open Capabilities')),
    h('div', { className: 'stoa-steplabel' }, 'What a scheduled job may change without asking'),
    h(Autonomy, { setup }))
}

function Setup({ state, go }) {
  const query = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000, refetchInterval: 15000 })
  const [busy, setBusy] = useState(false)
  const picker = useRef(null)
  const setup = query.data
  if (!setup) {
    if (!query.error) return h('p', { className: 'stoa-sub' }, 'Loading…')
    // Say what actually went wrong: this is the tab people open when something is wrong.
    return h('div', { className: 'stoa-box', style: { maxWidth: 720 } },
      h('div', { className: 'stoa-name' }, 'Setup is not answering'),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0', overflowWrap: 'anywhere' } }, why(query.error)),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'The rest of the desk is not affected. If Stoa was just updated, restart the gateway (hermes gateway restart) and reload.'),
      h('div', { className: 'stoa-btns' }, h(Btn, { disabled: query.isFetching, onClick: () => query.refetch() }, query.isFetching ? 'Trying…' : 'Try again')))
  }
  const data = setup.data, sets = setup.playbook_sets
  const wait = promise => { setBusy(true); return Promise.resolve(promise).then(() => setBusy(false)) }
  const step = Object.fromEntries(setup.checklist.map(item => [item.id, item]))
  const rule = Object.fromEntries(setup.rules.map(item => [item.label, item.value]))
  const terms = state.playbook.terms
  const exportAll = () => wait(rest('/export').then(file => { download(file.filename, file.csv, 'text/csv'); host.notify({ kind: 'success', message: plural(file.rows, 'record', 'records') + ' exported.' }) })
    .catch(error => host.notifyError(error, 'Stoa could not export')))
  const ready = step.playbook.done && step.records.done
  const extras = ['keys', 'notes', 'slack', 'sweep'].filter(id => step[id] && step[id].done).length
  const label = text => h('div', { className: 'stoa-steplabel' }, text)
  return h('div', { className: 'stoa-setup2' },
    (setup.problems || []).length ? h('div', { className: 'stoa-box', style: { borderColor: 'var(--ui-orange)', marginBottom: 14 } },
      h('div', { className: 'stoa-name' }, 'Some of this could not be read'),
      setup.problems.map((line, index) => h('p', { key: index, className: 'stoa-sub', style: { margin: '4px 0 0', overflowWrap: 'anywhere' } }, line))) : null,
    h('div', { className: 'stoa-setintro' },
      h('h2', null, ready ? 'Your desk is set up' : 'Set up your desk'),
      h('p', null, ready ? 'The two things it needs are done. Everything under “Make it do more” is optional: ' + extras + ' of 4 are on.'
        : 'Two steps make a working desk. The rest is optional and can wait.')),

    h('div', { className: 'stoa-setgroup' }, 'The two it needs'),
    h('div', { className: 'stoa-steps' },
      h(Step, { id: 'playbook', done: step.playbook.done, open: !step.playbook.done, title: 'Say what the desk is for', status: step.playbook.detail },
        h('div', { className: 'stoa-name', style: { fontSize: 14 } }, setup.playbook.shape_title || setup.playbook.title),
        h('div', { className: 'stoa-sub' }, setup.playbook.summary),
        h('div', { className: 'stoa-chain' }, sets.stages.join('  →  ')),
        setup.tour ? h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'Sample data is on show. Leave the tour (under “Your data” below) to set up your own desk.')
          : h('div', { className: 'stoa-btns', style: { marginTop: 4 } },
            step.playbook.done ? h(Btn, { kind: 'primary', onClick: () => $setupFlow.set({ mode: 'edit', step: 'stages' }) }, 'Edit stages')
              : h(Btn, { kind: 'primary', onClick: () => $setupFlow.set({ mode: state.fresh ? 'first' : 'edit', step: 'shape' }) }, 'Set it up'),
            step.playbook.done ? h(Btn, { onClick: () => $setupFlow.set({ mode: 'edit', step: 'shape' }) }, 'Change what it is for') : null,
            h(Btn, { kind: 'quiet', onClick: () => $draft.set('Change my Stoa playbook: ') }, 'Ask Hermes for anything else (fields, drafts, timing)')),
        h('div', { className: 'stoa-facts', style: { marginTop: 12 } },
          [['The words', sets.words], ['A nudge is due after', sets.quiet.map(item => item.stage + ' ' + item.days + 'd').join(' · ')], ['Reasons for a loss', sets.lost_reasons.join(' · ')],
            ['Extra facts on a record', sets.fields.join(' · ') || 'none'], ['Hermes drafts when a record enters', sets.drafts.join(' · ') || 'nothing'],
            ['A lookup finds', sets.lookup === 'none' ? 'nothing (switched off)' : 'the ' + sets.lookup]].map(pair =>
            h('div', { key: pair[0], style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1]))))),
      h(Step, { id: 'records', done: step.records.done, open: step.playbook.done && !step.records.done, title: 'Put your first ' + terms.deal + ' on it', status: step.records.detail },
        h('p', { className: 'stoa-sub', style: { margin: '0 0 8px' } }, 'Any of these works. Use whichever is closest to hand.'),
        h('div', { className: 'stoa-ways' },
          h('div', null, h('div', { className: 'stoa-name' }, 'Tell Hermes'), h('div', { className: 'stoa-sub' }, 'In the box at the foot of this page, in your own words.'),
            h('button', { type: 'button', className: 'stoa-sayline', style: { marginTop: 4 }, onClick: () => $draft.set(SAY[0]) }, '“' + SAY[0] + '”')),
          h('div', null, h('div', { className: 'stoa-name' }, 'Bring a list'), h('div', { className: 'stoa-sub' }, 'A CSV from a spreadsheet or another CRM. Stoa shows what it would add, and nothing is written until you press Apply.'),
            h('input', { ref: picker, type: 'file', hidden: true, accept: '.csv,.tsv,text/csv', onChange: event => { takeFile(event.target.files[0]); event.target.value = '' } }),
            h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: Boolean(setup.tour), onClick: () => picker.current && picker.current.click() }, 'Choose a file…'), h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'or drop one anywhere on the page'))),
          h('div', null, h('div', { className: 'stoa-name' }, 'Add one by hand'), h('div', { className: 'stoa-sub' }, 'A short form: a name is enough to start.'),
            h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: Boolean(setup.tour), onClick: () => go('add') }, '+  New ' + terms.deal))),
          h('div', null, h('div', { className: 'stoa-name' }, 'Or look around first'), h('div', { className: 'stoa-sub' }, setup.tour ? cap(setup.tour.title) + ' is on show. Your own desk is untouched.' : 'A full pipeline of made-up people, in a file of its own. Your desk is untouched.'),
            h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, setup.tour ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => wait(tour('leave')) }, 'Leave the tour')
              : setup.examples.map(example => h(Btn, { key: example.id, disabled: busy, onClick: () => wait(tour('start', example.id)) }, cap(example.title)))))))),

    h('div', { className: 'stoa-setgroup' }, 'Make it do more', h('span', null, 'each one is optional')),
    h('div', { className: 'stoa-steps' },
      h(Step, { id: 'keys', optional: true, done: step.keys.done, title: 'Find companies and fill in people', status: step.keys.detail },
        h('p', { className: 'stoa-sub', style: { margin: '0 0 4px' } }, 'Two outside services, each with a key. Hermes keeps your keys and Stoa only reads them: Add key opens Hermes\'s settings for Stoa, where you paste the key and press Save. Then come back and press Test.'),
        h(Keys, { setup, only: ['exa', 'pdl'] })),
      h(Step, { id: 'notes', optional: true, done: step.notes.done, title: 'Bring in your call notes', status: step.notes.detail },
        h(NotesSources, { setup })),
      h(Step, { id: 'slack', optional: true, done: step.slack.done, title: 'Work from Slack', status: step.slack.detail },
        setup.slack ? h('div', null,
          h('p', { style: { margin: '0 0 6px' } }, 'In Slack, type ', h('code', { className: 'stoa-code' }, '!gtm'), ' for the board, ', h('code', { className: 'stoa-code' }, '!gtm today'), ' for what needs you, or ', h('code', { className: 'stoa-code' }, '!gtm'), ' and a name for that record\'s card. The buttons on a card move it at once.'),
          h('div', { className: 'stoa-facts', style: { marginTop: 8 } },
            [['Where the desk answers', rule['Where the desk answers']], ['Who may press a button', rule['Who may press a Slack button']]].map(pair =>
              h('div', { key: pair[0], style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1])))),
          h('div', { className: 'stoa-btns' }, h(Btn, { onClick: openSettings }, 'Change either in Stoa\'s settings')),
          h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'Both settings take Slack\'s own ids. For a person: open their profile in Slack, press the three dots, and choose Copy member ID. For a channel: open the channel\'s details and copy the Channel ID at the bottom.'))
          : h('div', null,
            h('p', { style: { margin: '0 0 6px' } }, 'Open Messaging, choose Slack and follow its steps. When Hermes is in your Slack there is nothing more to do here: typing ', h('code', { className: 'stoa-code' }, '!gtm'), ' in Slack shows the board.'),
            h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', onClick: () => host.navigate('/messaging') }, 'Open Messaging')),
            setup.slack === false ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'Just connected it and this still says no? Restart Hermes once (hermes gateway restart) so Stoa\'s buttons are wired to Slack.') : null)),
      h(Step, { id: 'sweep', optional: true, done: step.sweep.done, title: 'Let Hermes keep the desk current', status: step.sweep.detail },
        h(Automations, { setup }))),

    h('div', { className: 'stoa-setgroup' }, 'Your desk', h('span', null, 'change these whenever you like')),
    h('div', { className: 'stoa-steps' },
      h(Step, { id: 'name', plain: true, title: 'The name at the head of the page', status: state.sign && state.sign.text ? state.sign.text : 'The stoa, with no name on it' }, h(NameBox, { state })),
      setup.tour ? null : h(Step, { id: 'pipes', plain: true, title: 'Pipelines', status: (state.pipelines || []).length > 1 ? (state.pipelines || []).length + ' side by side' : 'One. Add another to keep, say, your own sales apart from client work.' },
        h(Pipelines, { state, setup })),
      h(Step, { id: 'rules', plain: true, title: 'The rules the desk keeps', status: 'Where it answers, what it may read and write, paid lookups' },
        setup.rules.filter(item => !item.own).map(item => h('div', { key: item.label, className: 'stoa-setrow', style: { display: 'block' } },
          h('small', { className: 'stoa-eyebrow', style: { display: 'block' } }, item.label), h('span', { style: { overflowWrap: 'anywhere' } }, item.value))),
        h('div', { className: 'stoa-btns' }, h(Btn, { onClick: openSettings }, 'Change them in Stoa\'s settings'))),
      h(Step, { id: 'data', plain: true, title: 'Your data', status: (data.records ? plural(data.records, 'record', 'records') : 'Nothing on the desk yet') + (setup.tour ? ' · sample data is on show' : '') },
        h('div', { className: 'stoa-facts', style: { marginTop: 0, paddingTop: 0, borderTop: 0 } },
          [['Open', data.open], ['Won', data.won], ['Lost', data.lost], ['Log entries', data.activities], ['Market maps', data.maps], ['Lists waiting', data.lists_waiting],
            ['Changes on the ledger', data.ledger], ['Taken off the desk', data.archived]].map(pair =>
            h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))),
        h('div', { className: 'stoa-sub', style: { marginTop: 10, overflowWrap: 'anywhere' } }, 'One file on the computer Hermes runs on: ' + data.path),
        h('div', { className: 'stoa-sub', style: { marginTop: 4, overflowWrap: 'anywhere' } }, data.backups && data.backups.count
          ? 'A copy is kept each day, the last fourteen: ' + plural(data.backups.count, 'copy', 'copies') + ' so far, the newest from ' + data.backups.newest + ', in ' + data.backups.folder + '.'
          : 'A copy of the file is kept each day once there is something on the desk, the last fourteen, in a backups folder beside it.'),
        h('div', { className: 'stoa-btns' }, h(Btn, { disabled: busy, onClick: exportAll }, setup.tour ? 'Export the sample data as CSV' : 'Export everything as CSV'),
          setup.tour ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => wait(tour('leave')) }, 'Leave the tour') : null),
        h(HandedFiles),
        h(TakenOff),
        data.samples && !setup.tour ? h('div', { style: { marginTop: 10 } },
          h('div', { className: 'stoa-sub' }, plural(data.samples, 'sample record is', 'sample records are') + ' mixed into your own desk (an earlier version of Stoa put them there).'),
          h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: () => wait(tour('tidy')) }, 'Remove them from my desk'))) : null),
      h(Step, { id: 'howto', plain: true, title: 'How to work the desk', status: 'Things to say, press and type' },
        h('div', { className: 'stoa-cols', style: { gap: 22 } },
          h('div', null, h('div', { className: 'stoa-name' }, 'Say it'), h('div', { className: 'stoa-sub', style: { marginBottom: 6 } }, 'Press one to put it in the box below.'),
            SAY.map(line => h('button', { type: 'button', key: line, className: 'stoa-sayline', onClick: () => $draft.set(line.replace(/ \(then.*\)$/, '')) }, '“' + line + '”'))),
          h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Or do it by hand'), BY_HAND.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
          h('div', null, h('div', { className: 'stoa-name' }, 'Or type it'), h('div', { className: 'stoa-sub', style: { marginBottom: 6 } }, 'In a chat. Typed in the box below, a command opens one.'),
            TYPED.map(pair => h('p', { key: pair[0], className: 'stoa-tip' }, h('code', null, pair[0]), '  ' + pair[1]))))),
      h(Step, { id: 'always', plain: true, title: 'What the desk always does, and what your playbook decides', status: 'The parts that never change, and the parts that are yours' },
        h('div', { className: 'stoa-cols', style: { gap: 22 } },
          h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Always, whatever you sell'), ALWAYS.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
          h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Decided by your playbook'),
            [['What a record is called', sets.words], ['The stages', sets.stages.join(' → ')], ['How long before a nudge is due', 'per stage, ' + Math.min(...sets.quiet.map(item => item.days)) + ' to ' + Math.max(...sets.quiet.map(item => item.days)) + ' days'],
              ['Why records are lost', sets.lost_reasons.length + ' reasons to pick from'], ['The extra facts worth keeping', sets.fields.length ? sets.fields.join(', ') : 'none'],
              ['What Hermes writes at each stage', sets.drafts.length ? plural(sets.drafts.length, 'stage has', 'stages have') + ' a draft' : 'nothing yet']].map(pair =>
              h('p', { key: pair[0], className: 'stoa-tip' }, h('b', null, pair[0] + ': '), pair[1])))))),
    h('div', { className: 'stoa-sub', style: { textAlign: 'center', padding: '16px 0 4px' } }, 'Stoa ' + setup.version))
}

// -- what Hermes proposes: one decision each, with what it rests on -------------------------------
const EVIDENCE = { email: 'Email', call: 'Call', slack: 'Slack', meeting: 'Meeting', note: 'Note', calendar: 'Calendar' }

async function decide(item, verb) {
  try {
    const result = await rest('/intake/' + encodeURIComponent(item.intake) + '/row', { method: 'POST', body: { n: item.n, verb } })
    refresh()
    if (result.last) offerUndo(result.last)
    else if (result.note) host.notify({ kind: 'info', message: result.note })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

function Proposal({ item, busy, run, open, follow }) {
  const what = item.kind === 'new' ? 'New' + (item.stage ? ' at ' + item.stage : '') : cap(item.changes.map(changeText).join(' · '))
  const where = [item.company !== item.name ? item.company : '', item.group !== item.company ? item.group : ''].filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-proposal', 'data-follow': follow ? '1' : '0' },
    h('div', { className: 'stoa-prophead' },
      follow ? h('span', { className: 'stoa-sub' }, 'Also for ' + item.name)
        : item.deal ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', title: 'Open the record', onClick: open }, item.name) : h('span', { className: 'stoa-name' }, item.name),
      h('span', { className: 'stoa-sub stoa-clip stoa-grow' }, follow ? '' : where),
      h('span', { className: 'stoa-sub', style: { flex: 'none' } }, item.source + ' · ' + ago(item.at))),
    h('div', { className: 'stoa-propchange' }, what),
    item.case ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, item.case) : null,
    (item.evidence || []).map((entry, index) => h('blockquote', { key: index, className: 'stoa-evidence' },
      h('small', { className: 'stoa-eyebrow' }, [EVIDENCE[entry.kind] || cap(entry.kind) || 'Evidence', entry.at, entry.who].filter(Boolean).join(' · ')),
      entry.text ? h('span', null, entry.text) : null)),
    (item.warnings || []).map((warning, index) => h('div', { key: 'w' + index, className: 'stoa-sub' }, warning)),
    h('div', { className: 'stoa-btns', style: { marginTop: 10 } },
      h(Btn, { kind: 'go', disabled: busy, onClick: () => run('approve') }, 'Approve'),
      h(Btn, { kind: 'quiet', disabled: busy, title: 'Leave the desk as it is. Hermes will not propose this again.', onClick: () => run('decline') }, 'Decline')))
}

/** What sort of change a proposal is, by the heaviest thing it would do. */
const KINDS = [['steps', 'next step', 'next steps'], ['people', 'person', 'people'], ['stage', 'stage change', 'stage changes'],
  ['money', 'money entry', 'money entries'], ['new', 'new record', 'new records']]
function kindOf(item) {
  if (item.kind === 'new') return 'new'
  const fields = (item.changes || []).map(change => change.field)
  if (fields.includes('Invoice') || fields.includes('Payment')) return 'money'
  if (fields.includes('Stage')) return 'stage'
  if (fields.includes('Person')) return 'people'
  return 'steps'
}

/** The top of Today: everything waiting on a yes or a no. Nothing here has been written. */
function Approvals({ state, setOpenId }) {
  const items = (state.today && state.today.approvals) || []
  const [busy, setBusy] = useState('')
  if (!items.length) return null
  const key = item => item.intake + ':' + item.n
  // Suggestions about one record sit together, in the order the first of them arrived.
  const order = {}
  items.forEach((item, index) => { const about = item.list ? 'list:' + item.intake : item.deal || key(item); if (!(about in order)) order[about] = index })
  const sorted = items.map((item, index) => ({ item, index, at: order[item.list ? 'list:' + item.intake : item.deal || key(item)] }))
    .sort((a, b) => a.at - b.at || a.index - b.index).map(entry => entry.item)
  const proposals = sorted.filter(item => !item.list)
  const run = (item, verb) => { setBusy(key(item)); return decide(item, verb).then(() => setBusy('')) }
  const many = async (which, label) => {
    setBusy(label)
    try {
      const result = await rest('/approvals', { method: 'POST', body: { verb: 'approve', items: which.map(item => ({ intake: item.intake, n: item.n })) } })
      refresh()
      host.notify({ kind: 'success', message: plural(result.applied, 'change', 'changes') + ' made.' + ((result.notes || []).length ? ' ' + result.notes[0] : '') })
    } catch (error) {
      host.notifyError(error, 'Stoa could not do that')
    }
    setBusy('')
  }
  const kinds = KINDS.map(kind => ({ id: kind[0], one: kind[1], several: kind[2], items: proposals.filter(item => kindOf(item) === kind[0]) })).filter(kind => kind.items.length > 1)
  return h(Fold, { id: 'approvals', count: items.length, tone: 'good', title: 'Waiting for your approval',
    help: 'Changes Hermes suggests and lists you were sent. Nothing is written until you approve it.',
    summary: names(sorted.map(item => (item.list ? 'a list you were sent' : item.name))),
    aside: proposals.length > 1 ? h(Btn, { disabled: Boolean(busy), onClick: () => many(proposals, 'all') }, 'Approve all ' + proposals.length) : null },
    kinds.length && kinds[0].items.length < proposals.length ? h('div', { className: 'stoa-kinds' }, kinds.map(kind =>
      h(Btn, { key: kind.id, disabled: Boolean(busy), title: 'Approve every waiting ' + kind.one + ' at once. Each can still be taken back by itself.',
        onClick: () => many(kind.items, kind.id) }, 'Approve ' + kind.items.length + ' ' + kind.several))) : null,
    sorted.map((item, index) => item.list
      ? h('div', { key: item.intake, style: { marginBottom: 10 } }, h(Review, { id: item.intake }))
      : h(Proposal, { key: key(item), item, follow: index > 0 && !sorted[index - 1].list && Boolean(item.deal) && sorted[index - 1].deal === item.deal,
        busy: busy === 'all' || busy === kindOf(item) || busy === key(item), run: verb => run(item, verb), open: () => setOpenId(item.deal) })))
}

// -- taking a change back ----------------------------------------------------------------------------
function UndoBar() {
  const last = useValue($last)
  if (!last) return null
  return h('div', { className: 'stoa-undobar', role: 'status' },
    h('span', { className: 'stoa-clip' }, h('b', null, last.name), '  ' + last.text),
    h(Btn, { onClick: () => undo(last.deal, last.op) }, 'Undo'),
    h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, 'aria-label': 'Dismiss', onClick: () => $last.set(null) }, h(Icon, { name: 'x', size: 12 })))
}

// -- the other people on a record ---------------------------------------------------------------------
function People({ deal, term, title, whole }) {
  const pipe = useContext(InPipe)
  const blank = { name: '', email: '', title: '', role: '', phone: '' }
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState(blank)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => {
    setBusy(true)
    return rest('/deal/' + encodeURIComponent(deal.id) + '/person', { method: 'POST', body, ...inPipe(pipe) })
      .then(result => { refresh(); if (result.last) offerUndo(result.last); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const add = () => {
    if (!v.name.trim() || busy) return
    send({ verb: 'add', ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already on this record.' })
      setV(blank)
      setAdding(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) add() }
  const people = deal.people || []
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow', title: 'Email, calls and messages from these people are filed on this record too' }, title || 'Also on this ' + term),
      h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Person')),
    people.map(person => h('div', { key: person.id, className: 'stoa-person' },
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, person.name),
        person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
        person.email || person.phone || person.linkedin || person.also_on ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
          [person.email, person.phone, person.linkedin, person.also_on ? 'also on ' + plural(person.also_on, 'other record', 'other records') : ''].filter(Boolean).join(' · ')) : null),
      h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off this record',
        'aria-label': 'Remove ' + person.name, onClick: () => send({ verb: 'remove', person: String(person.id) }) }, h(Icon, { name: 'x', size: 12 })))),
    !people.length && !adding ? h('div', { className: 'stoa-sub' }, whole ? 'Nobody yet. Add the people on this ' + term + ' (the buyer, their boss, finance), and what they send is filed here too.'
      : 'Nobody else yet. Add the people around this ' + term + ' (a hiring manager, an assistant, finance), and what they send is filed here too.') : null,
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: set('name'), onKeyDown: enter }),
        h(Field, { label: 'Role here', value: v.role, placeholder: 'Decision maker, finance…', onChange: set('role'), onKeyDown: enter }),
        h(Field, { label: 'Title', value: v.title, onChange: set('title'), onKeyDown: enter }),
        h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email'), onKeyDown: enter }),
        h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone'), onKeyDown: enter })),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: add }, 'Add'))) : null)
}

/** Every company on the desk, biggest open value first. Press one for the whole picture. */
function Companies({ state, current, openCompany }) {
  const filter = useValue($filter)
  const pb = state.playbook
  const text = filter.text.toLowerCase()
  const rows = (state.companies || []).filter(row => !text || row.name.toLowerCase().includes(text))
  if (!rows.length) return h('p', { className: 'stoa-sub' }, text ? 'No company matches “' + filter.text + '”.' : 'No company is named on any record yet.')
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-crow stoa-chead' }, ['Company', 'Open', pb.won.label, 'People', 'Last touch'].map(label => h('span', { key: label, className: 'stoa-eyebrow' }, label))),
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openCompany(row.name) },
      h('span', { className: 'stoa-name stoa-clip' }, row.name),
      h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, String(row.people)),
      h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- a group: a search, an account, a client. Who it is for, where it stands, its terms, who is on their side ----
const GROUP_STATUS = [['open', 'Open'], ['hold', 'On hold'], ['done', 'Done'], ['closed', 'Closed']]

/** Every group on the desk: who it is for, where it stands, what is open in it. Open ones first. */
function Groups({ state, current, openGroup }) {
  const filter = useValue($filter)
  const pb = state.playbook, term = pb.terms.group
  const [name, setName] = useState('')
  const [client, setClient] = useState('')
  const [busy, setBusy] = useState(false)
  const text = filter.text.toLowerCase()
  const rows = (state.groups || []).filter(row => !text || (row.name + ' ' + row.client).toLowerCase().includes(text))
  const open = () => {
    if (!name.trim() || busy) return
    setBusy(true)
    rest('/group', { method: 'POST', body: { name: name.trim(), new: true, client: client.trim() } })
      .then(result => { refresh(); setName(''); setClient(''); openGroup(result.group.name) })
      .catch(error => host.notifyError(error, 'Stoa could not open that'))
      .then(() => setBusy(false))
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) open() }
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-newgroup' },
      h(Field, { label: 'New ' + term, value: name, placeholder: 'Its name', onChange: event => setName(event.target.value), onKeyDown: enter }),
      h(Field, { label: 'Client', value: client, placeholder: 'The company it is for', onChange: event => setClient(event.target.value), onKeyDown: enter }),
      h(Btn, { disabled: busy || !name.trim(), onClick: open }, 'Open it')),
    !rows.length ? h('p', { className: 'stoa-sub' }, text ? 'No ' + term + ' matches “' + filter.text + '”.' : 'No ' + term + ' yet. Open one above, or name one on a ' + pb.terms.deal + '.') : null,
    rows.length ? h('div', { className: 'stoa-crow stoa-chead', 'data-cols': '7' }, [cap(term), 'Client', 'Status', 'Open', pb.won.label, 'People', 'Last touch'].map(label =>
      h('span', { key: label, className: 'stoa-eyebrow' }, label))) : null,
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-cols': '7', 'data-dim': row.status === 'open' ? '0' : '1',
      'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openGroup(row.name) },
    h('span', { className: 'stoa-name stoa-clip' }, row.name),
    h('span', { className: 'stoa-clip' }, row.client || '—'),
    h('span', { className: 'stoa-num' }, row.status_label),
    h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, String(row.people)),
    h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- how much a scheduled job may change by itself ----------------------------------------------------
function Autonomy({ setup }) {
  const [busy, setBusy] = useState(false)
  const auto = setup.autonomy
  const pick = level => {
    if (level === auto.level || busy) return
    setBusy(true)
    rest('/autonomy', { method: 'POST', body: { level } })
      .then(result => { refresh(); host.notify({ kind: 'success', message: 'Scheduled jobs: ' + result.label + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not change that'))
      .then(() => setBusy(false))
  }
  const forget = () => {
    setBusy(true)
    rest('/declined/forget', { method: 'POST', body: {} })
      .then(result => { refresh(); host.notify({ kind: 'info', message: plural(result.forgotten, 'declined proposal', 'declined proposals') + ' forgotten.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not do that'))
      .then(() => setBusy(false))
  }
  return h('div', null,
    h('div', { className: 'stoa-sub', style: { marginBottom: 8 } }, 'In a chat, Hermes does what you ask. This is for the jobs that run when you are not there. Only you can change it here: Hermes has no way to.'),
    auto.levels.map(level => h('button', { type: 'button', key: level.id, className: 'stoa-level', 'data-on': level.id === auto.level ? '1' : '0', disabled: busy,
      'aria-pressed': level.id === auto.level, onClick: () => pick(level.id) },
    h(Lamp, { on: level.id === auto.level }),
    h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, level.label), h('span', { className: 'stoa-sub' }, level.summary)))),
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sub' }, setup.declined ? plural(setup.declined, 'proposal you declined is', 'proposals you declined are') + ' remembered for 90 days and not made again.'
        : 'Anything it may not do is held on Today for your yes or no. A no is remembered for 90 days and not asked again.'),
      setup.declined ? h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: forget }, 'Forget them')) : null))
}

// -- money: invoices on a record, where won work stands, and what is likely to come in ---------------
// Stoa sends no invoices and moves no money. It keeps the account of them, so the desk can say what
// is owed, what is late, and what was won but never billed.
const $billing = atom('') // the record whose "new invoice" form should be open when its panel appears
const BILL_TONE = { overdue: 'bad', partial: 'warn', paid: 'good' }
const BILL_WORD = { overdue: 'overdue', partial: 'part paid', sent: 'sent', draft: 'draft', paid: 'paid', void: 'void' }

/** An amount in full: an invoice is not rounded to the nearest thousand. */
function exact(value, currency) {
  const number = Number(value || 0)
  const text = number.toLocaleString(undefined, { minimumFractionDigits: Number.isInteger(number) ? 0 : 2, maximumFractionDigits: 2 })
  const sign = SYMBOL[currency] ?? ''
  return sign ? sign + text : text + ' ' + (currency || '')
}

const inDays = days => { const d = new Date(Date.now() + days * 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }

async function bill(dealId, body, pipe) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/invoice', { method: 'POST', body, ...inPipe(pipe) })
    refresh()
    if (result.last) offerUndo(result.last)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

/** One invoice, with the moves that fit where it stands. */
function Invoice({ invoice, dealId, client, currency, name, onOpen }) {
  const pipe = useContext(InPipe)
  const [busy, setBusy] = useState(false)
  const [part, setPart] = useState(null) // null, or the amount being typed for a part payment
  const run = body => {
    setBusy(true)
    return (client ? billClient(client, { invoice: String(invoice.id), ...body }) : bill(dealId, { invoice: String(invoice.id), ...body }, pipe))
      .then(result => { setBusy(false); return result })
  }
  const owing = invoice.state === 'sent' || invoice.state === 'partial' || invoice.state === 'overdue'
  const takePart = () => { const amount = Number(part); if (amount > 0) run({ verb: 'pay', amount }).then(result => { if (result) setPart(null) }) }
  const when = invoice.state === 'paid' ? (invoice.paid_on ? 'paid ' + shortDate(invoice.paid_on) : 'paid')
    : invoice.state === 'overdue' ? invoice.days_overdue + 'd late'
      : invoice.due_on && owing ? 'due ' + shortDate(invoice.due_on) : ''
  return h('div', { className: 'stoa-irow', 'data-state': invoice.state },
    h('span', { className: 'stoa-grow', style: { minWidth: 0 } },
      name ? h('button', { type: 'button', className: 'stoa-proplink stoa-name stoa-clip', style: { display: 'block', maxWidth: '100%' }, title: 'Open the record', onClick: onOpen }, name) : null,
      h('span', { className: name ? 'stoa-sub' : 'stoa-name' }, (invoice.number ? '#' + invoice.number + ' · ' : '') + exact(invoice.amount, currency)),
      invoice.state === 'partial' || (invoice.state === 'overdue' && invoice.paid) ? h('span', { className: 'stoa-sub' }, ' · ' + exact(invoice.owed, currency) + ' still owed') : null,
      invoice.note ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, invoice.note) : null),
    when ? h('span', { className: 'stoa-num', 'data-tone': BILL_TONE[invoice.state] || '' }, when) : null,
    h(Chip, { tone: BILL_TONE[invoice.state] || '' }, BILL_WORD[invoice.state] || invoice.state),
    h('span', { className: 'stoa-tacts' },
      part !== null ? [
        h('input', { key: 'amount', className: 'stoa-field', type: 'number', min: 0, step: 'any', autoFocus: true, value: part, placeholder: 'Amount received', 'aria-label': 'Amount received',
          style: { width: 124, marginTop: 0 }, onChange: event => setPart(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') takePart(); if (event.key === 'Escape') setPart(null) } }),
        h(Btn, { key: 'ok', kind: 'go', disabled: busy || !(Number(part) > 0), onClick: takePart }, 'Record'),
        h(Btn, { key: 'no', kind: 'quiet', onClick: () => setPart(null), 'aria-label': 'Cancel' }, '×')
      ] : [
        owing ? h(Btn, { key: 'paid', kind: 'go', disabled: busy, title: 'Everything still owed came in', onClick: () => run({ verb: 'pay' }) }, 'Paid') : null,
        owing ? h(Btn, { key: 'part', disabled: busy, title: 'Part of it came in', onClick: () => setPart('') }, 'Part') : null,
        invoice.state === 'draft' ? h(Btn, { key: 'send', kind: 'go', disabled: busy, onClick: () => run({ verb: 'send' }) }, 'Mark sent') : null,
        h('select', { key: 'more', className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'More', style: { maxWidth: 74 },
          onChange: event => { const verb = event.target.value; if (verb) run({ verb }) } },
        [h('option', { key: '', value: '' }, 'More…')].concat(invoice.state !== 'void' && invoice.state !== 'paid' ? [h('option', { key: 'void', value: 'void' }, 'Void it')] : [],
          [h('option', { key: 'remove', value: 'remove' }, 'Remove (entered by mistake)')]))
      ]))
}

/** The money on one record: what was billed, what came in, and a form to add an invoice. */
function Money({ deal, pb }) {
  const pipe = useContext(InPipe)
  const billing = useValue($billing)
  const money = deal.money || { invoiced: 0, paid: 0, owed: 0, uninvoiced: 0 }
  const invoices = deal.invoices || []
  const suggested = deal.status === 'won' ? money.uninvoiced : Math.max(0, (deal.value || 0) - money.invoiced)
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState({ amount: '', number: '', due: inDays(30), draft: false })
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (billing !== deal.id) return
    setAdding(true)
    $billing.set('')
  }, [billing, deal.id])
  useEffect(() => { if (adding) setV(now => ({ ...now, amount: now.amount || (suggested ? String(suggested) : '') })) }, [adding])
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const add = () => {
    if (!(Number(v.amount) > 0) || busy) return
    setBusy(true)
    bill(deal.id, { verb: 'add', amount: Number(v.amount), number: v.number.trim(), due: v.due, status: v.draft ? 'draft' : 'sent' }, pipe).then(result => {
      setBusy(false)
      if (result) { setAdding(false); setV({ amount: '', number: '', due: inDays(30), draft: false }) }
    })
  }
  if (!invoices.length && deal.status !== 'won' && !adding) {
    return h('div', { className: 'stoa-log' }, h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(true) }, '+  Invoice')))
  }
  const line = [money.invoiced ? 'invoiced ' + exact(money.invoiced, pb.currency) : '', money.paid ? 'paid ' + exact(money.paid, pb.currency) : '',
    money.owed ? 'owed ' + exact(money.owed, pb.currency) : '', deal.status === 'won' && money.uninvoiced ? exact(money.uninvoiced, pb.currency) + ' not invoiced' : '']
    .filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Invoice')),
    line ? h('div', { className: 'stoa-sub', style: { marginBottom: 2 } }, cap(line)) : null,
    deal.value ? h(BillBar, { value: deal.value, invoiced: money.invoiced, paid: money.paid }) : null,
    invoices.map(invoice => h(Invoice, { key: invoice.id, invoice, dealId: deal.id, currency: pb.currency })),
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Amount', type: 'number', min: 0, step: 'any', autoFocus: true, value: v.amount, onChange: set('amount') }),
        h(Field, { label: 'Number', value: v.number, placeholder: '1042', onChange: set('number') }),
        h(Field, { label: 'Due', type: 'date', value: v.due, onChange: set('due') }),
        h('label', { style: { alignSelf: 'end', display: 'flex', gap: 6, alignItems: 'center', height: 28, fontSize: 12 } },
          h('input', { type: 'checkbox', checked: v.draft, onChange: event => setV({ ...v, draft: event.target.checked }) }), 'Not sent yet')),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
        h(Btn, { kind: 'primary', disabled: busy || !(Number(v.amount) > 0), onClick: add }, 'Add invoice'),
        h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Stoa keeps the record. It does not send the invoice.'))) : null)
}

/** How much of a record's value has been billed, and how much of that has come in. */
function BillBar({ value, invoiced, paid }) {
  const of = Math.max(value || 0, invoiced || 0, 1)
  return h('div', { className: 'stoa-billbar', title: 'Paid, then invoiced, out of the full amount' },
    h('i', { 'data-kind': 'invoiced', style: { width: Math.min(100, (100 * (invoiced || 0)) / of) + '%' } }),
    h('i', { 'data-kind': 'paid', style: { width: Math.min(100, (100 * (paid || 0)) / of) + '%' } }))
}

const BASIS = {
  history: 'Each stage counts for how often records there have gone on to close on this desk.',
  playbook: 'Each stage counts for the odds your playbook gives it. Your own history takes over once twelve have closed.',
  even: 'Each stage counts for an even step toward closing. Your own history takes over once twelve have closed.'
}

/** The Cash tab: what came in and what is owed, what the open pipeline is likely to bring, and where won work stands. */
function Cash({ state, openId, setOpenId, openClient }) {
  const cash = state.cash, pb = state.playbook
  const [over, setOver] = useState('')
  if (!cash) return h('p', { className: 'stoa-sub' }, 'The money side needs the gateway restarted after this update (hermes gateway restart).')
  const strip = cash.strip, forecast = cash.forecast, lane = cash.delivery
  const records = pb.billing !== 'none'          // records themselves are billed here (not only clients by the month)
  const toBill = cash.to_bill || []
  const tiles = [
    [pb.won.label + ' this month', strip.won_month.value, plural(strip.won_month.count, pb.terms.deal, pb.terms.deals), '', 'What was closed as ' + pb.won.label.toLowerCase() + ' since the first of the month.'],
    ['Collected this month', strip.collected_month.value, 'payments recorded', 'good', 'Every payment recorded on an invoice this month.'],
    ['Invoiced, not yet paid', strip.owed.value, plural(strip.owed.count, 'invoice', 'invoices'), '', 'What is still owed on invoices that have been sent.'],
    ['Overdue', strip.overdue.value, plural(strip.overdue.count, 'invoice', 'invoices') + ' past due', strip.overdue.count ? 'bad' : '', 'Owed on invoices whose due date has passed.'],
    [pb.won.label + ', not invoiced', strip.uninvoiced.value, plural(strip.uninvoiced.count, pb.terms.deal, pb.terms.deals), strip.uninvoiced.count ? 'warn' : '', 'Won work whose invoices add up to less than its value.'],
    ['To bill', (strip.to_bill || { value: 0 }).value, plural((strip.to_bill || { count: 0 }).count, 'month', 'months') + ' ended, not billed', (strip.to_bill || { count: 0 }).count ? 'warn' : '', 'Months that have ended for clients billed by the month.']]
    .filter((tile, index) => (records || (index !== 0 && index !== 4)) && (index !== 5 || toBill.length || (cash.invoices || []).some(item => item.client)))
  const most = Math.max(1, ...forecast.buckets.map(bucket => bucket.value))
  const shown = forecast.buckets.filter(bucket => bucket.count)
  const stages = lane.stages.length ? lane.stages : [{ id: '', label: pb.won.label }]
  const invoiceProps = invoice => invoice.client
    ? { key: invoice.id, invoice, client: invoice.client, currency: pb.currency, name: invoice.client + (invoice.period ? ' · ' + monthLabel(invoice.period) : ''), onOpen: () => openClient(invoice.client) }
    : { key: invoice.id, invoice, dealId: invoice.deal, currency: pb.currency, name: invoice.name, onOpen: () => setOpenId(invoice.deal) }
  const owing = cash.invoices.filter(invoice => invoice.state !== 'paid')
  const settled = cash.invoices.filter(invoice => invoice.state === 'paid')
  const drop = (event, stage) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || !stage.id) return
    event.preventDefault()
    const record = lane.records.find(item => item.id === id)
    if (record && record.delivery !== stage.id) act(id, { verb: 'update', delivery: stage.id })
  }
  return h('div', { className: 'stoa-cash' },
    h(ProductionHead, { state, openClient, showPipe: id => { $pipe.set(id === 'main' ? '' : id); if (storage) storage.set('pipe', id === 'main' ? '' : id); refresh() } }),
    h('div', { className: 'stoa-strip' }, tiles.map(tile => h('div', { key: tile[0], className: 'stoa-tile', 'data-tone': tile[3], title: tile[4] },
      h('span', { className: 'stoa-eyebrow' }, tile[0]), h('b', null, money(tile[1], pb.currency) || '0'), h('span', { className: 'stoa-sub' }, tile[2])))),
    toBill.length ? h('div', { className: 'stoa-h2' }, 'To bill') : null,
    toBill.length ? h('div', { style: { maxWidth: 980 } }, toBill.map(row => h(BillMonth, { key: row.client + row.period, row, pb, showClient: true, openClient }))) : null,
    records ? h('div', { className: 'stoa-h2' }, 'Likely to come in') : null,
    records ? h('div', { className: 'stoa-box' },
      h('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '4px 14px' } },
        h('span', { className: 'stoa-name', style: { fontSize: 18 } }, money(forecast.weighted, pb.currency) || '0'),
        h('span', { className: 'stoa-sub' }, 'weighted, of ' + (money(forecast.value, pb.currency) || '0') + ' open')),
      shown.length ? h('div', { style: { marginTop: 10 } }, shown.map(bucket => h('div', { key: bucket.key, className: 'stoa-fc', 'data-key': bucket.key },
        h('span', { className: 'stoa-clip' }, bucket.label),
        h('u', null, h('i', { style: { width: (100 * bucket.value) / most + '%' } }), h('b', { style: { width: (100 * bucket.weighted) / most + '%' } })),
        h('span', { className: 'stoa-num' }, (money(bucket.weighted, pb.currency) || '0') + ' of ' + (money(bucket.value, pb.currency) || '0') + ' · ' + bucket.count))))
        : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Nothing is open.'),
      h('div', { className: 'stoa-sub', style: { marginTop: 10 } }, BASIS[forecast.basis] + ' A ' + pb.terms.deal + ' goes in a month by its expected close date (Edit on the record).'),
      h('div', { className: 'stoa-chain', style: { margin: '6px 0 0' } }, forecast.odds.map(item => item.label + ' ' + item.odds + '%').join('  ·  '))) : null,
    records ? h('div', { className: 'stoa-h2' }, 'After the win') : null,
    !records ? null : lane.records.length ? h('div', { className: 'stoa-board', style: { gridAutoColumns: 'minmax(200px, 1fr)' } }, stages.map(stage => {
      const here = lane.records.filter(record => (record.delivery || '') === stage.id)
      return h('div', { key: stage.id || 'won', className: 'stoa-col', 'data-over': over === stage.id && stage.id ? '1' : '0',
        onDragOver: event => { if (dragging(event) && stage.id) { event.preventDefault(); if (over !== stage.id) setOver(stage.id) } },
        onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
        onDrop: event => drop(event, stage) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, stage.label), h('span', { className: 'stoa-num' }, String(here.length))),
      h('div', { className: 'stoa-weight' }),
      here.length ? here.map(record => h('button', { type: 'button', key: record.id, className: 'stoa-card', 'data-open': openId === record.id ? '1' : '0',
        draggable: Boolean(stage.id), title: stage.id ? 'Drag to another column as the work moves on' : undefined,
        onDragStart: event => { event.dataTransfer.setData(DRAG, record.id); event.dataTransfer.effectAllowed = 'move' }, onDragEnd: () => setOver(''),
        onClick: () => setOpenId(openId === record.id ? '' : record.id) },
      h('h5', null, h('span', { className: 'stoa-clip' }, record.name), record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null),
      h('div', { className: 'stoa-sub stoa-clip' }, record.company || ' '),
      record.next_step ? h('div', { className: 'stoa-clip', style: { marginTop: 6, fontSize: 12 } }, record.next_step, record.next_due ? h('span', { className: 'stoa-sub' }, '  ' + shortDate(record.next_due)) : null) : null,
      record.value ? h(BillBar, { value: record.value, invoiced: record.invoiced, paid: record.paid }) : null,
      h('div', { className: 'stoa-chips', style: { marginTop: 6 } },
        record.overdue ? h(Chip, { tone: 'bad' }, 'invoice overdue') : null,
        !record.invoiced && record.value ? h(Chip, { tone: 'warn' }, 'not invoiced')
          : record.uninvoiced ? h(Chip, null, (money(record.uninvoiced, pb.currency) || '') + ' to bill') : null,
        record.value && record.paid >= record.value ? h(Chip, { tone: 'good' }, 'paid') : record.owed && !record.overdue ? h(Chip, null, 'awaiting payment') : null)))
        : h('div', { className: 'stoa-empty' }, over === stage.id && stage.id ? 'Drop it here' : '—'))
    })) : h('p', { className: 'stoa-sub' }, 'Nothing has been ' + pb.won.label.toLowerCase() + ' yet. When something is, it lands here: where the work stands, and whether it has been billed.'),
    h('div', { className: 'stoa-h2' }, 'Invoices'),
    owing.length || settled.length ? h('div', { style: { maxWidth: 980 } },
      owing.map(invoice => h(Invoice, invoiceProps(invoice))),
      settled.length ? h('div', { className: 'stoa-eyebrow', style: { margin: '14px 0 2px' } }, 'Lately paid') : null,
      settled.map(invoice => h(Invoice, invoiceProps(invoice))))
      : h('p', { className: 'stoa-sub' }, 'No invoices yet. Open a record and press + Invoice, or tell Stoa: “I invoiced Kestrel 12k today, net 30.”'))
}

// -- a record, a group, a company: each on a page of its own, laid out the way a CRM is read ----------
// Across the top: what it is, where it stands, and what to do next. Below it, three columns: the details on the
// left, what happened in the middle, and what it is connected to on the right. On a narrow screen they stack.

const isEmpty = value => value === null || value === undefined || value === ''
const sameName = (a, b) => Boolean(a) && Boolean(b) && String(a).trim().toLowerCase() === String(b).trim().toLowerCase()
const dayLabel = stamp => {
  const d = new Date(stamp)
  if (isNaN(d)) return ''
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return 'Today'
  if (d.toDateString() === new Date(Date.now() - 864e5).toDateString()) return 'Yesterday'
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
}
const clock = stamp => { const d = new Date(stamp); return isNaN(d) ? '' : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) }
const daysSince = stamp => { const then = Date.parse(stamp || ''); return then ? Math.max(0, Math.floor((Date.now() - then) / 864e5)) : null }
const webLink = url => (/^https?:\/\//i.test(url) ? url : 'https://' + url)
const bareLink = url => String(url || '').replace(/^https?:\/\/(www\.)?/i, '').replace(/\/$/, '')

function BackRow({ back, label }) {
  return h('div', { className: 'stoa-backrow' },
    h('button', { type: 'button', className: 'stoa-back', onClick: back, title: 'Back to ' + label }, h('span', { 'aria-hidden': true }, '‹'), ' ' + label))
}

function Section({ title, aside, children, main }) {
  return h('section', { className: 'stoa-box stoa-rpsec', 'data-main': main ? '1' : '0' },
    h('div', { className: 'stoa-rpsechead' }, h('h3', null, title), aside || null),
    children)
}

/** Label and value, one under the other; an empty one says so, so you can see what is not known yet. */
function Facts({ rows }) {
  return h('dl', { className: 'stoa-dl' }, rows.flatMap(row => [
    h('dt', { key: row[0] + ':k' }, row[0]),
    h('dd', { key: row[0] + ':v', 'data-empty': isEmpty(row[1]) ? '1' : '0' }, isEmpty(row[1]) ? '—' : row[1])]))
}

/** Records in a short list: name, where it stands, the next step, and what it is worth. */
function RecordList({ rows, pb, openRecord, showCompany, underStage }) {
  return h('div', { className: 'stoa-rlist' }, rows.map(record => {
    const open = record.status === 'open'
    const due = open && record.next_due && record.next_due <= today()
    return h('button', { type: 'button', key: record.id, className: 'stoa-rrow', 'data-closed': open ? '0' : '1', onClick: () => openRecord(record.id) },
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, record.name,
          showCompany && record.company && !sameName(record.company, record.name) ? h('span', { className: 'stoa-sub' }, '  ' + record.company) : null),
        h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } },
          [underStage && open ? '' : record.stage_label, open ? record.next_step || (underStage ? 'Nothing planned' : '') : ''].filter(Boolean).join(' · '))),
      due ? h(Chip, { tone: record.next_due < today() ? 'bad' : 'warn' }, record.next_due < today() ? 'overdue' : 'today') : null,
      record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null)
  }))
}

/** The stages as one bar, the record's place on it plain to see. A stage pressed moves it there (and can be taken back). */
function Path({ deal, pb, busy, run }) {
  const open = deal.status === 'open', active = open || Boolean(deal.in_delivery)      // a win still in delivery keeps its next step
  const end = deal.status === 'won' ? ['won', pb.won.label] : deal.status === 'lost' ? ['lost', pb.lost.label + (deal.lost_reason ? ': ' + deal.lost_reason : '')] : ['todo', pb.won.label]
  return h('div', { className: 'stoa-path', role: 'list', 'aria-label': 'Stages' },
    pb.stages.map((stage, index) => {
      const at = deal.status === 'won' || (open && index < deal.position) ? 'done' : open && index === deal.position ? 'now' : 'todo'
      return h('button', { type: 'button', key: stage.id, role: 'listitem', 'data-state': at, disabled: busy || !open || at === 'now', style: heatAt(index, pb.stages.length + 1),
        title: open && at !== 'now' ? 'Move to ' + stage.label : stage.label, onClick: () => run({ verb: 'move', stage: stage.id }) }, h('span', null, stage.label))
    }),
    h('span', { className: 'stoa-pathend', role: 'listitem', 'data-state': end[0], style: heatAt(pb.stages.length, pb.stages.length + 1) }, h('span', null, end[1])))
}

/** Every fact on a record, in one form: the playbook's own fields included. */
function DetailsForm({ deal, pb, state, done }) {
  const terms = pb.terms, open = deal.status === 'open'
  const base = { name: deal.name || '', company: deal.company || '', group: deal.group || '', email: deal.email || '', phone: deal.phone || '',
    linkedin: deal.linkedin || '', owner: deal.owner || '', value: deal.value ? String(deal.value) : '', close_on: deal.close_on || '', source: deal.source || '' }
  const baseFields = Object.fromEntries(pb.fields.map(field => [field.key, String((deal.fields || {})[field.key] || '')]))
  const [v, setV] = useState(base)
  const [f, setF] = useState(baseFields)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = () => {
    if (!v.name.trim() || busy) return
    const body = { verb: 'update' }
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now !== base[key]) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    const fields = {}
    for (const key of Object.keys(f)) if (String(f[key]).trim() !== baseFields[key]) fields[key] = String(f[key]).trim()
    if (Object.keys(fields).length) body.fields = fields
    if (Object.keys(body).length === 1) return done()
    setBusy(true)
    act(deal.id, body).then(result => { setBusy(false); if (result) done() })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save(); if (event.key === 'Escape') done() }
  return h('div', null,
    h('div', { className: 'stoa-form', 'data-one': '1' },
      h(Field, { label: 'Name', value: v.name, onChange: set('name'), onKeyDown: enter }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company'), onKeyDown: enter }),
      h(Field, { label: cap(terms.group), list: 'stoa-edit-groups', value: v.group, onChange: set('group'), onKeyDown: enter }),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email'), onKeyDown: enter }),
      h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: set('phone'), onKeyDown: enter }),
      h(Field, { label: 'LinkedIn', value: v.linkedin, placeholder: 'linkedin.com/in/…', onChange: set('linkedin'), onKeyDown: enter }),
      h(Field, { label: cap(terms.owner), list: 'stoa-edit-owners', value: v.owner, onChange: set('owner'), onKeyDown: enter }),
      noFees(state) ? null : h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value'), onKeyDown: enter }),
      open ? h(Field, { label: 'Expected close', type: 'date', value: v.close_on, title: 'The forecast puts it in this month', onChange: set('close_on') }) : null,
      pb.fields.map(field => h(Field, { key: field.key, label: field.label, value: f[field.key], onChange: event => setF({ ...f, [field.key]: event.target.value }), onKeyDown: enter })),
      h(Field, { label: 'Source', value: v.source, placeholder: 'Referral, LinkedIn, an event…', onChange: set('source'), onKeyDown: enter })),
    h('datalist', { id: 'stoa-edit-groups' }, uniq((state.deals || []).map(item => item.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-edit-owners' }, uniq((state.deals || []).map(item => item.owner)).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Save'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: done }, 'Cancel')))
}

function RecordDetails({ deal, pb, state, openGroup, openCompany }) {
  const [editing, setEditing] = useState(false)
  const terms = pb.terms, open = deal.status === 'open'
  const link = (text, onClick, title) => h('button', { type: 'button', className: 'stoa-proplink stoa-alink', title, onClick }, text)
  // Where an account or a client is the company, the two are one line, and it opens the one page about them.
  const oneLine = pb.group_is_company && deal.group && (!deal.company || sameName(deal.company, deal.group))
  const rows = [
    oneLine ? [cap(terms.group), link(deal.group, () => openGroup(deal.group), 'Everything about ' + deal.group)]
      : [cap(terms.company), deal.company ? link(deal.company, () => (pb.group_is_company && deal.group ? openGroup(deal.group) : openCompany(deal.company)), 'Everything at ' + deal.company) : ''],
    oneLine ? null : [cap(terms.group), deal.group ? link(deal.group, () => openGroup(deal.group), 'The ' + terms.group + ' it is in') : ''],
    ['Email', deal.email ? h('a', { className: 'stoa-alink', href: 'mailto:' + deal.email }, deal.email) : ''],
    ['Phone', deal.phone ? h('a', { className: 'stoa-alink', href: 'tel:' + deal.phone.replace(/[^+\d]/g, '') }, deal.phone) : ''],
    ['LinkedIn', deal.linkedin ? h('a', { className: 'stoa-alink', href: webLink(deal.linkedin), target: '_blank', rel: 'noreferrer' }, bareLink(deal.linkedin)) : ''],
    [cap(terms.owner), deal.owner],
    ...(noFees(state) ? [] : [[cap(terms.value), money(deal.value, pb.currency)]]),
    open ? ['Expected close', deal.close_on ? shortDate(deal.close_on) + (deal.close_on < today() ? ' (past)' : '') : ''] : null]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Source', deal.source], ['Added', deal.created_at ? shortDate(deal.created_at.slice(0, 10)) : '']])
    .filter(Boolean)
  return h(Section, { title: 'Details', aside: h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close' : 'Edit') },
    editing ? h(DetailsForm, { deal, pb, state, done: () => setEditing(false) }) : h(Facts, { rows }))
}

const LOG_PLACEHOLDER = { note: 'A thought, something to remember…', call: 'What was said on the call…', email: 'What the email said…', meeting: 'What came out of the meeting…' }

/** What happened, newest first, a day at a time, with a line at the top to add to it. */
function Timeline({ deal, sessionId }) {
  const [kind, setKind] = useState('call')
  const [line, setLine] = useState('')
  const [saving, setSaving] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const add = () => {
    const text = line.trim()
    if (!text || saving) return
    setSaving(true)
    act(deal.id, { verb: 'log', kind, text }, sessionId).then(result => { setSaving(false); if (result) setLine('') })
  }
  const entries = deal.activity || []
  let day = ''
  const items = []
  entries.forEach((entry, index) => {
    const label = dayLabel(entry.at)
    if (label !== day) { day = label; items.push(h('li', { key: 'day' + index, className: 'stoa-tlday' }, label)) }
    items.push(h('li', { key: index, className: 'stoa-tl', 'data-kind': entry.kind, 'data-undone': entry.undone ? '1' : '0' },
      h('span', { className: 'stoa-tlkind' }, LOG_KINDS[entry.kind] || cap(entry.kind)),
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-tltext' }, entry.text),
        h('div', { className: 'stoa-sub' }, [clock(entry.at), entry.by, entry.undone ? 'taken back' : ''].filter(Boolean).join(' · ')))))
  })
  return h(Section, { main: true, title: 'Activity',
    aside: deal.undo ? h(Btn, { kind: 'quiet', disabled: undoing, title: 'Put the record back as it was before: ' + deal.undo.text,
      onClick: () => { setUndoing(true); undo(deal.id, deal.undo.op).then(() => setUndoing(false)) } },
    h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Take back the last change')) : null },
  h('div', { className: 'stoa-logbox' },
    h('select', { className: 'stoa-select', value: kind, 'aria-label': 'What it was', onChange: event => setKind(event.target.value) },
      [['call', 'Call'], ['email', 'Email'], ['meeting', 'Meeting'], ['note', 'Note']].map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1]))),
    h('input', { className: 'stoa-field', value: line, disabled: saving, placeholder: LOG_PLACEHOLDER[kind], 'aria-label': 'Add to the log',
      onChange: event => setLine(event.target.value), onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); add() } } }),
    h(Btn, { kind: 'primary', disabled: saving || !line.trim(), onClick: add }, 'Log it')),
  items.length ? h('ol', { className: 'stoa-timeline' }, items) : h('p', { className: 'stoa-sub' }, 'Nothing logged yet.'))
}

/** What the record is connected to: the group it is in and the others there, others at the same company, its people, its money. */
function RecordRelated({ deal, pb, state, openRecord, openGroup, openCompany, openClient }) {
  const deals = state.deals || []
  const inGroup = deal.group ? deals.filter(item => item.id !== deal.id && sameName(item.group, deal.group)) : []
  // Others at the same company that the group above does not already show (a candidate's colleagues, say).
  const atCompany = !pb.group_is_company && deal.company
    ? deals.filter(item => item.id !== deal.id && sameName(item.company, deal.company) && !(deal.group && sameName(item.group, deal.group)))
    : []
  const sortOpen = rows => rows.slice().sort((a, b) => (a.status !== 'open') - (b.status !== 'open') || (b.position || 0) - (a.position || 0) || a.name.localeCompare(b.name))
  const billed = pb.billing !== 'none' && (deal.status === 'won' || (deal.invoices || []).length > 0)
  const groupSub = deal.client || deal.group_status ? h('span', null,
    deal.client ? ['for ', h('button', { key: 'c', type: 'button', className: 'stoa-proplink', onClick: () => openClient(deal.client) }, deal.client)] : null,
    deal.group_status ? (deal.client ? ' · ' : '') + deal.group_status.toLowerCase() : null) : null
  return h('div', { className: 'stoa-rpcol' },
    deal.group ? h(Section, { title: cap(pb.terms.group), aside: h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openGroup(deal.group) }, 'Open  ›') },
      h('button', { type: 'button', className: 'stoa-proplink stoa-name', style: { fontSize: 14 }, onClick: () => openGroup(deal.group) }, deal.group),
      groupSub ? h('div', { className: 'stoa-sub' }, groupSub) : null,
      inGroup.length ? h('div', { style: { marginTop: 10 } },
        h('small', { className: 'stoa-eyebrow' }, 'Also in it'),
        h(RecordList, { rows: sortOpen(inGroup).slice(0, 6), pb, openRecord, showCompany: !pb.group_is_company }),
        inGroup.length > 6 ? h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openGroup(deal.group) }, 'and ' + (inGroup.length - 6) + ' more  ›') : null)
        : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Nothing else in it yet.')) : null,
    atCompany.length ? h(Section, { title: 'Also at ' + deal.company, aside: h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => openCompany(deal.company) }, 'Open  ›') },
      h(RecordList, { rows: sortOpen(atCompany).slice(0, 5), pb, openRecord })) : null,
    billed ? h('section', { className: 'stoa-box stoa-rpsec' }, h(Money, { deal, pb })) : null)
}

function RecordPage({ id, state: shown, sessionId, back, backLabel, openRecord, openGroup, openCompany, openClient }) {
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [nextForm, setNextForm] = useState('') // '', 'done' or 'plan'
  const deal = query.data
  // The moment a record is won and its delivery begins, ask for the first step of that: the step it had was for the sale.
  const was = useRef('')
  const [justWon, setJustWon] = useState(false)
  useEffect(() => {
    if (!deal) return
    if (was.current === 'open' && deal.status === 'won' && deal.in_delivery) { setJustWon(true); setNextForm('plan') }
    was.current = deal.status
  }, [deal ? deal.status : ''])
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.'))
  if (!deal) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const elsewhere = Boolean(deal.pipeline && shown.pipeline && deal.pipeline.id !== shown.pipeline)
  const state = deal.playbook ? { ...shown, playbook: deal.playbook } : shown
  const pb = state.playbook, terms = pb.terms
  const open = deal.status === 'open', active = open || Boolean(deal.in_delivery)      // a win still in delivery keeps its next step
  const run = body => { setBusy(true); act(id, body, sessionId).then(() => setBusy(false)) }
  const inStage = daysSince(deal.stage_at)
  // Once it is won and in delivery, the step is that stage's: "Onboarding next step".
  const phase = (deal.delivery_label || 'delivery').toLowerCase(), stepName = !open && deal.in_delivery ? cap(phase) + ' next step' : 'Next step'
  const oneLine = pb.group_is_company && deal.group && (!deal.company || sameName(deal.company, deal.group))
  // Where it sits: "at" its company, "in" its group (and who that group is for, when the name does not already say).
  const where = []
  const forClient = deal.client && !pb.group_is_company
    ? h('span', null, ', for ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openClient(deal.client) }, deal.client)) : null
  if (oneLine) where.push(h('span', { key: 'g' }, h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openGroup(deal.group) }, deal.group)))
  else {
    if (deal.company) where.push(h('span', { key: 'c' }, 'at ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => (pb.group_is_company && deal.group ? openGroup(deal.group) : openCompany(deal.company)) }, deal.company)))
    if (deal.group) where.push(h('span', { key: 'g' }, 'in ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openGroup(deal.group) }, deal.group), forClient))
  }
  const status = deal.status === 'won' ? pb.won.label + (deal.delivery_label ? ' · ' + deal.delivery_label : '') : deal.status === 'lost' ? pb.lost.label : ''
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, [elsewhere ? deal.pipeline.name : '', cap(terms.deal)].filter(Boolean).join(' · ')),
        h('h2', { className: 'stoa-rptitle' }, deal.name),
        where.length ? h('div', { className: 'stoa-rpwhere' }, where.flatMap((item, index) => (index ? [h('span', { key: 's' + index, className: 'stoa-rpsep', 'aria-hidden': true }, ' / '), item] : [item]))) : null,
        h('div', { className: 'stoa-chips' },
          status ? h(Chip, { tone: deal.status === 'won' ? 'good' : '' }, status) : null,
          deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
          deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
          asleep(deal) ? h(Chip, null, 'snoozed to ' + shortDate(deal.snoozed_until)) : null,
          deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null)),
      h('div', { className: 'stoa-rpfig' },
        deal.value ? h('b', null, money(deal.value, pb.currency)) : null,
        h('span', { className: 'stoa-sub' }, [deal.owner, open && inStage !== null ? (inStage === 0 ? 'in this stage since today' : inStage + 'd in this stage') : ''].filter(Boolean).join(' · ')))),
    h(Path, { deal, pb, busy, run }),
    h('div', { className: 'stoa-rpbar' },
      h('div', { className: 'stoa-rpnext' },
        h('small', { className: 'stoa-eyebrow' }, stepName),
        active ? (deal.next_step
          ? h('div', { className: 'stoa-rpnextline' }, h('span', { className: 'stoa-name' }, deal.next_step), deal.next_due ? h('span', { className: 'stoa-num', 'data-tone': dueTone(deal) }, dueLabel(deal)) : null)
          : h('div', { className: 'stoa-sub' }, open ? 'Nothing planned.' : 'Nothing planned for ' + phase + ' yet.'))
          : h('div', { className: 'stoa-sub' }, (deal.status === 'won' ? pb.won.label + (deal.delivery_label ? ' and ' + deal.delivery_label.toLowerCase() : '') + '. What follows goes on its people and its company.'
            : pb.lost.label + (deal.lost_reason ? ': ' + deal.lost_reason : '') + '. Nothing more to do here unless it is reopened.'))),
      h('div', { className: 'stoa-btns', style: { marginTop: 0 } },
        active && deal.next_step ? h(Btn, { disabled: busy, title: 'The next step is done: log it and say what comes next', onClick: () => setNextForm(nextForm === 'done' ? '' : 'done') }, 'Done') : null,
        active ? h(Btn, { kind: !open && !deal.next_step ? 'primary' : '', disabled: busy, onClick: () => { setJustWon(false); setNextForm(nextForm === 'plan' ? '' : 'plan') } }, deal.next_step ? 'Change' : open ? 'Plan the next step' : 'Plan the ' + phase + ' step') : null,
        active ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
        active ? h(Snooze, { deal, busy, run }) : null,
        open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
          onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) => h('option', { key: index, value: String(index) }, reason)))) : null,
        deal.status === 'won' && (deal.delivery_stages || []).length ? h('select', { className: 'stoa-select', value: deal.delivery || '', disabled: busy,
          'aria-label': 'Where the work stands', title: 'Where the work stands', onChange: event => run({ verb: 'update', delivery: event.target.value }) },
        deal.delivery_stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))) : null,
        !open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
        open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null,
        h(RemoveRecord, { deal, after: back }))),
    nextForm && active ? h('div', { className: 'stoa-box', style: { marginTop: 10 } }, h(NextStep, { deal, mode: nextForm === 'done' && deal.next_step ? 'done' : 'plan',
      lead: justWon ? pb.won.label + ', and now in ' + phase + '. What is the first ' + phase + ' step?' + (deal.next_step ? ' It replaces “' + deal.next_step + '”.' : '') : '',
      close: () => { setNextForm(''); setJustWon(false) } })) : null,
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' }, h(RecordDetails, { deal, pb, state, openGroup, openCompany })),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Notes' }, h(RecordNotes, { key: 'notes-' + deal.id, deal, bare: true })),
        // On a deal everyone is a person on it. Where the record is itself a person (a candidate, a lead) these are the others.
        h('section', { className: 'stoa-box stoa-rpsec' }, h(People, { deal, term: pb.terms.deal, title: pb.shape === 'deals' ? 'People' : 'Other people', whole: pb.shape === 'deals' })),
        h(Timeline, { deal, sessionId })),
      h(RecordRelated, { deal, pb, state, openRecord, openGroup, openCompany, openClient })))
}

/** The records in a group or at a company, a stage at a time in the playbook's order, the closed ones last. */
function ByStage({ records, pb, openRecord, showCompany }) {
  const sections = pb.stages.map(stage => [stage.label, records.filter(record => record.status === 'open' && record.stage === stage.id)])
    .concat([[pb.won.label, records.filter(record => record.status === 'won')], [pb.lost.label, records.filter(record => record.status === 'lost')]])
    .filter(pair => pair[1].length)
  if (!sections.length) return h('p', { className: 'stoa-sub' }, 'None yet.')
  return h('div', null, sections.map(pair => h('div', { key: pair[0], className: 'stoa-stagegroup' },
    h('div', { className: 'stoa-stagehead' }, h('span', { className: 'stoa-eyebrow' }, pair[0]),
      h('span', { className: 'stoa-sub' }, [String(pair[1].length), money(pair[1].reduce((sum, record) => sum + (record.value || 0), 0), pb.currency)].filter(Boolean).join(' · '))),
    h(RecordList, { rows: pair[1], pb, openRecord, showCompany, underStage: true }))))
}

/** Someone known there: their name, what they are, where they are on the desk. */
function PersonLine({ person, openRecord, remove, busy }) {
  const where = person.primary ? '' : person.on === 'record' ? 'on ' + person.record_name : ''
  return h('div', { className: 'stoa-person' },
    h('span', { className: 'stoa-grow' },
      person.record && person.primary
        ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(person.record) }, person.name)
        : h('span', { className: 'stoa-name' }, person.name),
      person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
      person.email || person.phone || where ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
        [person.email, person.phone, where].filter(Boolean).join(' · ')) : null),
    remove ? h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off',
      'aria-label': 'Remove ' + person.name, onClick: remove }, h(Icon, { name: 'x', size: 12 })) : null)
}

/** Recent activity across several records, each line naming its record. */
function Lately({ entries, openRecord }) {
  if (!entries.length) return h('p', { className: 'stoa-sub' }, 'Nothing logged yet.')
  return h('ol', { className: 'stoa-timeline' }, entries.map((entry, index) => h('li', { key: index, className: 'stoa-tl', 'data-kind': entry.kind },
    h('span', { className: 'stoa-tlkind' }, LOG_KINDS[entry.kind] || cap(entry.kind)),
    h('div', { className: 'stoa-grow' },
      h('div', { className: 'stoa-tltext' }, h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(entry.deal) }, entry.name), '  ' + entry.text),
      h('div', { className: 'stoa-sub' }, [ago(entry.at), entry.by].filter(Boolean).join(' · '))))))
}

function GroupPage({ name, state, sessionId, back, backLabel, openRecord, openClient, onRenamed }) {
  const query = useQuery({ queryKey: ['stoa', 'group', name], queryFn: () => rest('/group', { method: 'POST', body: { name } }), staleTime: 1500 })
  const blank = { name: '', email: '', title: '', role: '', phone: '' }
  const [draft, setDraft] = useState(null) // null until something is edited
  const [editing, setEditing] = useState(false)
  const [note, setNote] = useState(null) // the notes, while being edited
  const [busy, setBusy] = useState(false)
  const [addingPerson, setAddingPerson] = useState(false)
  const [addingRecord, setAddingRecord] = useState(false)
  const [v, setV] = useState(blank)
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'That one is no longer on the desk.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, group = data.group, term = pb.terms.group
  const company = data.company // set when a group here is a company: one place for both
  const was = { title: group.name, client: group.client, status: group.status, value: group.value ? String(group.value) : '', note: group.note, fields: {},
    domain: company ? company.domain : '' }
  const now = draft || was
  const set = patch => setDraft({ ...now, ...patch })
  const fact = item => (item.key in now.fields ? now.fields[item.key] : item.value)
  const send = (path, body, said) => {
    setBusy(true)
    return rest(path, { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const saveNote = () => send('/group', { name: group.name, note: note }, 'Notes saved on ' + group.name + '.').then(result => { if (result) setNote(null) })
  const save = async () => {
    const body = { name: group.name, status: now.status, fields: now.fields }
    if (!company) body.client = now.client
    if (now.value.trim() !== was.value) body.value = now.value.trim() === '' ? 0 : now.value.trim()
    if (company && now.domain.trim().toLowerCase() !== (company.domain || '')) { body.company = company.name; body.domain = now.domain.trim() }
    const saved = await send('/group', body, 'Saved on ' + group.name + '.')
    if (!saved) return
    const title = now.title.trim()
    setDraft(null)
    setEditing(false)
    if (title && title !== group.name) {
      const moved = await send('/group', { name: group.name, to: title }, 'Renamed to ' + title + '.')
      if (moved && onRenamed) onRenamed(moved.group.name)
    }
  }
  const addPerson = () => {
    if (!v.name.trim() || busy) return
    send('/group/person', { verb: 'add', group: group.name, ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already here.' })
      setV(blank)
      setAddingPerson(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) addPerson() }
  const field = key => event => setV({ ...v, [key]: event.target.value })
  const exportIt = share => send('/export', { group: group.name, share }).then(file => {
    if (!file) return
    download(file.filename, file.csv, 'text/csv')
    host.notify({ kind: 'success', message: plural(file.rows, pb.terms.deal, pb.terms.deals) + ' exported: only ' + group.name + (share ? ', without your own columns.' : '.') })
  })
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost)], ['Last touch', ago(data.last_touch) || '—']]
  const sub = company ? [company.domain, group.status !== 'open' ? group.status_label.toLowerCase() : ''].filter(Boolean).join(' · ')
    : [group.client ? 'for ' + group.client : 'No client named yet', group.status !== 'open' ? group.status_label.toLowerCase() : ''].filter(Boolean).join(' · ')
  const ours = data.people.filter(person => person.on === 'group'), theirs = data.people.filter(person => person.on !== 'group')
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, cap(term)),
        h('h2', { className: 'stoa-rptitle' }, group.name),
        !company && group.client ? h('div', { className: 'stoa-rpwhere' }, 'for ', h('button', { type: 'button', className: 'stoa-proplink', onClick: () => openClient(group.client) }, group.client),
          group.status !== 'open' ? h('span', { className: 'stoa-sub' }, '  ·  ' + group.status_label.toLowerCase()) : null)
          : sub ? h('div', { className: 'stoa-rpwhere stoa-sub' }, sub) : null),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpbar', 'data-plain': '1' },
      h('div', { className: 'stoa-btns', style: { marginTop: 0 } },
        h(Btn, { kind: 'primary', onClick: () => setAddingRecord(!addingRecord) }, addingRecord ? 'Close' : '+  New ' + pb.terms.deal + ' here'),
        data.records.length ? h(Btn, { disabled: busy, title: 'A CSV of the ' + pb.terms.deals + ' here and nothing else, with every column', onClick: () => exportIt(false) }, 'Export') : null,
        data.records.length && !company ? h(Btn, { disabled: busy, title: 'The copy to hand the client: no value, owner, next steps, dates or sources', onClick: () => exportIt(true) }, 'Copy to share') : null)),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details', aside: h(Btn, { kind: 'quiet', onClick: () => { setEditing(!editing); setDraft(null) } }, editing ? 'Close' : 'Edit') },
          editing ? h('div', null,
            h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
              h(Field, { label: 'Name', value: now.title, onChange: event => set({ title: event.target.value }) }),
              company ? h(Field, { label: 'Email domain', value: now.domain, placeholder: 'kestrel.com', title: 'Mail from anyone at this domain is recognised as coming from here',
                onChange: event => set({ domain: event.target.value }) })
                : h(Field, { label: 'Client', value: now.client, placeholder: 'The company it is for', title: 'The company that pays: not always the company on a ' + pb.terms.deal,
                  onChange: event => set({ client: event.target.value }) }),
              h(Field, { label: 'Status' },
                h('select', { className: 'stoa-field', value: now.status, onChange: event => set({ status: event.target.value }) },
                  GROUP_STATUS.map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))),
              noFees(state) ? null : h(Field, { label: cap(pb.terms.value) + ' for each ' + pb.terms.deal, value: now.value, placeholder: 'Left blank: each says its own',
                title: 'A new ' + pb.terms.deal + ' here takes this unless it brings its own', onChange: event => set({ value: event.target.value }) }),
              (group.facts || []).filter(item => !(noFees(state) && FEE_FACTS.includes(item.key))).map(item => h(Field, { key: item.key, label: item.label, value: fact(item), onChange: event => set({ fields: { ...now.fields, [item.key]: event.target.value } }) }))),
            h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy || !now.title.trim() || !draft, onClick: save }, 'Save'),
              h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setDraft(null); setEditing(false) } }, 'Cancel')))
            : h(Facts, { rows: [company ? ['Email domain', company.domain ? company.domain + (company.domain_known ? '' : ' (from their email)') : ''] : ['Client', group.client],
              ['Status', group.status_label], ...(noFees(state) ? [] : [[cap(pb.terms.value) + ' for each ' + pb.terms.deal, group.value ? money(group.value, pb.currency) : '']])]
              .concat((group.facts || []).filter(item => !(noFees(state) && FEE_FACTS.includes(item.key))).map(item => [item.label, item.value])) })),
        h(Section, { title: 'Notes' },
          h('textarea', { className: 'stoa-field stoa-notes', style: { marginTop: 0 }, rows: 4, value: note === null ? group.note : note,
            placeholder: 'What to know about them: how they decide, what was agreed, what to avoid…', onChange: event => setNote(event.target.value) }),
          company && company.note && company.note !== (note === null ? group.note : note) ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0', whiteSpace: 'pre-line' } }, 'Kept on the company before: ' + company.note) : null,
          note !== null && note !== group.note ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: saveNote }, 'Save notes'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setNote(null) }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        addingRecord ? h(NewRecord, { state, preset: { group: group.name, company: company ? company.name : '' }, close: () => setAddingRecord(false),
          opened: id => { setAddingRecord(false); if (id) openRecord(id) } }) : null,
        h(Section, { main: true, title: cap(pb.terms.deals) + ' (' + data.records.length + ')' },
          h(ByStage, { records: data.records, pb, openRecord, showCompany: !company }))),
      h('div', { className: 'stoa-rpcol' },
        company && data.billing ? h(ClientBilling, { key: 'bill:' + group.name, name: group.name, billing: data.billing, pb }) : null,
        h(Section, { title: company ? 'People there' : 'On their side', aside: h(Btn, { kind: 'quiet', onClick: () => setAddingPerson(!addingPerson) }, addingPerson ? 'Close' : '+  Person') },
          ours.map(person => h(PersonLine, { key: 'g' + person.id, person, openRecord, busy,
            remove: () => send('/group/person', { verb: 'remove', group: group.name, person: String(person.id) }) })),
          theirs.map(person => h(PersonLine, { key: 'r' + person.id, person, openRecord })),
          !data.people.length && !addingPerson ? h('div', { className: 'stoa-sub' }, company ? 'Nobody yet.' : 'Nobody yet. Add the people you deal with there, and what they send is recognised.') : null,
          addingPerson ? h('div', null,
            h('div', { className: 'stoa-form', 'data-one': '1' },
              h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: field('name'), onKeyDown: enter }),
              h(Field, { label: 'Role here', value: v.role, placeholder: company ? 'Decision maker, finance…' : 'Hiring manager, sponsor…', onChange: field('role'), onKeyDown: enter }),
              h(Field, { label: 'Title', value: v.title, onChange: field('title'), onKeyDown: enter }),
              h(Field, { label: 'Email', type: 'email', value: v.email, onChange: field('email'), onKeyDown: enter }),
              h(Field, { label: 'Phone', type: 'tel', value: v.phone, onChange: field('phone'), onKeyDown: enter })),
            h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: addPerson }, 'Add'))) : null),
        h(Section, { title: 'Lately' }, h(Lately, { entries: data.activity || [], openRecord })))))
}

function CompanyPage({ name, state, back, backLabel, openRecord, openGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'company', name], queryFn: () => rest('/company', { method: 'POST', body: { name } }), staleTime: 1500 })
  const [note, setNote] = useState(null) // null until edited
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'Nothing on the desk is at that company any more.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, company = data.company || { name, domain: '', note: '' }
  const changed = (note !== null && note !== company.note) || (domain !== null && domain.trim().toLowerCase() !== company.domain)
  const save = () => {
    setBusy(true)
    rest('/company', { method: 'POST', body: { name: company.name, note: note === null ? company.note : note, domain: domain === null ? company.domain : domain } })
      .then(() => { refresh(); setNote(null); setDomain(null); host.notify({ kind: 'success', message: 'Saved on ' + company.name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save that'))
      .then(() => setBusy(false))
  }
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost || 0)], ['Last touch', ago(data.last_touch) || '—']]
  const groups = uniq((data.records || []).map(record => record.group))
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, 'Company'),
        h('h2', { className: 'stoa-rptitle' }, company.name),
        h('div', { className: 'stoa-rpwhere stoa-sub' }, company.domain || 'No email domain known yet')),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details' },
          h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
            h(Field, { label: 'Email domain', value: domain === null ? company.domain : domain, placeholder: 'kestrel.com',
              title: 'Mail from anyone at this domain is recognised as coming from this company', onChange: event => setDomain(event.target.value) })),
          groups.length ? h('div', { style: { marginTop: 12 } }, h('small', { className: 'stoa-eyebrow' }, cap(pb.terms.groups)),
            groups.map(item => h('div', { key: item }, h('button', { type: 'button', className: 'stoa-proplink stoa-alink', onClick: () => openGroup(item) }, item)))) : null,
          h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginTop: 12 } }, 'Notes'),
          h('textarea', { className: 'stoa-field stoa-notes', rows: 4, value: note === null ? company.note : note, placeholder: 'What is worth remembering about them: how they buy, who decides, when their year ends…',
            onChange: event => setNote(event.target.value) }),
          changed ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setNote(null); setDomain(null) } }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { main: true, title: cap(pb.terms.deals) + ' here (' + (data.records || []).length + ')' },
          h(ByStage, { records: data.records || [], pb, openRecord }))),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'People there' },
          (data.people || []).length ? data.people.map((person, index) => h(PersonLine, { key: index, person: { ...person, on: 'record' }, openRecord }))
            : h('div', { className: 'stoa-sub' }, 'Nobody yet.')),
        h(Section, { title: 'Lately' }, h(Lately, { entries: (data.activity || []).map(entry => ({ ...entry, deal: entry.deal || entry.record })), openRecord })))))
}

// -- a client: the company the work is for, its searches, and how it is billed when that is by the month ----
/** Whether the desk keeps money for clients billed by the month: then there is a Cash tab even where records are never billed. */
const hasClientMoney = data => Boolean(data.cash && (((data.cash.strip || {}).to_bill || { count: 0 }).count || (data.cash.invoices || []).some(item => item.client)))
  || (data.clients || []).some(row => row.terms && row.terms.kind)

async function billClient(client, body) {
  try {
    const result = await rest('/client/bill', { method: 'POST', body: { client, ...body } })
    refresh()
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

const monthLabel = period => { const d = new Date(period + '-15T12:00:00'); return isNaN(d) ? period : d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) }
const termsLine = (terms, currency, client) => {
  if (!terms || !terms.kind) return ''
  const to = terms.bill_to && !sameName(terms.bill_to, client) ? ' to ' + terms.bill_to : ''
  return terms.kind === 'hourly'
    ? exact(terms.rate, currency) + ' an hour · ' + terms.hours_week + ' h a week · billed monthly' + to
    : exact(terms.rate, currency) + ' a month · billed monthly' + to
}

/** A month that has ended and is not billed: what it comes to, and the invoice recorded in one go (or the month skipped). */
function BillMonth({ row, pb, showClient, openClient }) {
  const hourly = row.kind === 'hourly'
  const [open, setOpen] = useState(false)
  const [hours, setHours] = useState(hourly ? String(row.hours) : '')
  const [amount, setAmount] = useState(String(row.amount))
  const [number, setNumber] = useState('')
  const [busy, setBusy] = useState(false)
  const total = hourly ? Math.round((Number(hours) || 0) * row.rate * 100) / 100 : Number(amount) || 0
  const record = () => {
    if (busy || !(total > 0)) return
    setBusy(true)
    billClient(row.client, { verb: 'add', period: row.period, number: number.trim(), ...(hourly ? { hours: Number(hours) } : { amount: Number(amount) }) })
      .then(result => { setBusy(false); if (result) { setOpen(false); host.notify({ kind: 'success', message: row.label + ' recorded for ' + row.client + ': ' + exact(total, pb.currency) + '.' }) } })
  }
  const skip = () => { setBusy(true); billClient(row.client, { verb: 'skip', period: row.period }).then(() => setBusy(false)) }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) record(); if (event.key === 'Escape') setOpen(false) }
  return h('div', { className: 'stoa-irow' },
    h('span', { className: 'stoa-grow' },
      h('span', { className: 'stoa-name', style: { display: 'block' } },
        showClient ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openClient(row.client) }, row.client) : null,
        showClient ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ·  ') : null, row.label),
      h('span', { className: 'stoa-sub', style: { display: 'block' } },
        (hourly ? row.hours + ' h × ' + exact(row.rate, pb.currency) : 'Retainer') + (row.bill_to && !sameName(row.bill_to, row.client) ? '  ·  to ' + row.bill_to : ''))),
    h('span', { className: 'stoa-num' }, exact(row.amount, pb.currency)),
    h('span', { className: 'stoa-tacts' },
      h(Btn, { kind: 'go', disabled: busy, onClick: () => setOpen(!open) }, open ? 'Close' : 'Record invoice'),
      h(Btn, { disabled: busy, title: 'Not billed for this month: time off, a pause. It can be billed later from the client.', onClick: skip }, 'Skip')),
    open ? h('div', { className: 'stoa-billform' },
      hourly ? h(Field, { label: 'Hours in ' + row.label.split(' ')[0], type: 'number', min: 0, step: 'any', autoFocus: true, value: hours, onChange: event => setHours(event.target.value), onKeyDown: enter })
        : h(Field, { label: 'Amount', type: 'number', min: 0, step: 'any', autoFocus: true, value: amount, onChange: event => setAmount(event.target.value), onKeyDown: enter }),
      h(Field, { label: 'Invoice number', value: number, placeholder: 'optional', onChange: event => setNumber(event.target.value), onKeyDown: enter }),
      h('div', { className: 'stoa-billtotal' }, h('small', { className: 'stoa-eyebrow' }, 'The invoice'), h('b', null, exact(total, pb.currency))),
      h(Btn, { kind: 'primary', disabled: busy || !(total > 0), onClick: record }, 'Record it'),
      h('span', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, 'Stoa keeps the record; send the invoice itself the way you always do.')) : null)
}

/** How a client is billed by the month: the terms, the months waiting, and its invoices. */
function ClientBilling({ name, billing, pb, compact }) {
  const terms = billing.terms || {}
  const [editing, setEditing] = useState(false)
  const start = { kind: terms.kind || 'hourly', rate: terms.rate ? String(terms.rate) : '', hours_week: String(terms.hours_week || 40),
    bill_to: terms.bill_to || '', start: terms.start || new Date().toISOString().slice(0, 7), end: terms.end || '' }
  const [v, setV] = useState(start)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = clear => {
    setBusy(true)
    const body = clear ? { name, terms: { kind: '' } }
      : { name, terms: { kind: v.kind, rate: Number(v.rate), hours_week: v.kind === 'hourly' ? Number(v.hours_week) : undefined, bill_to: v.bill_to.trim(), start: v.start, end: v.end } }
    rest('/client', { method: 'POST', body })
      .then(() => { refresh(); setEditing(false); host.notify({ kind: 'success', message: clear ? 'Billing terms taken off ' + name + '.' : 'Billing terms saved on ' + name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save those terms'))
      .then(() => setBusy(false))
  }
  const invoices = billing.invoices || []
  const totals = [billing.billed ? 'billed ' + exact(billing.billed, pb.currency) : '', billing.paid ? 'paid ' + exact(billing.paid, pb.currency) : '',
    billing.owed ? 'owed ' + exact(billing.owed, pb.currency) : ''].filter(Boolean).join(' · ')
  return h(Section, { title: 'Billing', aside: h(Btn, { kind: 'quiet', onClick: () => { setV(start); setEditing(!editing) } }, editing ? 'Close' : terms.kind ? 'Edit terms' : 'Set terms') },
    editing ? h('div', null,
      h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
        h(Field, { label: 'Billed' }, h('select', { className: 'stoa-field', value: v.kind, onChange: set('kind') },
          h('option', { value: 'hourly' }, 'By the hour, every month'), h('option', { value: 'retainer' }, 'A fixed amount every month'))),
        h(Field, { label: v.kind === 'hourly' ? 'Rate per hour' : 'Amount each month', type: 'number', min: 0, step: 'any', value: v.rate, onChange: set('rate') }),
        v.kind === 'hourly' ? h(Field, { label: 'Usual hours a week', type: 'number', min: 1, max: 80, value: v.hours_week, onChange: set('hours_week') }) : null,
        h(Field, { label: 'Invoice goes to', value: v.bill_to, placeholder: name + ' (or the fund, a parent company…)', onChange: set('bill_to') }),
        h(Field, { label: 'First month billed', type: 'month', value: v.start, onChange: set('start') }),
        h(Field, { label: 'Last month (when it ends)', type: 'month', value: v.end, onChange: set('end') })),
      h('div', { className: 'stoa-btns' },
        h(Btn, { kind: 'primary', disabled: busy || !(Number(v.rate) > 0), onClick: () => save(false) }, 'Save terms'),
        terms.kind ? h(Btn, { kind: 'quiet', disabled: busy, onClick: () => save(true) }, 'Not billed by the month') : null))
      : terms.kind ? h('div', null,
        h('div', { className: 'stoa-name' }, termsLine(terms, pb.currency, name)),
        h('div', { className: 'stoa-sub' }, 'Since ' + monthLabel(terms.start) + (terms.end ? ', until ' + monthLabel(terms.end) : '')))
        : h('div', { className: 'stoa-sub' }, 'Not billed by the month. Set terms for contract hours or a retainer, and each month that ends is waiting on Today to be billed.'),
    (billing.to_bill || []).length ? h('div', { style: { marginTop: 12 } },
      h('small', { className: 'stoa-eyebrow' }, 'To bill'),
      billing.to_bill.map(row => h(BillMonth, { key: row.period, row, pb }))) : null,
    invoices.length ? h('div', { style: { marginTop: 12 } },
      h('small', { className: 'stoa-eyebrow' }, 'Invoices'),
      totals ? h('div', { className: 'stoa-sub', style: { margin: '2px 0 2px' } }, cap(totals)) : null,
      (compact ? invoices.slice(0, 6) : invoices).map(invoice => h(Invoice, { key: invoice.id, invoice, client: name, currency: pb.currency,
        name: invoice.period_label || '' }))) : null)
}

/** Every client: what is open for them, and how each is billed. */
function Clients({ state, openClient }) {
  const filter = useValue($filter)
  const pb = state.playbook
  const text = filter.text.toLowerCase()
  const rows = (state.clients || []).filter(row => !text || row.name.toLowerCase().includes(text))
  if (!rows.length) return h('p', { className: 'stoa-sub' }, text ? 'No client matches “' + filter.text + '”.' : 'No client yet. Name the client on a ' + pb.terms.group + ' (open it, then Edit), and it is listed here.')
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-crow stoa-chead', 'data-cols': '6' }, ['Client', cap(pb.terms.groups), 'Open', pb.won.label, 'Billing', 'Owed'].map(label => h('span', { key: label, className: 'stoa-eyebrow' }, label))),
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-cols': '6', onClick: () => openClient(row.name) },
      h('span', { className: 'stoa-name stoa-clip' }, row.name),
      h('span', { className: 'stoa-num' }, row.open_groups + (row.groups.length > row.open_groups ? ' of ' + row.groups.length : '')),
      h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, row.won ? String(row.won) : '—'),
      h('span', { className: 'stoa-clip stoa-sub' }, row.terms && row.terms.kind ? (row.terms.kind === 'hourly' ? exact(row.terms.rate, pb.currency) + '/h' : exact(row.terms.rate, pb.currency) + '/mo')
        + (row.to_bill ? ' · ' + row.to_bill + ' to bill' : '') : 'by placement'),
      h('span', { className: 'stoa-num', 'data-tone': row.overdue ? 'bad' : '' }, row.owed ? exact(row.owed, pb.currency) : '—'))))
}

function ClientPage({ name, state, back, backLabel, openRecord, openGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'client', name], queryFn: () => rest('/client', { method: 'POST', body: { name } }), staleTime: 1500 })
  const [note, setNote] = useState(null)
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const [opening, setOpening] = useState(false)
  const [newName, setNewName] = useState('')
  const data = query.data
  if (query.error) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-box stoa-sub' }, 'No client by that name is on the desk any more.'))
  if (!data) return h('div', null, h(BackRow, { back, label: backLabel }), h('div', { className: 'stoa-sub' }, 'Loading…'))
  const pb = state.playbook, client = data.client, term = pb.terms.group
  const changed = (note !== null && note !== client.note) || (domain !== null && domain.trim().toLowerCase() !== client.domain)
  const save = () => {
    setBusy(true)
    rest('/client', { method: 'POST', body: { name: client.name, note: note === null ? client.note : note, domain: domain === null ? client.domain : domain } })
      .then(() => { refresh(); setNote(null); setDomain(null); host.notify({ kind: 'success', message: 'Saved on ' + client.name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save that'))
      .then(() => setBusy(false))
  }
  const openSearch = () => {
    const title = newName.trim()
    if (!title || busy) return
    setBusy(true)
    rest('/group', { method: 'POST', body: { name: title, new: true, client: client.name } })
      .then(result => { refresh(); setNewName(''); setOpening(false); openGroup(result.group.name) })
      .catch(error => host.notifyError(error, 'Stoa could not open that'))
      .then(() => setBusy(false))
  }
  const billing = data.billing || {}
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [cap(pb.terms.groups), String(data.searches.filter(row => row.status === 'open' || row.status === 'hold').length)],
    [pb.won.label, String(data.won)], ['Owed', billing.owed ? exact(billing.owed, pb.currency) : '—']]
  return h('div', { className: 'stoa-rp' },
    h(BackRow, { back, label: backLabel }),
    h('div', { className: 'stoa-rphead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, 'Client'),
        h('h2', { className: 'stoa-rptitle' }, client.name),
        h('div', { className: 'stoa-rpwhere stoa-sub' }, [termsLine(billing.terms, pb.currency, client.name), client.domain].filter(Boolean).join('  ·  ') || 'Billed by placement')),
      h('div', { className: 'stoa-rpstats' }, stats.map(pair => h('div', { key: pair[0] }, h('span', { className: 'stoa-eyebrow' }, pair[0]), h('b', null, pair[1]))))),
    h('div', { className: 'stoa-rpbar', 'data-plain': '1' },
      opening ? h('div', { className: 'stoa-newgroup', style: { margin: 0 } },
        h(Field, { label: 'New ' + term + ' for ' + client.name, autoFocus: true, value: newName, placeholder: 'Its name (the role)',
          onChange: event => setNewName(event.target.value), onKeyDown: event => { if (event.key === 'Enter') openSearch(); if (event.key === 'Escape') setOpening(false) } }),
        h(Btn, { kind: 'primary', disabled: busy || !newName.trim(), onClick: openSearch }, 'Open it'),
        h(Btn, { kind: 'quiet', onClick: () => setOpening(false) }, 'Cancel'))
        : h('div', { className: 'stoa-btns', style: { marginTop: 0 } }, h(Btn, { kind: 'primary', onClick: () => setOpening(true) }, '+  New ' + term))),
    h('div', { className: 'stoa-rpgrid' },
      h('div', { className: 'stoa-rpcol' },
        h(Section, { title: 'Details' },
          h('div', { className: 'stoa-form', 'data-one': '1', style: { marginTop: 0 } },
            h(Field, { label: 'Email domain', value: domain === null ? client.domain : domain, placeholder: 'fernway.com',
              title: 'Mail from anyone at this domain is recognised as coming from this client', onChange: event => setDomain(event.target.value) })),
          h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginTop: 12 } }, 'Notes'),
          h('textarea', { className: 'stoa-field stoa-notes', rows: 4, value: note === null ? client.note : note,
            placeholder: 'What to know about them: who decides, how they hire, what was agreed…', onChange: event => setNote(event.target.value) }),
          changed ? h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
            h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setNote(null); setDomain(null) } }, 'Cancel')) : null)),
      h('div', { className: 'stoa-rpcol' },
        h(Section, { main: true, title: cap(pb.terms.groups) + ' (' + data.searches.length + ')' },
          data.searches.length ? h('div', { className: 'stoa-rlist' }, data.searches.map(row => h('button', { type: 'button', key: row.name, className: 'stoa-rrow',
            'data-closed': row.status === 'open' || row.status === 'hold' ? '0' : '1', onClick: () => openGroup(row.name) },
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, row.name),
            h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } },
              [row.status === 'open' ? '' : row.status_label, plural(row.open, 'open', 'open'), row.won ? row.won + ' ' + pb.won.label.toLowerCase() : '']
                .concat(row.facts.slice(0, 2).map(fact => fact.label + ' ' + fact.value)).filter(Boolean).join(' · '))),
          row.open_value ? h('span', { className: 'stoa-num' }, money(row.open_value, pb.currency)) : null)))
            : h('p', { className: 'stoa-sub' }, 'None yet. Open one above.')),
        h(Section, { title: cap(pb.terms.deals) + ' (' + data.records.length + ')' }, h(ByStage, { records: data.records, pb, openRecord }))),
      h('div', { className: 'stoa-rpcol' },
        h(ClientBilling, { key: 'bill:' + client.name, name: client.name, billing, pb }),
        h(Section, { title: 'On their side' },
          data.people.length ? data.people.map(person => h(PersonLine, { key: person.on + person.id, person: { ...person, record_name: person.record_name || person.group }, openRecord }))
            : h('div', { className: 'stoa-sub' }, 'Nobody yet. Add them on a ' + term + '.')),
        h(Section, { title: 'Lately' }, h(Lately, { entries: data.activity || [], openRecord })))))
}

// -- the full page -------------------------------------------------------------------------------
// The board is worked by hand as well as by talking: drag a card to another column to move it.
const DRAG = 'text/stoa-deal'
const dragging = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes(DRAG)

/** How long records have sat in a column, beside how long the playbook allows before a nudge is due. */
function Pace({ column }) {
  const days = column.here.map(deal => (Date.now() - Date.parse(deal.stage_at || '')) / 864e5).filter(value => value >= 0)
  if (!days.length || !column.stale_days) return h('div', { className: 'stoa-pace' }, column.stale_days ? 'quiet after ' + column.stale_days + 'd' : ' ')
  const average = Math.round(days.reduce((sum, value) => sum + value, 0) / days.length)
  return h('div', { className: 'stoa-pace',
    title: 'Records here have been in this stage ' + average + ' days on average. A record counts as quiet after ' + column.stale_days + ' days with nothing logged (your playbook sets that).' },
  'avg ' + average + 'd here · quiet after ' + column.stale_days + 'd')
}

/** One card on the board: what it is, who it is with, what happens next, and how it stands. */
function BoardCard({ deal, pb, column, open, onOpen, onDragEnd, onEnter, onLeave }) {
  const live = !column.won || deal.in_delivery                     // its next step still counts
  const named = sameName(deal.name, deal.company) || sameName(deal.name, deal.group)      // the record is named for its company
  const who = named
    ? (deal.contact ? [deal.contact, deal.contact_title].filter(Boolean).join(' · ') : '')
    : [deal.company, deal.group && !sameName(deal.group, deal.company) ? deal.group : ''].filter(Boolean).join(' · ')
  const late = live && deal.next_due && deal.next_due < today(), now = live && deal.next_due === today()
  const lateBy = late ? Math.max(1, Math.round((Date.parse(today()) - Date.parse(deal.next_due)) / 864e5)) : 0
  const here = daysSince(deal.stage_at)
  const tone = late ? 'bad' : now ? 'warn' : ''
  const facts = [
    live && deal.next_due ? h('span', { key: 'due', className: 'stoa-cardfact', 'data-tone': tone, title: 'The next step is due ' + shortDate(deal.next_due) },
      late ? 'overdue ' + lateBy + 'd' : now ? 'due today' : 'due ' + shortDate(deal.next_due)) : null,
    column.won ? (deal.delivery_label ? h('span', { key: 'del', className: 'stoa-cardfact' }, deal.delivery_label.toLowerCase()) : null)
      : here !== null ? h('span', { key: 'here', className: 'stoa-cardfact', title: 'In this stage since ' + shortDate(String(deal.stage_at).slice(0, 10)) }, here === 0 ? 'new here' : here + 'd here') : null,
    deal.stale && !deal.due ? h('span', { key: 'quiet', className: 'stoa-cardfact', 'data-tone': 'warn', title: 'Nothing logged for longer than this stage allows' }, 'quiet') : null,
    deal.pending ? h('span', { key: 'draft', className: 'stoa-cardfact', 'data-tone': 'good' }, 'draft waiting') : null,
    column.other ? h('span', { key: 'stage', className: 'stoa-cardfact' }, deal.stage_label) : null,
    deal.people_count > 1 ? h('span', { key: 'people', className: 'stoa-cardfact', title: plural(deal.people_count, 'person', 'people') + ' on it' }, deal.people_count + ' people') : null
  ].filter(Boolean)
  return h('button', { type: 'button', className: 'stoa-card', 'data-open': open ? '1' : '0', 'data-tone': tone, draggable: !column.won,
    onDragStart: event => { onLeave(); event.dataTransfer.setData(DRAG, deal.id); event.dataTransfer.effectAllowed = 'move' }, onDragEnd,
    onMouseEnter: event => onEnter(deal.id, event.currentTarget), onMouseLeave: onLeave, onFocus: event => onEnter(deal.id, event.currentTarget), onBlur: onLeave,
    onClick: onOpen },
  h('h5', null, h('span', { className: 'stoa-clip' }, deal.name), deal.value ? h('span', { className: 'stoa-num' }, money(deal.value, pb.currency)) : null),
  h('div', { className: 'stoa-cardwho stoa-clip', 'data-none': who ? '0' : '1' }, who || (named ? 'No contact yet' : ' ')),
  live ? h('div', { className: 'stoa-cardnext', 'data-none': deal.next_step ? '0' : '1' }, deal.next_step || 'Nothing planned') : null,
  facts.length || deal.owner ? h('div', { className: 'stoa-cardmeta' }, h('span', { className: 'stoa-cardfacts' }, facts), deal.owner ? h('em', null, deal.owner) : null) : null)
}

/** What is behind a card, shown beside it while the pointer rests there: its people, its notes, what happened last.
 *  It is only to read; pressing the card opens the record. */
function CardPeek({ deal, pb, at }) {
  const query = useQuery({ queryKey: ['stoa', 'deal', at.id], queryFn: () => rest('/deal/' + encodeURIComponent(at.id)), staleTime: 1500 })
  if (!deal) return null
  const full = query.data
  const people = full ? (full.people || []) : []
  // what happened, not the bookkeeping
  const log = full ? (full.activity || []).filter(entry => !entry.undone && !['created', 'updated', 'person', 'undo', 'restored'].includes(entry.kind)).slice(0, 3) : []
  const notes = String((full ? full.notes : deal.notes) || '').trim()
  const status = deal.status === 'won' ? pb.won.label + (deal.delivery_label ? ' · ' + deal.delivery_label : '') : deal.stage_label
  const here = daysSince(deal.stage_at)
  const line = [status, deal.status === 'open' && here !== null ? (here === 0 ? 'since today' : here + 'd in this stage') : '', deal.value ? money(deal.value, pb.currency) : '', deal.owner].filter(Boolean).join('  ·  ')
  const bits = [deal.source ? 'From ' + deal.source : '', deal.created_at ? 'added ' + ago(deal.created_at) : '', deal.last_touch ? 'last touched ' + ago(deal.last_touch) : ''].filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-peek', role: 'tooltip', style: { left: at.left, width: at.wide, top: at.top, bottom: at.bottom } },
    h('div', { className: 'stoa-peekhead' }, h('b', null, deal.name), h('span', { className: 'stoa-sub' }, line)),
    deal.next_step && (deal.status === 'open' || deal.in_delivery) ? h('div', { className: 'stoa-peeksec' }, h('small', { className: 'stoa-eyebrow' }, 'Next step'),
      h('div', null, deal.next_step, deal.next_due ? h('span', { className: 'stoa-sub' }, '  ' + dueLabel(deal)) : null)) : null,
    h('div', { className: 'stoa-peeksec' }, h('small', { className: 'stoa-eyebrow' }, 'People'),
      !full ? h('div', { className: 'stoa-sub' }, deal.contact || '…')
        : people.length ? people.slice(0, 3).map(person => h('div', { key: person.id, className: 'stoa-clip' }, h('span', { className: 'stoa-name' }, person.name),
          h('span', { className: 'stoa-sub' }, '  ' + [person.title || person.role, person.email].filter(Boolean).join(' · '))))
            .concat(people.length > 3 ? [h('div', { key: 'more', className: 'stoa-sub' }, 'and ' + (people.length - 3) + ' more')] : [])
          : h('div', { className: 'stoa-sub' }, deal.email ? deal.email : 'Nobody on it yet.')),
    notes ? h('div', { className: 'stoa-peeksec' }, h('small', { className: 'stoa-eyebrow' }, 'Notes'), h('div', { className: 'stoa-peektext' }, notes.length > 260 ? notes.slice(0, 260).trimEnd() + '…' : notes)) : null,
    h('div', { className: 'stoa-peeksec' }, h('small', { className: 'stoa-eyebrow' }, 'Lately'),
      !full ? h('div', { className: 'stoa-sub' }, '…')
        : log.length ? log.map((entry, index) => h('div', { key: index, className: 'stoa-peeklog' },
          h('span', { className: 'stoa-peekkind' }, LOG_KINDS[entry.kind] || cap(entry.kind)),
          h('span', { className: 'stoa-peektext' }, entry.text.length > 150 ? entry.text.slice(0, 150).trimEnd() + '…' : entry.text),
          h('span', { className: 'stoa-sub' }, ago(entry.at))))
          : h('div', { className: 'stoa-sub' }, 'Nothing logged yet.')),
    bits ? h('div', { className: 'stoa-peekfoot stoa-sub' }, bits) : null)
}

function Board({ state, openId, setOpenId }) {
  const pb = state.playbook
  const filter = useValue($filter)
  const [over, setOver] = useState('')
  const board = useRef(null)
  // A stage picked out in the funnel: bring its column into view. Every column keeps its cards.
  useEffect(() => {
    const el = board.current
    const column = el && filter.stage ? el.querySelector('[data-spot="1"]') : null
    if (column) el.scrollTo({ left: Math.max(0, column.offsetLeft - el.offsetLeft - (el.clientWidth - column.clientWidth) / 2), behavior: 'smooth' })
  }, [filter.stage])
  const open = state.deals.filter(deal => deal.status === 'open' && matches(deal, filter))
  const won = state.deals.filter(deal => deal.status === 'won' && matches(deal, { ...filter, due: false, quiet: false }))
    .sort((a, b) => (a.stage_at < b.stage_at ? 1 : -1))
  const known = new Set(pb.stages.map(stage => stage.id))
  const strays = open.filter(deal => !known.has(deal.stage))
  const columns = pb.stages.map(stage => ({ ...stage, here: open.filter(deal => deal.stage === stage.id) }))
    .concat(strays.length ? [{ id: '~other', label: 'Other stages', other: true, here: strays }] : [])
    .concat([{ id: '~won', label: pb.won.label, won: true, here: won.slice(0, 8) }])
    .map(column => ({ ...column, value: inPlay(column.here, state) }))
  const most = Math.max(1, ...columns.filter(column => !column.won).map(column => column.value))
  const drop = (event, column) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || column.other) return
    event.preventDefault()
    const deal = state.deals.find(item => item.id === id)
    const target = column.won ? pb.won.id : column.id
    if (deal && deal.stage !== target) act(id, { verb: 'move', stage: target })
  }
  const picked = filter.stage && known.has(filter.stage) ? filter.stage : ''       // a stage from another playbook picks out nothing
  // Resting on a card brings up what is behind it (its people, its notes, what happened last), beside the card.
  const wrap = useRef(null)
  const timer = useRef(0)
  const [peek, setPeek] = useState(null)
  const hide = () => { clearTimeout(timer.current); setPeek(null) }
  const show = (id, el) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      if (!wrap.current || !el.isConnected) return
      const box = wrap.current.getBoundingClientRect(), card = el.getBoundingClientRect(), wide = 330
      const right = card.right + 10 + wide <= Math.min(box.right, window.innerWidth) + 4
      const low = card.top > window.innerHeight * 0.52                  // low on the screen: grow upwards from the card's foot
      setPeek({ id, left: Math.max(0, right ? card.right - box.left + 10 : card.left - box.left - wide - 10), wide,
        top: low ? undefined : card.top - box.top, bottom: low ? box.bottom - card.bottom : undefined })
    }, 420)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  return h('div', { className: 'stoa-boardwrap', ref: wrap }, h('div', { className: 'stoa-board', ref: board, onScroll: hide }, columns.map((column, place) =>
    h('div', { key: column.id, className: 'stoa-col', 'data-over': over === column.id ? '1' : '0', 'data-dim': picked && column.id !== picked ? '1' : '0',
      'data-spot': picked && column.id === picked ? '1' : '0',
      onDragOver: event => { if (dragging(event) && !column.other) { event.preventDefault(); if (over !== column.id) setOver(column.id) } },
      onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
      onDrop: event => drop(event, column) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, column.label),
        h('span', { className: 'stoa-num' }, column.here.length + (column.value ? ' · ' + money(column.value, pb.currency) : ''))),
      h('div', { className: 'stoa-weight', 'data-won': column.won ? '1' : '0' }, h('i', { style: { width: (column.won ? 100 : Math.round((100 * column.value) / most)) + '%', ...heatAt(place, columns.length) } })),
      column.won || column.other ? null : h(Pace, { column }),
      column.here.length ? column.here.map(deal => h(BoardCard, { key: deal.id, deal, pb, column, open: openId === deal.id,
        onOpen: () => { hide(); setOpenId(openId === deal.id ? '' : deal.id) }, onDragEnd: () => setOver(''), onEnter: show, onLeave: hide }))
        : h('div', { className: 'stoa-empty' }, over === column.id ? 'Drop it here' : '—')))),
  peek ? h(CardPeek, { key: peek.id, deal: state.deals.find(item => item.id === peek.id), pb, at: peek }) : null)
}

function Company({ company, sessionId }) {
  const pipe = useContext(InPipe)
  const [busy, setBusy] = useState(false)
  const buyer = (company.people || [])[0]
  return h('div', { className: 'stoa-company' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-name stoa-clip' }, company.company), h(Fit, { value: company.fit })),
    h('div', { className: 'stoa-sub' }, [company.size, company.location, company.uses ? 'uses ' + company.uses : ''].filter(Boolean).join(' · ')),
    company.why ? h('div', { style: { marginTop: 4 } }, company.why) : null,
    buyer ? h('div', { className: 'stoa-sub', style: { marginTop: 4 } }, buyer.name + (buyer.title ? ', ' + buyer.title : '')) : null,
    h('div', { className: 'stoa-chips', style: { alignItems: 'center' } },
      company.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline')
        : company.status === 'dismissed' ? h(Chip, null, 'dismissed')
          : h(Btn, { disabled: busy, onClick: () => { setBusy(true); track(company, sessionId, pipe).then(() => setBusy(false)) } }, 'Add to pipeline'),
      company.signal ? h(Chip, { wrap: true }, company.signal) : null))
}

// -- the market map: one picture of a market, to read, act on, grow and adjust ---------------------------------------
// The same companies are drawn three ways (by segment, by who to call first, by what they use today). A mark is a
// disc of dots: its colour is how well the company fits, its size is whatever "Size by" says. Picking marks (a click,
// shift-click or a drag) is how anything is done to them.
const MAP_BANDS = ['1–49', '50–199', '200–999', '1,000+']
const bandOf = count => (!count ? '' : count < 50 ? MAP_BANDS[0] : count < 200 ? MAP_BANDS[1] : count < 1000 ? MAP_BANDS[2] : MAP_BANDS[3])
const WEIGHT_WORDS = ['Off', 'Nice', 'Matters', 'Must']
const MAP_LENSES = [['seg', 'Segments', 'Each ring is a segment that would get a different pitch. The best fit sits in the middle.'],
  ['pri', 'Priority', 'Further right is a better fit; higher up is a fresher reason to call. The top right is who to call first.'],
  ['held', 'Held by', 'Grouped by what each company uses today: who you would be replacing.']]
const MAP_SIZES = [['people', 'People there', 'How many people work at the company'], ['named', 'People named', 'How many people you have named there'], ['same', 'Same size', 'Every company the same']]
const heatOf = score => 1 + Math.min(6, Math.floor(score / 100 * 7))
const squaresOf = score => Math.max(1, Math.ceil(score / 20))
const hashOf = text => { let n = 2166136261; for (let i = 0; i < text.length; i++) { n ^= text.charCodeAt(i); n = Math.imul(n, 16777619) } return n >>> 0 }
/** How long ago a reason to call happened, in words; nothing when it carries no date. */
const agoWords = days => (days === null || days === undefined ? '' : days === 0 ? 'today' : days === 1 ? 'yesterday' : days < 60 ? days + ' days ago' : Math.round(days / 30) + ' months ago')
/** A company's name as it is written on the map: whole when short, cut with an ellipsis when long. The panel and the table give it in full. */
const shortName = name => (String(name).length > 22 ? String(name).slice(0, 21).trimEnd() + '…' : String(name))
/** What every company Hermes adds to a map should carry, said the same way wherever the page asks for more. */
const RECORD = 'For each one record its source, its headcount, what it uses today for the job we do, and any signal with the date it happened.'
const namesOf = list => (list.length <= 2 ? list.map(c => c.company).join(' and ') : list[0].company + ', ' + list[1].company + ' and ' + (list.length - 2) + ' more')

/** Every company on a map, with what the picture needs worked out: a fit out of 100 (Hermes's judgement, or the
    requirements weighed the way the person set them), whether there is a reason to call now, and who the buyer is. */
function mapCompanies(market, weights, seenAt) {
  const own = Object.values(weights || {}).some(weight => weight !== 2)
  // "New" is a company that arrived since you last had this map open: what Hermes has just found, not the whole of a map you are seeing for the first time.
  const isFresh = c => { const at = Date.parse(c.added_at || '') || 0; return Boolean(at) && Boolean(seenAt) && at > seenAt }
  const out = []
  for (const segment of market.segments) {
    for (const c of segment.companies) {
      let score = Math.max(1, Math.min(5, Number(c.fit) || 3)) * 20, weighed = false
      const reqs = c.requirements || []
      if (own && reqs.length) {
        let total = 0, got = 0, missed = false
        for (const req of reqs) {
          const weight = weights[req.label] === undefined ? 2 : weights[req.label]
          if (!weight) continue
          total += weight
          if (req.shown) got += weight
          else if (weight === 3) missed = true
        }
        if (total) { score = Math.round(100 * got / total); if (missed) score = Math.min(score, 35); weighed = true }
      }
      const buyer = (c.people || [])[0] || null
      // Timing: how fresh the reason to call is. A dated one fades over about two months; one with no date sits just above
      // the middle; no reason at all sits at the bottom.
      const days = c.signal && c.signal_date ? Math.max(0, Math.round((Date.now() - new Date(c.signal_date + 'T12:00:00').getTime()) / 86400000)) : null
      const timing = !c.signal ? 6 + hashOf(c.id) % 14 : days === null || isNaN(days) ? 58 : Math.max(30, Math.min(100, Math.round(100 - days * 0.9)))
      out.push({ ...c, segment: c.segment || segment.name, score, weighed, buyer, reason: Boolean(c.signal), days: isNaN(days) ? null : days, timing,
        fresh: c.status === 'new' && isFresh(c), band: bandOf(c.headcount), uses: c.uses || '', rank: Math.round(score * 0.65 + timing * 0.35) + (buyer ? 2 : 0) })
    }
  }
  return out
}

/** Where every mark sits for one view at one width. The same list and width always give the same picture. */
function mapLayout(list, lens, W, segments, sizeBy) {
  const narrow = W < 620, cols = narrow ? 2 : 3, sc = narrow ? 0.74 : 1
  const radius = c => {
    if (sizeBy === 'same') return 12
    if (sizeBy === 'named') return Math.min(26, 9 + (c.people || []).length * 3.4)
    return c.headcount ? Math.max(8.5, Math.min(27, 6.5 + Math.sqrt(c.headcount) * 0.62)) : 11
  }
  const ns = list.map(c => ({ c, id: c.id, r: sc * radius(c), show: false, bridge: null }))
  const out = { W, H: 0, narrow, groups: [], links: [], pri: null, ns }
  const relax = hold => {
    ns.forEach((n, i) => { n.x = n.ax + ((i * 37) % 7 - 3) * 0.4; n.y = n.ay + ((i * 53) % 7 - 3) * 0.4 })
    for (let k = 0; k < 240; k++) {
      const pull = k < 170 ? 0.07 : 0.02
      for (const n of ns) { n.x += (n.ax - n.x) * pull; n.y += (n.ay - n.y) * pull }
      for (let i = 0; i < ns.length; i++) {
        for (let j = i + 1; j < ns.length; j++) {
          const p = ns[i], q = ns[j]
          let dx = q.x - p.x, dy = q.y - p.y
          const d = Math.hypot(dx, dy) || 0.01, min = p.cr + q.cr
          if (d < min) { const o = (min - d) / 2; dx /= d; dy /= d; p.x -= dx * o; p.y -= dy * o; q.x += dx * o; q.y += dy * o }
        }
      }
      for (const n of ns) hold(n)
    }
  }
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v))
  if (lens === 'pri') {
    // The plot keeps a margin inside its axes, so a mark at either end of a scale, and its name, stay in the picture.
    const m = { l: narrow ? 30 : 46, r: 16, t: 26, b: 40 }, H = narrow ? 500 : 580
    const x0 = m.l + 40, xw = W - m.l - m.r - 112, y0 = H - m.b - 24, yh = H - m.t - m.b - 48
    out.H = H
    out.pri = { ...m, mid: y0 - yh * 0.5, fx: x0 + xw * 0.6 }
    ns.forEach(n => {
      // Fit comes in a few steps, so companies on the same step are spread across by a hair. A reason with no date has no
      // place of its own up the page, so those are spread a little there too.
      n.ax = x0 + xw * n.c.score / 100 + ((hashOf(n.id + 'x') % 100) / 100 - 0.5) * W * 0.05
      n.ay = y0 - yh * n.c.timing / 100 + (n.c.days === null ? ((hashOf(n.id) % 100) / 100 - 0.5) * yh * 0.08 : 0)
    })
    ns.slice().sort((a, b) => b.c.rank - a.c.rank).slice(0, narrow ? 5 : 9).forEach(n => { n.show = true })
    ns.forEach(n => { n.cr = n.r + (n.show ? 11 : 3.5) })
    relax(n => {
      n.x = clamp(n.x, m.l + n.r + 2, W - m.r - n.r - 2)
      n.y = clamp(n.y, m.t + n.r + 2, H - m.b - n.r - 18)
    })
    return out
  }
  let groups
  if (lens === 'seg') {
    let real = segments.map(segment => ({ key: segment.name, name: segment.name, m: ns.filter(n => n.c.segment === segment.name) })).filter(g => g.m.length)
    // Segments that share a company sit side by side where they can, so the line between them is short and crosses nothing.
    const pairs = []
    real.forEach((g, i) => g.m.forEach(n => {
      const j = n.c.also ? real.findIndex(q => q !== g && q.key.toLowerCase() === String(n.c.also).toLowerCase()) : -1
      if (j >= 0) pairs.push([i, j])
    }))
    if (pairs.length && real.length <= 7) {
      const cell = k => [k % cols, Math.floor(k / cols)]
      const cost = order => pairs.reduce((sum, pair) => { const p = cell(order.indexOf(pair[0])), q = cell(order.indexOf(pair[1])); return sum + Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]) }, 0)
      let best = real.map((_, i) => i), least = cost(best)
      const walk = (order, rest) => {
        if (!rest.length) { const c = cost(order); if (c < least) { best = order; least = c } return }
        rest.forEach((x, k) => walk(order.concat(x), rest.filter((_, m) => m !== k)))
      }
      if (least > pairs.length) walk([], real.map((_, i) => i))
      real = best.map(i => real[i])
    }
    groups = real
    groups.push({ ghost: true, key: '+', m: [] })
  } else {
    const by = new Map()
    ns.forEach(n => { const key = n.c.uses || 'Not known yet'; if (!by.has(key)) by.set(key, []); by.get(key).push(n) })
    groups = [...by.entries()].map(([key, m]) => ({ key, name: key, m })).sort((a, b) => (a.key === 'Not known yet') - (b.key === 'Not known yet') || b.m.length - a.m.length)
  }
  const rows = Math.ceil(groups.length / cols), rowH = narrow ? 256 : 304
  out.H = rows * rowH
  groups.forEach((g, i) => { g.colW = W / cols; g.cx = g.colW * (i % cols + 0.5); g.cy = rowH * Math.floor(i / cols) + rowH / 2 - 20; g.R = Math.min(g.colW / 2 - 10, rowH / 2 - 42) })
  groups.forEach((g, gi) => {
    g.m.sort((a, b) => b.c.rank - a.c.rank)
    g.m.forEach((n, i) => {
      const d = i ? (g.R - 24 * sc) * Math.sqrt(i / g.m.length) : 0, a = i * 2.39996 + gi * 1.1
      n.g = g; n.ax = g.cx + Math.cos(a) * d; n.ay = g.cy + Math.sin(a) * d
      if (i < (narrow ? 2 : 3)) n.show = true
      const other = lens === 'seg' && n.c.also ? groups.find(q => !q.ghost && q !== g && q.key.toLowerCase() === String(n.c.also).toLowerCase()) : null
      if (other) {
        const ux = other.cx - g.cx, uy = other.cy - g.cy, l = Math.hypot(ux, uy) || 1
        n.ax = g.cx + ux / l * (g.R - n.r - 2); n.ay = g.cy + uy / l * (g.R - n.r - 2); n.bridge = other; n.show = true
      }
    })
  })
  ns.forEach(n => { n.cr = n.r + (n.show ? 11 : 3.5) })
  relax(n => { const g = n.g, dx = n.x - g.cx, dy = n.y - g.cy, d = Math.hypot(dx, dy), max = g.R - n.r - 2; if (d > max) { n.x = g.cx + dx / d * max; n.y = g.cy + dy / d * max } })
  ns.forEach(n => {
    if (!n.bridge) return
    const o = n.bridge, ux = n.x - o.cx, uy = n.y - o.cy, l = Math.hypot(ux, uy) || 1
    out.links.push({ id: n.id, x1: n.x, y1: n.y, x2: o.cx + ux / l * o.R, y2: o.cy + uy / l * o.R })
  })
  out.groups = groups
  return out
}

/** The picture itself. It only draws and reports what was picked; what a pick means is the map's business. */
function MapPicture({ market, list, lens, sizeBy, sel, shown, onPick, onLasso, onClear, onGrow, onSuggest, say: said }) {
  const say = said || MAP_SALES
  const box = useRef(null), drag = useRef(null), lassoed = useRef(false)
  const [W, setW] = useState(0)
  const [hover, setHover] = useState('')
  const [lasso, setLasso] = useState(null)
  useEffect(() => {
    const el = box.current
    if (!el) return undefined
    const read = () => setW(el.clientWidth)
    read()
    const watcher = new ResizeObserver(read)
    watcher.observe(el)
    return () => watcher.disconnect()
  }, [])
  const L = useMemo(() => (W ? mapLayout(list, lens, W, market.segments, sizeBy) : null), [list, lens, W, market.segments, sizeBy])
  const down = event => {
    if (!L || event.pointerType !== 'mouse' || event.button !== 0 || event.target.closest('.stoa-mapco, button')) return
    const b = box.current.getBoundingClientRect()
    const d = drag.current = { x: event.clientX - b.left, y: event.clientY - b.top, add: event.shiftKey, moved: false, r: null }
    const move = ev => {
      const x = ev.clientX - b.left, y = ev.clientY - b.top
      if (Math.abs(x - d.x) + Math.abs(y - d.y) > 6) d.moved = true
      if (!d.moved) return
      d.r = { l: Math.min(x, d.x), t: Math.min(y, d.y), w: Math.abs(x - d.x), h: Math.abs(y - d.y) }
      setLasso(d.r)
    }
    const up = () => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up)
      drag.current = null; setLasso(null)
      if (!d.moved || !d.r) return
      lassoed.current = true; setTimeout(() => { lassoed.current = false }, 0)
      onLasso(L.ns.filter(n => shown(n.c) && n.x >= d.r.l && n.x <= d.r.l + d.r.w && n.y >= d.r.t && n.y <= d.r.t + d.r.h).map(n => n.id), d.add)
    }
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
  }
  const brackets = s => { const q = 5; return 'M' + -s + ',' + (-s + q) + 'V' + -s + 'H' + (-s + q) + 'M' + (s - q) + ',' + -s + 'H' + s + 'V' + (-s + q) + 'M' + s + ',' + (s - q) + 'V' + s + 'H' + (s - q) + 'M' + (-s + q) + ',' + s + 'H' + -s + 'V' + (s - q) }
  const hovered = L && hover ? L.ns.find(n => n.id === hover) : null
  const pri = L && L.pri
  return h('div', { ref: box, className: 'stoa-map', 'data-narrow': L && L.narrow ? '1' : '0', style: { height: L ? L.H : 420 }, onPointerDown: down,
    onClick: event => { if (lassoed.current || event.target.closest('.stoa-mapco, button')) return; onClear() } },
    L ? h('svg', { className: 'stoa-mapsvg', width: L.W, height: L.H, viewBox: '0 0 ' + L.W + ' ' + L.H, role: 'group', 'aria-label': 'Market map: ' + market.name },
      h('defs', null, [1, 2, 3, 4, 5, 6, 7].map(k => h('pattern', { key: k, id: 'stoa-st' + k, width: 4, height: 4, patternUnits: 'userSpaceOnUse' },
        h('rect', { width: 2.2, height: 2.2, style: { fill: 'var(--stoa-m' + k + ')' } }))),
        h('pattern', { id: 'stoa-wash', width: 8, height: 8, patternUnits: 'userSpaceOnUse' }, h('rect', { width: 1.6, height: 1.6, style: { fill: 'var(--stoa-m6)' }, opacity: 0.5 }))),
      pri ? h('g', { key: 'pri' },
        h('rect', { x: pri.fx, y: pri.t, width: L.W - pri.r - pri.fx, height: pri.mid - pri.t, fill: 'url(#stoa-wash)' }),
        h('line', { className: 'stoa-mapsplit', x1: pri.fx, y1: pri.t, x2: pri.fx, y2: L.H - pri.b }),
        h('line', { className: 'stoa-mapsplit', x1: pri.l, y1: pri.mid, x2: L.W - pri.r, y2: pri.mid }),
        h('line', { className: 'stoa-mapaxis', x1: pri.l, y1: L.H - pri.b, x2: L.W - pri.r, y2: L.H - pri.b }),
        h('line', { className: 'stoa-mapaxis', x1: pri.l, y1: pri.t, x2: pri.l, y2: L.H - pri.b })) : null,
      L.groups.map(g => h('circle', { key: lens + g.key, className: 'stoa-mapring', 'data-ghost': g.ghost ? '1' : '0', cx: g.cx, cy: g.cy, r: g.R })),
      L.links.map(link => h('line', { key: link.id, className: 'stoa-maplink', x1: link.x1, y1: link.y1, x2: link.x2, y2: link.y2 })),
      L.ns.map((n, index) => {
        const c = n.c, k = heatOf(c.score), picked = sel.has(n.id)
        return h('g', { key: n.id, className: 'stoa-mapco', 'data-state': c.status === 'tracked' ? 'in' : c.fresh ? 'fresh' : 'on', 'data-sel': picked ? '1' : '0', 'data-dim': shown(c) ? '0' : '1',
          style: { transform: 'translate(' + n.x.toFixed(1) + 'px,' + n.y.toFixed(1) + 'px)' }, tabIndex: 0, role: 'button', 'aria-pressed': picked,
          'aria-label': c.company + ', fit ' + c.score + ' of 100' + (c.status === 'tracked' ? ', in the pipeline' : '') + (c.reason ? ', has ' + say.reason : ''),
          onClick: event => { event.stopPropagation(); onPick(n.id, event.shiftKey || event.metaKey || event.ctrlKey) },
          onKeyDown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onPick(n.id, event.shiftKey) } },
          onPointerEnter: event => { if (event.pointerType !== 'touch') setHover(n.id) }, onPointerLeave: () => setHover('') },
          h('g', { className: 'stoa-mappop', style: { animationDelay: Math.min(index * 12, 500) + 'ms' } },
            c.reason ? h('circle', { className: 'stoa-mapsig', r: n.r + 4 }) : null,
            c.status === 'tracked' ? h('circle', { className: 'stoa-mapdisc', r: n.r, style: { fill: 'var(--stoa-m' + k + ')' } }) : h('circle', { className: 'stoa-mapdisc', r: n.r, fill: 'url(#stoa-st' + k + ')' }),
            h('circle', { className: 'stoa-mapedge', r: n.r, style: { stroke: c.fresh ? undefined : 'var(--stoa-m' + k + ')' } }),
            h('path', { className: 'stoa-mapbrk', d: brackets(n.r + 5) })))
      }),
      L.ns.map(n => h('text', { key: 'l' + n.id, className: 'stoa-maplbl', 'data-on': (n.show || sel.has(n.id)) && shown(n.c) ? '1' : '0', 'data-sel': sel.has(n.id) ? '1' : '0',
        style: { transform: 'translate(' + n.x.toFixed(1) + 'px,' + (n.bridge && n.y > n.g.cy + n.g.R * 0.45 ? n.y - n.r - 7 : n.y + n.r + 15).toFixed(1) + 'px)' } }, shortName(n.c.company)))) : null,
    L ? h('div', { className: 'stoa-mapover' },
      h('div', { className: 'stoa-maphint' }, 'Click to open · shift-click or drag to pick several'),
      pri ? [
        h('div', { key: 'a', className: 'stoa-mapq', style: { left: pri.fx + 10, top: pri.t + 8 } }, h('strong', null, 'Call now'), h('em', null, 'Fits, and the reason is fresh')),
        h('div', { key: 'b', className: 'stoa-mapq', style: { left: pri.fx + 10, top: pri.mid + 8 } }, 'Warm up', h('em', null, 'Fits, no fresh reason')),
        h('div', { key: 'c', className: 'stoa-mapq', style: { left: pri.l + 10, top: pri.t + 8 } }, 'Watch', h('em', null, 'A reason, but a weak fit')),
        h('div', { key: 'd', className: 'stoa-mapq', style: { left: pri.l + 10, top: pri.mid + 8 } }, 'Later'),
        h('div', { key: 'e', className: 'stoa-mapq', style: { left: pri.l, bottom: 12 } }, 'Fit →'),
        h('div', { key: 'f', className: 'stoa-mapq', style: { left: 10, top: pri.t + 96, transform: 'rotate(-90deg)', transformOrigin: 'left top' } }, 'Fresher →')] : null,
      L.groups.map(g => {
        if (g.ghost) {
          return h('div', { key: 'ghost', className: 'stoa-mapghost', style: { left: g.cx, top: g.cy, width: Math.min(2 * g.R - 34, 216) } },
            h('span', { className: 'stoa-eyebrow' }, say.recruiting ? 'Next to this role' : 'Next to this market'), h('b', null, say.recruiting ? 'A neighbouring pool' : 'A neighbouring segment'),
            h('p', null, 'Hermes can suggest who else has this problem.'), h(Btn, { onClick: onSuggest }, 'Ask Hermes'))
        }
        const count = g.m.length, avg = count ? Math.round(g.m.reduce((sum, n) => sum + n.c.score, 0) / count) : 0
        return h('div', { key: lens + g.key, className: 'stoa-mapcap', style: { left: g.cx, top: g.cy + g.R + 6, width: g.colW - 8 } },
          h('b', null, g.name), h('span', null, plural(count, say.thing, say.things) + (lens === 'held' ? ' · ' + Math.round(100 * count / L.ns.length) + '%' : ' · fit ' + avg)),
          lens === 'seg' && count <= 3 ? h('span', { className: 'stoa-chip', 'data-tone': 'warn', style: { marginLeft: 6 } }, 'thin') : null,
          lens === 'seg' ? h(Btn, { kind: 'quiet', title: 'Ask Hermes to find more ' + say.things + ' for this ' + (say.recruiting ? 'pool' : 'segment'), onClick: () => onGrow(g.name) }, '+ Grow') : null)
      }),
      hovered ? h('div', { className: 'stoa-maptip', style: { left: Math.max(4, hovered.x + hovered.r + 14 + 236 > L.W ? hovered.x - hovered.r - 14 - 232 : hovered.x + hovered.r + 14), top: Math.max(4, Math.min(L.H - 120, hovered.y - 24)) } },
        h('b', null, hovered.c.company), h(Fit, { value: squaresOf(hovered.c.score) }), h('span', { className: 'stoa-sub' }, '  fit ' + hovered.c.score + (hovered.c.size ? ' · ' + hovered.c.size : '') + (say.people && hovered.c.uses ? ' at ' + hovered.c.uses : '')),
        hovered.c.signal ? h('div', null, h(Chip, { wrap: true }, hovered.c.signal), agoWords(hovered.c.days) ? h('span', { className: 'stoa-sub' }, '  ' + agoWords(hovered.c.days)) : null) : null, hovered.c.why ? h('div', { className: 'stoa-sub' }, hovered.c.why) : null) : null,
      lasso ? h('div', { className: 'stoa-maplasso', style: { left: lasso.l, top: lasso.t, width: lasso.w, height: lasso.h } }) : null) : null)
}

/** A button that opens a short list beneath it. It closes on a choice, a click anywhere else, or Escape. */
function Menu({ label, title, kind, up, right, children }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const away = event => { if (box.current && !box.current.contains(event.target)) setOpen(false) }
    const key = event => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('pointerdown', away); document.removeEventListener('keydown', key) }
  }, [open])
  return h('div', { ref: box, className: 'stoa-menu', 'data-up': up ? '1' : '0', 'data-right': right ? '1' : '0' },
    h('button', { type: 'button', className: kind === 'title' ? 'stoa-menutitle' : kind === 'foot' ? 'stoa-footbtn' : 'stoa-btn', title, 'aria-haspopup': 'menu', 'aria-expanded': open, onClick: () => setOpen(!open) },
      label, '\u00a0', h('span', { className: 'stoa-menucaret', 'aria-hidden': true }, '▾')),
    open ? h('div', { className: 'stoa-menulist', role: 'menu', onClick: event => { if (event.target.closest('[role="menuitem"]')) setOpen(false) } }, children) : null)
}
const MenuItem = ({ title, sub, on, danger, onClick }) => h('button', { type: 'button', role: 'menuitem', className: 'stoa-menuitem', 'data-on': on ? '1' : '0', 'data-danger': danger ? '1' : '0', onClick },
  h('span', null, title), sub ? h('small', null, sub) : null)

/** Starting a map is Hermes's work: it has to go and find the companies. This takes what you sell and who to, and hands it over. */
function NewMap({ first, onClose, shape }) {
  const role = shape === 'roles'
  const mapPipe = useContext(InPipe) || $pipe.get()        // on the page itself nothing names a pipeline but the one it is showing
  const [goes, setGoes] = useState(() => (role ? $filter.get().group || '' : ''))       // the search it is for: the one the pipeline is narrowed to, to begin with
  const [findMore, setFindMore] = useState(true)        // look on the web for candidates, or keep to the people named in the request
  const [text, setText] = useState('')
  // What is sent is what the form shows: a search remembered from the board's filter that is done, closed or another
  // pipeline's is not one of the choices, so the map is for a new search.
  const searches = ((useDesk().data || {}).groups || []).filter(row => row.status === 'open' || row.status === 'hold')
  const search = searches.some(row => row.name === goes) ? goes : ''
  const go = () => {
    const said = text.trim()
    if (!said) return
    const sent = toChat(role ? roleMapAsk(said, search, findMore) : 'Map a new market for me and keep it on the desk as a map of its own: ' + said + ' Name the market, split it into segments that would each get a different pitch, set the requirements, '
      + 'then research companies. For each one record the evidence and its source, its headcount, what it uses today for the job we do, any signal with the date it happened, and the likely buyer if you come across one. Tell me when it is on the desk.', null, 'maps')
    if (!sent) return
    host.notify({ kind: 'info', message: 'Hermes is mapping it. The map appears here when it is saved, which takes a few minutes.' })
    setText('')
    if (onClose) onClose()
  }
  return h('div', { className: 'stoa-box stoa-mapform' },
    h('label', { className: 'stoa-name', htmlFor: 'stoa-newmap' }, role ? (first ? 'Map the candidates for a role' : 'A new candidate map') : first ? 'Map your first market' : 'A new map'),
    h('p', { className: 'stoa-sub', style: { margin: '4px 0 8px' } }, role ? 'Describe the role and who would fit it. Hermes splits the candidates into pools, finds people who could fill the role from public sources, each with their own fit and the evidence for it, and saves them as a map of its own. It takes a few minutes, and you can keep working while it does.' : 'Say what you sell and who you are after. Hermes names the market, splits it into segments, researches companies and saves it as a map of its own. It takes a few minutes, and you can keep working while it does.'),
    h('textarea', { id: 'stoa-newmap', className: 'stoa-field stoa-maptext', rows: 3, autoFocus: !first, value: text, placeholder: role ? 'A Head of Events for a 40-person trade association in Columbus. Has run a 3,000-person annual show end to end. $130–150k, hybrid.' : 'We sell a fraud prevention tool to fintechs and marketplaces in the US with a small risk team.', onChange: event => setText(event.target.value) }),
    role ? h(MapFor, { pipe: mapPipe, value: search, onChange: setGoes, web: findMore, onWeb: setFindMore }) : null,
    h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'primary', disabled: !text.trim(), onClick: go }, 'Have Hermes map it'), onClose ? h(Btn, { kind: 'quiet', onClick: onClose }, 'Cancel') : null))
}

/** A map by the numbers, in a window over the page: what is on it and how complete it is, where it came from, what it
    has cost so far, and the limits set on it. Hermes's cost is Hermes's own estimate, counted for the turns that worked on this map. */
function MapFacts({ market, onClose, onFill }) {
  const pipe = useContext(InPipe)
  const box = useRef(null)
  const query = useQuery({ queryKey: ['stoa', 'facts', market.id, pipe || ''], queryFn: () => rest('/market/' + encodeURIComponent(market.id) + '/facts', pipe ? inPipe(pipe) : undefined), staleTime: 2000 })
  const [typed, setTyped] = useState(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => { const el = box.current; if (el && !el.open && el.showModal) el.showModal() }, [])
  const facts = query.data
  useEffect(() => { if (facts && typed === null) setTyped(Object.fromEntries(Object.entries(facts.limits).map(([name, value]) => [name, value ? String(value) : '']))) }, [facts])
  const close = () => { const el = box.current; if (el && el.open) el.close(); else onClose() }
  const save = event => {
    event.preventDefault()
    setSaving(true)
    rest('/market/' + encodeURIComponent(market.id) + '/limits', { method: 'POST', ...inPipe(pipe), body: Object.fromEntries(Object.entries(typed || {}).map(([name, value]) => [name, Number(String(value).replace(/[^0-9.]/g, '')) || 0])) })
      .then(result => { refresh(); host.notify({ kind: result.ok ? 'success' : 'info', message: result.ok ? 'Limits saved for this map.' : result.error || 'Stoa could not save that.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save the limits')).then(() => setSaving(false))
  }
  const big = n => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'K' : String(n))
  const usd = n => '$' + Number(n || 0).toFixed(2)
  const day = stamp => { const d = new Date(String(stamp || '').slice(0, 10) + 'T12:00:00'); return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) }
  let body
  if (!facts) body = h('p', { className: 'stoa-sub' }, query.error ? 'The numbers could not be read: ' + why(query.error) : 'Counting…')
  else {
    const on = facts.on_it, cost = facts.cost, total = on.companies || 1
    const persons = (facts.rows || rowsOf(market)) === 'people'      // a candidate map: no headcount and no buyer, a title and an employer
    const cover = persons ? [['Checked against the role', on.checked, on.companies], ['With a source link', on.sourced, on.companies], ['Title known', on.sized, on.companies],
      ['Employer known', on.uses, on.companies], ['Reasons to move with a date', on.dated, on.signals]]
      : [['Checked against the requirements', on.checked, on.companies], ['With a source link', on.sourced, on.companies], ['Headcount known', on.sized, on.companies],
      ['What it uses today known', on.uses, on.companies], ['A buyer named', on.buyers, on.companies], ['Signals with a date', on.dated, on.signals]]
    const askable = facts.gaps.filter(gap => gap.ask)
    const limitRow = (name, label, hint, now) => h('label', { key: name, className: 'stoa-factlimit' },
      h('span', null, h('b', null, label), h('small', null, hint)),
      h('input', { className: 'stoa-field', inputMode: 'decimal', value: (typed || {})[name] || '', placeholder: 'No limit', 'aria-label': label, onChange: event => setTyped({ ...(typed || {}), [name]: event.target.value }) }),
      h('small', { className: 'stoa-factnow' }, now))
    body = [
      h('div', { key: 'grid', className: 'stoa-factgrid' },
        h('section', null, h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'What is on it')),
          h('div', { className: 'stoa-factfigs' }, [[persons ? 'Candidates' : 'Companies', on.companies], [persons ? 'Pools' : 'Segments', on.segments], ['In the pipeline', on.in_pipeline], ['Dismissed', on.dismissed]].map(pair =>
            h('div', { key: pair[0] }, h('b', null, pair[1]), h('small', null, pair[0])))),
          cover.map(row => h('div', { key: row[0], className: 'stoa-factrow' }, h('span', null, row[0]), h('span', { className: 'stoa-num' }, row[1] + ' of ' + row[2]),
            h('u', { className: 'stoa-mapmeter' }, h('i', { style: { width: (row[2] ? Math.round(100 * row[1] / row[2]) : 0) + '%' } })))),
          askable.length ? h('div', { className: 'stoa-btns', style: { marginTop: 12 } }, h(Btn, { title: 'Sends Hermes a line asking it to fill in ' + askable.map(gap => gap.what).join(', ') + ' where a source says so', onClick: () => { if (onFill(askable)) close() } }, 'Ask Hermes to fill in what is missing')) : null),
        h('section', null, h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Where it came from')),
          facts.sources.links ? [h('p', { key: 'p', style: { margin: '0 0 8px' } }, plural(facts.sources.links, 'source link', 'source links') + ' from ' + plural(facts.sources.sites, 'site', 'sites') + '.'),
            facts.sources.top.map(row => h('div', { key: row.site, className: 'stoa-factrow', 'data-plain': '1' }, h('span', { style: { overflowWrap: 'anywhere' } }, row.site), h('span', { className: 'stoa-num' }, plural(row.links, 'link', 'links'))))]
            : h('p', { className: 'stoa-sub' }, 'No ' + (persons ? 'candidate' : 'company') + ' on this map has a source link yet.'),
          h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px', marginTop: 18 } }, h('span', { className: 'stoa-eyebrow' }, 'What it has cost')),
          h('div', { className: 'stoa-factcost' },
            h('div', null, h('b', null, cost.runs ? (cost.usd === null ? 'No price known' : usd(cost.usd)) : usd(0)), h('small', null, 'Hermes\'s work, as Hermes estimates it')),
            h('p', { className: 'stoa-sub' }, cost.runs
              ? plural(cost.runs, 'turn', 'turns') + ' · ' + plural(cost.calls, 'model call', 'model calls') + ' · ' + cost.minutes + ' min of model time · ' + big(cost.tokens.in + cost.tokens.cache_read + cost.tokens.cache_write) + ' tokens in, ' + big(cost.tokens.out) + ' out'
                + (cost.usd !== null ? ' · ' + usd(cost.usd_month) + ' this month' : '') + (cost.unpriced && cost.usd !== null ? ' · ' + plural(cost.unpriced, 'call', 'calls') + ' had no price and is not in the figure' : '')
              : 'Nothing counted yet. Counting starts the next time Hermes works on this map.')),
          h('div', { className: 'stoa-factrow', 'data-plain': '1' }, h('span', null, 'People Data Labs lookups'), h('span', { className: 'stoa-num' }, cost.lookups_month + ' this month · ' + cost.lookups + ' in all')),
          h('div', { className: 'stoa-factrow', 'data-plain': '1' }, h('span', null, 'Exa searches through Stoa'), h('span', { className: 'stoa-num' }, cost.searches_month + ' this month · ' + cost.searches + ' in all')),
          h('p', { className: 'stoa-sub', style: { marginTop: 8 } }, 'The dollar figure is Hermes\'s own estimate from the provider\'s price list, for the turns that worked on this map' + (cost.since ? ' since ' + day(cost.since) : '') + '. It is not an invoice. Web research Hermes does with its own tools is inside that figure; only searches made through Stoa are counted on the Exa line.'))),
      h('form', { key: 'limits', className: 'stoa-factlimits', onSubmit: save },
        h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Limits for this map'), h('span', { className: 'stoa-sub' }, 'leave a box empty for no limit')),
        limitRow('companies', (persons ? 'Candidates' : 'Companies') + ' on the map', 'Hermes stops adding when the map holds this many', on.companies + ' now'),
        limitRow('budget', 'Hermes\'s work a month, in dollars', 'By Hermes\'s estimate; past it, Stoa refuses new ' + (persons ? 'candidates' : 'companies') + ' and searches for this map', usd(cost.usd_month) + ' this month'),
        limitRow('lookups', 'People Data Labs lookups a month', 'For this map alone, inside the cap for the whole desk', cost.lookups_month + ' this month'),
        limitRow('searches', 'Exa searches a month', 'Searches made through Stoa for this map', cost.searches_month + ' this month'),
        h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'At a limit, Stoa refuses the next step and tells Hermes to stop and tell you. It cannot stop a turn that is already running.'),
        h('div', { className: 'stoa-btns' }, h(Btn, { type: 'submit', kind: 'primary', disabled: saving || typed === null }, saving ? 'Saving…' : 'Save limits')))]
  }
  return h('dialog', { ref: box, className: 'stoa-dialog', 'aria-label': 'Map facts: ' + market.name, onClose, onClick: event => { if (event.target === box.current) close() } },
    h('div', { className: 'stoa-dialogin' },
      h('div', { className: 'stoa-dialoghead' },
        h('div', { className: 'stoa-grow', style: { minWidth: 0 } }, h('div', { className: 'stoa-eyebrow' }, 'Map facts'), h('div', { className: 'stoa-maph' }, market.name),
          facts ? h('div', { className: 'stoa-sub' }, ['made ' + day(facts.created), 'last changed ' + day(facts.updated)].join(' · ')) : null),
        h(Btn, { kind: 'quiet', 'aria-label': 'Close', onClick: close }, 'Close')),
      body))
}

/** The market map on the page: the picture, what is picked beside it, the brief you can weigh, and every company in a table. */
function MarketMap({ market, markets, onMarket, shape, openRecord }) {
  const pipe = useContext(InPipe)
  const [lens, setLens] = useState('seg')
  const [mode, setMode] = useState('')      // a form open under the map's name: new | rename | delete
  const [draft, setDraft] = useState('')
  const rail = useRef(null)
  const who = shape === 'roles' ? { one: 'person', many: 'people', Cap: 'Person', find: ['Find a person', 'Find people'], ask: 'Ask Hermes who to approach', likely: 'the best person to approach' }
    : { one: 'buyer', many: 'buyers', Cap: 'Buyer', find: ['Find a buyer', 'Find buyers'], ask: 'Ask Hermes who the buyer is', likely: 'the most likely buyer' }
  const [picked, setPicked] = useState([])
  const [quick, setQuick] = useState('all')
  const [find, setFind] = useState('')
  const [facet, setFacet] = useState(null)
  const [sort, setSort] = useState({ key: 'rank', dir: -1 })
  const [busy, setBusy] = useState('')
  const [saving, setSaving] = useState('')
  const [wanted, setWanted] = useState('')
  const [gone, setGone] = useState(false)
  const kept = key => { try { return storage ? storage.get(key + ':' + market.id, null) : null } catch { return null } }
  const keep = (key, value) => { try { if (storage) storage.set(key + ':' + market.id, value) } catch { /* no storage: it lasts as long as the page */ } }
  const [weights, setWeightsNow] = useState(() => kept('mapweights') || {})
  const [sizeBy, setSizeNow] = useState(() => kept('mapsize') || (shape === 'roles' ? 'named' : 'people'))
  useEffect(() => { setWeightsNow(kept('mapweights') || {}); setSizeNow(kept('mapsize') || (shape === 'roles' ? 'named' : 'people')); setPicked([]); setFacet(null); setFind(''); setQuick('all'); setMode('') }, [market.id])
  const setWeights = next => { setWeightsNow(next); keep('mapweights', next) }
  const setSize = next => { setSizeNow(next); keep('mapsize', next) }
  // When this map was last looked at, from the visit before this one. Companies that arrived after it are marked new for the whole of this visit.
  const [seenAt, setSeenAt] = useState(() => Number(kept('mapseen')) || Date.now())
  useEffect(() => { const id = market.id; setSeenAt(Number(kept('mapseen')) || Date.now()); return () => { try { if (storage) storage.set('mapseen:' + id, Date.now()) } catch { /* no storage */ } } }, [market.id])
  const list = useMemo(() => mapCompanies(market, weights, seenAt), [market, weights, seenAt])
  const mapSay = mapWords(shape, market)
  const byId = useMemo(() => new Map(list.map(c => [c.id, c])), [list])
  const sel = useMemo(() => new Set(picked.filter(id => byId.has(id))), [picked, byId])
  const chosen = [...sel].map(id => byId.get(id))
  const weighed = Object.values(weights).some(weight => weight !== 2)
  const words = find.trim().toLowerCase()
  const shown = c => {
    if (words && !(c.company + ' ' + (c.location || '') + ' ' + c.segment + ' ' + c.uses).toLowerCase().includes(words)) return false
    if (quick === 'open' && c.status === 'tracked') return false
    if (quick === 'reason' && !c.reason) return false
    if (quick === 'nobuyer' && c.buyer) return false
    if (quick === 'fresh' && !c.fresh) return false
    if (facet) {
      if (facet.kind === 'uses' && c.uses.toLowerCase() !== facet.value.toLowerCase()) return false
      if (facet.kind === 'size' && c.band !== facet.value) return false
      if (facet.kind === 'req' && !(c.requirements || []).some(req => req.label === facet.value && req.shown)) return false
    }
    return true
  }
  const pick = (id, many) => setPicked(now => (many ? (now.includes(id) ? now.filter(x => x !== id) : now.concat(id)) : now.length === 1 && now[0] === id ? [] : [id]))
  const send = (verb, companies, extra, said) => {
    if (!companies.length || busy) return Promise.resolve(null)
    setBusy(verb)
    return rest('/targets', { method: 'POST', body: { verb, ids: companies.map(c => c.id), ...(extra || {}) }, ...inPipe(pipe) })
      .then(result => { refresh(); const line = said(result); if (line) host.notify({ kind: result.ok === false ? 'info' : 'success', message: line }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not do that'); return null })
      .then(result => { setBusy(''); return result })
  }
  const ask = line => toChat(line, null)                 // the desk's conversation: writing, such as a first note
  const research = line => toChat(line, null, 'maps')   // the map's own: finding, checking and filling in companies
  const here = ' my market map “' + market.name + '”'
  // How many more companies to ask Hermes for: five, or what the limits a person set on this map leave room for. At a
  // limit it says so and asks for nothing, so no research is paid for that the map would then refuse.
  const roomFor = () => {
    const room = market.room
    if (room && (room.stop || room.companies === 0)) {
      host.notify({ kind: 'info', message: room.stop ? 'This map has used the budget set for Hermes\'s work this month. Raise it under Map facts to carry on.' : 'This map is at its limit of ' + plural(room.limit, mapSay.thing, mapSay.things) + '. Raise it under Map facts, or dismiss a ' + mapSay.thing + ' to make room.' })
      return ''
    }
    return ['one', 'two', 'three', 'four', 'five'][Math.min(5, room && room.companies !== null ? room.companies : 5) - 1]
  }
  const act = {
    add: companies => send('track', companies.filter(c => c.status !== 'tracked'), null, result => {
      const added = result.added || [], filled = added.filter(a => a.existing).length, opened = added.length - filled
      if (!added.length) return 'Nothing was added: ' + ((result.skipped || [])[0] || 'they are already in the pipeline.')
      return [opened ? plural(opened, 'record', 'records') + ' opened in the pipeline, each with its evidence.' : '',
        filled ? plural(filled, mapSay.thing, mapSay.things) + ' already had a record, so the map filled ' + (filled === 1 ? 'it' : 'them') + ' in instead.' : ''].filter(Boolean).join(' ')
    }),
    dismiss: companies => send('dismiss', companies.filter(c => c.status !== 'tracked'), null, result => { setPicked([]); return (result.dismissed || []).length ? plural(result.dismissed.length, mapSay.thing, mapSay.things) + ' taken off the map. “Dismissed” above the map puts one back.' : 'Records already in the pipeline stay on the map.' }),
    // One person at each company, with a title like the buyers already named on this map: one lookup apiece, never more.
    // With a key but no buyer named yet there is no title to look for, so Hermes works out who the buyer is instead.
    buyers: companies => {
      const need = companies.filter(c => !c.buyer)
      if (!need.length) return null
      if (!(market.buyer_titles || []).length || !market.lookups) {
        return research('Work out who ' + who.likely + ' is at each of these companies on' + here + ': ' + need.map(c => c.company).join(', ') + '. Name one person at each, with their title and why them, and save each on the map. '
          + (market.lookups ? 'People Data Labs is set up: use it with the job titles that fit what we sell, and ask for one person per company.' : 'Use your web search; no paid lookup is set up.'))
      }
      return send('buyers', need, null, result => [(result.found || []).length ? 'Named a buyer at ' + plural(result.found.length, 'company', 'companies') + '.' : '',
        (result.none || []).length ? 'Nobody with a title like that at ' + result.none.join(', ') + '.' : '', result.problem || ''].filter(Boolean).join(' ') || 'Every company picked already has a buyer.')
    },
    move: (company, segment) => send('move', [company], { segment }, result => (result.ok ? result.company + ' now sits in ' + result.segment + '.' : result.error)),
    draft: companies => ask(mapSay.draftAsk ? mapSay.draftAsk(here, companies.map(c => c.company)) : companies.length === 1
      ? 'Draft a first outreach note to ' + (companies[0].buyer ? companies[0].buyer.name + ' at ' : who.likely + ' at ') + companies[0].company + ', leading with the evidence on' + here + '. Show me the draft. Do not send anything.'
      : 'Draft a first outreach note for each of these companies from' + here + ', each leading with its own evidence: ' + companies.map(c => c.company).join(', ') + '. Show me the drafts. Do not send anything.'),
    more: companies => roomFor() && research(mapSay.moreAsk ? mapSay.moreAsk(here, roomFor(), namesOf(companies)) : 'Find up to ' + roomFor() + ' more ' + (roomFor() === 'one' ? 'company' : 'companies') + ' like ' + namesOf(companies) + ' for' + here + '. Keep to the map\'s requirements and check each one against them with the evidence. ' + RECORD + ' Add them to the map, then tell me who you added and why.'),
    grow: segment => roomFor() && research(mapSay.growAsk ? mapSay.growAsk(here, roomFor(), segment) : 'Find up to ' + roomFor() + ' more ' + (roomFor() === 'one' ? 'company' : 'companies') + ' for the “' + segment + '” segment of' + here + '. Keep to the map\'s requirements and check each one against them with the evidence. ' + RECORD + ' Add them to the map, then tell me who you added and why.'),
    fill: gaps => research('On' + here + ', fill in what is missing. For each company that lacks it: ' + gaps.map(gap => gap.ask).join('; ') + '. Keep what is already there, give a source for anything you add, leave out what no source says, and tell me what you could not find.'),
    suggest: () => research(mapSay.suggestAsk ? mapSay.suggestAsk(here) : 'Suggest two neighbouring segments for' + here + ': who they are, and why they would be worth mapping. Do not add anything yet; I will say which.'),
    require: text => research(mapSay.requireAsk ? mapSay.requireAsk(here, text) : 'Add this requirement to' + here + ' and check every company on it against it, with the evidence: ' + text)
  }
  const save = share => { setSaving(share ? 'share' : 'own'); savePage(market, share, pipe).then(() => setSaving('')) }
  const facetBtn = (kind, value, label, big, pct, text, small) => {
    const on = Boolean(facet) && facet.kind === kind && facet.value === value
    return h('button', { type: 'button', key: kind + value, className: 'stoa-mapfacet', 'aria-pressed': on, title: on ? 'Show every company again' : 'Light up these companies on the map',
      onClick: () => setFacet(on ? null : { kind, value, label }) },
    h('b', null, big), h('span', null, text, h('u', { className: 'stoa-mapmeter' }, h('i', { style: { width: Math.max(2, pct) + '%' } }))), h('small', null, small))
  }
  const count = key => list.filter(c => (key === 'open' ? c.status !== 'tracked' : key === 'reason' ? c.reason : key === 'nobuyer' ? !c.buyer : key === 'fresh' ? c.fresh : true)).length
  const showing = list.filter(shown)
  const filtered = quick !== 'all' || facet || words
  const calls = list.filter(c => c.status !== 'tracked').sort((a, b) => b.rank - a.rank).slice(0, 5)
  const actions = companies => {
    const open = companies.filter(c => c.status !== 'tracked'), nobody = companies.filter(c => !c.buyer)
    return [
      open.length ? h(Btn, { key: 'add', kind: 'primary', disabled: Boolean(busy), onClick: () => act.add(companies) }, busy === 'track' ? 'Adding…' : companies.length === 1 ? 'Add to pipeline' : 'Add ' + open.length + ' to pipeline') : null,
      nobody.length && !mapSay.people ? (market.lookups && (market.buyer_titles || []).length
        ? h(Btn, { key: 'buy', disabled: Boolean(busy), title: 'One People Data Labs lookup per company, for one person ranked manager or above with a title like the buyers already on this map (' + market.buyer_titles.slice(0, 3).join(', ') + '). Read the title before you use the name. One person is one credit, so this spends at most ' + plural(nobody.length, 'credit', 'credits') + ', inside your monthly cap.',
          onClick: () => act.buyers(companies) }, busy === 'buyers' ? 'Looking…' : (companies.length === 1 ? who.find[0] : who.find[1]) + ' · ' + plural(nobody.length, 'lookup', 'lookups'))
        : h(Btn, { key: 'buy', title: market.lookups ? 'No buyer is named on this map yet, so there is no job title to look for. Hermes works out who the buyer is at each company.' : 'Hermes works out who the buyer is with its web search. A People Data Labs key (Setup) lets it look people up.',
          onClick: () => act.buyers(companies) }, who.ask)) : null,
      h(Btn, { key: 'draft', onClick: () => act.draft(companies) }, 'Draft outreach'),
      h(Btn, { key: 'more', onClick: () => act.more(companies) }, companies.length === 1 ? 'More like this' : 'More like these'),
      open.length ? h(Btn, { key: 'no', kind: 'quiet', disabled: Boolean(busy), onClick: () => act.dismiss(companies) }, 'Dismiss') : null]
  }
  const status = c => (c.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline') : c.fresh ? h(Chip, { tone: 'warn' }, 'new') : h(Chip, null, 'on the map'))
  const label = text => h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px' } }, h('span', { className: 'stoa-eyebrow' }, text))

  let side
  if (chosen.length === 1 && mapSay.people) {
    const c = chosen[0]
    side = h(MapPerson, { c, market, busy: Boolean(busy), actions: actions([c]), open: c.status === 'tracked' && c.deal ? () => openRecord(c.deal) : null, move: segment => act.move(c, segment), close: () => setPicked([]) })
  } else if (chosen.length === 1) {
    const c = chosen[0]
    side = h('div', null,
      h('div', { className: 'stoa-eyebrow' }, c.segment + (c.also ? ' · also fits ' + c.also : '')),
      h('div', { className: 'stoa-maph' }, c.company),
      h('div', { className: 'stoa-sub' }, [c.domain, c.location].filter(Boolean).join(' · ')),
      h('div', { className: 'stoa-chips' }, status(c), c.signal ? h(Chip, { tone: 'warn', wrap: true }, c.signal) : null,
        c.signal ? h('span', { className: 'stoa-sub' }, agoWords(c.days) || 'no date on it') : null),
      h('div', { className: 'stoa-mapscore' }, h('span', { className: 'stoa-eyebrow' }, c.weighed ? 'Fit, by your weights' : 'Fit, Hermes\'s judgement'), h('b', null, c.score), h(Fit, { value: squaresOf(c.score) })),
      c.why ? h('p', { style: { margin: '10px 0' } }, c.why) : null,
      h('div', { className: 'stoa-btns' }, c.status === 'tracked' && c.deal ? h(Btn, { kind: 'primary', onClick: () => openRecord(c.deal) }, 'Open the record') : null, actions([c])),
      (c.requirements || []).length ? [label('Against your brief'), c.requirements.map(req => h('div', { key: req.label, className: 'stoa-mapreq', 'data-on': req.shown ? '1' : '0' },
        h('span', null, req.shown ? '[x]' : '[ ]'), h('span', null, h('b', null, req.label), h('span', { className: 'stoa-sub', style: { display: 'block' } }, req.shown ? (req.evidence || 'Shown; the source is on the map') : 'No evidence found'))))] : null,
      label('Facts'),
      h('div', { className: 'stoa-facts', style: { marginTop: 0, paddingTop: 0, borderTop: 0 } },
        [['Size', c.size || 'Not known'], [shape === 'roles' ? 'Works with' : 'Uses today', c.uses || 'Not known'], [who.Cap, c.buyer ? [c.buyer.name, c.buyer.title].filter(Boolean).join(', ') : 'Nobody named yet'],
          ['Source', c.source || 'None recorded']].map(pair => h('div', { key: pair[0], style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, pair[0]),
          pair[0] === 'Source' && /^https?:\/\//.test(pair[1]) ? h('a', { className: 'stoa-alink', href: pair[1], target: '_blank', rel: 'noreferrer', style: { display: 'block', overflowWrap: 'anywhere' } }, bareLink(pair[1])) : h('span', { style: { overflowWrap: 'anywhere' } }, pair[1])))),
      c.status !== 'tracked' && market.segments.length > 1 ? [label('Adjust'), h('select', { key: 'move', className: 'stoa-select', value: c.segment, disabled: Boolean(busy), 'aria-label': 'Move to another segment',
        onChange: event => act.move(c, event.target.value) }, market.segments.map(segment => h('option', { key: segment.name, value: segment.name }, (segment.name === c.segment ? 'In ' : 'Move to ') + segment.name)))] : null,
      h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'quiet', onClick: () => setPicked([]) }, 'Close')))
  } else if (chosen.length > 1) {
    const avg = Math.round(chosen.reduce((sum, c) => sum + c.score, 0) / chosen.length)
    side = h('div', null,
      h('div', { className: 'stoa-eyebrow' }, 'Picked'), h('div', { className: 'stoa-maph' }, chosen.length + ' companies'),
      h('div', { className: 'stoa-sub' }, 'Average fit ' + avg + ' · ' + chosen.filter(c => c.reason).length + ' with a reason to call · ' + chosen.filter(c => !c.buyer).length + (shape === 'roles' ? ' with nobody named' : ' without a buyer')),
      h('div', { className: 'stoa-btns' }, actions(chosen)),
      label('Who'),
      chosen.slice().sort((a, b) => b.rank - a.rank).map(c => h('div', { key: c.id, className: 'stoa-setrow', style: { alignItems: 'center', padding: '6px 0' } },
        h('button', { type: 'button', className: 'stoa-maprow stoa-grow', onClick: () => setPicked([c.id]) }, h('b', null, c.company), ' ', h(Fit, { value: squaresOf(c.score) }), h('span', { className: 'stoa-sub', style: { display: 'block' } }, c.segment)),
        status(c), h(Btn, { kind: 'quiet', 'aria-label': 'Take ' + c.company + ' out of the picked ones', onClick: () => setPicked(now => now.filter(id => id !== c.id)) }, '×'))),
      h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'quiet', onClick: () => setPicked([]) }, 'Clear')))
  } else {
    side = h('div', null,
      market.reading ? [label('Reading the map'), String(market.reading).split('\n').filter(Boolean).map((text, index) => h('p', { key: index, style: { margin: '0 0 8px' } }, text))] : null,
      (market.implications || []).length ? [label('What this means'), market.implications.map((text, index) => h('p', { key: 'i' + index, className: 'stoa-tip' }, text))] : null,
      h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px' } }, h('span', { className: 'stoa-eyebrow' }, mapSay.people ? 'Who to approach first' : 'Next calls'),
        calls.length > 1 ? h(Btn, { disabled: Boolean(busy), onClick: () => act.add(calls) }, 'Add all ' + calls.length) : null),
      calls.length ? calls.map((c, index) => h('div', { key: c.id, className: 'stoa-setrow' },
        h('span', { className: 'stoa-stagenum', style: { width: 18, margin: 0 } }, '0' + (index + 1)),
        h('button', { type: 'button', className: 'stoa-maprow stoa-grow', onClick: () => setPicked([c.id]) }, h('b', null, c.company), ' ', h(Fit, { value: squaresOf(c.score) }),
          h('span', { className: 'stoa-sub', style: { display: 'block' } }, [c.segment, c.signal, agoWords(c.days)].filter(Boolean).join(' · ')), c.why ? h('span', { style: { display: 'block' } }, c.why) : null),
        h(Btn, { disabled: Boolean(busy), onClick: () => act.add([c]) }, 'Add')))
        : h('p', { className: 'stoa-sub' }, 'Every company on the map is in your pipeline. Grow a segment to find more.'))
  }

  const sortBy = key => setSort(now => ({ key, dir: now.key === key ? -now.dir : ['company', 'segment', 'uses'].includes(key) ? 1 : -1 }))
  const value = c => (sort.key === 'buyer' ? (c.buyer ? c.buyer.name : '~') : sort.key === 'headcount' ? (c.headcount || 0) : sort.key === 'reason' ? c.timing : c[sort.key])
  const rows = list.slice().sort((a, b) => { const x = value(a), y = value(b); return (typeof x === 'number' ? x - y : String(x || '').localeCompare(String(y || ''))) * sort.dir || b.rank - a.rank })
  const dismissed = market.dismissed || []
  const shortDay = stamp => { const d = new Date(String(stamp || '') + 'T12:00:00'); return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
  // Picked from the table, the company's panel must be where the eye is. Beside a wide page it rides along as you
  // scroll; on a narrow one it sits under the map, so the page goes to it.
  const pickHere = (id, many) => {
    pick(id, many)
    requestAnimationFrame(() => { const panel = rail.current; if (panel && getComputedStyle(panel).position !== 'sticky') panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) })
  }
  const manage = (verb, body, said) => {
    setBusy(verb)
    return rest('/market/' + encodeURIComponent(market.id) + '/' + verb, { method: 'POST', body, ...inPipe(pipe) })
      .then(result => { if (result.ok) { setMode(''); host.notify({ kind: 'success', message: said(result) }) } else host.notify({ kind: 'info', message: result.error || 'Stoa could not do that.' }); refresh(); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not do that'); return null })
      .then(result => { setBusy(''); return result })
  }
  const clamp = (text, lines) => h('span', { className: 'stoa-clamp', style: lines ? { WebkitLineClamp: lines } : undefined, title: text }, text)
  const cols = [['company', mapSay.thingCap || 'Company', '18%'], ['segment', mapSay.people ? 'Pool' : 'Segment', null], ['score', 'Fit', '104px'], ['reason', 'Why now', null], ['headcount', 'People', '74px'], ['uses', mapSay.people ? 'Works at' : shape === 'roles' ? 'Works with' : 'Uses today', null], ['buyer', mapSay.people ? 'Title' : who.Cap, null], ['status', 'Pipeline', '128px']].filter(col => !(mapSay.people && col[0] === 'headcount'))
  return h('div', { className: 'stoa-mapwrap' },
    h('div', { className: 'stoa-maptop' },
      h('div', { className: 'stoa-grow', style: { minWidth: 0 } },
        h(Menu, { kind: 'title', label: market.name, title: 'Your market maps: switch, start a new one, rename or delete' },
          (markets || []).map(item => h(MenuItem, { key: item.id, on: item.id === market.id, title: item.name,
            sub: (shape === 'roles' ? item.counts.mapped + ' on the map' : plural(item.counts.mapped, 'company', 'companies')) + (shortDay(item.updated) ? ' · updated ' + shortDay(item.updated) : ''), onClick: () => onMarket(item.id) })),
          h('div', { className: 'stoa-menurule' }),
          h(MenuItem, { title: '+  New map…', sub: shape === 'roles' ? 'Describe the role, and the candidates for it are found and mapped' : 'Tell Hermes what you sell and who to, and it maps the market', onClick: () => setMode('new') }),
          h(MenuItem, { title: 'Rename this map…', onClick: () => { setDraft(market.name); setMode('rename') } }),
          rowsOf(market) ? h(MenuItem, { title: rowsOf(market) === 'people' ? 'Dots are candidates · change to companies' : 'Dots are companies · change to candidates',
            sub: rowsOf(market) === 'people' ? 'For a map of where the people work, with people named under each company' : 'For a map where every row is one person. Nothing on the map is changed, only how it is read',
            onClick: () => manage('rows', { rows: rowsOf(market) === 'people' ? 'companies' : 'people' }, result => (result.rows === 'people' ? 'This map’s dots are now candidates.' : 'This map’s dots are now companies.')) }) : null,
          h(MenuItem, { title: 'Delete this map…', danger: true, onClick: () => setMode('delete') })),
        h('div', { className: 'stoa-sub' }, [plural(list.length, mapSay.thing, mapSay.things), market.numbers.in_pipeline + mapSay.inPipe, list.filter(c => c.reason).length + ' with ' + mapSay.reason,
          mapSay.people ? plural(new Set(list.map(c => (c.uses || '').toLowerCase()).filter(Boolean)).size, 'employer', 'employers') : plural(list.filter(c => c.buyer).length, who.one + ' named', who.many + ' named'), shortDay(market.updated) ? 'updated ' + shortDay(market.updated) : ''].filter(Boolean).join(' · '))),
      h('div', { className: 'stoa-btns', style: { margin: 0 } },
        h(ModelPick, { lane: 'maps' }),
        h(Btn, { title: 'This map by the numbers: what is on it, where it came from, what it has cost, and its limits', onClick: () => setMode('facts') }, 'Map facts'),
        h(Btn, { disabled: Boolean(saving), title: 'The whole map as one web page, with buyers\' names and what is in your pipeline', onClick: () => save(false) }, saving === 'own' ? 'Saving…' : 'Download page'),
        h(Btn, { disabled: Boolean(saving), title: 'The same page without buyers\' names or your pipeline: the one to host or send to a client', onClick: () => save(true) }, saving === 'share' ? 'Saving…' : 'Download a copy to share'))),
    h(MapSearch, { market }),
    mode === 'facts' ? h(MapFacts, { market, onClose: () => setMode(''), onFill: act.fill }) : null,
    mode === 'new' ? h(NewMap, { shape, onClose: () => setMode('') }) : null,
    mode === 'rename' ? h('form', { className: 'stoa-box stoa-mapform', onSubmit: event => { event.preventDefault(); if (draft.trim() && draft.trim() !== market.name) manage('rename', { name: draft.trim() }, result => 'The map is now called ' + result.name + '.') } },
      h('label', { className: 'stoa-name', htmlFor: 'stoa-mapname' }, 'What this map is called'),
      h('div', { className: 'stoa-pathrow', style: { marginTop: 8 } },
        h('input', { id: 'stoa-mapname', className: 'stoa-field', style: { fontFamily: 'inherit' }, autoFocus: true, maxLength: 120, value: draft, onChange: event => setDraft(event.target.value) }),
        h(Btn, { type: 'submit', kind: 'primary', disabled: Boolean(busy) || !draft.trim() || draft.trim() === market.name }, busy === 'rename' ? 'Saving…' : 'Save'),
        h(Btn, { kind: 'quiet', onClick: () => setMode('') }, 'Cancel'))) : null,
    mode === 'delete' ? h('div', { className: 'stoa-box stoa-mapform', role: 'alertdialog', 'aria-label': 'Delete this map' },
      h('div', { className: 'stoa-name' }, 'Delete “' + market.name + '”?'),
      h('p', { className: 'stoa-sub', style: { margin: '4px 0 0' } }, 'The map and the ' + plural(list.length + dismissed.length, mapSay.thing, mapSay.things) + ' on it go for good: this cannot be taken back from the page. '
        + (market.numbers.in_pipeline ? 'The ' + plural(market.numbers.in_pipeline, 'record', 'records') + ' already in your pipeline ' + (market.numbers.in_pipeline === 1 ? 'stays' : 'stay') + ' there.' : 'Nothing in your pipeline is touched.')),
      h('div', { className: 'stoa-btns' }, h(Btn, { kind: 'danger', disabled: Boolean(busy), onClick: () => manage('remove', {}, result => result.name + ' was deleted, with ' + plural(result.companies, mapSay.thing, mapSay.things) + '.') }, busy === 'remove' ? 'Deleting…' : 'Delete the map'),
        h(Btn, { autoFocus: true, onClick: () => setMode('') }, 'Keep it'))) : null,
    gone && dismissed.length ? h('div', { className: 'stoa-box', style: { marginBottom: 12 } }, h('div', { className: 'stoa-sub', style: { marginBottom: 6 } }, 'Taken off this map. ' + mapSay.gone),
      h('div', { className: 'stoa-chips' }, dismissed.map(row => h(Btn, { key: row.id, disabled: Boolean(busy), title: 'Put ' + row.company + ' back on the map',
        onClick: () => send('restore', [row], null, result => ((result.restored || []).length ? row.company + ' is back on the map.' : 'Nothing was put back.')) }, row.company + '  ↩')))) : null,
    h('div', { className: 'stoa-mapbody' },
      h('div', { className: 'stoa-mapa' },
        h('div', { className: 'stoa-mapbar' },
          h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How the map is drawn' }, MAP_LENSES.map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 28 },
            'data-on': lens === item[0] ? '1' : '0', 'aria-pressed': lens === item[0], onClick: () => setLens(item[0]) }, (mapSay.lens && mapSay.lens[item[0]]) || item[1]))),
          h('div', { className: 'stoa-mapquick' },
            [['all', 'All'], ['open', 'Not in the pipeline'], ['reason', shape === 'roles' ? 'Reason to move' : 'Reason to call'], ['nobuyer', shape === 'roles' ? 'Nobody named' : 'No buyer named'], ['fresh', 'New']].filter(item => (item[0] !== 'fresh' || count('fresh')) && !(item[0] === 'nobuyer' && mapSay.people)).map(item =>
              h(Btn, { key: item[0], kind: 'quiet', 'aria-pressed': quick === item[0], 'data-on': quick === item[0] ? '1' : '0', onClick: () => setQuick(item[0]) }, item[1] + ' ' + count(item[0]))),
            facet ? h(Btn, { title: 'Show every company again', onClick: () => setFacet(null) }, facet.label + ' ×') : null,
            filtered && showing.length ? h(Btn, { onClick: () => setPicked(showing.map(c => c.id)) }, 'Pick these ' + showing.length) : null,
            dismissed.length ? h(Btn, { kind: 'quiet', 'aria-expanded': gone, onClick: () => setGone(!gone) }, dismissed.length + ' dismissed') : null)),
        h('div', { className: 'stoa-maphow' }, h('p', { className: 'stoa-sub' }, (mapSay.how && mapSay.how[lens]) || MAP_LENSES.find(item => item[0] === lens)[2]),
          h('input', { className: 'stoa-field stoa-mapfind', type: 'search', value: find, placeholder: mapSay.find || 'Find a company', 'aria-label': mapSay.find || 'Find a company', onChange: event => setFind(event.target.value) })),
        h(MapPicture, { say: mapSay, market, list, lens, sizeBy, sel, shown, onPick: pick, onClear: () => setPicked(now => (now.length ? [] : now)), onGrow: act.grow, onSuggest: act.suggest,
          onLasso: (ids, add) => setPicked(now => (add ? [...new Set(now.concat(ids))] : ids)) }),
        h('div', { className: 'stoa-maplegend' },
          h('div', null,
            h('span', null, 'Fit ', h('i', { className: 'stoa-mapheat' }), ' cold to hot'),
            h('span', null, h('i', { className: 'stoa-mapkey', 'data-kind': 'on' }), 'on the map'), h('span', null, h('i', { className: 'stoa-mapkey', 'data-kind': 'in' }), 'in the pipeline'),
            h('span', null, h('i', { className: 'stoa-mapkey', 'data-kind': 'fresh' }), 'new since you last looked'), h('span', null, h('i', { className: 'stoa-mapkey', 'data-kind': 'sig' }), 'a reason to call')),
          mapSay.people ? null : h('div', null, h('span', null, 'Size by'), h('span', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'What a mark\'s size shows' }, MAP_SIZES.map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab',
            style: { height: 22, padding: '0 8px', fontSize: 10.5 }, title: item[2], 'data-on': sizeBy === item[0] ? '1' : '0', 'aria-pressed': sizeBy === item[0], onClick: () => setSize(item[0]) }, item[1])))))),
      h('aside', { ref: rail, className: 'stoa-box stoa-mapside', 'aria-live': 'polite' }, side),
      h('div', { className: 'stoa-mapb' },
        h('div', { className: 'stoa-mapcols' },
          h('div', null, label('What matters'),
            h('p', { className: 'stoa-sub stoa-mapnote' }, weighed ? 'Fit is worked out from your weights, for every company Hermes checked against the brief.' : 'Fit is Hermes\'s judgement. Change how much a requirement counts and the map re-scores itself.'),
            (market.rates || []).map(rate => { const weight = weights[rate.label] === undefined ? 2 : weights[rate.label]
              return h('div', { key: rate.label, className: 'stoa-mapweight' },
                h('div', { className: 'stoa-mapwlabel' }, rate.label),
                h('div', { className: 'stoa-mapwrow' },
                  h('span', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How much “' + rate.label + '” counts' }, WEIGHT_WORDS.map((word, index) =>
                    h('button', { type: 'button', key: word, className: 'stoa-tab', style: { height: 22, padding: '0 7px', fontSize: 10 }, 'data-on': weight === index ? '1' : '0', 'aria-pressed': weight === index,
                      onClick: () => setWeights({ ...weights, [rate.label]: index }) }, word))),
                  facetBtn('req', rate.label, 'Shows: ' + rate.label, rate.rate + '%', rate.rate, '', rate.of ? rate.shown + ' of ' + rate.of + ' show it' : 'not checked yet'))) }),
            weighed ? h('div', { className: 'stoa-btns', style: { marginTop: 10 } }, h(Btn, { onClick: () => setWeights({}) }, 'Back to Hermes\'s judgement')) : null,
            h('form', { className: 'stoa-pathrow', style: { marginTop: 12 }, onSubmit: event => { event.preventDefault(); if (wanted.trim() && act.require(wanted.trim())) setWanted('') } },
              h('input', { className: 'stoa-field', style: { fontFamily: 'inherit' }, value: wanted, placeholder: 'A new requirement, in your words', 'aria-label': 'A new requirement', onChange: event => setWanted(event.target.value) }),
              h(Btn, { type: 'submit', disabled: !wanted.trim(), title: 'Hermes adds the requirement and checks every company on the map against it' }, 'Ask Hermes'))),
          h('div', null, label('Who holds this market'),
            (market.holds || []).length ? [h('p', { key: 'p', className: 'stoa-sub stoa-mapnote' }, 'What the ' + market.holds_of + ' companies we could place use today, and how many use each. Press one to light it up on the map.'),
              market.holds.map(hold => facetBtn('uses', hold.name, 'Uses: ' + hold.name, hold.share + '%', hold.share, hold.name, String(hold.count)))] : h('p', { className: 'stoa-sub stoa-mapnote' }, 'Nothing recorded yet about what these companies use today.')),
          h('div', null, label('How big they are'),
            MAP_BANDS.some(name => list.some(c => c.band === name)) ? [h('p', { key: 'p', className: 'stoa-sub stoa-mapnote' }, 'Share of companies by the people who work there, how many that is, and their average fit. Press one to light it up on the map.'),
              MAP_BANDS.map(name => { const group = list.filter(c => c.band === name); if (!group.length) return null
                return facetBtn('size', name, name + ' people', Math.round(100 * group.length / list.length) + '%', Math.round(100 * group.length / list.length), name + ' people', group.length + ' · fit ' + Math.round(group.reduce((sum, c) => sum + c.score, 0) / group.length)) })]
              : h('p', { className: 'stoa-sub stoa-mapnote' }, 'No headcounts recorded yet. Ask Hermes to add each company\'s size.'))),
        h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px', marginTop: 22 } }, h('span', { className: 'stoa-eyebrow' }, 'The ' + (mapSay.things || 'companies')), h('span', { className: 'stoa-sub' }, showing.length + ' of ' + list.length + ' showing')),
        h('div', { className: 'stoa-maptablewrap' }, h('table', { className: 'stoa-maptable' },
          h('colgroup', null, h('col', { style: { width: 36 } }), cols.map(col => h('col', { key: col[0], style: col[2] ? { width: col[2] } : undefined }))),
          h('thead', null, h('tr', null, h('th', null), cols.map(col =>
            h('th', { key: col[0], 'data-num': col[0] === 'headcount' ? '1' : '0' }, h('button', { type: 'button', 'aria-pressed': sort.key === col[0], onClick: () => sortBy(col[0]) }, col[1] + (sort.key === col[0] ? (sort.dir < 0 ? ' ↓' : ' ↑') : '')))))),
          h('tbody', null, rows.map(c => h('tr', { key: c.id, 'data-sel': sel.has(c.id) ? '1' : '0', 'data-dim': shown(c) ? '0' : '1', onClick: event => { if (!event.target.closest('button, input, a')) pickHere(c.id, event.shiftKey || event.metaKey || event.ctrlKey) } },
            h('td', null, h('input', { type: 'checkbox', checked: sel.has(c.id), 'aria-label': 'Pick ' + c.company, onChange: () => pickHere(c.id, true) })),
            h('td', null, h('b', null, clamp(c.company, 2)), c.location ? h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' }, title: c.location }, c.location) : null),
            h('td', null, clamp(c.segment, 2)),
            h('td', { style: { whiteSpace: 'nowrap' } }, h(Fit, { value: squaresOf(c.score) }), ' ', h('span', { className: 'stoa-num' }, c.score)),
            h('td', null, c.signal ? [h('span', { key: 's' }, clamp(c.signal, 2)), agoWords(c.days) ? h('span', { key: 'a', className: 'stoa-sub', style: { display: 'block' } }, agoWords(c.days)) : null] : h('span', { className: 'stoa-sub' }, 'None yet')),
            mapSay.people ? null : h('td', { className: 'stoa-num', 'data-num': '1' }, c.headcount ? c.headcount.toLocaleString() : ''),
            h('td', null, c.uses ? clamp(c.uses, 2) : null),
            h('td', null, mapSay.people ? (c.size ? clamp(c.size, 2) : null) : c.buyer ? [h('span', { key: 'n' }, clamp(c.buyer.name, 2)), c.buyer.title ? h('span', { key: 't', className: 'stoa-sub stoa-clamp', style: { WebkitLineClamp: 1 }, title: c.buyer.title }, c.buyer.title) : null] : h('span', { className: 'stoa-sub' }, 'Not named')),
            h('td', null, c.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline') : h(Btn, { disabled: Boolean(busy), title: 'Add ' + c.company + ' to the pipeline', onClick: () => act.add([c]) }, c.fresh ? 'Add · new' : 'Add'))))))),
        chosen.length > 1 ? h('div', { className: 'stoa-mapabar' }, h('b', null, chosen.length + ' picked'), actions(chosen), h(Btn, { kind: 'quiet', onClick: () => setPicked([]) }, 'Clear')) : null)))
}

/** Save a map as one web page: a file to keep, host or send. The copy to share leaves out buyers' names and the pipeline. */
function savePage(market, share, pipe) {
  return rest('/market/' + encodeURIComponent(market.id) + '/page?share=' + (share ? 1 : 0), pipe ? inPipe(pipe) : undefined)
    .then(file => {
      download(file.filename, file.html, 'text/html')
      host.notify({ kind: 'success', message: share ? 'Saved a copy to share: no buyers\' names, and nothing about your pipeline.' : 'Saved the map as one web page, buyers and pipeline included.' })
    })
    .catch(error => host.notifyError(error, 'Stoa could not save the page'))
}

function Market({ market, sessionId, compact }) {
  const pipe = useContext(InPipe)
  const numbers = market.numbers
  const [saving, setSaving] = useState('')
  const save = share => { setSaving(share ? 'share' : 'own'); savePage(market, share, pipe).then(() => setSaving('')) }
  return h('div', null,
    h('div', { className: 'stoa-name', style: { fontSize: compact ? 14 : 16 } }, market.name),
    h('div', { className: 'stoa-sub' }, [plural(numbers.companies, 'company', 'companies'), numbers.in_pipeline + ' in the pipeline',
      numbers.with_signal ? numbers.with_signal + ' with a signal' : '', numbers.buyers_named ? plural(numbers.buyers_named, 'buyer named', 'buyers named') : '']
      .filter(Boolean).join(' · ')),
    compact ? null : h('div', { className: 'stoa-btns', style: { margin: '8px 0 4px' } },
      h(Btn, { disabled: Boolean(saving), title: 'The whole map as one web page, with buyers\' names and what is in your pipeline', onClick: () => save(false) }, saving === 'own' ? 'Saving…' : 'Download page'),
      h(Btn, { disabled: Boolean(saving), title: 'The same page without buyers\' names or your pipeline: the one to host or send to a client', onClick: () => save(true) }, saving === 'share' ? 'Saving…' : 'Download a copy to share')),
    market.reading && !compact ? h('p', { className: 'stoa-reading' }, market.reading) : null,
    (market.first_calls || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'First calls'),
      market.first_calls.slice(0, compact ? 3 : 7).map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default', padding: '6px 0' } },
        h(Fit, { value: call.fit }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, call.company),
          h('span', { className: 'stoa-sub' }, '  ' + [call.segment, call.buyer].filter(Boolean).join(' · ')),
          h('span', { style: { display: 'block' } }, call.why)),
        call.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline') : h(Btn, { onClick: () => track(call, sessionId, pipe) }, 'Add')))) : null,
    h('div', { className: 'stoa-h2' }, 'The map'),
    h('div', { className: 'stoa-cols' }, market.segments.map(segment => h('div', { key: segment.name, className: 'stoa-box' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
        h('span', { className: 'stoa-eyebrow' }, segment.name), h('span', { className: 'stoa-num' }, segment.count + ' · fit ' + segment.avg_fit)),
      segment.description && !compact ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, segment.description) : null,
      segment.companies.slice(0, compact ? 4 : 30).map(company => h(Company, { key: company.id, company, sessionId }))))),
    compact ? null : h('div', { className: 'stoa-cols', style: { marginTop: 6 } },
      (market.rates || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'How rare each requirement is'),
        market.rates.map(rate => h('div', { key: rate.label, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, rate.label), h('u', null, h('i', { style: { width: rate.rate + '%' } })),
          h('span', { className: 'stoa-num' }, rate.rate + '%')))) : null,
      (market.holds || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'Who holds this market today'),
        market.holds.map(hold => h('div', { key: hold.name, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, hold.name), h('u', null, h('i', { style: { width: hold.share + '%' } })),
          h('span', { className: 'stoa-num' }, hold.share + '%')))) : null))
}

// -- reports ----------------------------------------------------------------------------------------
// Every figure on this tab is counted by the plugin from the desk's own records (/reports): nothing here asks a
// model for a number. Hermes comes into it only on a press: "Ask for a report" and "Change it in words" (it saves
// one from your words, as a draft you keep or discard) and "Ask Hermes about it".
const $rep = atom({ period: '90', compare: true, list: false, open: '', making: false })
const setRep = patch => {
  const next = { ...$rep.get(), ...patch }
  $rep.set(next)
  if (storage && ('period' in patch || 'compare' in patch)) storage.set('reports', { period: next.period, compare: next.compare })
}
const repQuery = rep => '?period=' + encodeURIComponent(rep.period) + '&compare=' + (rep.compare ? 1 : 0)
// A cell that a spreadsheet would run as a formula is handed over as text.
const csvCell = cell => {
  let text = cell === null || cell === undefined ? '' : String(cell)
  if (/^[=+\-@]/.test(text) && isNaN(Number(text))) text = "'" + text
  return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text
}
const csvText = (header, rows) => [header].concat(rows).map(row => row.map(csvCell).join(',')).join('\n') + '\n'
const fileName = text => String(text || 'report').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || 'report'
const reached = row => (row.parts || []).filter(part => part.k === 'won' || part.k === 'late').reduce((sum, part) => sum + part.count, 0)

async function repAct(id, body, done) {
  try {
    const out = await rest('/reports/' + encodeURIComponent(id), { method: 'POST', body })
    if (out && out.ok === false) host.notify({ kind: 'warning', message: out.error || 'That could not be done.' })
    else if (done) done(out)
    refresh()
    return out
  } catch (error) {
    host.notifyError(error, 'Stoa could not change the report')
    return null
  }
}

/** A report's rows as dotted bars. With few enough records each one is a square of its own; `big` is the opened report. */
function RepBars({ result, pb, big, sel, onPick, rows }) {
  const list = rows || result.rows || []
  const most = Math.max(1, ...list.map(row => Math.max(row.n || 0, row.mark || 0)))
  const pitch = big ? 14 : 10
  const squares = Boolean(result.squares) && result.unit === 'records' && Math.max(0, ...list.map(row => row.count || 0)) <= (big ? 36 : 20)
  const late = (result.legend || []).find(item => item.k === 'late')
  const ended = (result.legend || []).some(item => item.k === 'lost') ? (result.legend || []).find(item => item.k === 'won') : null
  const right = row => {
    if (result.unit === 'days') return h('b', null, row.n === null || row.n === undefined ? '–' : Math.round(row.n) + 'd')
    if (result.unit === 'money') return h('b', null, money(row.n, pb.currency) || '0')
    // A bar coloured by how far its records got says what share got somewhere; one coloured by how they ended, its win rate.
    const count = k => ((row.parts || []).find(part => part.k === k) || { count: 0 }).count
    const closed = ended ? count('won') + count('lost') : 0
    const share = late && row.count ? Math.round((100 * reached(row)) / row.count) : closed ? Math.round((100 * count('won')) / closed) : null
    return [h('b', { key: 'n' }, String(row.count)),
      result.staged && row.value ? ' · ' + money(row.value, pb.currency) : '',
      share !== null ? h('span', { key: 's', title: late ? 'The share that ' + late.label : 'Of the ' + closed + ' that closed, the share that were ' + ended.label }, ' · ' + share + '%') : null]
  }
  const was = row => {
    if (!big || !row.before || !result.before) return null
    const parts = (row.before.parts || []).filter(part => part.count).map(part => part.count + ' ' + ((result.legend || []).find(item => item.k === part.k) || { label: part.k }).label)
    return h('small', { className: 'stoa-repwas' }, result.before.label + ': ' + (row.before.count
      ? (result.unit === 'money' ? money(row.before.n, pb.currency) : plural(row.before.count, pb.terms.deal, pb.terms.deals)) + (parts.length ? ' · ' + parts.join(' · ') : '') : 'none'))
  }
  const about = row => {
    if (!big || result.unit !== 'days') return null
    const bits = [plural(row.count, pb.terms.deal, pb.terms.deals) + ' here now', 'quiet after ' + row.mark + 'd']
    if (row.quiet) bits.push(row.quiet + ' gone quiet')
    if (row.left) bits.push('left after ' + Math.round(row.left.days) + 'd on average (' + row.left.count + ')')
    return h('small', { className: 'stoa-repwas' }, bits.join(' · '))
  }
  return h('div', { className: 'stoa-reprows', 'data-big': big ? '1' : '0' }, list.map(row => {
    const parts = row.parts && row.parts.length ? row.parts.filter(part => part.n > 0) : [{ k: '', n: row.n || 0, count: row.count || 0 }]
    const picked = Boolean(sel) && sel === row.key
    const can = Boolean(onPick) && Boolean(row.records) && row.records.length > 0
    const track = squares
      ? h('div', { className: 'stoa-repsqs' }, parts.map(part => h('i', { key: part.k, className: 'stoa-repsq', 'data-k': part.k || undefined, style: { width: part.count * pitch } })))
      : h('div', { className: 'stoa-reptrack' },
        parts.filter(part => part.n > 0).map(part => h('i', { key: part.k, className: 'stoa-repfill', 'data-k': part.k || undefined, style: { width: (100 * part.n) / most + '%' } })),
        h('i', { className: 'stoa-represt' }),
        row.mark ? h('u', { className: 'stoa-repmark', style: { left: Math.min(100, (100 * row.mark) / most) + '%' }, title: 'Quiet after ' + row.mark + ' days' }) : null)
    return h('div', { key: row.key, className: 'stoa-reprow', 'data-on': picked ? '1' : '0', 'data-tone': row.tone || '', 'data-heat': row.heat === undefined ? undefined : Math.min(6, row.heat) },
      can ? h('button', { type: 'button', className: 'stoa-replabel', 'aria-pressed': picked, title: 'See the ' + pb.terms.deals + ' behind ' + row.label, onClick: () => onPick(picked ? '' : row.key) }, row.label)
        : h('span', { className: 'stoa-replabel', title: row.label }, row.label),
      track, h('span', { className: 'stoa-repnum' }, right(row)), was(row), about(row))
  }))
}

function RepLegend({ result }) {
  const items = (result.legend || []).map(item => h('span', { key: item.k }, h('i', { 'data-k': item.k }), item.label))
  if (result.unit === 'days') items.push(h('span', { key: 'mark' }, h('u', null), 'the stage\'s quiet mark'))
  return items.length ? h('div', { className: 'stoa-replegend' }, items) : null
}

/** Win rate: the figure, how it moved, and one square for each record that closed, month by month. */
function RepRate({ result }) {
  const figure = result.figure
  const dots = (count, k, title) => Array.from({ length: Math.min(count, 48) }, (_, index) => h('i', { key: k + index, 'data-k': k, title }))
  return h('div', null,
    h('div', { className: 'stoa-repfig' }, h('b', { className: 'stoa-repbig' }, figure.text),
      h('span', null, h('small', null, figure.label), figure.change ? h('small', { 'data-tone': figure.up ? 'good' : 'bad' }, figure.change) : null)),
    (result.months || []).length ? h('div', { className: 'stoa-repmonths' }, result.months.map(month => h('div', { key: month.key },
      h('div', { className: 'stoa-repdots' }, dots(month.won, 'won', result.rows[0].label), dots(month.lost, 'lost', result.rows[1].label)),
      h('small', { className: 'stoa-eyebrow' }, month.label)))) : null)
}

function RepTiles({ figures }) {
  return h('div', { className: 'stoa-reptiles' }, figures.map(figure => h('div', { key: figure.label, 'data-tone': figure.tone || '' }, h('b', null, figure.text), h('small', null, figure.label))))
}

function RepBody({ result, pb, big, sel, onPick }) {
  if (result.draw === 'rate') return h(RepRate, { result })
  if (result.draw === 'figures') {
    const strip = (result.strip || []).filter(part => part.n > 0), total = strip.reduce((sum, part) => sum + part.n, 0)
    return h('div', null, h(RepTiles, { figures: result.figures || [] }),
      total ? h('div', { className: 'stoa-repstrip', role: 'img', 'aria-label': strip.map(part => part.n + ' ' + part.label).join(', ') },
        strip.map(part => h('i', { key: part.k, className: 'stoa-repsq', 'data-k': part.k, title: part.n + ' ' + part.label, style: { width: Math.min(part.n, 60) * 14 } }))) : null)
  }
  return h('div', null, h(RepBars, { result, pb, big, sel, onPick }),
    (result.figures || []).length ? h('div', { style: { marginTop: 14 } }, h(RepTiles, { figures: result.figures })) : null)
}

const RepSays = ({ says }) => (says && (says[0] || says[1]) ? h('p', { className: 'stoa-repsays' }, h('b', null, says[0]), says[1] ? (says[0] ? ' ' : '') + says[1] : '') : null)

function ReportCard({ card, pb, open }) {
  if (!card.ok) {
    return h('section', { className: 'stoa-repcard' }, h('div', { className: 'stoa-rephead' }, h('h3', { className: 'stoa-eyebrow' }, card.title)),
      h('p', { className: 'stoa-sub' }, 'This one could not be worked out: ' + card.error))
  }
  const wide = card.draw === 'figures' || card.id === 'stages' || (card.legend || []).length > 0
  return h('section', { className: 'stoa-repcard', 'data-wide': wide ? '1' : '0' },
    h('div', { className: 'stoa-rephead' }, h('h3', { className: 'stoa-eyebrow' }, card.title),
      h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'Open it: the ' + pb.terms.deals + ' behind each figure, and how it is made', onClick: () => open(card.id) }, 'Open ›')),
    card.empty ? h('p', { className: 'stoa-sub', style: { margin: 0 } }, card.empty) : h(RepBody, { result: card, pb }),
    card.empty ? null : h(RepLegend, { result: card }),
    h(RepSays, { says: card.says }))
}

/** The few choices a report is made from. A person sets them here with no model involved. */
function RepBuilder({ choices, start, pb, busy, onSave, onCancel, saveLabel }) {
  const spec = (start && start.spec) || { count: 'records', by: 'source', when: 'entered', split: 'none', only: {} }
  const [form, setForm] = useState(() => ({ title: (start && start.title) || '', count: spec.count, by: spec.by, when: spec.when, split: spec.split,
    status: (spec.only || {}).status || '', owner: (spec.only || {}).owner || '', min: (spec.only || {}).min_value ? String((spec.only || {}).min_value) : '' }))
  const set = (key, value) => setForm(now => ({ ...now, [key]: value }))
  const pick = (key, options, label, wide) => h(Field, { label, wide }, h('select', { className: 'stoa-select', value: form[key], onChange: event => set(key, event.target.value) },
    options.map(option => h('option', { key: option[0], value: option[0] }, option[1]))))
  const lostOnly = form.by === 'reason', openOnly = form.when === 'open'
  const save = () => onSave({ title: form.title.trim(), count: form.count, by: form.by, when: form.when, split: openOnly && form.split === 'outcome' ? 'none' : form.split,
    only: { ...(spec.only || {}), status: lostOnly ? 'lost' : form.status, owner: form.owner, min_value: form.min } })
  return h('div', null,
    h('div', { className: 'stoa-repform' },
      h(Field, { label: 'Title', wide: true, value: form.title, placeholder: 'Win rate by owner', maxLength: 80, onChange: event => set('title', event.target.value) }),
      pick('count', choices.count, 'Counts'), pick('by', choices.by, 'Grouped by'), pick('when', choices.when, 'Which ' + pb.terms.deals),
      pick('split', openOnly ? choices.split.filter(option => option[0] !== 'outcome') : choices.split, 'Coloured by'),
      lostOnly ? null : pick('status', openOnly ? choices.status.filter(option => option[0] === '' || option[0] === 'open') : choices.status, 'Only'),
      (choices.owners || []).length > 1 ? pick('owner', [['', 'Anyone']].concat(choices.owners.map(name => [name, name])), 'Only owner') : null,
      h(Field, { label: 'Only worth at least', value: form.min, placeholder: '50k', inputMode: 'decimal', onChange: event => set('min', event.target.value) })),
    h('div', { className: 'stoa-actions', style: { marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' } },
      h(Btn, { kind: 'go', disabled: busy || !form.title.trim(), onClick: save }, saveLabel || 'Save'),
      onCancel ? h(Btn, { kind: 'quiet', onClick: onCancel }, 'Cancel') : null))
}

/** One record's missing source, typed in the report itself. Saved on Enter or on leaving the box; the record then moves to its own bar. */
function SourceBox({ record }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async () => {
    const text = value.trim()
    if (!text || busy) return
    setBusy(true)
    await act(record.id, { verb: 'update', source: text })
    setBusy(false)
  }
  return h('input', { className: 'stoa-field', style: { marginTop: 0, minWidth: 130 }, list: 'stoa-rep-sources', 'aria-label': 'Where ' + record.name + ' came from', placeholder: 'Referral, an event…',
    value, disabled: busy, onChange: event => setValue(event.target.value), onBlur: save, onKeyDown: event => { if (event.key === 'Enter') { event.preventDefault(); save() } } })
}

function RecordsBehind({ result, row, pb, openRecord, sources }) {
  const records = row.records || []
  const days = result.unit === 'days'
  // The bar of records with no source is the one place to give them one, without opening each.
  const fill = Boolean(result.spec) && result.spec.by === 'source' && row.key === 'not recorded'
  const save = () => download('stoa-' + fileName(result.title + ' ' + row.label) + '.csv', csvText(
    ['Name', 'Company', cap(pb.terms.group), 'Stage', 'Value', 'Entered', 'Closed', 'Owner', 'Reason'],
    records.map(r => [r.name, r.company, r.group, r.stage, r.value, r.entered, r.closed, r.owner, r.reason])), 'text/csv')
  return h(Section, { title: 'The ' + plural(records.length, pb.terms.deal, pb.terms.deals) + ' behind ' + row.label,
    aside: h(Btn, { kind: 'quiet', onClick: save }, 'Download these') },
  fill ? h('p', { className: 'stoa-sub', style: { margin: '0 0 8px' } }, 'Type where each came from and press Enter. It is saved on the ' + pb.terms.deal + ', which then moves to its own bar; the change can be undone.') : null,
  fill ? h('datalist', { id: 'stoa-rep-sources' }, (sources || []).map(name => h('option', { key: name, value: name }))) : null,
  h('div', { className: 'stoa-repscroll' }, h('table', { className: 'stoa-reptable' },
    h('thead', null, h('tr', null, h('th', null, cap(pb.terms.deal)), h('th', null, 'Stage'), h('th', { 'data-num': '1' }, 'Value'),
      h('th', null, days ? 'In the stage' : 'Entered'), h('th', null, fill ? 'Source' : 'Owner'))),
    h('tbody', null, records.map(r => h('tr', { key: r.id },
      h('td', null, h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(r.id) }, r.name), h('small', null, r.company || r.group)),
      h('td', null, h('i', { 'data-k': r.k || '' }), r.stage, r.reason ? h('small', null, r.reason) : null),
      h('td', { 'data-num': '1' }, money(r.value, pb.currency) || '–'),
      h('td', { 'data-num': days ? '1' : '0' }, days ? r.days + 'd' : shortDate(r.entered)),
      h('td', null, fill ? h(SourceBox, { record: r }) : r.owner || '–')))))))
}

function ReportOpen({ id, state, choices, back, openRecord }) {
  const rep = useValue($rep), pb = state.playbook
  const query = useQuery({ queryKey: ['stoa', 'report', state.pipeline || '', id, rep.period, rep.compare],
    queryFn: () => rest('/reports/' + encodeURIComponent(id) + repQuery(rep)), staleTime: 1500 })
  const [sel, setSel] = useState('')
  const [shape, setShape] = useState('bars')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [words, setWords] = useState('')
  const [removing, setRemoving] = useState(false)
  const result = query.data
  // The first bar with records behind it is picked out, so the page opens showing what a bar is made of.
  useEffect(() => {
    if (!result || sel) return
    const first = (result.rows || []).find(row => row.records && row.records.length)
    if (first && result.draw === 'bars') setSel(first.key)
  }, [Boolean(result), id])
  if (query.error && !result) return h('div', { className: 'stoa-rep' }, h(BackRow, { back, label: 'Reports' }), h('p', { className: 'stoa-sub' }, 'That report is not here any more.'))
  if (!result) return h('div', { className: 'stoa-rep' }, h(BackRow, { back, label: 'Reports' }), h('p', { className: 'stoa-sub' }, 'Working it out…'))
  const row = (result.rows || []).find(item => item.key === sel) || null
  const run = async (body, done) => { setBusy(true); await repAct(result.id, body, done); setBusy(false) }
  const savePage = () => rest('/reports/' + encodeURIComponent(result.id) + '/page' + repQuery(rep))
    .then(file => { download(file.filename, file.html, 'text/html'); host.notify({ kind: 'success', message: 'Saved the report as one web page. It names no ' + pb.terms.deal + ', only the figures.' }) })
    .catch(error => host.notifyError(error, 'Stoa could not save the page'))
  const saveCsv = () => download('stoa-' + fileName(result.title) + '.csv', csvText(result.csv.header, result.csv.rows), 'text/csv')
  const askAbout = () => toChat('Read the Stoa report "' + result.title + '" with gtm_report (action "read", report "' + result.id + '", period "' + rep.period
    + '") and tell me what stands out and what you would do about it.')
  const change = event => {
    event.preventDefault()
    const text = words.trim()
    if (!text) return
    const sent = toChat(result.custom
      ? 'Change the Stoa report "' + result.title + '" like this: "' + text + '". Use gtm_report with action "save" and `replaces` set to "' + result.id + '", then tell me in a line what changed.'
      : 'Make a Stoa report like "' + result.title + '" but: "' + text + '". Use gtm_report with action "save" (a short title, and my words as `asked`), then tell me in a line what it shows.')
    if (sent) { setWords(''); host.notify({ kind: 'info', message: 'Hermes is on it. What it makes shows up here as a draft for you to keep or discard.' }) }
  }
  const views = [['bars', 'Bars'], ['table', 'Table']].concat(result.over && result.over.length > 1 ? [['time', 'Over time']] : [])
  const table = () => h('div', { className: 'stoa-repscroll' }, h('table', { className: 'stoa-reptable' },
    h('thead', null, h('tr', null, result.csv.header.map((name, index) => h('th', { key: index, 'data-num': index ? '1' : '0' }, name)))),
    h('tbody', null, result.csv.rows.map((cells, index) => h('tr', { key: index }, cells.map((cell, at) => h('td', { key: at, 'data-num': at ? '1' : '0' },
      at && result.csv.header[at] === 'Value' ? money(cell, pb.currency) || '–' : cell === '' || cell === null ? '–' : String(cell))))))))
  return h('div', { className: 'stoa-rep' },
    h(BackRow, { back, label: 'Reports' }),
    result.draft ? h('div', { className: 'stoa-repdraft', role: 'status' },
      h('span', null, h('b', null, result.replaces ? 'Hermes made a new version of a report.' : 'Hermes made this report.'),
        result.asked ? ' You asked: “' + result.asked + '”.' : '', ' It is a draft until you keep it.'),
      h('div', null, h(Btn, { kind: 'go', disabled: busy, onClick: () => run({ verb: 'keep' }, out => setRep({ open: (out && out.id) || result.id })) }, result.replaces ? 'Keep it in place of the old one' : 'Keep it'),
        h(Btn, { disabled: busy, onClick: () => run({ verb: 'discard' }, back) }, 'Discard'))) : null,
    h('div', { className: 'stoa-reptitle' },
      h('div', { style: { minWidth: 0 } }, h('div', { className: 'stoa-eyebrow' }, 'Report'), h('h2', null, result.title), h('p', null, result.line),
        h('div', { style: { display: 'flex', gap: 12, marginTop: 6, flexWrap: 'wrap' } },
          result.pinned ? h(Chip, { tone: 'good' }, 'on the dashboard') : null, result.custom && !result.draft ? h(Chip, null, 'made here') : null,
          result.draft ? h(Chip, { tone: 'warn' }, 'draft from Hermes') : null)),
      h('div', null,
        result.period ? h('select', { className: 'stoa-select', 'aria-label': 'Period', value: rep.period, onChange: event => setRep({ period: event.target.value }) },
          choices.periods.map(option => h('option', { key: option[0], value: option[0] }, option[1]))) : null,
        result.draw === 'bars' ? h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How it is shown' }, views.map(item =>
          h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 26, padding: '0 11px', fontSize: 12 }, 'data-on': shape === item[0] ? '1' : '0',
            'aria-pressed': shape === item[0], onClick: () => setShape(item[0]) }, item[1]))) : null,
        result.draft ? null : h(Btn, { disabled: busy, onClick: () => run({ verb: result.pinned ? 'unpin' : 'pin' }) }, result.pinned ? 'Take off the dashboard' : 'Put on the dashboard'),
        h(Menu, { label: 'Download', right: true },
          h(MenuItem, { title: 'As a page', sub: 'One file that opens in any browser and prints. Figures only.', onClick: savePage }),
          h(MenuItem, { title: 'As a spreadsheet', sub: 'The rows of this report, as a CSV.', onClick: saveCsv })))),
    h('div', { className: 'stoa-repopen' },
      h('div', null,
        h(Section, { title: result.draw === 'bars' && result.squares && result.unit === 'records' && shape === 'bars' ? plural(result.total.count, pb.terms.deal, pb.terms.deals) + ' · one square each' : 'What it shows',
          aside: shape === 'bars' || result.draw !== 'bars' ? h(RepLegend, { result }) : null },
        result.empty ? h('p', { className: 'stoa-sub' }, result.empty)
          : result.draw !== 'bars' ? h(RepBody, { result, pb, big: true })
            : shape === 'table' ? table()
              : shape === 'time' ? h(RepBars, { result, pb, big: true, rows: result.over })
                : h(RepBody, { result, pb, big: true, sel, onPick: setSel }),
        h(RepSays, { says: result.says })),
        result.draw === 'rate' && !result.empty ? h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } }, result.rows.filter(item => (item.records || []).length).map(item =>
          h(Btn, { key: item.key, kind: sel === item.key ? 'go' : '', onClick: () => setSel(sel === item.key ? '' : item.key) }, 'The ' + item.count + ' ' + item.label.toLowerCase()))) : null,
        row && row.records && row.records.length && shape !== 'time' ? h(RecordsBehind, { result, row, pb, openRecord, sources: choices.sources_in_use }) : null),
      h('div', null,
        h(Section, { title: 'How this report is made', aside: result.custom && !result.draft && !editing ? h(Btn, { kind: 'quiet', onClick: () => setEditing(true) }, 'Change') : null },
          editing && result.custom ? h(RepBuilder, { choices, start: result, pb, busy, saveLabel: 'Save', onCancel: () => setEditing(false),
            onSave: body => run({ ...body, verb: 'save' }, () => setEditing(false)) })
            : h('dl', { className: 'stoa-repmade' }, (result.made || []).flatMap(line => [h('dt', { key: line[0] + 'k' }, line[0]), h('dd', { key: line[0] + 'v' }, line[1])])
              .concat(result.asked ? [h('dt', { key: 'ak' }, 'Asked as'), h('dd', { key: 'av', style: { color: 'var(--ui-text-secondary)' } }, '“' + result.asked + '”')] : [])),
          h('p', { className: 'stoa-repsays', style: { marginTop: 12 } }, 'Stoa works this out from your desk each time you open it. No model is asked, and nothing is sent anywhere.')),
        result.draft ? null : h(Section, { title: result.custom ? 'Change it in words' : 'Ask for one like it' },
          h('form', { onSubmit: change },
            h('textarea', { className: 'stoa-field', style: { height: 'auto', padding: '7px 8px', fontFamily: 'var(--stoa-mono)', lineHeight: 1.5, resize: 'vertical' }, rows: 3, value: words,
              'aria-label': result.custom ? 'How to change this report' : 'What the new report should show', placeholder: 'Split it by owner. Or: only ' + pb.terms.deals + ' over 50k.', onChange: event => setWords(event.target.value) }),
            h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', justifyContent: 'space-between', marginTop: 8 } },
              h('span', { className: 'stoa-sub', style: { flex: '1 1 160px' } }, 'This one goes to Hermes. What it makes waits here as a draft: you keep it or discard it.'),
              h(Btn, { type: 'submit', disabled: !words.trim() }, result.custom ? 'Change' : 'Make it')))),
        h(Section, { title: 'More' },
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 8 } },
            h(Btn, { onClick: askAbout, title: 'Sends these figures to Hermes, in the talk box below' }, 'Ask Hermes about it'),
            result.custom && !result.draft && !removing ? h(Btn, { disabled: busy, onClick: () => setRemoving(true) }, 'Remove this report') : null,
            removing ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'remove' }, back) }, 'Yes, remove it') : null,
            removing ? h(Btn, { kind: 'quiet', onClick: () => setRemoving(false) }, 'Keep it') : null),
          removing ? h('p', { className: 'stoa-sub', style: { margin: '8px 0 0' } }, 'Only the report goes. No ' + pb.terms.deal + ' is touched.') : null))))
}

function Reports({ state, openRecord }) {
  const rep = useValue($rep), pb = state.playbook
  const query = useQuery({ queryKey: ['stoa', 'reports', state.pipeline || '', rep.period, rep.compare], queryFn: () => rest('/reports' + repQuery(rep)), refetchInterval: 20000, staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [ask, setAsk] = useState('')
  const data = query.data
  // A draft that was not there a moment ago (Hermes has just made it) is the one to show.
  const draftIds = data ? data.reports.filter(item => item.draft).map(item => item.id).join('|') : null
  const known = useRef(null)
  useEffect(() => {
    if (draftIds === null) return
    const ids = draftIds ? draftIds.split('|') : []
    if (known.current) { const fresh = ids.find(id => !known.current.includes(id)); if (fresh) setRep({ open: fresh, making: false }) }
    known.current = ids
  }, [draftIds])
  // Another pipeline has its own reports: one opened here does not follow you there.
  const pipe = state.pipeline || ''
  const lastPipe = useRef(pipe)
  useEffect(() => { if (lastPipe.current !== pipe) { lastPipe.current = pipe; setRep({ open: '', making: false }) } }, [pipe])
  if (!data) return h('div', { className: 'stoa-rep' }, h('p', { className: 'stoa-sub' }, query.error ? 'The reports could not be loaded. Restart Hermes if you have just updated Stoa.' : 'Working them out…'))
  if (rep.open && data.reports.some(item => item.id === rep.open)) {
    return h(ReportOpen, { key: rep.open, id: rep.open, state, choices: data.choices, back: () => setRep({ open: '' }), openRecord })
  }
  const open = id => setRep({ open: id, making: false })
  const drafts = data.reports.filter(item => item.draft)
  const savePage = () => rest('/reports/page' + repQuery(rep))
    .then(file => { download(file.filename, file.html, 'text/html'); host.notify({ kind: 'success', message: 'Saved the dashboard as one web page. It names no ' + pb.terms.deal + ', only the figures.' }) })
    .catch(error => host.notifyError(error, 'Stoa could not save the page'))
  const make = async body => {
    setBusy(true)
    try {
      const out = await rest('/reports', { method: 'POST', body })
      if (out && out.ok === false) host.notify({ kind: 'warning', message: out.error || 'That could not be saved.' })
      else { refresh(); await query.refetch(); setRep({ open: out.id, making: false }) }
    } catch (error) {
      host.notifyError(error, 'Stoa could not save the report')
    }
    setBusy(false)
  }
  const send = event => {
    event.preventDefault()
    const text = ask.trim()
    if (!text) return
    const sent = toChat('Make a report on the Stoa desk for this: "' + text + '". Use gtm_report with action "save" (give it a short title, and pass my words as `asked`), then tell me in a line what it shows.')
    if (sent) { setAsk(''); host.notify({ kind: 'info', message: 'Hermes is on it. The report shows up here as a draft for you to keep or discard.' }) }
  }
  const hints = ['Value of open ' + pb.terms.deals + ' by owner', 'Lost ' + pb.terms.deals + ' by stage', 'New ' + pb.terms.deals + ' each month, and how they ended']
  return h('div', { className: 'stoa-rep' },
    h('div', { className: 'stoa-repbar' },
      h('div', null, h('div', { className: 'stoa-tabs', role: 'group', 'aria-label': 'Reports' },
        h('button', { type: 'button', className: 'stoa-tab', 'data-on': rep.list ? '0' : '1', 'aria-pressed': !rep.list, onClick: () => setRep({ list: false }) }, 'Dashboard'),
        h('button', { type: 'button', className: 'stoa-tab', 'data-on': rep.list ? '1' : '0', 'aria-pressed': rep.list, onClick: () => setRep({ list: true }) }, 'All reports', h('small', null, String(data.reports.length))))),
      h('div', null,
        h('select', { className: 'stoa-select', 'aria-label': 'Period', value: rep.period, onChange: event => setRep({ period: event.target.value }) },
          data.choices.periods.map(option => h('option', { key: option[0], value: option[0] }, option[1]))),
        data.period.before ? h(Btn, { kind: rep.compare ? 'go' : '', 'aria-pressed': rep.compare, title: 'Set each figure against the same stretch of time before it', onClick: () => setRep({ compare: !rep.compare }) },
          'Against ' + data.period.before) : null,
        h(Btn, { onClick: savePage, title: 'The dashboard as one web page: figures only, no ' + pb.terms.deal + ' is named' }, 'Download'),
        h(Btn, { onClick: () => setRep({ making: !rep.making }) }, '+  New report'))),
    drafts.length ? h('div', { className: 'stoa-repdraft', role: 'status' },
      h('span', null, h('b', null, drafts.length === 1 ? 'Hermes made a report: ' + drafts[0].title + '.' : 'Hermes made ' + drafts.length + ' reports.'), ' A draft until you keep it.'),
      h('div', null, drafts.slice(0, 3).map(item => h(Btn, { key: item.id, kind: 'go', onClick: () => open(item.id) }, drafts.length === 1 ? 'Look at it' : item.title)))) : null,
    rep.making ? h('section', { className: 'stoa-box', style: { marginBottom: 14 } },
      h('div', { className: 'stoa-rephead', style: { marginBottom: 10 } }, h('h3', { className: 'stoa-eyebrow' }, 'A new report'),
        h('span', { className: 'stoa-sub' }, 'Made here from a few choices. No model involved.')),
      h(RepBuilder, { choices: data.choices, pb, busy, saveLabel: 'Make it', onSave: make, onCancel: () => setRep({ making: false }) })) : null,
    rep.list
      ? h('div', { className: 'stoa-replist' }, data.reports.map(item => h('div', { key: item.id, className: 'stoa-repitem' },
        h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => open(item.id) }, item.title), h('span', { className: 'stoa-sub' }, item.line)),
        h('div', null,
          item.draft ? h(Chip, { tone: 'warn' }, 'draft from Hermes') : item.custom ? h(Chip, null, 'made here') : null,
          item.draft ? null : h(Btn, { kind: 'quiet', onClick: () => repAct(item.id, { verb: item.pinned ? 'unpin' : 'pin' }) }, item.pinned ? 'Take off the dashboard' : 'Put on the dashboard'),
          item.pinned && !item.draft ? h(Btn, { kind: 'quiet', title: 'Move it earlier on the dashboard', 'aria-label': 'Move ' + item.title + ' earlier', onClick: () => repAct(item.id, { verb: 'up' }) }, '↑') : null,
          item.pinned && !item.draft ? h(Btn, { kind: 'quiet', title: 'Move it later on the dashboard', 'aria-label': 'Move ' + item.title + ' later', onClick: () => repAct(item.id, { verb: 'down' }) }, '↓') : null,
          h(Btn, { onClick: () => open(item.id) }, 'Open')))))
      : data.cards.length
        ? h('div', { className: 'stoa-repgrid' }, data.cards.map(card => h(ReportCard, { key: card.id, card, pb, open })))
        : h('p', { className: 'stoa-sub' }, 'Nothing is on the dashboard. Put a report on it from All reports.'),
    h('div', { className: 'stoa-repask' },
      h('label', { className: 'stoa-eyebrow', htmlFor: 'stoa-repask' }, 'Ask for a report'),
      h('form', { onSubmit: send },
        h('input', { id: 'stoa-repask', className: 'stoa-field', value: ask, placeholder: 'Win rate by owner this quarter', onChange: event => setAsk(event.target.value) }),
        h(Btn, { type: 'submit', disabled: !ask.trim() }, 'Make it')),
      h('div', { className: 'stoa-repchips' }, hints.map(text => h(Btn, { key: text, onClick: () => setAsk(text) }, text))),
      h('p', { className: 'stoa-sub', style: { margin: 0, maxWidth: '76ch' } }, 'Hermes turns the words into a saved report, once, and it waits as a draft for you to keep. After that Stoa works it out from your desk each time you open it: no model call, nothing sent anywhere.')))
}

// -- the database: every account, deal and person on the desk, to look through ------------------------
// The Pipeline tab shows work by stage. This shows everything that is kept, as rows: search it, filter it, sort it,
// group it, choose the columns, and keep a way of looking as a view. The rows come from the plugin's backend; all of
// the looking is done here, so it answers as fast as you can click.
const DB_THINGS = [['accounts', 'Accounts'], ['deals', 'Deals'], ['people', 'People']]
const DB_OPS = {
  text: [['contains', 'contains'], ['not_contains', 'does not contain'], ['is', 'is'], ['is_not', 'is not'], ['empty', 'is empty'], ['not_empty', 'is not empty']],
  pick: [['is', 'is'], ['is_not', 'is not'], ['empty', 'is empty'], ['not_empty', 'is not empty']],
  number: [['gt', 'is at least'], ['lt', 'is at most'], ['is', 'is exactly']],
  date: [['next', 'is due within (days)'], ['last', 'was in the last (days)'], ['older', 'was more than (days) ago'], ['before', 'is before'], ['after', 'is after'], ['empty', 'is empty'], ['not_empty', 'is not empty']],
  yes: [['is', 'is']]
}
const DB_BARE = ['empty', 'not_empty']                    // these need no value
const DB_DAYS = ['next', 'last', 'older']                  // these take a number of days
const $db = atom({ thing: 'deals', accounts: null, deals: null, people: null })      // what is being looked at, kept while the page is open

const dbValue = (row, key) => (key.startsWith('fields.') ? (row.fields || {})[key.slice(7)] : row[key])
const dbText = value => (value === true ? 'Yes' : value === false ? 'No' : value === null || value === undefined ? '' : String(value))
const dbDay = offset => { const d = new Date(); d.setDate(d.getDate() + offset); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }
const dbKind = col => (col.type === 'money' || col.type === 'number' ? 'number' : col.type === 'date' ? 'date' : col.type === 'yes' ? 'yes' : col.type === 'pick' ? 'pick' : 'text')

/** The columns of each kind of row. `on` marks the ones shown until a person chooses otherwise. */
function dbColumns(thing, data, pb) {
  const terms = pb.terms, one = pb.group_is_company
  if (thing === 'deals') return [
    { key: 'name', label: cap(terms.deal), type: 'text', on: true },
    { key: 'company', label: cap(terms.company || 'company'), type: 'pick', on: true },
    { key: 'group', label: cap(terms.group), type: 'pick', on: !one },
    { key: 'status', label: 'Status', type: 'pick', on: true, order: ['Open', pb.won.label, pb.lost.label] },
    { key: 'stage', label: 'Stage', type: 'pick', on: true, order: data.stages },
    { key: 'delivery', label: 'After the win', type: 'pick', order: data.delivery },
    ...(noFees() ? [] : [{ key: 'value', label: cap(terms.value || 'value'), type: 'money', on: true }]),
    { key: 'owner', label: cap(terms.owner || 'owner'), type: 'pick', on: true },
    { key: 'next_step', label: 'Next step', type: 'text', on: true },
    { key: 'next_due', label: 'Due', type: 'date', on: true },
    { key: 'contact', label: 'Main contact', type: 'text', on: true },
    { key: 'contact_title', label: 'Contact title', type: 'text' },
    { key: 'email', label: 'Email', type: 'text' },
    { key: 'phone', label: 'Phone', type: 'text' },
    { key: 'linkedin', label: 'LinkedIn', type: 'text' },
    { key: 'people', label: 'People', type: 'number' },
    { key: 'source', label: 'Source', type: 'pick' },
    { key: 'close_on', label: 'Expected close', type: 'date' },
    { key: 'last_touch', label: 'Last touch', type: 'date', on: true },
    { key: 'stage_at', label: 'In stage since', type: 'date' },
    { key: 'created_at', label: 'Added', type: 'date' },
    { key: 'lost_reason', label: 'Lost because', type: 'pick' },
    { key: 'notes', label: 'Notes', type: 'text' }
  ].concat((data.fields || []).map(field => ({ key: 'fields.' + field.key, label: field.label, type: 'text' })))
  if (thing === 'accounts') return [
    { key: 'name', label: one ? cap(terms.group) : 'Company', type: 'text', on: true },
    { key: 'status', label: 'Status', type: 'pick', on: true, order: ['Customer', 'Open', 'Lost', 'On a map'] },
    { key: 'open', label: 'Open ' + terms.deals, type: 'number', on: true },
    { key: 'open_value', label: 'Open value', type: 'money', on: true },
    { key: 'won', label: pb.won.label, type: 'number' },
    { key: 'won_value', label: 'Won value', type: 'money', on: true },
    { key: 'lost', label: pb.lost.label, type: 'number' },
    { key: 'deals', label: 'All ' + terms.deals, type: 'number' },
    { key: 'people', label: 'People', type: 'number', on: true },
    { key: 'owners', label: cap(terms.owner || 'owner'), type: 'pick', many: true },
    { key: 'next_step', label: 'Next step', type: 'text', on: true },
    { key: 'next_due', label: 'Due', type: 'date', on: true },
    { key: 'last_touch', label: 'Last touch', type: 'date', on: true },
    { key: 'sources', label: 'Source', type: 'pick', many: true },
    { key: 'domain', label: 'Website', type: 'text' },
    { key: 'location', label: 'Location', type: 'text' },
    { key: 'size', label: 'Size', type: 'text' },
    { key: 'segment', label: 'Segment', type: 'pick' },
    { key: 'fit', label: 'Fit', type: 'number' },
    { key: 'maps', label: 'Market map', type: 'pick', many: true },
    { key: 'signal', label: 'Signal', type: 'text' },
    { key: 'note', label: 'Notes', type: 'text' },
    { key: 'created_at', label: 'Added', type: 'date' },
    { key: 'in_pipeline', label: 'In the pipeline', type: 'yes' }
  ]
  return [
    { key: 'name', label: 'Name', type: 'text', on: true },
    { key: 'title', label: 'Title', type: 'text', on: true },
    { key: 'company', label: 'Company', type: 'pick', on: true },
    { key: 'email', label: 'Email', type: 'text', on: true },
    { key: 'phone', label: 'Phone', type: 'text' },
    { key: 'linkedin', label: 'LinkedIn', type: 'text' },
    { key: 'role', label: 'Role', type: 'text', on: true },
    { key: 'record_names', label: 'On', type: 'text', on: true },
    { key: 'record_count', label: cap(terms.deals), type: 'number' },
    { key: 'open', label: 'Open ' + terms.deals, type: 'number' },
    { key: 'kind', label: 'Kind', type: 'pick' },
    { key: 'last_touch', label: 'Last touch', type: 'date', on: true },
    { key: 'created_at', label: 'Added', type: 'date' },
    { key: 'note', label: 'Notes', type: 'text' }
  ]
}

/** The views that come with the tab. A person's own are kept by the backend and listed after these. */
function dbStarters(thing, data, pb) {
  const f = (field, op, value) => ({ field, op, value: value === undefined ? '' : String(value) })
  if (thing === 'deals') return [
    { id: '@all', name: 'All ' + pb.terms.deals, filters: [] },
    { id: '@open', name: 'Open', filters: [f('status', 'is', 'Open')], sort: { field: 'value', dir: 'desc' } },
    { id: '@due', name: 'Due this week', filters: [f('next_due', 'next', 7)], sort: { field: 'next_due', dir: 'asc' } },
    { id: '@unplanned', name: 'No next step', filters: [f('status', 'is_not', pb.lost.label), f('next_step', 'empty')] },
    { id: '@quiet', name: 'Quiet for two weeks', filters: [f('status', 'is', 'Open'), f('last_touch', 'older', 14)], sort: { field: 'last_touch', dir: 'asc' } }
  ].concat(data.delivery.length ? [{ id: '@won', name: 'After the win', filters: [f('status', 'is', pb.won.label)], group: 'delivery' }] : [{ id: '@won', name: pb.won.label, filters: [f('status', 'is', pb.won.label)] }])
    .concat([{ id: '@lost', name: pb.lost.label, filters: [f('status', 'is', pb.lost.label)], group: 'lost_reason' }, { id: '@stage', name: 'By stage', filters: [f('status', 'is', 'Open')], group: 'stage' }])
  if (thing === 'accounts') return [
    { id: '@in', name: 'In the pipeline', filters: [f('in_pipeline', 'is', 'Yes')] },
    { id: '@customers', name: 'Customers', filters: [f('status', 'is', 'Customer')], sort: { field: 'won_value', dir: 'desc' } },
    { id: '@nobody', name: 'Nobody named yet', filters: [f('in_pipeline', 'is', 'Yes'), f('people', 'is', 0)] },
    { id: '@map', name: 'On a map, not in the pipeline', filters: [f('status', 'is', 'On a map')], sort: { field: 'fit', dir: 'desc' },
      columns: ['name', 'status', 'segment', 'fit', 'size', 'location', 'signal', 'maps'] },
    { id: '@all', name: 'All accounts', filters: [] }
  ]
  return [
    { id: '@all', name: 'All people', filters: [] },
    { id: '@active', name: 'On open ' + pb.terms.deals, filters: [f('open', 'gt', 1)] },
    { id: '@noemail', name: 'No email yet', filters: [f('email', 'empty')] },
    { id: '@company', name: 'By company', filters: [], group: 'company' }
  ]
}

/** Whether a row passes one filter. */
function dbPass(row, filter, col) {
  const raw = dbValue(row, filter.field), text = dbText(raw).trim(), want = String(filter.value || '').trim()
  const kind = dbKind(col), nothing = kind === 'number' ? !Number(raw) : text === ''
  if (filter.op === 'empty') return nothing
  if (filter.op === 'not_empty') return !nothing
  if (want === '') return true                                   // a filter still being filled in holds nothing back
  const low = text.toLowerCase(), wanted = want.toLowerCase()
  const parts = col.many ? low.split(',').map(part => part.trim()) : [low]
  if (kind === 'number') {
    const n = Number(raw) || 0, w = Number(want)
    if (isNaN(w)) return true
    return filter.op === 'gt' ? n >= w : filter.op === 'lt' ? n <= w : filter.op === 'is_not' ? n !== w : n === w
  }
  if (kind === 'date') {
    const days = Math.max(0, Math.round(Number(want)) || 0), day = text.slice(0, 10)
    if (filter.op === 'next') return Boolean(day) && day <= dbDay(days)
    if (filter.op === 'last') return Boolean(day) && day >= dbDay(-days) && day <= dbDay(0)
    if (filter.op === 'older') return Boolean(day) && day < dbDay(-days)
    if (filter.op === 'before') return Boolean(day) && day < want
    if (filter.op === 'after') return Boolean(day) && day > want
    return true
  }
  if (filter.op === 'is') return parts.includes(wanted)
  if (filter.op === 'is_not') return !parts.includes(wanted)
  if (filter.op === 'contains') return low.includes(wanted)
  if (filter.op === 'not_contains') return !low.includes(wanted)
  return true
}

/** The rows a way of looking shows: several "is" on one column are either-or, everything else must all hold. */
function dbShown(rows, look, columns, shownCols) {
  const byKey = new Map(columns.map(col => [col.key, col]))
  const live = look.filters.filter(filter => byKey.has(filter.field))
  const either = new Map()
  const rest_ = []
  for (const filter of live) {
    if (filter.op === 'is' && String(filter.value || '').trim()) either.set(filter.field, (either.get(filter.field) || []).concat([filter]))
    else rest_.push(filter)
  }
  const words = look.search.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const searched = shownCols.map(col => col.key).concat(['name', 'company', 'email'])
  let out = rows.filter(row => rest_.every(filter => dbPass(row, filter, byKey.get(filter.field)))
    && [...either.values()].every(list => list.some(filter => dbPass(row, filter, byKey.get(filter.field))))
    && (!words.length || (() => { const hay = searched.map(key => dbText(dbValue(row, key))).join(' \n ').toLowerCase(); return words.every(word => hay.includes(word)) })()))
  const col = look.sort && byKey.get(look.sort.field)
  if (col) {
    const way = look.sort.dir === 'desc' ? -1 : 1, num = dbKind(col) === 'number'
    const rank = col.order ? new Map(col.order.map((label, index) => [label, index])) : null
    out = out.map((row, index) => [row, index]).sort((a, b) => {
      const x = dbValue(a[0], col.key), y = dbValue(b[0], col.key)
      const xe = num ? x === '' || x == null : !dbText(x), ye = num ? y === '' || y == null : !dbText(y)
      if (xe !== ye) return xe ? 1 : -1                         // nothing there goes last, whichever way it is sorted
      let d = 0
      if (num) d = (Number(x) || 0) - (Number(y) || 0)
      else if (rank && rank.has(x) && rank.has(y)) d = rank.get(x) - rank.get(y)
      else d = dbText(x).localeCompare(dbText(y), undefined, { numeric: true, sensitivity: 'base' })
      return d * way || a[1] - b[1]
    }).map(pair => pair[0])
  }
  return out
}

/** One filter, as a sentence you can change: the column, how it is compared, and with what. */
function DbFilter({ filter, columns, rows, change, remove }) {
  const col = columns.find(item => item.key === filter.field) || columns[0], kind = dbKind(col), ops = DB_OPS[kind]
  const values = useMemo(() => {
    if (kind !== 'pick' && kind !== 'yes') return []
    if (kind === 'yes') return ['Yes', 'No']
    const seen = new Set()
    for (const row of rows) for (const part of (col.many ? dbText(dbValue(row, col.key)).split(',') : [dbText(dbValue(row, col.key))])) if (part.trim()) seen.add(part.trim())
    const list = [...seen].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
    return col.order ? col.order.filter(label => seen.has(label)).concat(list.filter(label => !col.order.includes(label))) : list
  }, [rows, col.key])
  const setOp = op => change({ ...filter, op, value: DB_BARE.includes(op) ? '' : DB_DAYS.includes(op) !== DB_DAYS.includes(filter.op) || DB_BARE.includes(filter.op) ? (DB_DAYS.includes(op) ? '7' : '') : filter.value })
  const input = DB_BARE.includes(filter.op) ? null
    : (kind === 'pick' || kind === 'yes') ? h('select', { value: filter.value, 'aria-label': 'Value', onChange: event => change({ ...filter, value: event.target.value }) },
      [h('option', { key: '', value: '' }, 'choose…')].concat((values.includes(filter.value) || !filter.value ? values : [filter.value].concat(values)).map(value => h('option', { key: value, value }, value))))
      : kind === 'number' || DB_DAYS.includes(filter.op) ? h('input', { type: 'number', min: 0, value: filter.value, 'aria-label': DB_DAYS.includes(filter.op) ? 'Days' : 'Value', placeholder: DB_DAYS.includes(filter.op) ? 'days' : '0',
        onChange: event => change({ ...filter, value: event.target.value }) })
        : kind === 'date' ? h('input', { type: 'date', value: filter.value, 'aria-label': 'Date', onChange: event => change({ ...filter, value: event.target.value }) })
          : h('input', { value: filter.value, autoFocus: !filter.value, placeholder: 'type…', 'aria-label': 'Value', onChange: event => change({ ...filter, value: event.target.value }) })
  return h('span', { className: 'stoa-dbfilter' },
    h('b', null, col.label),
    h('select', { value: filter.op, 'aria-label': 'How ' + col.label + ' is compared', onChange: event => setOp(event.target.value) }, ops.map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1]))),
    input,
    h('button', { type: 'button', className: 'stoa-icon', title: 'Remove this filter', 'aria-label': 'Remove the filter on ' + col.label, onClick: remove }, h(Icon, { name: 'x', size: 11 })))
}

/** What changing a cell saves, or null where the cell only shows something worked out from elsewhere.
 *  Every save goes the way the record's own page saves it, so it is logged there and can be taken back. */
function dbEditor(thing, row, col, pb) {
  const key = col.key
  const send = (path, body) => rest(path, { method: 'POST', body }).then(result => { refresh(); if (result && result.last) offerUndo(result.last); return result || true })
    .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
  const update = body => act(row.id, { verb: 'update', ...body })
  if (thing === 'deals') {
    if (key === 'stage') return row.state === 'open' ? { kind: 'choice', options: pb.stages.map(stage => [stage.id, stage.label]), value: row.stage_id, save: value => act(row.id, { verb: 'move', stage: value }) } : null
    if (key === 'delivery') return row.state === 'won' && (pb.delivery || []).length ? { kind: 'choice', options: pb.delivery.map(item => [item.id, item.label]), value: row.delivery_id, save: value => update({ delivery: value }) } : null
    if (key === 'next_step' || key === 'next_due') return row.working ? { kind: key === 'next_due' ? 'date' : 'text', save: value => update({ [key]: value }) } : null
    if (key === 'name') return { kind: 'text', must: true, save: value => update({ name: value }) }
    if (key === 'value') return { kind: 'number', save: value => update({ value: value === '' ? 0 : Number(value) }) }
    if (key === 'close_on') return { kind: 'date', save: value => update({ close_on: value }) }
    if (key === 'owner' || key === 'source') return { kind: 'suggest', save: value => update({ [key]: value }) }
    if (key === 'notes') return { kind: 'text', save: value => update({ notes: value }) }
    if (key.startsWith('fields.')) return { kind: 'text', save: value => update({ fields: { [key.slice(7)]: value } }) }
    return null
  }
  if (thing === 'people') {
    const how = row.edit || {}
    if (!['name', 'title', 'email', 'phone', 'linkedin'].includes(key)) return null
    const must = key === 'name'
    if (how.record) return { kind: 'text', must, save: value => act(how.record, { verb: 'update', ...(key === 'title' ? { fields: { title: value } } : { [key]: value }) }) }
    const body = value => ({ verb: 'update', person: how.person, [key === 'name' ? 'new_name' : key]: value })
    if (how.deal) return { kind: 'text', must, save: value => send('/deal/' + encodeURIComponent(how.deal) + '/person', body(value)) }
    if (how.group) return { kind: 'text', must, save: value => send('/group/person', { ...body(value), group: how.group }) }
    return null
  }
  if ((key === 'domain' || key === 'note') && row.in_pipeline) return { kind: 'text', save: value => send('/company', { name: row.name, [key]: value }) }
  return null
}

/** A cell while it is being changed. Enter or a click elsewhere saves; Escape leaves it as it was. */
function DbEdit({ editor, start, suggestions, done }) {
  const [value, setValue] = useState(start)
  const [busy, setBusy] = useState(false)
  const left = useRef(false)
  const commit = next => {
    if (busy || left.current) return
    const clean = typeof next === 'string' ? next.trim() : next
    if (String(clean) === String(start) || (editor.must && !clean)) { left.current = true; done(); return }
    setBusy(true)
    Promise.resolve(editor.save(clean)).then(result => { left.current = true; done(result ? clean : undefined) })
  }
  const keys = event => {
    if (event.key === 'Escape') { event.stopPropagation(); left.current = true; done() }
    else if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); commit(value) }
  }
  if (editor.kind === 'choice') return h('select', { className: 'stoa-dbedit', autoFocus: true, value, disabled: busy, 'aria-label': 'Change it',
    onChange: event => { setValue(event.target.value); commit(event.target.value) }, onBlur: () => { if (!busy && !left.current) { left.current = true; done() } }, onKeyDown: keys },
  editor.options.map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))
  const listId = editor.kind === 'suggest' ? 'stoa-dbsuggest' : undefined
  return h('span', null,
    h('input', { className: 'stoa-dbedit', autoFocus: true, disabled: busy, value, list: listId, 'aria-label': 'Change it',
      type: editor.kind === 'number' ? 'number' : editor.kind === 'date' ? 'date' : 'text', min: editor.kind === 'number' ? 0 : undefined, step: editor.kind === 'number' ? 'any' : undefined,
      onFocus: event => { if (editor.kind !== 'date' && event.target.select) event.target.select() },
      onChange: event => setValue(event.target.value), onBlur: () => commit(value), onKeyDown: keys }),
    listId ? h('datalist', { id: listId }, (suggestions || []).map(item => h('option', { key: item, value: item }))) : null)
}

/** One cell, drawn for what it holds. A name opens what it names. A cell that can be changed is changed in place:
 *  press it. Everything can be selected and copied, a cell being changed included. */
function DbCell({ row, col, thing, pb, currency, open, editor, editing, patched, edit, done, suggestions }) {
  const raw = patched !== undefined ? patched : dbValue(row, col.key), text = dbText(raw)
  const number = dbKind(col) === 'number'
  if (editing && editor) return h('td', { 'data-num': '0', 'data-editing': '1' },
    h(DbEdit, { editor, start: editor.kind === 'choice' ? editor.value || '' : col.type === 'date' ? text.slice(0, 10) : number ? (Number(raw) ? String(raw) : '') : text, suggestions, done }))
  // Pressing a cell that can be changed changes it, unless the press was the end of selecting its text.
  const press = editor && col.key !== 'name' ? () => { if (!String(window.getSelection ? window.getSelection() : '').trim()) edit() } : undefined
  const props = { 'data-num': number ? '1' : '0', 'data-edit': editor && col.key !== 'name' ? '1' : '0', title: editor && col.key !== 'name' ? (text.length > 28 ? text + '\n' : '') + 'Press to change' : text.length > 28 ? text : undefined, onClick: press }
  if (col.key === 'name') return h('td', { 'data-num': '0' }, h('span', { className: 'stoa-dbname' },
    open && open.row ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: open.row }, text || '—') : h('span', { className: 'stoa-name' }, text || '—'),
    editor ? h('button', { type: 'button', className: 'stoa-icon stoa-dbrename', title: 'Rename', 'aria-label': 'Rename ' + text, onClick: edit }, '✎') : null))
  if (!text || (number && !Number(raw))) return h('td', { ...props, 'data-empty': '1' }, col.type === 'number' && raw === 0 ? '0' : '—')
  if (col.type === 'money') return h('td', props, exact(Number(raw), currency) || '—')
  if (col.type === 'date') {
    const late = col.key === 'next_due' && text < today(), now = col.key === 'next_due' && text === today()
    return h('td', { ...props, 'data-tone': late ? 'bad' : now ? 'warn' : '' }, shortDate(text.slice(0, 10)) + (late ? ' · overdue' : now ? ' · today' : ''))
  }
  if (col.key === 'status') return h('td', props, h(Chip, { tone: text === pb.won.label || text === 'Customer' ? 'good' : '' }, text))
  if (col.key === 'fit') return h('td', { ...props, 'data-num': '0' }, h(Fit, { value: Number(raw) }))
  if (col.key === 'company' && open && open.company) return h('td', props, h('button', { type: 'button', className: 'stoa-proplink', onClick: () => open.company(text) }, text))
  if (col.key === 'record_names' && thing === 'people' && open) return h('td', props, (row.records || []).map((item, index) => [index ? ', ' : null,
    h('button', { key: item.id, type: 'button', className: 'stoa-proplink', onClick: () => open.record(item.id) }, item.name)]), row.record_count > (row.records || []).length ? ' and ' + (row.record_count - row.records.length) + ' more' : null)
  return h('td', props, text)
}

/** What can be done to the rows that are ticked: the same change to each, said once. */
function DbBulk({ thing, picked, pb, data, noun, suggestions, onDone, clear, exportRows, copyEmails, emails }) {
  const [doing, setDoing] = useState(null)      // { kind, value, due } while a change is being filled in
  const [busy, setBusy] = useState(false)
  const n = picked.length
  const targets = thing === 'accounts' ? picked.flatMap(row => row.targets || []) : []
  const said = result => {
    const skipped = result.skipped || []
    return skipped.length ? '  ' + plural(skipped.length, 'was', 'were') + ' left as ' + (skipped.length === 1 ? 'it was' : 'they were') + ' (' + skipped[0].name + ': ' + skipped[0].why + (skipped.length > 1 ? ', and ' + (skipped.length - 1) + ' more' : '') + ').' : ''
  }
  const apply = (body, done) => {
    setBusy(true)
    rest('/database/change', { method: 'POST', body: { ids: picked.map(row => row.id), ...body } }).then(result => {
      setBusy(false)
      if (!result.ok) { host.notify({ kind: 'info', message: result.error }); return }
      refresh()
      setDoing(null)
      host.notify({ kind: 'success', message: done(result.changed) + said(result) + '  Each is logged on its record, where it can be taken back.' })
      onDone()
    }).catch(error => { setBusy(false); host.notifyError(error, 'Stoa could not make that change') })
  }
  const many = count => plural(count, noun[0], noun[1])
  const go = () => {
    const value = String(doing.value || '').trim()
    if (doing.kind === 'owner') apply({ set: { owner: value } }, count => (value ? value + ' now owns ' + many(count) + '.' : 'Owner cleared on ' + many(count) + '.'))
    else if (doing.kind === 'source') apply({ set: { source: value } }, count => 'Source set on ' + many(count) + '.')
    else if (doing.kind === 'next') apply({ set: { next_step: value, next_due: doing.due || '' } }, count => 'Next step set on ' + many(count) + '.')
    else if (doing.kind === 'close') apply({ set: { close_on: value } }, count => 'Expected close set on ' + many(count) + '.')
    else if (doing.kind === 'stage') apply({ stage: value }, count => many(count) + ' moved to ' + ((pb.stages.find(stage => stage.id === value) || {}).label || value) + '.')
    else if (doing.kind === 'lost') apply({ lost: value }, count => many(count) + ' closed as ' + pb.lost.label.toLowerCase() + '.')
  }
  const track = () => {
    setBusy(true)
    rest('/targets', { method: 'POST', body: { verb: 'track', ids: targets.slice(0, 50) } }).then(result => {
      setBusy(false); refresh(); onDone()
      const added = (result.added || []).length
      host.notify({ kind: added ? 'success' : 'info', message: added ? plural(added, 'company is', 'companies are') + ' now in the pipeline, each with what the map knows.' : 'Nothing was added: ' + ((result.skipped || [])[0] || 'they are already in the pipeline.') })
    }).catch(error => { setBusy(false); host.notifyError(error, 'Stoa could not add those') })
  }
  const ready = doing && (doing.kind === 'owner' || (doing.kind === 'next' ? Boolean(String(doing.value || '').trim() || doing.due) : Boolean(String(doing.value || '').trim())))
  const set = patch => setDoing({ ...doing, ...patch })
  const enter = event => { if (event.key === 'Enter' && ready && !busy) go(); if (event.key === 'Escape') setDoing(null) }
  const form = !doing ? null
    : doing.kind === 'stage' ? h('select', { className: 'stoa-select', autoFocus: true, value: doing.value, 'aria-label': 'Stage', onChange: event => set({ value: event.target.value }) },
      [h('option', { key: '', value: '' }, 'Move to…')].concat(pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))))
      : doing.kind === 'lost' ? h('select', { className: 'stoa-select', autoFocus: true, value: doing.value, 'aria-label': 'Why', onChange: event => set({ value: event.target.value }) },
        [h('option', { key: '', value: '' }, 'Why…')].concat((pb.lost.reasons || []).concat(['Other']).map(reason => h('option', { key: reason, value: reason }, reason))))
        : doing.kind === 'close' ? h('input', { className: 'stoa-field', type: 'date', autoFocus: true, value: doing.value, 'aria-label': 'Expected close', onChange: event => set({ value: event.target.value }), onKeyDown: enter })
          : [h('input', { key: 'v', className: 'stoa-field', autoFocus: true, value: doing.value, list: doing.kind === 'next' ? undefined : 'stoa-dbbulklist', onKeyDown: enter,
            placeholder: doing.kind === 'owner' ? 'Who owns them (empty for nobody)' : doing.kind === 'source' ? 'Where they came from' : 'The next step', 'aria-label': 'Value', onChange: event => set({ value: event.target.value }) }),
            doing.kind === 'next' ? h('input', { key: 'd', className: 'stoa-field', type: 'date', value: doing.due || '', min: today(), 'aria-label': 'Due', style: { width: 140 }, onChange: event => set({ due: event.target.value }), onKeyDown: enter }) : null,
            doing.kind === 'next' ? null : h('datalist', { key: 'l', id: 'stoa-dbbulklist' }, (suggestions[doing.kind] || []).map(item => h('option', { key: item, value: item })))]
  const start = (kind, label) => h(Btn, { key: kind, disabled: busy, onClick: () => setDoing({ kind, value: '', due: '' }) }, label)
  const NAMES = { owner: 'Owner', source: 'Source', next: 'Next step', close: 'Expected close', stage: 'Stage', lost: pb.lost.label }
  return h('div', { className: 'stoa-dbbulk', role: 'region', 'aria-label': 'What to do with the rows ticked' },
    h('b', null, plural(n, noun[0], noun[1]) + ' ticked'),
    doing
      ? [h('span', { key: 'what', className: 'stoa-eyebrow' }, NAMES[doing.kind]), h('span', { key: 'form', className: 'stoa-dbbulkform' }, form),
        h(Btn, { key: 'go', kind: doing.kind === 'lost' ? 'danger' : 'primary', disabled: busy || !ready, onClick: go }, busy ? 'Saving…' : 'Apply to ' + n),
        h(Btn, { key: 'no', kind: 'quiet', disabled: busy, onClick: () => setDoing(null) }, 'Cancel')]
      : (thing === 'deals' ? [start('owner', 'Set owner'), start('stage', 'Move to stage'), start('next', 'Set next step'), start('source', 'Set source'), start('close', 'Set expected close'), start('lost', 'Close as ' + pb.lost.label.toLowerCase())] : [])
        .concat(targets.length ? [h(Btn, { key: 'track', kind: 'go', disabled: busy, title: 'Open a ' + pb.terms.deal + ' for each company that is on a map and not yet in the pipeline', onClick: track }, busy ? 'Adding…' : 'Add ' + targets.length + ' to the pipeline')] : [])
        .concat([h('span', { key: 'gap', className: 'stoa-grow' }),
          emails.length ? h(Btn, { key: 'copy', kind: 'quiet', onClick: copyEmails }, 'Copy ' + plural(emails.length, 'email', 'emails')) : null,
          h(Btn, { key: 'csv', kind: 'quiet', onClick: exportRows }, 'Export these'),
          h(Btn, { key: 'clear', kind: 'quiet', onClick: clear }, 'Untick all')]))
}

function Database({ state, openRecord, openGroup, openCompany, openMap }) {
  const pb = state.playbook
  const query = useQuery({ queryKey: ['stoa', 'database', state.pipeline || ''], queryFn: () => rest('/database'), refetchInterval: 20000, staleTime: 1500 })
  const db = useValue($db)
  const [saving, setSaving] = useState(null)            // the name being typed for a view, or null
  const [shut, setShut] = useState({})                  // the groups folded away
  const [editing, setEditing] = useState(null)          // the cell being changed: { row, col }
  const [patches, setPatches] = useState({})            // what was just saved, shown until the desk answers with it
  const [picked, setPicked] = useState(() => new Set()) // the rows ticked
  const lastPick = useRef(null)
  // Where you were (accounts, deals or people; the view, its filters and columns) is there when you come back.
  useEffect(() => {
    const kept = storage ? storage.get('database', null) : null
    if (kept && typeof kept === 'object' && DB_THINGS.some(pair => pair[0] === kept.thing)) $db.set({ thing: kept.thing, accounts: kept.accounts || null, deals: kept.deals || null, people: kept.people || null })
  }, [])
  useEffect(() => { if (storage) storage.set('database', db) }, [db])
  const data = query.data
  const thing = db.thing
  const columns = useMemo(() => (data ? dbColumns(thing, data, pb) : []), [data, thing, pb])
  const starters = useMemo(() => (data ? dbStarters(thing, data, pb) : []), [data, thing, pb])
  useEffect(() => { setPatches({}) }, [data])
  if (!data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  const rows = data[thing] || []
  const kept = (data.views || []).filter(view => view.object === thing)
  const views = starters.filter(view => view.id !== '@map' || (data.accounts || []).some(row => !row.in_pipeline)).concat(kept)
  const blank = view => ({ view: view.id, filters: (view.filters || []).map(item => ({ ...item })), search: view.search || '', sort: view.sort && view.sort.field ? { ...view.sort } : null,
    group: view.group || '', columns: view.columns && view.columns.length ? view.columns.slice() : null })
  const look = db[thing] || blank(views[0])
  const set = patch => $db.set({ ...db, [thing]: { ...look, ...patch } })
  const current = views.find(view => view.id === look.view) || views[0]
  const pick = view => { $db.set({ ...db, [thing]: blank(view) }); setSaving(null); setShut({}); setEditing(null) }
  const keyOf = row => row.id || row.key
  const shownCols = (look.columns ? look.columns.map(key => columns.find(col => col.key === key)).filter(Boolean) : columns.filter(col => col.on))
  const cols = shownCols.some(col => col.key === 'name') ? shownCols : [columns[0]].concat(shownCols)
  const shown = dbShown(rows, look, columns, cols)
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
  const base = blank(current)
  const edited = !same({ ...look, view: '' }, { ...base, view: '' })
  const own = !String(current.id).startsWith('@')
  const noun = { accounts: ['account', 'accounts'], deals: [pb.terms.deal, pb.terms.deals], people: ['person', 'people'] }[thing]
  // what a name opens
  const open = {
    record: openRecord,
    company: name => { const row = (data.accounts || []).find(item => sameName(item.name, name)); if (row && row.in_pipeline && pb.group_is_company && row.group) openGroup(row.group); else openCompany(name) },
    row: null
  }
  const opener = row => (thing === 'deals' ? () => openRecord(row.id)
    : thing === 'accounts' ? (row.in_pipeline ? () => open.company(row.name) : row.map_id ? () => openMap(row.map_id) : null)
      : row.records && row.records.length ? () => openRecord(row.records[0].id) : null)
  // a number worth adding up, for the foot of the table and each group
  const sumCol = columns.find(col => col.key === (thing === 'deals' ? 'value' : thing === 'accounts' ? 'open_value' : ''))
  const total = list => (sumCol ? list.reduce((sum, row) => sum + (Number(dbValue(row, sumCol.key)) || 0), 0) : 0)
  // the breakdown: the rows by one column, to see the spread and press into it
  const facetCol = columns.find(col => col.key === (look.group || { deals: 'stage', accounts: 'status', people: 'company' }[thing]))
  const facetOf = row => (facetCol ? (facetCol.many ? dbText(dbValue(row, facetCol.key)).split(',').map(part => part.trim()).filter(Boolean) : [dbText(dbValue(row, facetCol.key))]) : [])
  const facets = (() => {
    if (!facetCol) return []
    // counted over the rows the other filters leave, so the values not picked stay there to press
    const among = look.filters.some(filter => filter.field === facetCol.key) ? dbShown(rows, { ...look, sort: null, filters: look.filters.filter(filter => filter.field !== facetCol.key) }, columns, cols) : shown
    const count = new Map()
    for (const row of among) for (const label of (facetOf(row).length ? facetOf(row) : [''])) count.set(label, (count.get(label) || 0) + 1)
    const list = [...count.entries()].map(([label, n]) => ({ label, n })).filter(item => item.label)
    const rank = facetCol.order ? new Map(facetCol.order.map((label, index) => [label, index])) : null
    const place = label => (rank && rank.has(label) ? rank.get(label) : 999)         // the playbook's order where it has one, then the commonest
    list.sort((a, b) => place(a.label) - place(b.label) || b.n - a.n || a.label.localeCompare(b.label))
    return list.slice(0, 9)
  })()
  const most = Math.max(1, ...facets.map(item => item.n))
  const facetOn = label => look.filters.some(filter => filter.field === facetCol.key && filter.op === 'is' && filter.value === label)
  const toggleFacet = label => set({ filters: facetOn(label) ? look.filters.filter(filter => !(filter.field === facetCol.key && filter.op === 'is' && filter.value === label))
    : look.filters.concat([{ field: facetCol.key, op: 'is', value: label }]) })
  // groups
  const groupCol = look.group ? columns.find(col => col.key === look.group) : null
  const sections = (() => {
    if (!groupCol) return [{ label: '', rows: shown }]
    const by = new Map()
    for (const row of shown) { const label = dbText(dbValue(row, groupCol.key)); by.set(label, (by.get(label) || []).concat([row])) }
    const list = [...by.entries()].map(([label, list_]) => ({ label, rows: list_ }))
    const rank = groupCol.order ? new Map(groupCol.order.map((label, index) => [label, index])) : null
    const place = label => (rank && rank.has(label) ? rank.get(label) : 999)
    list.sort((a, b) => (!a.label) - (!b.label) || place(a.label) - place(b.label) || (dbKind(groupCol) === 'number' ? Number(b.label) - Number(a.label) : a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: 'base' })))
    return list
  })()
  const sortBy = key => set({ sort: !look.sort || look.sort.field !== key ? { field: key, dir: ['money', 'number', 'date'].includes(columns.find(col => col.key === key).type) && key !== 'next_due' ? 'desc' : 'asc' }
    : look.sort.dir === (['money', 'number', 'date'].includes(columns.find(col => col.key === key).type) && key !== 'next_due' ? 'desc' : 'asc') ? { field: key, dir: look.sort.dir === 'asc' ? 'desc' : 'asc' } : null })
  const toggleCol = key => { const now = cols.map(col => col.key); const next = now.includes(key) ? now.filter(item => item !== key) : columns.map(col => col.key).filter(item => now.includes(item) || item === key); set({ columns: next }) }
  const addFilter = col => { const kind = dbKind(col); set({ filters: look.filters.concat([{ field: col.key, op: DB_OPS[kind][0][0], value: kind === 'date' ? '7' : kind === 'yes' ? 'Yes' : '' }]) }) }
  const keep = (name, id) => rest('/database/view', { method: 'POST', body: { id: id || '', name, object: thing, filters: look.filters.filter(filter => DB_BARE.includes(filter.op) || String(filter.value).trim()),
    search: look.search, sort: look.sort || {}, group: look.group, columns: look.columns || [] } })
    .then(result => {
      if (!result.ok) { host.notify({ kind: 'info', message: result.error }); return }
      queryClient.setQueryData(['stoa', 'database', state.pipeline || ''], old => (old ? { ...old, views: result.views } : old))
      $db.set({ ...db, [thing]: { ...look, view: result.view.id, filters: result.view.filters.map(item => ({ ...item })) } })
      setSaving(null)
      host.notify({ kind: 'success', message: id ? '“' + name + '” now opens like this.' : 'Kept as “' + name + '”. It is with the other views, marked with a star.' })
    }).catch(error => host.notifyError(error, 'Stoa could not keep that view'))
  const drop = view => rest('/database/view', { method: 'POST', body: { remove: view.id } }).then(result => {
    if (!result.ok) { host.notify({ kind: 'info', message: result.error }); return }
    queryClient.setQueryData(['stoa', 'database', state.pipeline || ''], old => (old ? { ...old, views: result.views } : old))
    $db.set({ ...db, [thing]: blank(starters[0]) })
    host.notify({ kind: 'success', message: '“' + view.name + '” is no longer kept. Nothing on the desk changed.' })
  }).catch(error => host.notifyError(error, 'Stoa could not remove that view'))
  const ticked = shown.filter(row => picked.has(keyOf(row)))        // the ticked rows that are on screen
  const scope = ticked.length ? ticked : shown
  const csv = () => {
    const cell = value => { const text = dbText(value); return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text }
    download('stoa-' + thing + '-' + today() + '.csv', [cols.map(col => cell(col.label)).join(',')].concat(scope.map(row => cols.map(col => cell(dbValue(row, col.key))).join(','))).join('\n'), 'text/csv')
  }
  const emails = [...new Set(scope.map(row => String(row.email || '').trim()).filter(Boolean))]
  const suggestions = { owner: [...new Set((data.deals || []).map(row => row.owner).filter(Boolean))].sort(), source: [...new Set((data.deals || []).map(row => row.source).filter(Boolean))].sort() }
  const order = sections.flatMap(section => (shut[section.label || '—'] ? [] : section.rows))
  const tick = (row, event) => {
    const key = keyOf(row), next = new Set(picked), on = !picked.has(key)
    const from = event && event.shiftKey && lastPick.current ? order.findIndex(item => keyOf(item) === lastPick.current) : -1, to = order.findIndex(item => keyOf(item) === key)
    const span = from >= 0 && to >= 0 ? order.slice(Math.min(from, to), Math.max(from, to) + 1) : [row]      // Shift ticks every row between this one and the last
    for (const item of span) { if (on) next.add(keyOf(item)); else next.delete(keyOf(item)) }
    lastPick.current = key
    setPicked(next)
  }
  const allTicked = shown.length > 0 && ticked.length === shown.length
  const tickAll = () => setPicked(allTicked ? new Set() : new Set(shown.map(keyOf)))
  const copyEmails = async () => {
    try { await navigator.clipboard.writeText(emails.join(', ')); host.notify({ kind: 'success', message: plural(emails.length, 'address', 'addresses') + ' copied.' }) }
    catch { host.notify({ kind: 'info', message: 'This window would not let Stoa copy. Add the Email column and select the addresses in the table.' }) }
  }
  const figure = [plural(shown.length, noun[0], noun[1]) + (shown.length !== rows.length ? ' of ' + rows.length : ''), sumCol && total(shown) ? money(total(shown), data.currency) + ' ' + sumCol.label.toLowerCase() : ''].filter(Boolean).join('  ·  ')
  const head = h('thead', null, h('tr', null, [h('th', { key: '@tick', className: 'stoa-dbtick' },
    h('input', { type: 'checkbox', checked: allTicked, ref: el => { if (el) el.indeterminate = ticked.length > 0 && !allTicked }, title: allTicked ? 'Untick them all' : 'Tick every row shown',
      'aria-label': allTicked ? 'Untick every row' : 'Tick every row shown', onChange: tickAll }))].concat(cols.map(col => h('th', { key: col.key, 'data-num': dbKind(col) === 'number' && col.key !== 'fit' ? '1' : '0', 'data-sorted': look.sort && look.sort.field === col.key ? '1' : '0',
    'aria-sort': look.sort && look.sort.field === col.key ? (look.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
    h('button', { type: 'button', title: 'Sort by ' + col.label.toLowerCase(), onClick: () => sortBy(col.key) }, col.label,
      look.sort && look.sort.field === col.key ? h('span', { 'aria-hidden': true }, look.sort.dir === 'asc' ? '↑' : '↓') : null))))))
  const body = sections.flatMap(section => {
    const folded = Boolean(shut[section.label || '—'])
    const lead = groupCol ? [h('tr', { key: 'g:' + section.label, className: 'stoa-dbgroup', onClick: () => setShut({ ...shut, [section.label || '—']: !folded }) },
      h('td', { colSpan: cols.length + 1 }, h('span', null,
        h('span', { className: 'stoa-sub', 'aria-hidden': true }, folded ? '▸' : '▾'),
        h('b', null, section.label || 'No ' + groupCol.label.toLowerCase()),
        h('span', { className: 'stoa-num' }, [String(section.rows.length), sumCol && total(section.rows) ? money(total(section.rows), data.currency) : ''].filter(Boolean).join('  ·  ')))))] : []
    return lead.concat(folded ? [] : section.rows.map(row => {
      const key = keyOf(row), on = picked.has(key)
      return h('tr', { key, 'data-on': on ? '1' : '0' },
        [h('td', { key: '@tick', className: 'stoa-dbtick' }, h('input', { type: 'checkbox', checked: on, 'aria-label': (on ? 'Untick ' : 'Tick ') + (row.name || 'this row'),
          onChange: () => {}, onClick: event => tick(row, event) }))].concat(cols.map(col => {
          const editor = dbEditor(thing, row, col, pb), here = Boolean(editing) && editing.row === key && editing.col === col.key
          return h(DbCell, { key: col.key, row, col, thing, pb, currency: data.currency, open: { ...open, row: opener(row) }, editor, editing: here,
            patched: (patches[key] || {})[col.key], suggestions: suggestions[col.key],
            edit: () => setEditing({ row: key, col: col.key }),
            done: value => {
              if (value !== undefined) setPatches(now => ({ ...now, [key]: { ...(now[key] || {}), [col.key]: editor.kind === 'choice' ? (editor.options.find(pair => pair[0] === value) || [0, value])[1] : value } }))
              setEditing(null)
            } })
        })))
    }))
  })
  return h('div', { className: 'stoa-dbpage' },
    h('div', { className: 'stoa-dbtop' },
      h('div', { className: 'stoa-tabs', role: 'tablist', 'aria-label': 'What to look through' }, DB_THINGS.map(pair =>
        h('button', { type: 'button', key: pair[0], role: 'tab', 'aria-selected': thing === pair[0], className: 'stoa-tab', 'data-on': thing === pair[0] ? '1' : '0',
          onClick: () => { $db.set({ ...db, thing: pair[0] }); setSaving(null); setShut({}); setEditing(null); setPicked(new Set()); lastPick.current = null } }, pair[0] === 'deals' ? cap(pb.terms.deals) : pair[0] === 'accounts' && pb.group_is_company ? cap(pb.terms.groups) : pair[1],
        h('small', null, String((data[pair[0]] || []).length))))),
      h('span', { className: 'stoa-sub' }, 'Everything on the desk. Press a name to open it, a cell to change it, a box to tick the row. Anything can be selected and copied.')),
    h('div', { className: 'stoa-dbviews', role: 'group', 'aria-label': 'Views' },
      views.map(view => h('button', { type: 'button', key: view.id, className: 'stoa-dbview', 'data-on': current.id === view.id ? '1' : '0', 'data-own': String(view.id).startsWith('@') ? '0' : '1',
        'aria-pressed': current.id === view.id, title: String(view.id).startsWith('@') ? 'A view that comes with Stoa' : 'A view you kept', onClick: () => pick(view) },
      view.name, current.id === view.id && edited ? h('small', null, 'changed') : null)),
      saving === null
        ? [edited && own ? h(Btn, { key: 'update', kind: 'go', title: 'Keep these changes in “' + current.name + '”', onClick: () => keep(current.name, current.id) }, 'Save changes') : null,
          edited || !own ? h(Btn, { key: 'new', kind: edited && !own ? 'go' : 'quiet', title: 'Keep what you are looking at now (its filters, order, grouping and columns) as a view of your own', onClick: () => setSaving('') }, '+  Save as a view') : null,
          edited ? h(Btn, { key: 'reset', kind: 'quiet', onClick: () => pick(current) }, 'Reset') : null,
          own ? h(Btn, { key: 'drop', kind: 'quiet', title: 'Stop keeping this view. Nothing on the desk changes.', onClick: () => drop(current) }, 'Remove view') : null]
        : h('span', { className: 'stoa-dbsave' },
          h('input', { className: 'stoa-field', autoFocus: true, value: saving, placeholder: 'Name this view', 'aria-label': 'The name of the view', maxLength: 60,
            onChange: event => setSaving(event.target.value), onKeyDown: event => { if (event.key === 'Enter' && saving.trim()) keep(saving.trim()); if (event.key === 'Escape') setSaving(null) } }),
          h(Btn, { kind: 'primary', disabled: !saving.trim(), onClick: () => keep(saving.trim()) }, 'Keep'), h(Btn, { kind: 'quiet', onClick: () => setSaving(null) }, 'Cancel'))),
    facets.length > 1 || (facets.length === 1 && facetOn(facets[0].label)) ? h('div', { className: 'stoa-dbfacets', role: 'group', 'aria-label': 'By ' + facetCol.label.toLowerCase() }, facets.map(item =>
      h('button', { type: 'button', key: item.label, className: 'stoa-dbfacet', 'data-on': facetOn(item.label) ? '1' : '0', 'aria-pressed': facetOn(item.label),
        title: facetOn(item.label) ? 'Showing only these. Press to let go.' : 'Show only ' + facetCol.label.toLowerCase() + ' “' + item.label + '”', onClick: () => toggleFacet(item.label) },
      h('span', null, h('b', null, item.label), h('span', { className: 'stoa-num' }, String(item.n))),
      h('i', { style: { width: Math.max(6, Math.round(100 * item.n / most)) + '%' } })))) : null,
    h('div', { className: 'stoa-dbbar' },
      h('input', { className: 'stoa-input', value: look.search, placeholder: 'Search ' + noun[1] + '…', 'aria-label': 'Search', onChange: event => set({ search: event.target.value }) }),
      h(Menu, { label: '+  Filter', title: 'Show only the rows where a column is, contains or is past something' }, columns.map(col => h(MenuItem, { key: col.key, title: col.label, onClick: () => addFilter(col) }))),
      h(Menu, { label: groupCol ? 'Grouped by ' + groupCol.label.toLowerCase() : 'Group', title: 'Put the rows under a heading for each value of one column' },
        [h(MenuItem, { key: '', title: 'No grouping', on: !groupCol, onClick: () => { set({ group: '' }); setShut({}) } })].concat(columns.filter(col => col.type === 'pick' || col.type === 'yes' || col.key === 'fit')
          .map(col => h(MenuItem, { key: col.key, title: col.label, on: look.group === col.key, onClick: () => { set({ group: col.key }); setShut({}) } })))),
      h(Menu, { label: 'Columns  ' + cols.length, title: 'Choose what each row shows' },
        columns.map(col => h('label', { key: col.key, className: 'stoa-dbcheck' },
          h('input', { type: 'checkbox', checked: cols.some(item => item.key === col.key), disabled: col.key === 'name', onChange: () => toggleCol(col.key) }), col.label))
          .concat(look.columns ? [h('div', { key: 'rule', className: 'stoa-menurule' }), h(MenuItem, { key: 'reset', title: 'Back to the usual columns', onClick: () => set({ columns: current.columns && current.columns.length ? current.columns.slice() : null }) })] : [])),
      h('span', { className: 'stoa-grow' }),
      emails.length && !ticked.length ? h(Btn, { kind: 'quiet', title: 'Copy the email addresses of the rows shown', onClick: copyEmails }, 'Copy ' + plural(emails.length, 'email', 'emails')) : null,
      ticked.length ? null : h(Btn, { kind: 'quiet', disabled: !shown.length, title: 'Save the rows and columns shown as a spreadsheet file', onClick: csv }, 'Export CSV')),
    look.filters.length ? h('div', { className: 'stoa-dbfilters' },
      look.filters.map((filter, index) => h(DbFilter, { key: index + ':' + filter.field, filter, columns, rows,
        change: next => set({ filters: look.filters.map((item, at) => (at === index ? next : item)) }), remove: () => set({ filters: look.filters.filter((item, at) => at !== index) }) })),
      h(Btn, { kind: 'quiet', onClick: () => set({ filters: [] }) }, 'Clear filters')) : null,
    rows.length === 0
      ? h('div', { className: 'stoa-box stoa-sub' }, thing === 'people' ? 'Nobody is on the desk yet. People appear here as they are added to a ' + pb.terms.deal + '.' : 'Nothing here yet. It fills in as ' + pb.terms.deals + ' are added.')
      : shown.length === 0
        ? h('div', { className: 'stoa-box stoa-sub' }, 'No ' + noun[1] + ' match what you are looking for.  ',
          h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => set({ filters: [], search: '' }) }, 'Show them all'))
        : [ticked.length ? h(DbBulk, { key: 'bulk', thing, picked: ticked, pb, data, noun, suggestions, emails, onDone: () => {}, clear: () => setPicked(new Set()), exportRows: csv, copyEmails }) : null,
          h('div', { key: 'table', className: 'stoa-dbwrap' }, h('table', { className: 'stoa-db' }, head, h('tbody', null, body)))],
    h('div', { className: 'stoa-dbfoot' }, h('span', { className: 'stoa-num' }, figure),
      h('span', { className: 'stoa-sub' }, look.sort ? 'Sorted by ' + (columns.find(col => col.key === look.sort.field) || { label: '' }).label.toLowerCase() + (look.sort.dir === 'asc' ? ', lowest first' : ', highest first') + '. Press a heading to change it.' : 'Press a heading to sort by it.')))
}

function Page() {
  const query = useDesk()
  const tab = useValue($tab)
  // What is open on a page of its own: a record, a group or a company, in the order they were opened, so Back
  // goes to the one before. Empty: the tab's own list is on screen.
  const [stack, setStack] = useState([])
  const [adding, setAdding] = useState(false)
  const [marketId, setMarketId] = useState('')
  const savedView = useValue($view)
  const [dropping, setDropping] = useState(false)
  const [welcomed, setWelcomed] = useState(() => Boolean(storage && storage.get('welcomed', false)))
  const split = useRef(null)
  const typing = useValue($typing)
  // While the page is on screen, a click that calls for writing goes to its own dock.
  useEffect(() => { onPage = true; $onPage.set(true); return () => { onPage = false; $onPage.set(false); $last.set(null) } }, [])
  // A record picked in the pane beside the desk opens here, on a tab that has room for it.
  const want = useValue($want)
  useEffect(() => {
    if (!want) return
    if (!['today', 'pipeline', 'database', 'cash', 'reports', 'activity'].includes($tab.get())) $tab.set('pipeline')
    setAdding(false)
    setStack(now => (now.length && now[now.length - 1].kind === 'record' && now[now.length - 1].id === want ? now : now.concat([{ kind: 'record', id: want }]).slice(-12)))
    $want.set('')
  }, [want])
  const top = stack.length ? stack[stack.length - 1] : null
  // A record, a group or a company opens at the top of the page.
  useEffect(() => {
    const page = split.current && split.current.closest ? split.current.closest('.stoa-page') : document.querySelector('.stoa-page')
    if (top && page) page.scrollTo({ top: 0 })
  }, [top ? top.kind + ':' + top.id : ''])
  // The form for a new record opens beside the list: bring it clear of the dock if it would sit under it.
  useEffect(() => {
    if (!adding) return undefined
    const timer = setTimeout(() => {
      const box = split.current && split.current.querySelector(':scope > .stoa-box')
      const page = split.current && split.current.closest('.stoa-page')
      if (!box || !page) return
      const frame = page.getBoundingClientRect(), rect = box.getBoundingClientRect()
      const hidden = rect.bottom - (frame.bottom - 150)
      if (hidden > 0) page.scrollBy({ top: Math.min(hidden, Math.max(0, rect.top - frame.top - 12)), behavior: 'smooth' })
    }, 260)
    return () => clearTimeout(timer)
  }, [adding, tab])
  // A desk whose records are never billed has no Cash tab: if that was the last one shown, show Today.
  const cashShown = Boolean(query.data && query.data.playbook && (query.data.playbook.billing !== 'none' || hasClientMoney(query.data)))
  useEffect(() => { if (tab === 'cash' && query.data && !cashShown) $tab.set('today') }, [tab, cashShown, Boolean(query.data)])
  // A map that was not there a moment ago (Hermes has just saved it) is the one to show.
  const mapIds = query.data ? query.data.markets.map(item => item.id).join('|') : null
  const knownMaps = useRef(null)
  useEffect(() => {
    if (mapIds === null) return
    const ids = mapIds ? mapIds.split('|') : []
    if (knownMaps.current) { const fresh = ids.find(id => !knownMaps.current.includes(id)); if (fresh) setMarketId(fresh) }
    knownMaps.current = ids
  }, [mapIds])
  if (!query.data) return h('div', { className: 'stoa stoa-page' }, h(Unavailable, { error: query.error, loading: query.isLoading }))
  const state = query.data, pb = state.playbook, totals = state.totals
  const sign = state.sign || { text: '', place: 'art' }, signed = String(typing ?? sign.text ?? '').trim(), ink = sign.ink || 'auto'
  const day = state.today || { counts: {}, needs: 0 }
  const market = state.markets.find(item => item.id === marketId) || state.markets[0]
  const setTab = name => { $tab.set(name); setStack([]); setAdding(false) }
  const add = () => { if (tab !== 'today' && tab !== 'pipeline') $tab.set('pipeline'); setStack([]); setAdding(true) }
  const push = (kind, id) => {
    if (!id) return
    setAdding(false)
    setStack(now => (now.length && now[now.length - 1].kind === kind && now[now.length - 1].id === id ? now : now.concat([{ kind, id }]).slice(-12)))
  }
  const openRecord = id => push('record', id)
  const openGroup = name => push('group', name)
  const openCompany = name => push('company', name)
  const openClient = name => push('client', name)
  const back = () => setStack(now => now.slice(0, -1))
  const renamed = name => setStack(now => now.slice(0, -1).concat([{ kind: 'group', id: name }]))
  // Where an account or a client is the company, there is one place to look: the group's own page. No separate list of companies.
  const view = (savedView === 'companies' || savedView === 'clients') && pb.group_is_company ? 'groups' : savedView
  const groups = state.groups || []
  const pipes = state.pipelines || []
  const showPipe = id => {
    if (id === state.pipeline) return
    $pipe.set(id === 'main' ? '' : id)
    if (storage) storage.set('pipe', id === 'main' ? '' : id)
    setStack([]); setAdding(false)
    refresh()
  }
  const waiting = (day.counts && day.counts.approvals) || 0
  const welcome = Boolean(state.fresh) && !welcomed
  const skip = () => { setWelcomed(true); if (storage) storage.set('welcomed', true) }
  // Each number says what it counts when you rest on it; the ones that are lists take you there.
  const stats = [
    ['Open', totals.open, cap(pb.terms.deals) + ' still in play: not ' + pb.won.label.toLowerCase() + ', not ' + pb.lost.label.toLowerCase() + '.', 'pipeline'],
    ['Due', totals.due, 'Next steps dated today or earlier. Snoozed ' + pb.terms.deals + ' are left out. Press to see them.', 'today'],
    ['Quiet', totals.stale, 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage. Press to see them.', 'today'],
    ['Mapped', totals.mapped, 'Companies on your market maps.', 'market'],
    ['From the map', totals.tracked, cap(pb.terms.deals) + ' that started on a market map.', ''],
    ['Won', totals.won + (totals.won_value ? ' · ' + money(totals.won_value, pb.currency) : ''), 'Closed as ' + pb.won.label.toLowerCase() + ', all time.', '']]
  const side = adding ? h(NewRecord, { state, close: () => setAdding(false), opened: id => { setAdding(false); openRecord(id) } }) : null
  const tabLabel = { today: 'Today', pipeline: view === 'groups' ? cap(pb.terms.groups) : view === 'companies' ? 'Companies' : view === 'clients' ? 'Clients' : 'Pipeline', database: 'Database', cash: 'Cash', reports: 'Reports', activity: 'Activity' }[tab] || 'Back'
  const before = stack.length > 1 ? stack[stack.length - 2] : null
  const backLabel = !before ? tabLabel : before.kind === 'record' ? ((state.deals.find(item => item.id === before.id) || {}).name || 'Back') : before.id
  const sheet = !top ? null
    : top.kind === 'record' ? h(RecordPage, { key: 'r:' + top.id, id: top.id, state, back, backLabel, openRecord, openGroup, openCompany, openClient })
      : top.kind === 'group' ? h(GroupPage, { key: 'g:' + top.id, name: top.id, state, back, backLabel, openRecord, openClient, onRenamed: renamed })
        : top.kind === 'client' ? h(ClientPage, { key: 'cl:' + top.id, name: top.id, state, back, backLabel, openRecord, openGroup })
          : h(CompanyPage, { key: 'c:' + top.id, name: top.id, state, back, backLabel, openRecord, openGroup })
  const paged = Boolean(sheet) && ['today', 'pipeline', 'database', 'cash', 'reports', 'activity'].includes(tab)
  const nothing = h('div', { className: 'stoa-sub' },
    h('p', { style: { marginTop: 0 } }, 'Nothing on the desk yet. Add a ' + pb.terms.deal + ' with the button above, tell Stoa about one below, drop a list on the page, or ask it to map a market.'),
    h(Btn, { onClick: () => setTab('setup') }, 'See how it works, or look around with sample data'))
  // A file dragged in from the computer lands on the desk, wherever on the page it is let go.
  const carrying = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes('Files')
  return h('div', { className: 'stoa stoa-page', 'data-drop': dropping ? '1' : '0',
    onDragOver: event => { if (carrying(event)) { event.preventDefault(); if (!dropping) setDropping(true) } },
    onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setDropping(false) },
    onDrop: async event => { if (!carrying(event)) return; event.preventDefault(); setDropping(false); for (const one of [...event.dataTransfer.files]) await takeFile(one) } },
  h(Style),
  dropping ? h('div', { className: 'stoa-dropnote' }, 'Drop it on the desk: a CSV becomes a review to approve, anything else goes to Hermes with your next message') : null,
  h('div', { className: 'stoa-wrap' },
    state.tour ? h('div', { className: 'stoa-tour', role: 'status' },
      h('span', null, h('b', null, 'Sample data'), ' · ' + state.tour.title + '. Everyone on it is made up, and your own desk is untouched.'),
      h(Btn, { onClick: () => { setStack([]); tour('leave') } }, 'Leave the tour')) : null,
    pipes.length > 1 ? h('div', { className: 'stoa-pipes', role: 'tablist', 'aria-label': 'Pipelines' }, pipes.map(item =>
      h('button', { type: 'button', key: item.id, role: 'tab', 'aria-selected': item.id === state.pipeline, className: 'stoa-pipe', 'data-on': item.id === state.pipeline ? '1' : '0',
        title: item.title + ': its own ' + 'records and its own playbook', onClick: () => showPipe(item.id) }, item.name, h('small', null, String(item.open))))) : null,
    h('div', { className: 'stoa-head', 'data-sign': welcome || !signed ? '' : sign.place === 'beside' ? 'beside' : 'art' },
      welcome ? null : h('div', { className: 'stoa-headart' },
        signed && sign.place !== 'beside' ? h(NameArt, { text: signed, side: 'right', ink }) : h(Art, { name: 'head', ink })),
      h('div', { className: 'stoa-headmain' },
        // Beside the stoa the name is always here. In place of the stoa it is here only when the page is too narrow for it there.
        signed && !welcome ? h('div', { className: 'stoa-headname', 'data-narrow': sign.place === 'beside' ? '0' : '1' }, h(NameArt, { text: signed, side: 'left', ink })) : null,
        h('div', { className: 'stoa-eyebrow' }, welcome ? 'Stoa' : 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, h(DotFigure, { text: money(totals.open_value, pb.currency) || String(totals.open) }), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open'))),
      h('div', { className: 'stoa-headstats' }, stats.map(item => h('button', { type: 'button', key: item[0], className: 'stoa-stat', title: item[2], 'data-press': item[3] ? '1' : '0',
        onClick: item[3] ? () => setTab(item[3]) : undefined }, h('span', { className: 'stoa-eyebrow' }, item[0]), h('b', null, String(item[1])))))),
    h('div', { className: 'stoa-rule' }),
    h('div', { className: 'stoa-tabsrow' },
      h('div', { className: 'stoa-tabs', role: 'tablist' }, [['today', 'Today', day.needs], ['market', 'Market map', totals.mapped], ['pipeline', 'Pipeline', totals.open], ['reports', 'Reports', 0], ['database', 'Database', 0], ['activity', 'Activity', 0]]
        .concat(cashShown ? [['cash', 'Cash', state.cash ? state.cash.strip.overdue.count + (state.cash.strip.to_bill || { count: 0 }).count : 0]] : [])
        .concat([['setup', 'Setup', 0]]).map(item =>
        h('button', { type: 'button', key: item[0], role: 'tab', 'aria-selected': tab === item[0], className: 'stoa-tab', 'data-on': tab === item[0] ? '1' : '0',
          onClick: () => setTab(item[0]) }, item[1], item[2] ? h('small', null, String(item[2])) : null))),
      (tab === 'today' || tab === 'pipeline') && !welcome ? h(Btn, { onClick: add }, '+  New ' + pb.terms.deal) : null),
    waiting && tab !== 'today' && tab !== 'setup' ? h('button', { type: 'button', className: 'stoa-waiting', onClick: () => setTab('today') },
      h('b', null, String(waiting)), ' waiting for your approval', h('span', { className: 'stoa-sub' }, '  ·  see Today  ›')) : null,
    (tab === 'today' || tab === 'pipeline') && welcome && !waiting ? h(Welcome, { skip, ink }) : null,
    paged ? h('div', { ref: split }, sheet) : null,
    !paged && tab === 'today' && (!welcome || waiting) ? h('div', { className: 'stoa-split', ref: split, 'data-side': side || state.deals.length ? '1' : '0' },
      state.deals.length || waiting || (day.to_bill || []).length ? h(Today, { state, openId: '', setOpenId: openRecord, openClient }) : nothing,
      side || (state.deals.length ? h(Ahead, { state, setOpenId: openRecord }) : null)) : null,
    !paged && tab === 'pipeline' && (!welcome || waiting) ? h('div', null,
      h(Billing, { key: state.pipeline || 'main', state }),          // each pipeline its own: a form left open does not follow the page to another
      state.deals.length && view === 'board' ? h(Funnel, { state }) : null,
      state.deals.length || groups.length ? h('div', { className: 'stoa-viewrow' },
        h('div', { className: 'stoa-grow', style: { maxWidth: 760 } }, h(Filters, { state })),
        h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How the pipeline is shown' },
          [['board', 'Board']].concat(pb.group_is_company ? [] : [['clients', 'Clients']]).concat([['groups', cap(pb.terms.groups || 'groups')]])
            .concat(pb.group_is_company ? [] : [['companies', 'Companies']]).map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 26, padding: '0 11px', fontSize: 12 },
            'data-on': view === item[0] ? '1' : '0', 'aria-pressed': view === item[0], onClick: () => $view.set(item[0]) }, item[1])))) : null,
      h('div', { className: 'stoa-split', ref: split, 'data-side': side ? '1' : '0' },
        view === 'clients' && !pb.group_is_company ? h(Clients, { state, openClient })
          : view === 'groups' && (state.deals.length || groups.length) ? h(Groups, { state, current: '', openGroup })
          : !state.deals.length ? nothing : view === 'companies' ? h(Companies, { state, current: '', openCompany })
            : h(Board, { state, openId: '', setOpenId: openRecord }), side)) : null,
    !paged && tab === 'database' ? h(Database, { state, openRecord, openGroup, openCompany, openMap: id => { setMarketId(id); setTab('market') } }) : null,
    !paged && tab === 'cash' ? h('div', { className: 'stoa-split', ref: split, 'data-side': '0' },
      h(Cash, { state, openId: '', setOpenId: openRecord, openClient })) : null,
    tab === 'market' ? (market
      ? h(MarketMap, { market, markets: state.markets, onMarket: setMarketId, shape: state.playbook.shape, openRecord })
      : h('div', { style: { maxWidth: 720 } }, h(NewMap, { first: true, shape: state.playbook.shape }))) : null,
    !paged && tab === 'reports' ? h(Reports, { state, openRecord }) : null,
    !paged && tab === 'activity' ? h('div', { style: { maxWidth: 800 } }, state.activity.length ? state.activity.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default', alignItems: 'center' } },
      h('i', { className: 'stoa-dot', style: { marginTop: 0 }, 'data-tone': entry.kind === 'stage' && !entry.undone ? 'good' : '' }),
      h('span', { className: 'stoa-grow', style: entry.undone ? { opacity: 0.55 } : undefined },
        state.deals.some(item => item.id === entry.deal)
          ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', onClick: () => openRecord(entry.deal) }, entry.name)
          : h('span', { className: 'stoa-name' }, entry.name), h('span', { style: entry.undone ? { textDecoration: 'line-through' } : undefined }, '  ' + entry.text),
        h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + (entry.undone ? 'taken back' : entry.kind) + (entry.by ? ' · ' + entry.by : ''))),
      entry.can_undo ? h(Btn, { kind: 'quiet', title: 'This is the last change on ' + entry.name + ': put it back as it was', onClick: () => undo(entry.deal, entry.op) },
        h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Undo')) : null,
      entry.can_restore ? h(Btn, { kind: 'quiet', title: entry.name + ' was taken off the desk, not destroyed', onClick: () => putBack(entry.deal) }, 'Put back') : null))
      : h('p', { className: 'stoa-sub' }, 'Everything that happens on the desk is logged here: who did it, when, and what changed.')) : null,
    tab === 'setup' ? h(Setup, { state, go: what => { if (what === 'add') add() } }) : null),
  h(Dock, { state }),
  h(SetupFlow))
}

// -- drawn inside a reply: ::stoa-card{id="…"} and ::stoa-map{market="…"} ------------------------
// Attributes are model output: checked, and nothing is drawn when they do not name something real.
// -- parts drawn in a conversation ------------------------------------------------------------------------
// The agent writes a line such as ::stoa-card{id="..."} and the app draws the live part there. On a desk with more than
// one pipeline the line also names its pipeline (pipeline="p2"), and the part shows that pipeline whatever the page is
// showing: a chat with a Bot that keeps one client's pipeline, say. A line without one (an older chat) is the page's.
const pipeAttr = attrs => {
  const pipe = String((attrs && attrs.pipeline) || '').trim()
  return /^(main|p[0-9]{1,3})$/.test(pipe) ? pipe : null
}

/** The name of the pipeline a desk state is for, when the desk has more than one ('' otherwise). */
function pipeName(state) {
  const pipes = (state && state.pipelines) || []
  if (pipes.length < 2) return ''
  const found = pipes.find(item => item.id === state.pipeline)
  return found ? found.name : ''
}

/** Open the Stoa page on one tab, showing the pipeline a part drawn in a chat is about. */
function openDesk(tab, pipe) {
  if (pipe) {
    const id = pipe === 'main' ? '' : pipe
    if ($pipe.get() !== id) { $pipe.set(id); if (storage) storage.set('pipe', id); refresh() }
  }
  $tab.set(tab)
  host.navigate('/stoa')
}

// Today in a chat: the lists about records, with the same buttons as the page. What needs the page (lists waiting for
// approval, months to bill) is counted at the foot, with a way there.
const CHAT_DAY = ['overdue', 'due_today', 'drafts', 'invoices_overdue', 'to_invoice', 'quiet', 'new', 'unplanned']
const CHAT_ROWS = 5

function TodayInChat({ attrs, sessionId }) {
  const pipe = pipeAttr(attrs)
  const query = useDesk(pipe)
  const [openId, setOpenId] = useState('')
  const [form, setForm] = useState(null) // the row with its "what comes next" form open: { id, mode }
  const [busy, setBusy] = useState('')
  const state = query.data
  if (!state || !state.today || !state.playbook) return null
  const pb = state.playbook, day = state.today, counts = day.counts || {}
  const here = sessionId ?? null
  const byId = new Map((state.deals || []).map(deal => [deal.id, deal]))
  const run = (id, body) => { setBusy(id); return act(id, body, here, pipe).then(() => setBusy('')) }
  const facts = dayFacts(pb)
  const name = pipeName(state)
  const where = name ? ' (the ' + name + ' pipeline)' : ''
  const snooze = deal => h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })
  const moves = {
    overdue: deal => [h(Btn, { key: 'done', kind: 'go', title: 'Log it as done and say what comes next', onClick: () => setForm({ id: deal.id, mode: 'done' }) }, '✓  Done'),
      h(Btn, { key: 'draft', title: 'Hermes drafts what this step needs', onClick: () => draftFor(deal, false, here, pipe, where) }, 'Draft it'), snooze(deal)],
    drafts: deal => [h(Btn, { key: 'write', kind: 'go', onClick: () => toChat('Do the writing task waiting on ' + deal.name + ' on the Stoa desk' + where + ' (record ' + deal.id + ').', here) }, 'Write it now')],
    invoices_overdue: (deal, row) => [
      h(Btn, { key: 'paid', kind: 'go', title: 'Everything still owed came in', onClick: () => bill(deal.id, { verb: 'pay', invoice: String(row.invoice) }, pipe) }, 'Paid'),
      h(Btn, { key: 'remind', title: 'Hermes drafts a payment reminder', onClick: () => toChat('On the Stoa desk' + where + ', ' + deal.name + ' (record ' + deal.id + ') has an invoice'
        + (row.number ? ' #' + row.number : '') + ' with ' + exact(row.owed, pb.currency) + ' still owed, ' + row.days + ' days past due. Draft a short, polite payment reminder and show me.', here) }, 'Draft a reminder'),
      snooze(deal)],
    to_invoice: deal => [h(Btn, { key: 'open', kind: 'go', title: 'Open the record: its invoices are at the foot', onClick: () => setOpenId(deal.id) }, 'Add invoice'), snooze(deal)],
    quiet: deal => [h(Btn, { key: 'draft', kind: 'go', title: 'Hermes drafts a follow-up from the record and its log', onClick: () => draftFor(deal, true, here, pipe, where) }, 'Draft a nudge'), snooze(deal)],
    new: deal => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the next step')],
    unplanned: (deal, row) => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the ' + (row.delivery || 'next').toLowerCase() + ' step')]
  }
  moves.due_today = moves.overdue
  const lists = CHAT_DAY.map(key => ({ ...TODAY_LISTS.find(item => item.key === key), rows: day[key] || [] })).filter(list => list.rows.length)
  const row = (list, item) => {
    const deal = byId.get(item.id)
    if (!deal) return null
    const [sub, right] = facts[list.key](deal, item)
    const opened = openId === deal.id
    return h('div', { key: list.key + deal.id + ':' + (item.invoice || ''), className: 'stoa-trow', 'data-open': opened ? '1' : '0' },
      h('button', { type: 'button', className: 'stoa-tmain', title: opened ? 'Close the record' : 'Open the record here', 'aria-expanded': opened, onClick: () => setOpenId(opened ? '' : deal.id) },
        h('i', { className: 'stoa-dot', 'data-tone': list.tone }),
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name,
            deal.value ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ' + money(deal.value, pb.currency)) : null),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub))),
      right ? h('span', { className: 'stoa-num', 'data-tone': list.tone }, right) : null,
      h('span', { className: 'stoa-tacts' }, (moves[list.key] || moves.new)(deal, item)),
      form && form.id === deal.id ? h(NextStep, { deal, mode: form.mode, close: () => setForm(null) }) : null,
      opened ? h('div', { className: 'stoa-chatrec' }, h(Record, { id: deal.id, state, sessionId: here, close: () => setOpenId('') })) : null)
  }
  const elsewhere = [
    counts.approvals ? plural(counts.approvals, 'list waiting for your approval', 'lists waiting for your approval') : '',
    (day.to_bill || []).length ? plural(day.to_bill.length, 'month to bill', 'months to bill') : '',
    (day.client_overdue || []).length ? plural(day.client_overdue.length, 'client invoice overdue', 'client invoices overdue') : '',
    counts.coming ? counts.coming + ' coming up this week' : '',
    counts.snoozed ? counts.snoozed + ' snoozed' : ''].filter(Boolean)
  return h(InPipe.Provider, { value: pipe }, h('div', { className: 'stoa stoa-box stoa-chatwide stoa-chatday' }, h(Style),
    h('div', { className: 'stoa-chathead' },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, 'Today' + (name ? ' · ' + name : '') + (state.tour ? ' · sample data' : '')),
        h('div', { className: 'stoa-name', style: { fontSize: 15, marginTop: 2 } },
          day.needs ? plural(day.needs, 'thing needs you', 'things need you') : 'Nothing needs you today'))),
    lists.length ? lists.map(list => h('section', { key: list.key, className: 'stoa-chatlist' },
      h('div', { className: 'stoa-chatlisthead', title: list.help },
        h('b', { className: 'stoa-foldcount', 'data-tone': list.tone || '' }, String(counts[list.key] || list.rows.length)),
        h('span', { className: 'stoa-eyebrow' }, list.key === 'to_invoice' ? pb.won.label + ', not invoiced' : list.title)),
      list.rows.slice(0, CHAT_ROWS).map(item => row(list, item)),
      list.rows.length > CHAT_ROWS ? h('div', { className: 'stoa-sub', style: { padding: '4px 0 0 23px' } }, 'and ' + (list.rows.length - CHAT_ROWS) + ' more on the desk') : null))
      : h('p', { className: 'stoa-sub', style: { margin: 0 } }, (state.deals || []).length ? 'No next step is due and nobody has gone quiet.' : 'The desk is empty.'),
    h('div', { className: 'stoa-chatfoot' },
      h('span', { className: 'stoa-sub' }, elsewhere.join(' · ')),
      h(Btn, { kind: 'quiet', onClick: () => openDesk('today', pipe) }, 'Open the desk  ›'))))
}

// One report in a chat: its chart, worked out from the desk each time it is drawn, the records behind a bar, and Keep or
// Discard while it is a draft Hermes made.
const CHAT_PERIODS = [['30', 'Last 30 days'], ['90', 'Last 90 days'], ['quarter', 'This quarter'], ['year', 'This year'], ['all', 'All time']]

function ReportInChat({ attrs, sessionId }) {
  const pipe = pipeAttr(attrs)
  const asked = String((attrs && attrs.id) || '').trim()
  const valid = /^[a-z0-9][a-z0-9-]{0,48}$/i.test(asked)
  const [id, setId] = useState(asked)
  const [period, setPeriod] = useState(() => (CHAT_PERIODS.some(item => item[0] === (attrs && attrs.period)) ? attrs.period : '90'))
  const [sel, setSel] = useState('')
  const [openId, setOpenId] = useState('')
  const [busy, setBusy] = useState(false)
  const [gone, setGone] = useState(false)
  const desk = useDesk(pipe)
  const own = pipe ? { pipeline: pipe } : {}
  const query = useQuery({ queryKey: ['stoa', 'report', 'in', pipe || '', id, period],
    queryFn: () => rest('/reports/' + encodeURIComponent(id) + '?period=' + encodeURIComponent(period) + '&compare=1', pipe ? own : undefined),
    enabled: valid && !gone, staleTime: 1500, retry: false })
  if (!valid) return null
  const frame = (...children) => h(InPipe.Provider, { value: pipe }, h('div', { className: 'stoa stoa-box stoa-chatwide stoa-rep' }, h(Style), ...children))
  if (gone) return frame(h('p', { className: 'stoa-sub', style: { margin: 0 } }, 'Discarded. Nothing on the desk was touched.'))
  const result = query.data, state = desk.data
  if (query.error && !result) return frame(h('p', { className: 'stoa-sub', style: { margin: 0 } }, 'That report is not on the desk any more.'))
  if (!result || !state || !state.playbook) return frame(h('p', { className: 'stoa-sub', style: { margin: 0 } }, 'Working it out…'))
  const pb = state.playbook, here = sessionId ?? null
  const press = async verb => {
    setBusy(true)
    try {
      const out = await rest('/reports/' + encodeURIComponent(result.id), { method: 'POST', body: { verb }, ...own })
      if (out && out.ok === false) host.notify({ kind: 'warning', message: out.error || 'That could not be done.' })
      else if (verb === 'discard') setGone(true)
      else {
        if (out && out.id && out.id !== id) setId(out.id)
        host.notify({ kind: 'success', message: 'Kept. It is on the Reports tab.' })
      }
      refresh()
    } catch (error) {
      host.notifyError(error, 'Stoa could not change the report')
    }
    setBusy(false)
  }
  const picked = (result.rows || []).find(item => item.key === sel && (item.records || []).length) || null
  const name = pipeName(state)
  return frame(
    h('div', { className: 'stoa-chathead' },
      h('div', { className: 'stoa-grow', style: { minWidth: 0 } },
        h('div', { className: 'stoa-eyebrow' }, 'Report' + (name ? ' · ' + name : '')),
        h('div', { className: 'stoa-name', style: { fontSize: 15, marginTop: 2 } }, result.title),
        result.line ? h('div', { className: 'stoa-sub' }, result.line) : null),
      result.period ? h('select', { className: 'stoa-select', 'aria-label': 'Period', value: period, onChange: event => { setPeriod(event.target.value); setSel('') } },
        CHAT_PERIODS.map(item => h('option', { key: item[0], value: item[0] }, item[1]))) : null),
    result.draft ? h('div', { className: 'stoa-repdraft', role: 'status', style: { marginBottom: 10 } },
      h('span', null, h('b', null, result.replaces ? 'Hermes made a new version of a report.' : 'Hermes made this report.'), ' It is a draft until you keep it.'),
      h('div', null,
        h(Btn, { kind: 'go', disabled: busy, onClick: () => press('keep') }, result.replaces ? 'Keep it in place of the old one' : 'Keep it'),
        h(Btn, { disabled: busy, onClick: () => press('discard') }, 'Discard'))) : null,
    result.empty ? h('p', { className: 'stoa-sub', style: { margin: 0 } }, result.empty) : h(RepBody, { result, pb, sel, onPick: key => { setSel(key); setOpenId('') } }),
    result.empty ? null : h('div', { style: { marginTop: 8 } }, h(RepLegend, { result })),
    h(RepSays, { says: result.says }),
    picked ? h('div', { className: 'stoa-chatlist' },
      h('div', { className: 'stoa-chatlisthead' }, h('span', { className: 'stoa-eyebrow' }, 'The ' + plural(picked.records.length, pb.terms.deal, pb.terms.deals) + ' behind ' + picked.label)),
      picked.records.slice(0, 8).map(item => h('div', { key: item.id, className: 'stoa-trow', 'data-open': openId === item.id ? '1' : '0' },
        h('button', { type: 'button', className: 'stoa-tmain', 'aria-expanded': openId === item.id, onClick: () => setOpenId(openId === item.id ? '' : item.id) },
          h('i', { className: 'stoa-dot', 'data-tone': '' }),
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, item.name),
            h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, [item.stage, item.company || item.group].filter(Boolean).join(' · ')))),
        h('span', { className: 'stoa-num' }, result.unit === 'days' ? item.days + 'd' : money(item.value, pb.currency) || ''),
        openId === item.id ? h('div', { className: 'stoa-chatrec' }, h(Record, { id: item.id, state, sessionId: here, close: () => setOpenId('') })) : null)),
      picked.records.length > 8 ? h('div', { className: 'stoa-sub', style: { padding: '4px 0 0 23px' } }, 'and ' + (picked.records.length - 8) + ' more in the report on the desk') : null) : null,
    h('div', { className: 'stoa-chatfoot' },
      h('span', { className: 'stoa-sub' }, (result.rows || []).some(item => (item.records || []).length) && !picked ? 'Press a bar\'s name to see what is behind it.' : ''),
      h('span', { className: 'stoa-btns', style: { margin: 0 } },
        h(Btn, { kind: 'quiet', title: 'Sends these figures to Hermes in this chat', onClick: () => toChat('Read the Stoa report "' + result.title + '" with gtm_report (action "read", report "'
          + result.id + '", period "' + period + '"' + (name ? ', pipeline "' + name + '"' : '') + ') and tell me what stands out and what you would do about it.', here) }, 'Ask about it'),
        h(Btn, { kind: 'quiet', onClick: () => { setRep({ open: result.id, list: false, making: false, period }); openDesk('reports', pipe) } }, 'Open on the desk  ›'))))
}

function CardInChat({ attrs, sessionId }) {
  const pipe = pipeAttr(attrs)
  const query = useDesk(pipe)
  const id = String((attrs && attrs.id) || '').trim()
  if (!id || id.length > 80 || /[\s/\\]/.test(id) || !query.data) return null
  return h(InPipe.Provider, { value: pipe }, h('div', { className: 'stoa stoa-inchat' }, h(Style), h(Record, { id, state: query.data, sessionId: sessionId ?? null })))
}

function MapInChat({ attrs, sessionId }) {
  const pipe = pipeAttr(attrs)
  const query = useDesk(pipe)
  const want = String((attrs && attrs.market) || '').trim().toLowerCase()
  if (!query.data || want.length > 120) return null
  const markets = query.data.markets
  const market = markets.find(item => item.id === want || item.name.toLowerCase() === want) || (want ? null : markets[0])
  if (!market) return null
  return h(InPipe.Provider, { value: pipe }, h('div', { className: 'stoa stoa-box', style: { margin: '6px 0', maxWidth: 760 } }, h(Style),
    h(Market, { market, sessionId: sessionId ?? null, compact: true })))
}

function ReviewInChat({ attrs }) {
  const id = String((attrs && attrs.id) || '').trim()
  if (!/^in-[0-9a-f]{8}$/.test(id)) return null
  return h('div', { className: 'stoa', style: { margin: '6px 0', maxWidth: 620 } }, h(Style), h(Review, { id }))
}

export default {
  id: 'stoa',
  name: 'Stoa',
  register(ctx) {
    storage = ctx.storage
    try { $pipe.set(String((storage && storage.get('pipe', '')) || '')) } catch { /* no storage: the first pipeline */ }
    // Every request names the pipeline the page is showing (an opaque id such as p2, never its name).
    // A part drawn in a chat may name its own: rest(path, { pipeline: 'p2', ... }). 'main' is the first.
    rest = (path, options) => {
      const { pipeline, ...rest_ } = options || {}
      const pipe = typeof pipeline === 'string' ? pipeline : $pipe.get()
      return ctx.rest(pipe && pipe !== 'main' ? path + (path.includes('?') ? '&' : '?') + 'pipeline=' + encodeURIComponent(pipe) : path, options ? rest_ : options)
        .then(answer => seenAnswer(path, pipe, answer))        // how the pipeline is paid for is noted, and a contract pipeline's fees are left out
    }
    try { $folds.set({ ...(storage.get('folds', {}) || {}) }) } catch { /* no storage: every group starts closed */ }
    for (const lane of Object.keys(LANES)) {
      const was = (ctx.storage.get(LANES[lane].key, {}) || {}).model
      if (was && typeof was.model === 'string' && typeof was.provider === 'string') setModel({ [lane]: { model: was.model, provider: was.provider, ...(typeof was.effort === 'string' && was.effort ? { effort: was.effort } : {}) } })
    }
    const lastRep = ctx.storage.get('reports', {}) || {}
    if (['30', '90', 'quarter', 'year', 'all'].includes(lastRep.period)) $rep.set({ ...$rep.get(), period: lastRep.period, compare: lastRep.compare !== false })
    const kept = ctx.storage.get('filter', {}) || {}      // an older version kept every filter, a picked-out stage included: take only these two
    $filter.set({ ...NO_FILTER, owner: typeof kept.owner === 'string' ? kept.owner : '', group: typeof kept.group === 'string' ? kept.group : '' })
    // While the app is open the desk knows it can draw cards and maps in the conversation here.
    const hello = () => { ctx.rest('/hello', { method: 'POST', body: {} }).catch(() => {}) }
    hello()
    ctx.setInterval(hello, 30000)
    // The agent, a Slack click or another window changed the desk: redraw now, not at the next poll.
    ctx.onEvent('plugin.stoa.changed', refresh)
    // The dock on the page: hear its own conversation, and nobody else's.
    const mine = event => Boolean(sessions[talking.lane].runtime) && $talk.get().busy
      && String((event && (event.session_id || event.sessionId)) || '') === sessions[talking.lane].runtime
    ctx.onEvent('message.delta', event => {
      const text = event && event.payload && event.payload.text
      if (mine(event) && typeof text === 'string') setTurn(last => ({ reply: last.reply + text, note: '' }))
    })
    ctx.onEvent('tool.start', event => {
      if (mine(event)) setTurn(last => ({ note: 'Working on the desk…', reply: last.reply && !last.reply.endsWith('\n') ? last.reply + '\n\n' : last.reply }))
    })
    // A model that will not take the thinking level picked for a conversation refuses every turn in it, and what
    // Hermes then says points at commands the talk box does not run. The box adds the way out that is on this page.
    const wayOut = text => {
      const pick = $model.get()[talking.lane]
      return pick && pick.effort && /reasoning[ _]effort|thinking (level|budget)|reasoning\.effort/i.test(String(text || ''))
        ? 'This conversation is set to think at “' + pick.effort + '”, which this model did not accept. Open the model menu ' + LANES[talking.lane].where + ', choose another level on the model\'s row, and send it again.' : ''
    }
    ctx.onEvent('message.complete', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      const failed = typeof payload.error === 'string' ? payload.error : ''
      setTurn(last => {
        const final = [payload.text, payload.rendered].find(value => typeof value === 'string' && value.trim()) || last.reply
        return { note: '', reply: final || (failed ? '' : 'Done.'), error: failed, hint: wayOut(final + ' ' + failed) }
      })
      setTalk({ busy: false })
      refresh()
    })
    ctx.onEvent('error', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      const said = String(payload.message || payload.error || 'Hermes could not answer that.')
      setTurn({ note: '', error: said, hint: wayOut(said) })
      setTalk({ busy: false })
    })
    ctx.registerMany([
      { id: 'pane', area: 'panes', title: 'Stoa', data: { placement: 'right', width: '340px' }, render: () => h(Pane) },
      { id: 'page', area: 'routes', data: { path: '/stoa' }, render: () => h(Page) },
      { id: 'nav', area: 'sidebar.nav', data: { path: '/stoa', label: 'Stoa', codicon: 'graph' } },
      { id: 'card', area: 'transcript.directives', data: { name: 'stoa-card', render: props => h(CardInChat, props) } },
      { id: 'map', area: 'transcript.directives', data: { name: 'stoa-map', render: props => h(MapInChat, props) } },
      { id: 'intake', area: 'transcript.directives', data: { name: 'stoa-intake', render: props => h(ReviewInChat, props) } },
      { id: 'today', area: 'transcript.directives', data: { name: 'stoa-today', render: props => h(TodayInChat, props) } },
      { id: 'report', area: 'transcript.directives', data: { name: 'stoa-report', render: props => h(ReportInChat, props) } },
      { id: 'open', area: 'palette', data: { id: 'stoa.open', label: 'Stoa: open the desk', keywords: ['pipeline', 'gtm', 'market map'], run: () => host.navigate('/stoa') } }
    ])
  }
}
