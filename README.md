![Stoa. The CRM for Hermes.](docs/banner.png)

# Stoa

**The CRM for [Hermes Agent](https://github.com/NousResearch/hermes-agent): a lightweight one you talk to. Hermes runs it; you approve.**


A two-person sales team does not need a CRM with forty tabs. It needs to know who to sell to,
the pipeline kept honest, the follow-ups written, and the calls turned into next steps, in the
places the team already works. Tell it what happened, send it a list, or press a button, and it
keeps the pipeline. Stoa is that, in one plugin:

**Map the market → find the leads → run the pipeline.**

![The market map: each ring a segment that would get a different pitch, each mark a company, the best fit in the middle](docs/desktop-market.jpg)

## The market map

Say what you sell and who you are after. Hermes names the market, splits it into segments that
would each get a different pitch, researches the companies, checks every one against your
requirements with the evidence and its source, and saves the map. Then it is yours to work:

- **See the market three ways.** *Segments* shows who is where. *Priority* puts fit against how
  fresh the reason to call is, so the top right is who to call first. *Held by* groups companies by
  what they use today, so you can see who you would be replacing.
- **Act on what you see.** Click a company to open it: why it fits, each requirement with its
  evidence, headcount, what it uses today, the buyer, the source. Shift-click or drag to pick
  several. One press adds them to the pipeline, names a buyer at each, or has Hermes draft the first
  note from the evidence.
- **Grow it.** **+ Grow** under a segment, **More like these** on companies you picked, or the
  faint ring beside the map for a neighbouring segment. Hermes finds more and checks each one the
  same way.
- **Adjust it.** Weigh each requirement (off, nice to have, matters, must) and the map re-scores
  on the spot. Add a requirement and Hermes checks every company against it. Move a company to
  another segment, or dismiss it.
- **Know what it holds and what it cost.** **Map facts** shows how complete the map is, where its
  evidence came from, and what Hermes's work on it has cost, by Hermes's own estimate, with paid
  lookups and searches beside it. Set limits there: companies, dollars a month, lookups, searches.
- **Pick the model that researches it.** Map research runs in a conversation of its own, on any
  model Hermes can use, apart from the one the talk box runs on.
- **Hand it to a client.** **Download a copy to share** saves the whole map as one web page with
  the same working map at its head, without buyers' names or your pipeline. Host it or send it.

![Priority: fit against how fresh the reason to call is, with one company open beside it](docs/desktop-market-priority.jpg)

![Several companies picked: add them to the pipeline, draft outreach, or find more like them](docs/desktop-market-picked.jpg)

![Map facts: what is on the map, where it came from, what it has cost, and its limits](docs/desktop-map-facts.jpg)

![The copy to share, as a client sees it: the working map, with no buyers' names and nothing about your pipeline](docs/page-market.jpg)

The pictures show the sample map that comes with the tour; every company in it is made up.

## And the desk behind it

- **Your call notes, wherever they are.** Plaud, Obsidian, Granola, Fireflies, or any recorder that
  saves a file. Point Stoa at the folder (or add a key), see at once which record each note is
  about, and Hermes files it there with the next step it implies.
- **Send it anything.** A PDF of leads, a spreadsheet, a photo of a sign-up sheet. Hermes reads it,
  Stoa shows what it would add and what it would change, and nothing is written until you press Apply.
- **Click a button, the stage changes at once.** No model in the loop, no waiting, no wrong stage.
- **Reports you can trust the arithmetic of.** A Reports tab with a dashboard: pipeline by stage,
  win rate, time in stage, where records come from, why they were lost, what the market map turned
  into, and what Hermes proposed and what you did with it. Stoa counts every figure itself from
  your records, so opening one costs nothing and reads the same each time. Ask for another in
  plain words and Hermes saves it as a draft for you to keep.
- **Hermes proposes, you approve.** What it finds in your email, calls and Slack is logged on the
  right record. Anything more (a stage it thinks should move, a person to add) waits for your yes
  or no with the evidence beside it, a no is remembered, and any change can be taken back.
- **Then Hermes does the writing.** The same click hands the follow-up (the outreach note, the
  proposal email, the decline) to the agent, in the card's thread.
- **Talk to it on the page.** A talk box at the foot of the desk takes plain sentences, runs on
  the model you pick for it, and folds away when you are done.
- **Make it yours.** Type your business name under Setup and it is drawn in dots at the head of
  the desk, lit the way the stoa is: in color on a dark theme, in one ink on a light one, or
  whichever you fix it to.
- **Three shapes, your stages.** A pipeline is for closing deals, filling roles or generating
  leads; you set its stages in your own words (up to seven), on the page or by telling Hermes.
  Underneath, a playbook names the stages, the button labels and what gets drafted at each step,
  and Hermes can write you a whole new one.

It grew out of a setup a recruiting agency runs its searches on every day. This is that idea,
rebuilt as a plugin anyone can install.

```
hermes plugins install stoa                        # from the Hermes plugin catalog
hermes plugins install jarrett-del/hermes-stoa     # or straight from this repository
hermes plugins enable stoa
hermes stoa demo saas     # optional: look around a worked example first (or: hermes stoa demo recruiting)
hermes gateway restart    # so the Slack buttons bind
```

Then, in any Hermes chat: *"Map the market for us"*, *"What's on my plate?"*, or `/gtm`.
In Slack the command is `!gtm` (Slack keeps the slash for its own commands), and
`!gtm Dana Whitfield` or `!gtm map` posts the card or the map itself, buttons included.

![Today: everything that needs you, in groups that open, most pressing first](docs/desktop-today.jpg)

![The pipeline board, here for the recruiting example](docs/desktop-pipeline.jpg)

![Searches: who each one is for, where it stands, and who is on the client's side](docs/desktop-searches.jpg)

![Cash: what came in, what is owed, and what is likely to come in](docs/desktop-cash.jpg)

![Reports: a dashboard counted from your own records, each card with one line on what it shows](docs/desktop-reports.jpg)

## The first five minutes

1. Install and enable it (above) and restart the gateway.
2. In the Desktop app or the Webapp, switch it on under **Capabilities → Plugins → Stoa** and open
   the Stoa page. In a terminal or any chat, `/gtm` works straight away.
3. The first screen offers a tour of sample data (a recruiting agency, or a company selling
   software) or your own desk on the playbook that fits. A tour lives in a file of its own and never
   touches your desk.
4. Tell it something: *"We are starting a Staff Backend search for Northwind. Add Dana Whitfield,
   call her Friday."* Or drop a spreadsheet of leads on the page and approve what it would add.
5. Open **Setup** to see what is connected, add a key for company search or enrichment if you want
   one, set what a scheduled job may do by itself, and see how to work the desk. `/blueprint` switches on the sweeps that keep it current from your email, calls and
   notes; what they find waits for your approval.

## Read more

- **[Setting up Stoa](docs/setup.md)**: one pass from install to keys, Slack and automations.
- **[Using Stoa](docs/guide.md)**: two worked examples, talking to it, and what it looks like in chat.
- **[What is in the plugin](docs/reference.md)**: the tools, market maps, intake, the pipeline,
  approvals and undo, cash, people, searches and accounts, playbooks, and every setting.
- **[The web dashboard tab](docs/dashboard.md)**.
- **[Limits](docs/limits.md)**: what it does not do yet.
- **[Development](docs/development.md)**: tests, and how it is put together.

## Disclosure

What the plugin does that you would want to know before installing it:

- **Network calls.** Only when you add the matching key, and only to four hosts:
  `api.peopledatalabs.com` (the name, email, LinkedIn URL or company of the one record being
  enriched, or a company name and job titles for a buyer lookup), `api.exa.ai` (the search
  phrase for a market-map search), `public-api.granola.ai` (reads your note list and note
  summaries; transcripts are never requested) and `api.fireflies.ai` (reads your recent meetings'
  titles, dates and attendees, and one meeting's summary and action items when it is read; the
  transcript, audio and video are never requested). No telemetry, no usage reporting, no other
  outbound requests. Research the agent does with its own web tools is Hermes's, not the plugin's.
  Pressing **Test** beside a key on the Setup page makes one small request to that key's service:
  a lookup for a made-up address at People Data Labs (it bills a match only, so this is free), one
  search for one result at Exa (billed like any search), the first page of your note list at Granola, or the id of one meeting at Fireflies.
- **Dictation.** The microphone in the page's dock uses your browser's own speech recognition,
  not anything of the plugin's or the agent's. In Chrome that means the audio goes to Google's
  speech service while you dictate. It listens only while the button is on, and the button is not
  shown where the browser has no speech recognition.
- **Paid lookups.** People Data Labs charges per person returned. **Find buyers** on the map asks
  for one person at each company picked that has nobody named, with a job title like the buyers
  already named on that map and a rank of manager or above as People Data Labs records it, so it
  spends at most one credit a company and saves nobody where no one of that rank matches. Every lookup counts against
  `pdl_monthly_cap`, and a buyer lookup returns at most five people.
  A map can carry a lower cap of its own, set under **Map facts**.
- **What a map has cost.** The plugin listens to two of Hermes's hooks, `post_api_request` and
  `post_llm_call`, and takes only counts from them: for a turn that worked on a market map, how many
  model calls it made, their token counts and seconds, and what Hermes itself estimates they cost
  (`agent.usage_pricing.estimate_usage_cost`, Hermes's own price list). No prompt, reply or tool
  result is read or kept. The totals are stored per map in the plugin's own database and shown under
  **Map facts**; they go nowhere else. The dollar figure is an estimate, not an invoice, and a model
  Hermes has no price for shows no figure. Turns on other work are not counted at all.
- **Slack.** Cards are posted and updated through the Slack app Hermes is already connected
  with. The plugin never reads, stores or sees the Slack token. A button moves a stage, applies a
  list and can spend a paid lookup, so a press is taken only from the person the card was posted
  for, or, when you list people in `slack_users`, only from them, and only in a chat the desk is
  available in. Anyone else gets a private "Not allowed" and nothing changes.
- **Credentials.** `PDL_API_KEY`, `EXA_API_KEY`, `GRANOLA_API_KEY` and `FIREFLIES_API_KEY` are read through Hermes's secret scope
  (`agent.secret_scope.get_secret`). The plugin reads no `.env` or credential file. A button click
  or a web-tab request arrives outside any chat turn; on a gateway serving several profiles the
  plugin then installs its own profile's scope around the read, the way Hermes does for its own
  background work (`set_secret_scope(build_profile_secret_scope(home))`), and removes it after.
- **Desktop app and Webapp.** `desktop/plugin.js` is one file with no build step. It imports only
  the Hermes plugin SDK and React and calls the plugin's own backend (`/api/plugins/stoa/`). After a
  click that calls for writing it sends one line into the open chat through `host.composer.submit`,
  the same as typing it. The dock on the page keeps one conversation of its own, named **Stoa
  desk**, and the market map keeps another for its research, named **Stoa map research**: each is created and resumed through the gateway (`host.request`: `session.create`,
  `session.resume`, `prompt.submit`; **New conversation** makes another, with the day and time
  in its name), it is sent only what you type or dictate there or what a button you press on the page asks for (with a short
  note of context on its first message and, on a desk with more than one pipeline, a line saying
  which pipeline the page is showing; both can be read in that conversation), and the page listens
  to the gateway's events for those two conversations alone to show you the reply. The model pickers (under the dock, and **Research:** on the market map) ask Hermes for the models this profile can
  use (`model.options`) and set the one you pick on that conversation alone (`config.set` with
  `--session`, the call the app's own picker makes); the pick is kept in the plugin's page storage
  on that computer, and the profile's default model is never changed. A file is read only
  when you pick it or drop it on the page: a CSV's text goes to the plugin's backend to be held as a
  review; a small text file goes to the agent inside your message; and any other file (a PDF, a Word
  file, a picture, up to 25 MB) is handed to Hermes with your next message through the gateway's own
  attach calls, the ones the app's chat box uses (`file.attach` for a file, `image.attach_bytes` for
  a picture), so it is Hermes that stores and reads it: Hermes keeps its own copy in the profile's
  home folder, in `attachments/` (a picture in `images/`), and nothing of Hermes's clears those two
  folders. So the page tells the plugin's backend the path Hermes gave back, the plugin keeps a list
  of those copies (`handed-files.json` in its data folder: paths, names and sizes, no contents), and
  it removes each after seven days, or the number you set under **Setup → Your data**
  (`files_keep_days`; 0 keeps them until you press **Remove them now** there). The tidy-up runs once
  a day, the first time the desk is opened, like the daily copy. The model
  menu is the app's own (`ModelCatalogMenu`), and a thinking level picked in it is set on that
  conversation with `config.set reasoning`. The picture at the head of the page is drawn in the browser from a grid of
  dots that ships with the plugin (`desktop/art.json`), and a name you type under Setup is kept in the
  plugin's own database (with whether the two are in color, in one ink, or follow the theme) and drawn the same way; neither is sent anywhere. It does not touch the app's
  DOM or other plugins' storage, and it checks in with the backend every 30 seconds while open so
  the agent knows a card can be drawn in the conversation.
- **Web dashboard.** The tab is set-up and status only: it shows what is set up and the desk in
  totals (counts, and the names of pipelines and maps; no person or company), and it has no pipeline
  board or map. It calls only the plugin's own routes under `/api/plugins/stoa/`, behind the
  dashboard's own sign-in: it reads the set-up and the totals, checks and saves the notes folder,
  saves what a scheduled job may change, ends a tour, and hands the pipeline to your browser as a
  CSV download when you press Export. It has no box for a key and makes no call to the dashboard's
  key store: **Open Keys** takes you to the dashboard's own Keys page. Those routes are shared with
  the page in the Desktop app and Webapp, which is what reads and changes the pipeline. They call no
  model and no outside service, except **Test** beside a key (above) and the paid lookups described
  above.
- **Reports.** Every figure on the Reports tab is counted by the plugin from its own database: no
  model is called to make one and nothing is sent anywhere. The reports you make, and which are on
  the dashboard, are kept per pipeline in that pipeline's file. Three presses on the tab send words
  to Hermes, in the talk box where you can read them: **Ask for a report** and **Change it in
  words** send what you typed, and **Ask Hermes about it** asks the agent to read that report, so
  its figures (counts and totals by stage, source, owner and so on; no list of people) go to the
  model you have picked. A report Hermes makes is a draft until you keep it, and a scheduled job
  cannot add or remove one. A report saved as a page is one file with no script that asks nothing
  of the network; it carries figures and the names of sources, owners, stages and groups, and names
  no record. A row saved as a spreadsheet does list its records.
- **The Setup tab.** It shows, to anyone signed in to your Hermes, which chats and Slack members
  the desk is limited to, the folders it reads and writes, where its database file is, and whether
  each key is set. It never shows a key or a token.
  **Add key** and **Change key** open Hermes's own settings page for the plugin, which is where a
  key is typed and saved; Stoa's page has no box for one.
- **Sample data.** A tour writes two things in the plugin's own data folder: `tour.json`, which
  says a tour is on, and one `tour-<id>.db` holding the made-up records. Both are deleted when
  you leave. While they are there the plugin does not read or write your own `desk.db`, apart
  from counting what is in it for the Setup tab.
- **Files outside the plugin's own data.** It reads call notes in the folders you set as
  `notes_dir` (up to eight), each and up to three folders below it: Markdown, text, Word (`.docx`),
  subtitle (`.srt`, `.vtt`), web-page and rich-text files. Listing the notes waiting (on a sweep,
  or when you press **See what is waiting** on Setup) opens the first part of each to see which
  record it names; the rest is read when the agent reads that note. A PDF there is not opened by
  the plugin: its path is given to the agent, which reads it with its own tools. Recordings are
  counted and never opened. By path it reads only `.csv` and `.tsv` files, and only from the plugin's
  own `imports/` folder, the folder you name in `import_dir`, and the working directory when that
  is a project folder; a path the agent passes anywhere else is refused. (At a terminal,
  `hermes stoa import <file>` reads the CSV you name.) It writes nowhere outside its own data
  folder: exports (the pipeline as CSV, a market map as a web page) always go to its `exports/`
  folder, and a file name is all the agent can choose.
  On the Setup page, **Check** counts the note files in a folder you name, and **Look for a folder**
  does the same for a short fixed list of usual places (`~/Documents/Meeting notes`,
  `~/Documents/Meetings`, `~/Documents/Call notes`, `~/Documents/Notes`, `~/Documents/Plaud`,
  `~/Plaud`, `~/Documents/Transcripts`, `~/Documents/Obsidian`, `~/Obsidian`, the iCloud Obsidian
  folder, `~/Documents/Granola`, `~/Notes`), only when you press it. Neither opens a note.
  **Add folder** there stores the folder in the plugin's own `notes_dir` setting, the way Hermes
  saves any plugin setting.
- **Notes in a connector of Hermes's own.** If you list a connector under `notes_connections`
  (for example Plaud's), the plugin does not contact it and holds no access to it. The notes inbox
  tells the agent that notes live there; the agent reads them with that connector's own tools and
  files a short summary on the desk. The plugin stores only the id the agent gives each note, so
  none is filed twice.
- **Hermes's scheduled jobs.** To show which of its automations are switched on, Setup reads the
  name, schedule and on-or-off state of this profile's scheduled jobs. It changes none of them.
- **Messages to the agent.** With `allow_gateway_injection` granted, a button click queues one
  instruction for the agent in the card's thread. The agent drafts; it is told not to send.
- **Stored data.** Names, emails, notes and enrichment results for the people in your pipeline,
  the other people you add to a record or to a search or account (the client's side), what you
  keep on a search or account itself (its client, its terms, its notes), and the invoices and
  payments you record (amounts, numbers and dates; no bank or card details, which Stoa never asks
  for) sit unencrypted in `plugin-data/stoa/desk.db`. Each further pipeline you open has a file of
  its own beside it (`pipe-p2.db` and so on), and `pipelines.json` names them.
- **Daily copies.** Once a day, the first time the desk is opened, a copy of that file is written
  beside it in `plugin-data/stoa/backups/` and the fourteen newest are kept. They are as
  unencrypted as the file itself, and they are on the same disk: they guard against a mistake, not
  against losing the machine.
  Every change is kept on a ledger in that file with the values before and after it, and the
  ledger is never trimmed; a record or a person taken off the desk is archived there, not erased;
  what you declined is kept for 90 days; evidence quoted in a proposal is kept with it. To erase
  someone for good, delete the file or edit it with a SQLite tool. Treat it like the customer
  list it is.
- **What runs without you.** A scheduled job reaches the desk only if you list `cron` in
  `channels`, and then changes only what the autonomy setting allows; the rest is held for you.
- **The only files it deletes outside its own data folder** are those copies: a file is removed
  only if it is on that list, is a plain file directly in this profile's `attachments/` or `images/`
  folder, and is still the size and age it was when Hermes handed back its path. A file from another
  chat is never on the list; one that has changed since is left alone and marked. Your own file,
  where you had it, is never touched.
- **No shell commands, no background processes, no self-updates, no Python dependencies.**

## License

MIT
