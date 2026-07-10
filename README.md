# iMessages Wrapped

A private, local dashboard for your Mac's iMessage history. The script reads
`chat.db` on your machine, computes stats, and embeds everything into HTML
files you open in a browser. **Your data never leaves your Mac** — no server,
no uploads, no external libraries.

## TL;DR

1. **Download the project zip** and unzip it.
2. **Grant Full Disk Access** to your terminal app (one-time), then restart it.
3. **Run** `python3 build_dashboard.py` from the unzipped folder.
4. **Open** `iMessage_Dashboard.html` in your browser.

The three files the script actually needs are `build_dashboard.py`,
`dashboard_template.html`, and `lookup_template.html` — all included in the zip.

Need more detail? See the [full setup guide](#detailed-setup).

## What's in iMessages Wrapped

A date-range picker (All time or a specific year) filters most stats. Sections
can be collapsed by clicking their headers.

**Main dashboard** (`iMessage_Dashboard.html`)

- **People & Groups** — Top 10 people (sent vs. received), top 10 group chats
  (expand for per-person breakdown), group chat spotlight, and a 1:1 balance
  view showing who texts more
- **Texting Habits** — message trend chart, hour/day heatmap, emoji frequency,
  vocabulary (words, 2–5 word phrases, repeated full messages), and longest
  messages
- **Year over Year** — sent vs. received by calendar year
- **Search People & Groups** — link to the lookup page

**Lookup page** (`iMessage_Dashboard_Lookup.html`)

- Search any contact or group chat by name, number, email, or group name
- Per-person: activity trend, reply times, when they text, top words/phrases,
  emoji, shared group chats, Game Pigeon stats
- Per-group: activity trend, who sends most, peak hours, top words/phrases,
  emoji

## Known limitations

- **Rich messages** (edited text, some tapbacks, styled content) use an
  undocumented Apple format. A small fraction may decode as blank in word stats,
  though they're still counted as messages.
- **Group participants** include everyone who has ever been in a chat, even if
  they were later removed.
- **Contact matching** normalizes US phone numbers to the last 10 digits;
  international numbers or short codes may not auto-match.
- **De-duplication** merges contacts or group chats only on an exact name match.
  Two different chats with the same name will be combined.

## Detailed setup

**1. Download and unzip the project.**

Grab the zip from the repo's releases or download page, then unzip it anywhere
on your Mac (e.g. `~/Documents/iMessages-Wrapped`). The zip includes the README,
a sample dashboard, and the three files the script depends on:

- `build_dashboard.py` — reads your Messages data and generates the dashboards
- `dashboard_template.html` — main dashboard layout and logic
- `lookup_template.html` — lookup page layout and logic

All three must stay in the same folder. Don't move or rename them separately.

**2. Grant Full Disk Access (one-time).**

`chat.db` is a protected file on macOS, so your terminal needs permission:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click **+** and add your terminal app (Terminal, iTerm2, etc.)
3. Toggle it on
4. **Quit and reopen** the terminal app — the permission doesn't apply until you
   restart it

**3. Run the script.**

Open Terminal, `cd` into the unzipped folder, then:

```bash
python3 build_dashboard.py
```

This reads `~/Library/Messages/chat.db` automatically via a temporary copy, so
your real database is never locked. If it can't find your database, point it at
the file directly:

```bash
python3 build_dashboard.py /path/to/chat.db
```

**4. Open the output in your browser.**

The script creates two files in the same folder:

- `iMessage_Dashboard.html` — main dashboard
- `iMessage_Dashboard_Lookup.html` — search any person or group chat

Double-click either file, or right-click → **Open With** → Safari/Chrome. No
internet connection required.

To preview the UI without your data, open `iMessage_Dashboard_SAMPLE.html`.

## Extra: naming contacts (optional)

Names are pulled automatically from the Mac **Contacts** app — no setup needed
for people already saved there.

If some numbers still show as raw phone numbers or emails, the script writes
`contacts_template.json` listing only the unmatched ones. To fix them:

1. Copy `contacts_template.json` to `contacts.json`
2. Fill in names:
   ```json
   {
     "+15559999999": "Alex"
   }
   ```
3. Re-run `python3 build_dashboard.py`

`contacts.json` overrides automatic matching, so you can also use it to fix a
wrong auto-match. Edit `contacts.json`, not `contacts_template.json` (that
file is regenerated every run).

The script also auto-merges duplicate contacts (same person, multiple
numbers/emails) and duplicate group threads (same group name, split threads).
