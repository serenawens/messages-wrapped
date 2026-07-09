# iMessages Wrapped

## TL;DR

1. Download `build_dashboard.py` and `dashboard_template.html` into the **same folder**.
2. Grant **Full Disk Access** to Terminal (one-time), then restart Terminal.
3. Open Terminal, `cd` into the folder, and run:
   ```bash
   python3 build_dashboard.py
   ```
4. When it finishes, open `iMessage_Dashboard.html` in your web browser (double-click it or use **Open With → Safari/Chrome**).

Your Messages database stays on your Mac the entire time—no data is uploaded or sent anywhere.

---

Generates a private, local HTML dashboard from your Mac's Messages history:
top contacts, top group chats, who's most active in each one, a "who
out-texts who" balance view, activity trends, and your most frequently used
words, phrases, and full messages.

**Your data never leaves your machine.** The script reads `chat.db` locally,
computes everything on your computer, and embeds the results directly into a
single HTML file with no external code libraries, no server, and no network
calls for your data.

## Files in this folder

| File | What it is |
|---|---|
| `build_dashboard.py` | The script you run. Reads your Messages data and generates the dashboard. |
| `dashboard_template.html` | The dashboard's design/logic. Must stay in the same folder as the script — don't rename or move it separately. |
| `README.md` | This file. |
| `iMessage_Dashboard_SAMPLE.html` | A demo filled with fake data, just so you can see what the real thing looks like. Safe to open, not connected to your data. |

## Quick start

**1. Put both files in a folder together.** `build_dashboard.py` and
`dashboard_template.html` need to sit side by side — the script reads the
template file from the same directory it's run from.

**2. Grant Full Disk Access (one-time).** `chat.db` (and your Contacts
database, used for name lookup) are protected files on macOS, so your
terminal needs permission to read them:

1. **System Settings → Privacy & Security → Full Disk Access**
2. Click **+** and add your terminal app (Terminal, iTerm2, etc.)
3. Toggle it on
4. **Quit and reopen** your terminal app — the permission doesn't apply until you restart it

**3. Run the script.** Open Terminal, `cd` into the folder, then:

```bash
python3 build_dashboard.py
```

This reads `~/Library/Messages/chat.db` automatically. It works off a
temporary copy, so your real Messages database is never touched or locked.
If it can't find your database, point it at the file directly:

```bash
python3 build_dashboard.py /path/to/chat.db
```

**4. View iMessages Wrapped.** The script creates `iMessage_Dashboard.html` in the
same folder.

Open it in your web browser using either of these methods:

- Double-click `iMessage_Dashboard.html` in Finder (it should open in your default browser), or
- Right-click → **Open With** → choose Safari, Chrome, Firefox, or another browser.

No web server or internet connection is required—the entire dashboard is a
single local HTML file.

## Naming your contacts

The script looks up real names automatically from your Mac's **Contacts**
app — no manual step needed for anyone already in there. Full Disk Access
(step 2 above) already covers this, so there's nothing extra to grant.

It matches by phone number and email, normalizing formatting differences
(`+1 (555) 123-4567` vs `555-123-4567` vs `+15551234567` all match the same
person). You'll see a line like this when you run it:

```
Looking up names in Contacts...
  Matched 41/47 contacts automatically from Contacts.
  6 contact(s) couldn't be auto-matched. Wrote contacts_template.json --
  fill in names, save as contacts.json, and re-run to relabel just those.
```

That template file only lists the leftovers — people the automatic lookup
couldn't match (usually because the number/email in `chat.db` doesn't match
what's saved in Contacts). For those:

1. Copy `contacts_template.json` to a new file named `contacts.json`
2. Fill in the names:
   ```json
   {
     "+15559999999": "Alex"
   }
   ```
3. Re-run `python3 build_dashboard.py` — it relabels those and re-runs the
   Contacts lookup for everyone else

A few notes:

- `contacts_template.json` is scratch output, rewritten every run — don't
  edit it directly, edit `contacts.json`.
- `contacts.json` always wins, even over the automatic Contacts-app match —
  so you can use it to fix a wrong auto-match too, not just fill in gaps.
- If Contacts can't be read at all (not found, or Full Disk Access wasn't
  granted), everyone falls back to phone number/email, same as before.

### Automatic de-duplication

Two kinds of duplicates get merged automatically, with no setup needed:

- **Same person, multiple numbers/emails** — if someone's phone and email
  both resolve to the same name (via Contacts or your `contacts.json`),
  they're combined into a single contact everywhere: Top 10 People, Balance,
  and group chat sender breakdowns.
- **Same group chat, multiple threads** — if two chat threads have the exact
  same group name (this commonly happens when iMessage/SMS splits one real
  group conversation into separate threads), they're merged into one group
  with the combined participant list and message history.

Both merges are case/whitespace-insensitive ("John Smith" and "john  smith"
count as a match) but otherwise require an exact name match — nothing is
merged on a guess. The console output tells you how many of each were
merged on a given run.

**Worth knowing:** if you've deliberately given two genuinely different
group chats the identical name, they'll get merged too — same tradeoff that
applies to the people-matching. If that happens, renaming one of the group
chats in Messages before re-running will keep them separate.

## What's in iMessages Wrapped

The dashboard is split into three tabs. A date-range picker (year tabs, or
"All time") and the hero stats at the top apply across all of them — except
where noted below. Every section can be collapsed by clicking its header.

**People & Groups** (default tab)
- **Top 10 People** — your most-texted 1:1 contacts, with each bar split
  into sent (teal) vs. received (amber); hover either half for exact counts,
  hover a name for their phone number/email
- **Top 10 Group Chats** — your most active group chats; click one to
  expand a per-person message breakdown
- **Group Chat Spotlight** — your top 3 group chats, with who's most active
  in each and that chat's most common words (words are all-time)
- **Balance** — for each 1:1 contact, exact sent/received counts and who's
  ahead, e.g. "You lead 2.3x"

**Patterns** (date-range dependent)
- **Message Trend** — a weekly-averaged messages-per-day line chart, with
  date labels and a hover tooltip
- **Signal Pattern** — a heatmap of which hours/days you text most, hover
  any tile for the exact count

**All-Time** (not affected by the date-range picker)
- **Year over Year** — a bar chart comparing sent vs. received per calendar
  year, hover a bar for exact counts
- **Vocabulary** — your most frequent words, 2-word phrases, and full
  repeated messages (whitespace-only messages are excluded), each
  filterable by you / everyone else / overall

## Known limitations

- **Rich messages** (edited messages, some tapback reactions, certain styled
  text) are stored in a binary format Apple doesn't document. The script
  does a best-effort decode; a small fraction of these may show up as blank
  in word-frequency stats, though they're still counted as messages.
- **Group chat "who's in it"** reflects everyone who has *ever* been in that
  chat, including people who were later removed.
- Automatic contact matching is normalized to the last 10 digits of a phone
  number, which is US-centric — international numbers or short codes may
  not match even if they're in Contacts, and will fall back to manual entry.
- Name-based de-duplication (contacts and group chats) merges on an exact
  name match — see "Automatic de-duplication" above for the tradeoff.
