#!/usr/bin/env python3
"""
build_dashboard.py

Reads your local iMessage database (~/Library/Messages/chat.db), aggregates
stats, and generates a single self-contained HTML dashboard.

Nothing in this script sends your data anywhere. It only reads the database
on disk and writes an HTML file next to itself.

Usage:
    python3 build_dashboard.py

Requires "Full Disk Access" for your terminal app, since chat.db is a
protected file on macOS:
    System Settings -> Privacy & Security -> Full Disk Access -> add Terminal
    (or iTerm2 / whatever you run this from), then restart the terminal app.
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
CONTACTS_FILE = os.path.join(SCRIPT_DIR, "contacts.json")
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "dashboard_template.html")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "iMessage_Dashboard.html")

APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

STOPWORDS = set("""
the a an and or but if then so to of in on for with at by from as is it its
this that these those i you he she we they me him her us them my your his
its our their im ive youre youve theyre theyve dont don didnt cant wont
wasnt isnt arent werent im ill youll hell shell well theyll id youd hed
shed wed theyd be been being am are was were do does did have has had
will would shall should may might must can could not no yes just like so
about into out up down over under again further here there when where why
how all any both each few more most other some such only own same than
too very s t can will just don now ok okay lol yeah yea ya haha lmao omg
gonna wanna gotta kinda sorta u r ur thats whats hes shes theres
""".split())

TAPBACK_PREFIXES = ("Loved ", "Liked ", "Disliked ", "Laughed at ", "Emphasized ", "Questioned ")


def find_db_path():
    if len(os.sys.argv) > 1:
        return os.sys.argv[1]
    return DEFAULT_DB_PATH


def copy_db(db_path):
    """Copy chat.db (+ -wal/-shm sidecars) to a temp dir to avoid lock issues
    and to avoid touching the real file at all."""
    tmp_dir = tempfile.mkdtemp(prefix="imessage_dash_")
    tmp_db = os.path.join(tmp_dir, "chat.db")
    shutil.copy2(db_path, tmp_db)
    for suffix in ("-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, tmp_db + suffix)
    return tmp_db


def apple_time_to_unix_ms(value):
    if value is None or value == 0:
        return None
    # Newer macOS stores nanoseconds since 2001-01-01; older stores seconds.
    if value > 10**12:
        seconds = value / 1e9
    else:
        seconds = value
    unix_seconds = seconds + APPLE_EPOCH_OFFSET
    return int(unix_seconds * 1000)


def parse_attributed_body(body):
    """Best-effort extraction of plain text from the NSAttributedString
    archive macOS uses for rich messages (edits, some reactions, styled
    text). Not every message will decode perfectly -- that's OK, we fall
    back to an empty string rather than guessing."""
    if not body:
        return ""
    try:
        idx = body.find(b"NSString")
        if idx == -1:
            return ""
        i = idx + len(b"NSString")
        plus_idx = body.find(b"+", i, i + 40)
        if plus_idx == -1:
            return ""
        j = plus_idx + 1
        if j >= len(body):
            return ""
        length_byte = body[j]
        if length_byte == 0x81:
            strlen = int.from_bytes(body[j + 1:j + 3], "little")
            start = j + 3
        elif length_byte == 0x82:
            strlen = int.from_bytes(body[j + 1:j + 5], "little")
            start = j + 5
        else:
            strlen = length_byte
            start = j + 1
        raw = body[start:start + strlen]
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def tokenize_raw(text):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return [w.strip("'") for w in words if 2 <= len(w) <= 20]


def unigrams_from_raw(raw_words):
    return [w for w in raw_words if w not in STOPWORDS]


def bigrams_from_raw(raw_words):
    grams = []
    for i in range(len(raw_words) - 1):
        w1, w2 = raw_words[i], raw_words[i + 1]
        if w1 in STOPWORDS and w2 in STOPWORDS:
            continue
        grams.append(f"{w1} {w2}")
    return grams


def load_contacts_overrides():
    if os.path.exists(CONTACTS_FILE):
        try:
            with open(CONTACTS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def normalize_phone(s):
    """Last-10-digits normalization so '+1 (555) 123-4567', '555-123-4567',
    and '+15551234567' all match each other."""
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) >= 10 else digits


def find_addressbook_dbs():
    base = os.path.expanduser("~/Library/Application Support/AddressBook")
    dbs = []
    main_db = os.path.join(base, "AddressBook-v22.abcddb")
    if os.path.exists(main_db):
        dbs.append(main_db)
    sources_dir = os.path.join(base, "Sources")
    if os.path.isdir(sources_dir):
        for entry in os.listdir(sources_dir):
            p = os.path.join(sources_dir, entry, "AddressBook-v22.abcddb")
            if os.path.exists(p):
                dbs.append(p)
    return dbs


def load_mac_contacts():
    """Best-effort read of the macOS Contacts app so handles can be
    auto-labeled with real names. Returns (phone_lookup, email_lookup),
    both possibly empty if Contacts data isn't present or readable --
    callers should fall back to manual contacts.json entries either way."""
    phone_lookup, email_lookup = {}, {}
    dbs = find_addressbook_dbs()

    for db_path in dbs:
        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="imessage_dash_ab_")
            tmp_db = os.path.join(tmp_dir, "ab.db")
            shutil.copy2(db_path, tmp_db)
            for suffix in ("-wal", "-shm"):
                src = db_path + suffix
                if os.path.exists(src):
                    shutil.copy2(src, tmp_db + suffix)

            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            names = {}
            for r in cur.execute(
                "SELECT Z_PK as pk, ZFIRSTNAME as first, ZLASTNAME as last, "
                "ZORGANIZATION as org FROM ZABCDRECORD"
            ):
                full = " ".join(p for p in [r["first"], r["last"]] if p) or r["org"]
                if full:
                    names[r["pk"]] = full

            for r in cur.execute("SELECT ZOWNER as owner, ZFULLNUMBER as num FROM ZABCDPHONENUMBER"):
                name = names.get(r["owner"])
                if name and r["num"]:
                    phone_lookup[normalize_phone(r["num"])] = name

            for r in cur.execute("SELECT ZOWNER as owner, ZADDRESS as addr FROM ZABCDEMAILADDRESS"):
                name = names.get(r["owner"])
                if name and r["addr"]:
                    email_lookup[r["addr"].lower()] = name

            conn.close()
        except Exception:
            pass  # best-effort -- an unreadable/missing source just contributes nothing
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return phone_lookup, email_lookup


def main():
    db_path = find_db_path()
    if not os.path.exists(db_path):
        print(f"Could not find chat.db at: {db_path}")
        print("Pass a custom path: python3 build_dashboard.py /path/to/chat.db")
        return

    print(f"Reading {db_path} ...")
    try:
        tmp_db = copy_db(db_path)
    except PermissionError:
        print("\nPermission denied reading chat.db.")
        print("Grant Full Disk Access to your terminal app in:")
        print("  System Settings -> Privacy & Security -> Full Disk Access")
        print("...then quit and reopen your terminal and try again.\n")
        return

    conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("Loading handles...")
    handle_rows = cur.execute("SELECT ROWID as id, id as address FROM handle").fetchall()
    # NOTE: sqlite column name collision above (both named 'id') is fine in
    # row_factory access by index; use explicit aliasing instead to be safe.
    handle_rows = cur.execute("SELECT ROWID as hid, id as address FROM handle").fetchall()
    handles_raw = {str(r["hid"]): r["address"] for r in handle_rows}

    print("Looking up names in Contacts...")
    phone_lookup, email_lookup = load_mac_contacts()
    overrides = load_contacts_overrides()

    resolved_name = {}  # hid -> matched name, or None if unresolved
    auto_matched = 0
    for hid, address in handles_raw.items():
        if address in overrides or hid in overrides:
            resolved_name[hid] = overrides.get(address, overrides.get(hid))
            continue
        guess = None
        if address and "@" in address:
            guess = email_lookup.get(address.lower())
        else:
            guess = phone_lookup.get(normalize_phone(address))
        resolved_name[hid] = guess
        if guess:
            auto_matched += 1

    if phone_lookup or email_lookup:
        print(f"  Matched {auto_matched}/{len(handles_raw)} contacts automatically from Contacts.")
    else:
        print("  Couldn't read Contacts (not found, or no Full Disk Access) -- "
              "falling back to numbers/emails for everyone.")

    # Consolidate: if two handles (e.g. a phone number and an email, or two
    # entirely separate Contacts cards) resolve to the same first+last name,
    # treat them as one person everywhere downstream. Matching is
    # case/whitespace-insensitive ("John Smith" == "john  smith"), but the
    # display name keeps whichever casing was seen first. Anyone still
    # unresolved keeps their own separate, un-mergeable id -- we only merge
    # on a positive name match, never guess.
    name_to_canonical = {}   # normalized name -> canonical id
    canonical_display = {}   # canonical id -> display name (first seen casing)
    canonical_of = {}
    for hid, name in resolved_name.items():
        if name:
            norm = re.sub(r"\s+", " ", name).strip().lower()
            if norm not in name_to_canonical:
                cid = f"p{len(name_to_canonical)}"
                name_to_canonical[norm] = cid
                canonical_display[cid] = re.sub(r"\s+", " ", name).strip()
            canonical_of[hid] = name_to_canonical[norm]
        else:
            canonical_of[hid] = f"h{hid}"

    handles = {}
    handle_contacts = defaultdict(list)
    for hid, address in handles_raw.items():
        cid = canonical_of[hid]
        handles[cid] = canonical_display.get(cid, resolved_name[hid] or address)
        handle_contacts[cid].append(address)
    handle_contacts = {cid: sorted(set(addrs)) for cid, addrs in handle_contacts.items()}

    merged = len(handles_raw) - len(handles)
    if merged:
        print(f"  Consolidated {merged} duplicate handle(s) (same person, multiple numbers/emails).")

    print("Loading chats...")
    chat_rows = cur.execute(
        "SELECT ROWID as cid, chat_identifier, display_name FROM chat"
    ).fetchall()
    chat_meta = {str(r["cid"]): dict(r) for r in chat_rows}

    chat_handle_rows = cur.execute(
        "SELECT chat_id, handle_id FROM chat_handle_join"
    ).fetchall()
    chat_participants = defaultdict(set)
    for r in chat_handle_rows:
        raw_hid = str(r["handle_id"])
        chat_participants[str(r["chat_id"])].add(canonical_of.get(raw_hid, raw_hid))

    chats_raw = {}
    for cid, meta in chat_meta.items():
        participants = sorted(chat_participants.get(cid, []))
        is_group = len(participants) > 1
        display_name = (meta.get("display_name") or "").strip()
        if display_name:
            name = display_name
        elif is_group:
            names = [handles.get(p, p) for p in participants[:3]]
            extra = len(participants) - len(names)
            name = ", ".join(names) + (f" +{extra} more" if extra > 0 else "")
        elif participants:
            name = handles.get(participants[0], participants[0])
        else:
            name = meta.get("chat_identifier") or f"Chat {cid}"
        chats_raw[cid] = {"name": name, "group": is_group, "participants": participants}

    # Consolidate group chats that share the same name (case/whitespace
    # insensitive) into one logical chat -- this is the common "same group,
    # duplicate thread" case (e.g. iMessage/SMS service switches). Only
    # group chats are merged this way; 1:1 chats are left as-is.
    group_name_to_canonical = {}
    canonical_chat_of = {}
    chats = {}
    for cid, meta in chats_raw.items():
        if not meta["group"]:
            canonical_chat_of[cid] = cid
            chats[cid] = meta
            continue
        norm = re.sub(r"\s+", " ", meta["name"]).strip().lower()
        if norm not in group_name_to_canonical:
            new_id = f"g{len(group_name_to_canonical)}"
            group_name_to_canonical[norm] = new_id
            chats[new_id] = {"name": meta["name"], "group": True, "participants": list(meta["participants"])}
        else:
            new_id = group_name_to_canonical[norm]
            merged_participants = sorted(set(chats[new_id]["participants"]) | set(meta["participants"]))
            chats[new_id]["participants"] = merged_participants
        canonical_chat_of[cid] = new_id

    merged_chats = len(chats_raw) - len(chats)
    if merged_chats:
        print(f"  Consolidated {merged_chats} duplicate group chat thread(s) (same group name).")

    print("Loading messages (this can take a while for large histories)...")
    msg_rows = cur.execute("""
        SELECT m.ROWID as mid, m.text, m.attributedBody, m.date, m.is_from_me,
               m.handle_id, m.associated_message_type, cmj.chat_id as chat_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    """).fetchall()

    messages = []  # [ts_ms, chat_id, sender_id_or_None, is_from_me]
    uni_sent, uni_received = Counter(), Counter()
    bi_sent, bi_received = Counter(), Counter()
    msg_sent, msg_received = Counter(), Counter()
    chat_uni = defaultdict(Counter)   # chat_id -> Counter (all senders combined)
    chat_bi = defaultdict(Counter)
    chat_msg_count = Counter()

    seen_pairs = set()
    for r in msg_rows:
        key = (r["mid"], r["chat_id"])
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        ts = apple_time_to_unix_ms(r["date"])
        if ts is None:
            continue
        raw_chat_id = str(r["chat_id"])
        chat_id = canonical_chat_of.get(raw_chat_id, raw_chat_id)
        if chat_id not in chats:
            continue
        is_from_me = bool(r["is_from_me"])
        raw_sender = str(r["handle_id"]) if r["handle_id"] else None
        sender = None if is_from_me else canonical_of.get(raw_sender, raw_sender)

        messages.append([ts, chat_id, sender, 1 if is_from_me else 0])
        chat_msg_count[chat_id] += 1

        # word/phrase/full-message frequency: skip tapback/reaction rows
        assoc_type = r["associated_message_type"] or 0
        if assoc_type != 0:
            continue
        text = r["text"] or ""
        if not text.strip():
            text = parse_attributed_body(r["attributedBody"])
        if not text or text.startswith(TAPBACK_PREFIXES):
            continue

        clean_text = re.sub(r"\s+", " ", text).strip()
        raw = tokenize_raw(text)
        uni = unigrams_from_raw(raw)
        bi = bigrams_from_raw(raw)

        if is_from_me:
            uni_sent.update(uni)
            bi_sent.update(bi)
            if clean_text:
                msg_sent[clean_text] += 1
        else:
            uni_received.update(uni)
            bi_received.update(bi)
            if clean_text:
                msg_received[clean_text] += 1
        chat_uni[chat_id].update(uni)
        chat_bi[chat_id].update(bi)

    if not messages:
        print("No messages found. Nothing to build.")
        return

    messages.sort(key=lambda m: m[0])
    date_min = messages[0][0]
    date_max = messages[-1][0]

    uni_overall = uni_sent + uni_received
    bi_overall = bi_sent + bi_received
    msg_overall = msg_sent + msg_received

    # Per-chat vocabulary only for the top 5 group chats (by all-time volume),
    # to keep the output file a reasonable size.
    top_group_chat_ids = [
        cid for cid, _ in chat_msg_count.most_common()
        if chats.get(cid, {}).get("group")
    ][:5]
    chat_word_freq = {
        cid: {
            "unigrams": chat_uni[cid].most_common(30),
            "bigrams": chat_bi[cid].most_common(15),
        }
        for cid in top_group_chat_ids
    }

    data = {
        "meta": {
            "generated_at": int(datetime.now().timestamp() * 1000),
            "total_messages": len(messages),
            "date_min": date_min,
            "date_max": date_max,
        },
        "handles": handles,
        "handle_contacts": handle_contacts,
        "chats": chats,
        "messages": messages,
        "word_freq": {
            "unigrams": {
                "overall": uni_overall.most_common(200),
                "sent": uni_sent.most_common(200),
                "received": uni_received.most_common(200),
            },
            "bigrams": {
                "overall": bi_overall.most_common(150),
                "sent": bi_sent.most_common(150),
                "received": bi_received.most_common(150),
            },
            "messages": {
                "overall": [m for m in msg_overall.most_common(300) if m[1] > 1][:100],
                "sent": [m for m in msg_sent.most_common(300) if m[1] > 1][:100],
                "received": [m for m in msg_received.most_common(300) if m[1] > 1][:100],
            },
        },
        "chat_word_freq": chat_word_freq,
    }

    print(f"Loaded {len(messages):,} messages across {len(chats):,} chats.")

    # Write/refresh a template listing only the contacts that neither the
    # Contacts app lookup nor contacts.json could resolve -- not everyone.
    unresolved = sorted({addr for hid, addr in handles_raw.items() if handles[canonical_of[hid]] == addr})
    if unresolved:
        template_path = os.path.join(SCRIPT_DIR, "contacts_template.json")
        with open(template_path, "w") as f:
            json.dump({addr: "" for addr in unresolved}, f, indent=2, ensure_ascii=False)
        print(f"  {len(unresolved)} contact(s) couldn't be auto-matched. Wrote {template_path} -- "
              f"fill in names, save as contacts.json, and re-run to relabel just those.")

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__IMESSAGE_DATA__", json.dumps(data))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    conn.close()
    shutil.rmtree(os.path.dirname(tmp_db), ignore_errors=True)

    print(f"\nDone. Open: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
