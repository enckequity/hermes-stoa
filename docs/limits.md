# Limits

- **If you run scheduled jobs against the desk, use 0.13.2 or later.** Earlier versions did not
  recognise a scheduled run as one (Hermes gives it no platform and marks the run another way), so
  the `cron` opt-in in `channels` and the autonomy setting did not apply to it: a job could change
  the desk without asking. Found by running a real scheduled job; fixed in 0.13.2.
- **Pipelines are separate.** A record does not move from one to another, and no view or report
  adds them together except "what needs me today". `/gtm` and `!gtm`, and a scheduled job that does
  not name a pipeline, are about the first one. A market map drawn inside a chat shows only while
  the page is on that map's pipeline.
- **A file dropped in the talk box is copied by Hermes, and Stoa clears only its own.** Hermes keeps
  the copy in its `attachments` (or `images`) folder on the machine it runs on, outside Stoa's data
  folder, so it is not in Stoa's export or daily copies and does not go when a record or a
  conversation does. Stoa removes the copies its talk box caused after seven days (change it, or
  remove them at once, under **Setup → Your data**). It cannot tell which other files in those
  folders are whose: ones attached in Hermes's own chat box, or dropped in Stoa before 0.24.1, stay
  until you clear them. After a copy is removed, Hermes cannot read that file again in the
  conversation it was dropped in; drop it again if you need to. A file sent to Hermes in Slack is a
  different matter: Hermes clears those itself within a day.
- **Reports count and add up; they do not do sums of your own.** A report is records or their value,
  grouped one way, over one set of records. There is no formula, no average of a field, and no
  report across pipelines. A bar coloured by how its records ended shows its win rate beside the
  count; other ratios you read off the parts. Money that came in is on the Production tab, not here.
- **"Where records come from" needs a source on each record.** Stoa marks the ones from a market map,
  a list or an import; the rest have one only if you or Hermes gave it. A desk that never recorded
  sources shows one bar, **Not recorded**, until they are filled in (from the report itself, or on
  each record).
- **Time in stage knows what the desk saw.** How long records now in a stage have been there is
  exact. How long the ones that left had stayed is read from the ledger, so a record imported
  half-way through its life starts counting on the day it was imported.
- **"What Hermes did" counts proposals**, the changes a scheduled job leaves for your approval, and
  the call notes filed. What you ask Hermes to do in a chat is done at once and is not counted
  there, and a proposal you altered before approving counts as approved.
- **A report is not sent anywhere on a schedule.** Open it, download it, or ask Hermes about it.
- **The database is a way of looking, not a second store.** It has no formulas and no columns of
  your own beyond the fields your playbook names. A view belongs to one pipeline and to one list
  (accounts, deals or people), and no table adds pipelines together. A cell that is worked out from
  other things (a status, a count, a last touch) cannot be typed into. A change to many rows is made
  one record at a time; a record it does not fit is skipped and named, and each is undone on its own.
- **The panel beside a card needs a pointer.** Resting on a card opens it; on a touch screen there
  is nothing to rest, so press the card and read its page.
- **A record, a map, the day and a report are drawn in a conversation only in the Desktop app and
  the Webapp.** Messaging apps, the phone app and the terminal get the same answer as text.
- **Days are the days of the machine Hermes runs on.** Reports, dates and "today" follow that
  machine's time zone, not the browser's, so a desk hosted in another zone turns over at its
  midnight.
- **The web dashboard tab is set-up and status only.** It shows what is set up, how to set up the
  rest, and the desk in totals. It has no pipeline board, no map and no record: those are on the
  Stoa page in the Desktop app and the Webapp. Its totals name pipelines and maps, never a person or
  a company.
- A search or account that is on hold is marked so and sorted last, but its records still show up
  as due or quiet. Snooze them if you want them out of the way.
- A person is kept once as a contact, but a record's own person is not: the same candidate in two
  searches is two records.
- A scheduled job cannot put someone on a search's or account's side by itself below the highest
  autonomy level, and there is no proposal for it to leave instead; it says so in its report.
- A change you declined is not proposed again for 90 days, even when something new comes in. Make it
  yourself, or clear what was declined under Setup.
- A proposal waits on the page or in a chat ("approve the next steps"). There is no approve button
  for one in Slack yet.
- Buttons are Slack-only. Other platforms get the text card, the text map and `/gtm`.
- In the Desktop app and Webapp the half ships switched off, like every plugin's: turn it on under
  **Capabilities → Plugins**. A click there needs an open chat to send its line into; with none
  open, the task waits on the record.
- If your agent already keeps people somewhere else, it may look there first. Naming the desk ("on the Stoa
  desk") or using `!gtm <name>` settles it.
- A map's cost under **Map facts** is Hermes's estimate for the turns that used the map tool, from
  the day that version was installed. It is not an invoice, it leaves out anything a turn did after
  being interrupted, and a turn that worked on two maps is split evenly between them. A budget stops
  the next company or search, not a turn already running, so a map can end a little over its budget.
- A deleted map is gone: it is not on the ledger and Undo does not bring it back. The daily copies
  of the desk made before it was deleted still hold it.
- On the page's map, the weights under **What matters** and the **Size by** choice are kept with the
  page on that computer, per map. Slack, the downloaded page and Hermes still show Hermes's own fit.
  A reason to call recorded before signals carried dates has none, so the dial places it as a middling one until
  Hermes looks at that company again. On the downloaded page, weights last until the page is reloaded.
- A map is as good as the research behind it. Fit scores are the agent's judgement against your
  brief; the source link is there so you can check.
- One Slack workspace per profile.
- The Google Sheet is a mirror, not the database: it updates when Hermes mirrors it, and edits made
  in it come back when you approve them. Deleting a row in the Sheet does not remove the record.
- After updating the plugin, restart the gateway so the Slack buttons bind to the new code.
- A team shares one desk. There are owners on records, but no per-person permissions.
- A card posted by a scheduled job, or before 0.9, has no one on record as having asked for it:
  list the people who may press such cards in `slack_users`, or ask for the card again.
- The activity sweep is only as good as the tools your Hermes has connected and the model reading
  the mail. It summarises; check the record's log against the email when it matters.
- Setup knows an automation is switched on by its name among Hermes's scheduled jobs. Rename the job
  in Hermes and Setup shows that automation as off. Switching one on still happens in a chat.
  Today, approvals, undo, people and companies are in the Desktop app and Webapp page; the web
  dashboard tab has set-up and totals.
- The autonomy setting governs scheduled jobs. In a chat the agent can still change something you
  did not ask for; the skill tells it to propose instead, and whatever it does can be taken back.
- Undo covers changes made since 0.12, when the ledger began. Changes to a market map, notes on
  a company, and a proposal you declined are not undone by it.
- A record's own person (its name and email) is not yet a shared contact: the same buyer on two
  deals is linked only where they were added as an extra person. The company view shows both.
- The ledger grows for as long as the desk is used. It is small, and nothing prunes it.
- A proposal is as good as the evidence the agent quotes. Read it before you approve.
- Stoa keeps the account of invoices; it does not create, send or collect them, and it has no link
  to an accounting system. One currency per desk (the playbook's). An invoice belongs to one
  record, or to one client billed by the month. "Collected this month" counts payments recorded since 0.13.
- Billing a client by the month is monthly only (not weekly or every two weeks), one rate per
  client, and the suggested hours count weekdays: public holidays and time off are yours to take
  off before recording. Stoa does not track time.
- A field added from an import goes onto the desk's own copy of its playbook, so every pipeline on
  that playbook gets it, and later versions of the shipped playbook no longer replace that copy.
- The forecast is arithmetic, not a prediction: stage odds times amounts, by the dates you set.
- On a tour, what you do is thrown away when you leave, Slack cards posted from it stop working,
  lookups are off, and scheduled jobs do nothing until it ends.
- The models picked under the dock and for map research are kept per computer and apply to the
  page's own two conversations only: Slack, scheduled jobs and other chats stay on Hermes's default. The list shows a price where
  Hermes has one for that provider, which it does not for every provider.
- Notes in a folder are matched to a record by what is written: a name in a file's title, attendees
  at its head, or a person or company named in the text. A recording alone, with no transcript or
  summary exported, cannot be read. A filed note stays filed when its file is edited, saved again,
  renamed or moved, so a change made to a note afterwards is not picked up by itself: ask Hermes to
  go through that note again. Fireflies has been built from its documentation and tested
  against a stand-in, not a live Fireflies account. Reading notes through a connector of Hermes's
  own (Plaud's) is new and has not been run against a real Plaud account.
- The dock on the page runs a turn of your agent's model, the same as typing in a chat. A file
  handed to it goes to Hermes as it would from the chat box, so what Hermes makes of a PDF, a Word
  file or a picture depends on the model and tools your Hermes has. A file over 25 MB, or more than
  six at once, goes through the full chat, which is one click away. Speed (fast mode) is not offered
  in the box's model menu. A thinking level the model does not accept makes it refuse the turn; the
  box then says so and how to change it. Dictation is not available in the Desktop app; the full chat's microphone is.
