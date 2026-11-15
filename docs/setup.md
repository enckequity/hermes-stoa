# Setting up Stoa

One pass, start to finish. Steps 1 to 4 get you a working desk. Steps 5 to 8 are optional and can
wait until you want them. The Setup tab on the Stoa page walks through the same steps and ticks each
off as it is done.

Stoa needs Hermes Agent 0.23.0 or later.

## 1. Install it

In a terminal on the machine Hermes runs on:

```
hermes plugins install stoa
hermes plugins enable stoa
hermes gateway restart
```

The restart is what makes Hermes load the plugin and bind its Slack buttons. To check it took,
run `hermes stoa status`.

## 2. Switch on the page

Open the Hermes Desktop app or the Webapp.

1. Go to **Capabilities → Plugins**.
2. Find **Stoa** and switch it on. If you just installed or updated it and it is not listed, press
   **Rescan**.
3. **Stoa** now appears in the sidebar. Open it.

If the page says *The desk is not answering*, the plugin is not enabled or the gateway was not
restarted: go back to step 1.

In a terminal or any chat you do not need the page at all. `/gtm` works straight away.

## 3. Say what the desk is for

The first screen offers two things.

- **A tour.** A full pipeline of made-up people and companies (a recruiting agency, or a company
  selling software). It lives in a file of its own and never touches your desk. **Leave the tour**
  ends it.
- **Your own desk.** Pick what the pipeline is for (closing deals, filling roles or generating
  leads), then name its stages in your own words, up to seven. Not sure? Describe your business
  and Hermes sets the stages for you.

You can change the stages later under **Setup → Edit stages**.

## 4. Put something on it

Any of these works:

- **Tell Hermes.** *"We are starting a Staff Backend search for Northwind. Add Dana Whitfield, call
  her Friday."*
- **Drop a list on the page.** A CSV of leads or candidates. Stoa shows what it would add and what
  it would change, and nothing is written until you press **Apply**.
- **Add one by hand.** **+ New** at the top of the page.
- **Map a market.** *"We sell a fraud prevention tool to fintechs and marketplaces. Who should we go
  after?"* Hermes researches it and the map appears on the **Market map** tab, where one press puts
  a company in your pipeline.

That is a working desk. The rest makes it do more.

Steps 5 to 8 are all on **Stoa → Setup**, under **Make it do more**. Each is a line that says where it
stands; press it and it opens onto the way to do it.

## 5. Find companies and fill in people (optional)

Stoa works without any key. Two outside services add something each:

| Key | What it adds | Cost |
|---|---|---|
| **Exa** | A market map searches for companies directly, from a plain description of who you are after | Exa bills each search |
| **People Data Labs** | **Enrich** fills in a person's title, company and LinkedIn, and a map can name the buyers at a company | Bills each match; Stoa stops at a monthly cap you set (50 to begin with) |

To add one:

1. Open the step. Each key's row says how to get one and links to the page where it is made.
2. Copy the key from that service.
3. Press **Add key**. This opens Hermes's own settings for Stoa (also at **Settings → Plugins →
   Stoa**).
4. Paste the key into its box and press **Save settings**.
5. Go back to **Stoa → Setup**. The row now shows the key as set. Press **Test** to have the
   service confirm it.

Hermes keeps the key, in its own settings file. Stoa reads it when it needs it and never shows it.

What **Test** does: at People Data Labs it looks up an address nobody has, which is not billed. At
Exa it runs one search for one result, which Exa bills like any other search.

## 6. Bring in your call notes (optional)

Each note is filed on the record it is about, with the next step it implies. Notes can come from a
folder, from Granola, or both.

**From a folder.** Any folder on the computer Hermes runs on where notes are saved as files ending
in `.md` or `.txt`: an Obsidian folder, or wherever your meeting recorder saves its notes.

1. Open the step and paste the folder's path into the box. On a Mac, to copy a folder's path: in
   Finder, right-click the folder, hold the Option key, and choose **Copy as Pathname**.
2. Press **Check**. Stoa says how many notes it found there, or what is wrong with the path.
3. Press **Save**.

Not sure where your notes are? **Look for a folder** tries a few usual places (Documents,
Obsidian, Notes) and offers any that hold notes. It counts the notes and opens none.

**From Granola.** Add a Granola key the same way as in step 5: in the Granola app, **Settings →
Connectors → API keys → Create new key**. Granola offers API keys on its Business and Enterprise
plans. **Test** reads the first page of your note list.

## 7. Work from Slack (optional)

Slack is connected in Hermes, not in Stoa. Open the step and press **Open Messaging**, choose
Slack, and follow its steps. Once Hermes is in your Slack there is nothing more to do: records
post as cards whose buttons move the pipeline, and `!gtm` shows the board. Try
`!gtm Dana Whitfield` or `!gtm map`.

Two settings decide who can do what, both under **Settings → Plugins → Stoa**:

- **Limit to these chats.** The chat ids the desk answers in. Empty means everywhere.
- **Who may press Slack buttons.** Empty means only the person a card was posted for.

Both take Slack's own ids. For a person: open their profile in Slack, press the three dots, and
choose **Copy member ID**. For a channel: open the channel's details and copy the **Channel ID** at
the bottom.

## 8. Let Hermes keep the desk current (optional)

Seven jobs Hermes can run on a schedule:

- Pipeline morning brief
- File meeting notes
- Log email, call and Slack activity
- Market watch
- Mirror the desk to a Google Sheet
- Bring Sheet edits back to the desk
- Friday pipeline review

Open the step and press **Switch on…** beside one. A chat opens with the command typed. Press
Enter, and Hermes asks when it should run and where to report. Back on Setup, the job now shows as
on, with when it runs next. **Change…** opens Hermes's scheduled jobs, where it is paused, changed
or removed.

Under the list is **What a scheduled job may change without asking**. It starts on **Ask me
first**: a job may log what it finds, and every other change waits for your approval.

The sweep of email, calls and Slack reads them through the tools Hermes itself is connected to. If
it finds nothing, connect them under **Capabilities**.

## Where everything is afterwards

- **Stoa → Setup** shows where things stand: the two steps a desk needs, the four optional ones, and
  under **Your desk** the name on the page, your pipelines, the rules in force and your data with a
  CSV export.
- **Settings → Plugins → Stoa** is where every setting and key is changed.
- **Market map → Download page** saves a map as one web page. **Download a copy to share** saves
  the same page without buyers' names or your pipeline, for hosting or sending to a client.

## If something is off

- **The page says the desk is not answering.** Enable the plugin and restart the gateway (step 1),
  then reload.
- **A key shows as not set after you saved it.** Check you pressed **Save settings**, and that you
  are looking at the same profile you saved it in.
- **Test says the service refused the key.** Copy the key again from the service and save it. A
  missing first or last character is the usual cause.
- **Slack buttons do nothing after an update.** Restart the gateway so they bind to the new code.
- **After updating Stoa the page looks unchanged.** Press **Rescan** under **Capabilities →
  Plugins**.
