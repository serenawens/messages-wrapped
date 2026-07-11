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
LOOKUP_TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "lookup_template.html")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "iMessage_Dashboard.html")
LOOKUP_OUTPUT_FILE = os.path.join(SCRIPT_DIR, "iMessage_Dashboard_Lookup.html")

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

# Subset used only to reject phrases where every token is a function/filler word.
PHRASE_STOPWORDS = set("""
the a an and or but to of in on for with at by from as
i you he she we they me him her us them my your his its our their
is are was were be been being am have has had
this that these those
just like very so
lol yeah yea ya haha lmao omg
gonna wanna gotta kinda sorta
ok okay
""".split())

TAPBACK_PREFIXES = ("Loved ", "Liked ", "Disliked ", "Laughed at ", "Emphasized ", "Questioned ")

# Emoji detection — broad Unicode ranges covering all major emoji blocks
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF"   # Misc symbols, emoticons, transport, supplemental
    "\U0001FA00-\U0001FAFF"    # Chess, bubbles, etc.
    "\U00002600-\U000027BF"    # Misc symbols + dingbats
    "\U0001F1E0-\U0001F1FF"    # Regional indicator flags
    "\U00002300-\U000023FF"    # Misc technical
    "\U000025A0-\U000025FF"    # Geometric shapes
    "]+",
    flags=re.UNICODE,
)

GP_GAMES = {
    "8ball": "8-Ball", "basketball": "Basketball", "basketballstar": "Basketball",
    "mancala": "Mancala", "archery": "Archery", "minigolf": "Mini Golf",
    "cup_pong": "Cup Pong", "soccer": "Soccer", "hockey": "Air Hockey",
    "fourinarow": "Four in a Row", "shuffleboard": "Shuffleboard",
    "boxing": "Boxing", "checkers": "Checkers", "go": "Go",
    "knockout": "Knockout", "pool": "Pool", "putt": "Putt-Putt Golf",
    "darts": "Darts", "spaceship": "Spaceship", "wordhunt": "Word Hunt",
    "wordbrush": "Word Hunt", "anagram": "Anagram", "facerace": "Face Race",
}


def gp_game_name(bundle_id):
    parts = (bundle_id or "").lower().split(".")
    for i, p in enumerate(parts):
        if p == "gamepigeon" and i + 1 < len(parts):
            return GP_GAMES.get(parts[i + 1], parts[i + 1].replace("_", " ").title())
    return "Game Pigeon"


def _median(lst):
    if not lst:
        return None
    s = sorted(lst)
    n = len(s)
    return int((s[n // 2 - 1] + s[n // 2]) / 2) if n % 2 == 0 else s[n // 2]


def _response_buckets(gaps_ms):
    """Reply-time distribution split at 15 minutes.

    Summary buckets use % of all replies. Detail buckets use % within their
    parent group (under-15 or over-15 each sum to 100%). When over-1-day
    replies exceed 2% of all replies, adds an over_long sub-breakdown (% of
    only those multi-day replies).
    """
    if not gaps_ms:
        return {}
    n = len(gaps_ms)
    quick_ms = 15 * 60 * 1000
    one_day = 86_400_000
    three_days = 3 * one_day
    one_week = 7 * one_day
    two_weeks = 14 * one_day

    under_thresholds = [60_000, 300_000, quick_ms]
    under_keys = ["<1m", "1–5m", "5–15m"]
    over_thresholds = [1_800_000, 3_600_000, 21_600_000, one_day]
    over_keys = ["15–30m", "30m–1h", "1–6h", "6–24h", "Over 1 day"]
    long_thresholds = [three_days, one_week, two_weeks]
    long_keys = ["1–3 days", "3 days – 1 week", "1–2 weeks", "Over 2 weeks"]

    under_counts = [0, 0, 0]
    over_counts = [0, 0, 0, 0, 0]
    long_counts = [0, 0, 0, 0]
    under_total = over_total = long_total = 0

    for g in gaps_ms:
        if g <= quick_ms:
            under_total += 1
            for i, t in enumerate(under_thresholds):
                if g <= t:
                    under_counts[i] += 1
                    break
        else:
            over_total += 1
            if g > one_day:
                long_total += 1
                placed_long = False
                for i, t in enumerate(long_thresholds):
                    if g <= t:
                        long_counts[i] += 1
                        placed_long = True
                        break
                if not placed_long:
                    long_counts[3] += 1
            placed = False
            for i, t in enumerate(over_thresholds):
                if g <= t:
                    over_counts[i] += 1
                    placed = True
                    break
            if not placed:
                over_counts[4] += 1

    def _pct_of(part, whole):
        return round(part / whole * 100) if whole else 0

    result = {
        "summary": {
            "under_15": _pct_of(under_total, n),
            "over_15": _pct_of(over_total, n),
        },
        "under": {k: _pct_of(c, under_total) for k, c in zip(under_keys, under_counts)},
        "over": {k: _pct_of(c, over_total) for k, c in zip(over_keys, over_counts)},
    }

    if _pct_of(long_total, n) > 2 and long_total > 0:
        result["over_long"] = {
            k: _pct_of(c, long_total) for k, c in zip(long_keys, long_counts)
        }

    return result


def _burst_responses(msgs_sorted):
    """
    Group consecutive messages from the same sender into bursts.
    Measure response time from end of one burst to start of the next.
    msgs_sorted: list of (ts_ms, is_from_me_bool)
    Returns {your: [ms,...], their: [ms,...]}
    """
    if len(msgs_sorted) < 2:
        return {"your": [], "their": []}
    bursts = []
    cur_sender, burst_start, burst_end = None, None, None
    for ts, is_from_me in msgs_sorted:
        s = "me" if is_from_me else "them"
        if s != cur_sender:
            if cur_sender is not None:
                bursts.append({"s": cur_sender, "start": burst_start, "end": burst_end})
            cur_sender, burst_start = s, ts
        burst_end = ts
    if cur_sender is not None:
        bursts.append({"s": cur_sender, "start": burst_start, "end": burst_end})

    your_gaps, their_gaps = [], []
    for i in range(1, len(bursts)):
        gap = bursts[i]["start"] - bursts[i - 1]["end"]
        if gap <= 0 or gap > 30 * 86_400_000:
            continue
        (your_gaps if bursts[i]["s"] == "me" else their_gaps).append(gap)
    return {"your": your_gaps, "their": their_gaps}


def _avg(lst):
    return round(sum(lst) / len(lst), 1) if lst else None


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


# URL patterns and fragments to exclude from vocabulary analysis.
_URL_RE = re.compile(
    r"https?://[^\s<>\"']+|www\.[^\s<>\"']+",
    re.IGNORECASE,
)
_URL_FRAGMENTS = frozenset({
    "http", "https", "www", "com", "org", "net", "io", "co", "edu", "gov",
    "youtube", "youtu", "tiktok", "instagram", "facebook", "twitter", "vm",
    "html", "php", "asp", "tik", "watch", "share", "link",
})

_PUNCT_NORMALIZE = str.maketrans({
    "\u2019": "'",  # '
    "\u2018": "'",  # '
    "\u02bc": "'",  # ʼ
    "\u201c": '"',  # "
    "\u201d": '"',  # "
})

# Unicode letters with optional internal apostrophe (contractions).
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def normalize_text_punctuation(text):
    """Convert typographic punctuation to ASCII before tokenization."""
    return (text or "").translate(_PUNCT_NORMALIZE)


def strip_urls(text):
    return _URL_RE.sub(" ", text or "")


def is_vocab_item(text):
    """Filter URL-heavy tokens/phrases/messages from vocabulary output."""
    if not text or not str(text).strip():
        return False
    lower = normalize_text_punctuation(str(text)).lower().strip()
    if _URL_RE.search(lower):
        return False
    tokens = _TOKEN_RE.findall(lower)
    if not tokens:
        return False
    urlish = sum(1 for t in tokens if t in _URL_FRAGMENTS)
    if urlish and urlish >= max(1, len(tokens) // 2):
        return False
    return True


def tokenize_raw(text):
    text = normalize_text_punctuation(text)
    text = strip_urls(text)
    words = _TOKEN_RE.findall(text.lower())
    return [
        w for w in words
        if 1 <= len(w) <= 20
        and w not in _URL_FRAGMENTS
        and not w.isdigit()
    ]


# Object-replacement / zero-width / BOM chars used as attachment placeholders.
_PLACEHOLDER_CHARS = "\ufffc\ufeff\u200b\u200c\u200d\u2060"


def is_countable_full_message(text):
    """True if a message has real content worth counting as a 'full message'.
    Filters out attachment/image placeholders and whitespace-only rows that
    otherwise dominate the frequency list as invisible 'empty' messages."""
    stripped = text
    for ch in _PLACEHOLDER_CHARS:
        stripped = stripped.replace(ch, "")
    stripped = stripped.strip()
    if len(stripped) < 2:
        return False
    if _URL_RE.search(stripped.lower()):
        return False
    # Require at least one alphanumeric word character (unicode-aware).
    return bool(re.search(r"\w", stripped, re.UNICODE))


def unigrams_from_raw(raw_words):
    return [w for w in raw_words if len(w) >= 2 and w not in STOPWORDS]


def phrases_from_raw(raw_words, nmin=2, nmax=5):
    """Common phrases of length nmin..nmax words. Reject only when every token
    is a phrase stop word (function/filler words)."""
    grams = []
    n = len(raw_words)
    for size in range(nmin, nmax + 1):
        for i in range(n - size + 1):
            window = raw_words[i:i + size]
            if all(w in PHRASE_STOPWORDS for w in window):
                continue
            grams.append(" ".join(window))
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
    both possibly empty if Contacts data isn't present."""
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

            names: dict = {}
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
            pass  # best-effort
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
               m.handle_id, m.associated_message_type, m.balloon_bundle_id,
               cmj.chat_id as chat_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    """).fetchall()

    messages = []  # [ts_ms, chat_id, sender_id_or_None, is_from_me, is_game_pigeon]
    uni_sent, uni_received = Counter(), Counter()
    bi_sent, bi_received = Counter(), Counter()
    msg_sent, msg_received = Counter(), Counter()
    uni_by_year_sent: dict = defaultdict(Counter)
    uni_by_year_received: dict = defaultdict(Counter)
    bi_by_year_sent: dict = defaultdict(Counter)
    bi_by_year_received: dict = defaultdict(Counter)
    msg_by_year_sent: dict = defaultdict(Counter)
    msg_by_year_received: dict = defaultdict(Counter)
    chat_uni = defaultdict(Counter)
    chat_bi = defaultdict(Counter)
    chat_msg_count = Counter()

    # Emoji tracking
    emoji_sent_ctr, emoji_received_ctr = Counter(), Counter()
    emoji_by_year_sent: dict = defaultdict(Counter)
    emoji_by_year_received: dict = defaultdict(Counter)

    # Message-length tracking (chars, words) per message
    len_sent_chars: list = []
    len_received_chars: list = []
    len_by_year_sent: dict = defaultdict(list)   # year -> [char_counts]
    len_by_year_received: dict = defaultdict(list)

    # Longest messages (keep top-10 heap per side, all-time + per year)
    longest_sent: list = []    # [(char_count, ts, chat_name, text)]
    longest_received: list = []
    longest_by_year_sent: dict = defaultdict(list)
    longest_by_year_received: dict = defaultdict(list)
    import heapq

    # Game Pigeon (global by type)
    gp_by_type: Counter = Counter()

    # Per-person data (1:1 chats only) for fast JS lookups
    person_1on1_ts: dict = defaultdict(list)    # pid -> [(ts, is_from_me)]
    person_hour_counts: dict = defaultdict(lambda: [0] * 24)
    person_word_sent: dict = defaultdict(Counter)
    person_word_received: dict = defaultdict(Counter)
    person_phrase_sent: dict = defaultdict(Counter)
    person_phrase_received: dict = defaultdict(Counter)
    person_emoji_sent: dict = defaultdict(Counter)
    person_emoji_received: dict = defaultdict(Counter)
    person_gp_types: dict = defaultdict(Counter)
    person_gp_count: dict = defaultdict(int)
    person_chars_sent: dict = defaultdict(list)   # pid -> [char_counts]
    person_chars_received: dict = defaultdict(list)
    person_monthly_sent: dict = defaultdict(Counter)    # pid -> "YYYY-MM" -> count
    person_monthly_received: dict = defaultdict(Counter)
    person_daily_sent: dict = defaultdict(Counter)      # pid -> "YYYY-MM-DD" -> count
    person_daily_received: dict = defaultdict(Counter)

    # Per-chat detailed tracking (for group chat lookup page)
    chat_emoji: dict = defaultdict(Counter)
    chat_hour_counts: dict = defaultdict(lambda: [0] * 24)
    chat_monthly: dict = defaultdict(Counter)           # chat_id -> "YYYY-MM" -> count
    chat_sender_monthly: dict = defaultdict(lambda: defaultdict(Counter))  # chat_id -> pid -> month -> n
    chat_daily: dict = defaultdict(Counter)             # chat_id -> "YYYY-MM-DD" -> count
    chat_sender_daily: dict = defaultdict(lambda: defaultdict(Counter))    # chat_id -> pid -> day -> n

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
        bundle = (r["balloon_bundle_id"] or "").lower()
        is_game_pigeon = 1 if "gamepigeon" in bundle else 0

        messages.append([ts, chat_id, sender, 1 if is_from_me else 0, is_game_pigeon])
        chat_msg_count[chat_id] += 1

        # Per-chat tracking for group chat lookup
        msg_dt = datetime.fromtimestamp(ts / 1000)
        month_key = msg_dt.strftime("%Y-%m")
        day_key_str = msg_dt.strftime("%Y-%m-%d")
        chat_monthly[chat_id][month_key] += 1
        chat_daily[chat_id][day_key_str] += 1
        hour = msg_dt.hour
        chat_hour_counts[chat_id][hour] += 1
        if is_from_me:
            chat_sender_monthly[chat_id]["me"][month_key] += 1
            chat_sender_daily[chat_id]["me"][day_key_str] += 1
        elif sender:
            chat_sender_monthly[chat_id][sender][month_key] += 1
            chat_sender_daily[chat_id][sender][day_key_str] += 1

        # Per-person 1:1 tracking (for response times, word freq, emojis, lengths)
        # Count ALL 1:1 messages here (including reactions / empty bodies) so
        # monthly/daily totals match Top 10 and person-profile hero stats.
        is_group_chat = chats[chat_id]["group"]
        other_in_1on1 = None
        if not is_group_chat and len(chats[chat_id]["participants"]) == 1:
            other_in_1on1 = chats[chat_id]["participants"][0]
            person_1on1_ts[other_in_1on1].append((ts, is_from_me))
            if is_from_me:
                person_monthly_sent[other_in_1on1][month_key] += 1
                person_daily_sent[other_in_1on1][day_key_str] += 1
            else:
                person_monthly_received[other_in_1on1][month_key] += 1
                person_daily_received[other_in_1on1][day_key_str] += 1
                # "When They Text": only messages this person sent to you 1:1
                person_hour_counts[other_in_1on1][msg_dt.hour] += 1

        # Game Pigeon tracking
        if is_game_pigeon and bundle:
            game_name = gp_game_name(bundle)
            gp_by_type[game_name] += 1
            if other_in_1on1:
                person_gp_types[other_in_1on1][game_name] += 1
                person_gp_count[other_in_1on1] += 1

        # word/phrase/full-message frequency: skip tapback/reaction rows
        assoc_type = r["associated_message_type"] or 0
        if assoc_type != 0:
            continue
        text = r["text"] or ""
        if not text.strip():
            text = parse_attributed_body(r["attributedBody"])
        if not text or text.startswith(TAPBACK_PREFIXES):
            continue

        clean_text = normalize_text_punctuation(re.sub(r"\s+", " ", text).strip())
        if not clean_text:
            continue

        raw = tokenize_raw(clean_text)
        uni = unigrams_from_raw(raw)
        bi = phrases_from_raw(raw)

        # Emoji extraction
        found_emojis = EMOJI_RE.findall(clean_text)
        year = datetime.fromtimestamp(ts / 1000).year

        # Message character count (skip Game Pigeon placeholder texts)
        if not is_game_pigeon:
            char_count = len(clean_text)
            if is_from_me:
                len_sent_chars.append(char_count)
                len_by_year_sent[year].append(char_count)
                # Track longest (maintain top-10 min-heap all-time, top-5 per year)
                entry = (char_count, ts, chats[chat_id]["name"], clean_text)
                if len(longest_sent) < 10:
                    heapq.heappush(longest_sent, entry)
                elif char_count > longest_sent[0][0]:
                    heapq.heapreplace(longest_sent, entry)
                yr_heap = longest_by_year_sent[year]
                if len(yr_heap) < 5:
                    heapq.heappush(yr_heap, entry)
                elif char_count > yr_heap[0][0]:
                    heapq.heapreplace(yr_heap, entry)
            else:
                len_received_chars.append(char_count)
                len_by_year_received[year].append(char_count)
                entry = (char_count, ts, chats[chat_id]["name"], clean_text)
                if len(longest_received) < 10:
                    heapq.heappush(longest_received, entry)
                elif char_count > longest_received[0][0]:
                    heapq.heapreplace(longest_received, entry)
                yr_heap = longest_by_year_received[year]
                if len(yr_heap) < 5:
                    heapq.heappush(yr_heap, entry)
                elif char_count > yr_heap[0][0]:
                    heapq.heapreplace(yr_heap, entry)

        countable_full = is_countable_full_message(clean_text)
        if is_from_me:
            uni_sent.update(uni)
            bi_sent.update(bi)
            uni_by_year_sent[year].update(uni)
            bi_by_year_sent[year].update(bi)
            if countable_full:
                msg_sent[clean_text] += 1
                msg_by_year_sent[year][clean_text] += 1
            if found_emojis:
                emoji_sent_ctr.update(found_emojis)
                emoji_by_year_sent[year].update(found_emojis)
                chat_emoji[chat_id].update(found_emojis)
            # Per-person 1:1 text stats (words/emoji/length only — volume is counted above)
            if other_in_1on1:
                person_word_sent[other_in_1on1].update(uni)
                person_phrase_sent[other_in_1on1].update(bi)
                person_emoji_sent[other_in_1on1].update(found_emojis)
                if not is_game_pigeon:
                    person_chars_sent[other_in_1on1].append(len(clean_text))
        else:
            uni_received.update(uni)
            bi_received.update(bi)
            uni_by_year_received[year].update(uni)
            bi_by_year_received[year].update(bi)
            if countable_full:
                msg_received[clean_text] += 1
                msg_by_year_received[year][clean_text] += 1
            if found_emojis:
                emoji_received_ctr.update(found_emojis)
                emoji_by_year_received[year].update(found_emojis)
                chat_emoji[chat_id].update(found_emojis)
            # Per-person 1:1 text stats (words/emoji/length only — volume is counted above)
            if other_in_1on1:
                person_word_received[other_in_1on1].update(uni)
                person_phrase_received[other_in_1on1].update(bi)
                person_emoji_received[other_in_1on1].update(found_emojis)
                if not is_game_pigeon:
                    person_chars_received[other_in_1on1].append(len(clean_text))
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

    # ── Chat word freq: expand to all group chats (capped at 30 per chat) ──
    all_group_ids = [cid for cid, _ in chat_msg_count.most_common()
                     if chats.get(cid, {}).get("group")]
    chat_word_freq = {
        cid: {
            "unigrams": chat_uni[cid].most_common(30),
            "bigrams": chat_bi[cid].most_common(15),
        }
        for cid in all_group_ids
        if chat_uni[cid] or chat_bi[cid]
    }

    # ── Emoji stats ──
    def _top_emoji(counter, n=30):
        return [[e, c] for e, c in counter.most_common(n)]

    emoji_by_year_sent_serializable = {
        str(y): _top_emoji(ctr) for y, ctr in sorted(emoji_by_year_sent.items())
    }
    emoji_by_year_received_serializable = {
        str(y): _top_emoji(ctr) for y, ctr in sorted(emoji_by_year_received.items())
    }

    # ── Message length stats ──
    def _len_stats(char_list):
        if not char_list:
            return None
        s = sorted(char_list)
        n = len(s)
        return {
            "avg_chars": round(sum(s) / n, 1),
            "median_chars": (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2],
            "count": n,
        }

    def _len_stats_by_year(year_dict):
        return {str(y): _len_stats(lst) for y, lst in sorted(year_dict.items())}

    msg_length = {
        "sent": _len_stats(len_sent_chars),
        "received": _len_stats(len_received_chars),
        "by_year_sent": _len_stats_by_year(len_by_year_sent),
        "by_year_received": _len_stats_by_year(len_by_year_received),
    }

    # ── Longest messages ──
    def _serialise_longest(heap):
        out = sorted(heap, reverse=True)[:5]  # top-5 by char count
        return [
            {
                "chars": item[0],
                "ts": item[1],
                "chat": item[2],
                "preview": item[3][:120] + ("…" if len(item[3]) > 120 else ""),
                "full": item[3],
            }
            for item in out
        ]

    all_longest_years = set(longest_by_year_sent.keys()) | set(longest_by_year_received.keys())
    longest_messages = {
        "all": {
            "sent": _serialise_longest(longest_sent),
            "received": _serialise_longest(longest_received),
        },
        "by_year": {
            str(y): {
                "sent": _serialise_longest(longest_by_year_sent.get(y, [])),
                "received": _serialise_longest(longest_by_year_received.get(y, [])),
            }
            for y in sorted(all_longest_years)
        },
    }

    # ── Person profiles (precomputed for fast JS lookup) ──
    print("  Computing per-person profiles...")
    # Map person -> their group chat ids + message count in that group
    person_group_chats_data: dict = defaultdict(list)
    for cid, chat in chats.items():
        if chat["group"]:
            gchat_counts: Counter = Counter()
            your_count_in_chat = 0
            for m in messages:
                if m[1] == cid:
                    if m[2]:        # incoming
                        gchat_counts[m[2]] += 1
                    elif m[3]:      # outgoing
                        your_count_in_chat += 1
            sender_monthly = chat_sender_monthly.get(cid, {})
            for pid in chat["participants"]:
                person_group_chats_data[pid].append({
                    "id": cid,
                    "name": chat["name"],
                    "count": gchat_counts.get(pid, 0),
                    "your_count": your_count_in_chat,
                    "their_monthly": dict(sender_monthly.get(pid, {})),
                    "your_monthly": dict(sender_monthly.get("me", {})),
                })

    person_profiles = {}
    for pid in handles:
        one_on_one_msgs = sorted(person_1on1_ts.get(pid, []), key=lambda x: x[0])
        bursts = _burst_responses(one_on_one_msgs)

        sent_1on1 = sum(1 for _, m in one_on_one_msgs if m)
        received_1on1 = sum(1 for _, m in one_on_one_msgs if not m)

        your_med = _median(bursts["your"])
        their_med = _median(bursts["their"])

        gc = sorted(
            person_group_chats_data.get(pid, []),
            key=lambda x: x["count"], reverse=True
        )[:5]

        combined_words = person_word_sent[pid] + person_word_received[pid]
        combined_phrases = person_phrase_sent[pid] + person_phrase_received[pid]

        person_profiles[pid] = {
            "sent": sent_1on1,
            "received": received_1on1,
            "your_response_ms": your_med,
            "their_response_ms": their_med,
            "your_response_pcts": _response_buckets(bursts["your"]),
            "their_response_pcts": _response_buckets(bursts["their"]),
            "hour_counts": person_hour_counts[pid],
            "group_chats": gc,
            "game_pigeon": person_gp_count.get(pid, 0),
            "game_pigeon_types": [[k, v] for k, v in
                                  person_gp_types[pid].most_common(10)],
            "top_words": combined_words.most_common(20) if combined_words else [],
            "top_phrases": combined_phrases.most_common(10) if combined_phrases else [],
            "emojis_sent": _top_emoji(person_emoji_sent.get(pid, Counter()), 15),
            "emojis_received": _top_emoji(person_emoji_received.get(pid, Counter()), 15),
            "avg_chars_sent": _avg(person_chars_sent.get(pid, [])),
            "avg_chars_received": _avg(person_chars_received.get(pid, [])),
            "monthly_sent": dict(person_monthly_sent.get(pid, {})),
            "monthly_received": dict(person_monthly_received.get(pid, {})),
            "daily_sent": dict(person_daily_sent.get(pid, {})),
            "daily_received": dict(person_daily_received.get(pid, {})),
        }

    # ── Group chat profiles (for the Lookup page) ──
    chat_profiles = {}
    for cid, chat in chats.items():
        if not chat["group"]:
            continue
        # Compute per-sender totals from messages list (outgoing counted as "me")
        sender_cnts: Counter = Counter()
        for m in messages:
            if m[1] == cid:
                if m[2]:        # incoming: has sender ID
                    sender_cnts[m[2]] += 1
                elif m[3]:      # outgoing: is_from_me
                    sender_cnts["me"] += 1
        chat_profiles[cid] = {
            "name": chat["name"],
            "total": chat_msg_count.get(cid, 0),
            "participants": chat["participants"],
            "sender_counts": [[pid, cnt] for pid, cnt in sender_cnts.most_common()],
            "top_words": chat_uni[cid].most_common(20),
            "top_phrases": chat_bi[cid].most_common(10),
            "top_emojis": _top_emoji(chat_emoji.get(cid, Counter()), 15),
            "hour_counts": chat_hour_counts[cid],
            "activity_by_month": dict(chat_monthly.get(cid, {})),
            "activity_by_sender_month": {
                pid: dict(monthly)
                for pid, monthly in chat_sender_monthly.get(cid, {}).items()
            },
            "activity_by_day": dict(chat_daily.get(cid, {})),
            "activity_by_sender_day": {
                pid: dict(daily)
                for pid, daily in chat_sender_daily.get(cid, {}).items()
            },
        }
    print(f"  Built profiles for {len(chat_profiles)} group chats.")

    def _filter_vocab_pairs(pairs, limit, repeated_only=False):
        out = []
        for text, count in pairs:
            if repeated_only:
                if count <= 1 or not is_countable_full_message(text):
                    continue
            if not is_vocab_item(text):
                continue
            out.append([text, count])
            if len(out) >= limit:
                break
        return out

    def _serialise_vocab_by_year(sent_by_year, recv_by_year, limit, repeated_only=False):
        years = sorted(set(sent_by_year) | set(recv_by_year))
        sent_out, recv_out = {}, {}
        for y in years:
            sent_out[str(y)] = _filter_vocab_pairs(
                sent_by_year[y].most_common(limit * 2), limit, repeated_only
            )
            recv_out[str(y)] = _filter_vocab_pairs(
                recv_by_year[y].most_common(limit * 2), limit, repeated_only
            )
        return {"sent": sent_out, "received": recv_out}

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
                "overall": _filter_vocab_pairs(uni_overall.most_common(400), 200),
                "sent": _filter_vocab_pairs(uni_sent.most_common(400), 200),
                "received": _filter_vocab_pairs(uni_received.most_common(400), 200),
            },
            "bigrams": {
                "overall": _filter_vocab_pairs(bi_overall.most_common(300), 150),
                "sent": _filter_vocab_pairs(bi_sent.most_common(300), 150),
                "received": _filter_vocab_pairs(bi_received.most_common(300), 150),
            },
            "messages": {
                "overall": _filter_vocab_pairs(msg_overall.most_common(300), 100, repeated_only=True),
                "sent": _filter_vocab_pairs(msg_sent.most_common(300), 100, repeated_only=True),
                "received": _filter_vocab_pairs(msg_received.most_common(300), 100, repeated_only=True),
            },
            "by_year": {
                "unigrams": _serialise_vocab_by_year(
                    uni_by_year_sent, uni_by_year_received, 200
                ),
                "bigrams": _serialise_vocab_by_year(
                    bi_by_year_sent, bi_by_year_received, 150
                ),
                "messages": _serialise_vocab_by_year(
                    msg_by_year_sent, msg_by_year_received, 100, repeated_only=True
                ),
            },
        },
        "chat_word_freq": chat_word_freq,
        "emoji_freq": {
            "sent": _top_emoji(emoji_sent_ctr),
            "received": _top_emoji(emoji_received_ctr),
            "overall": _top_emoji(emoji_sent_ctr + emoji_received_ctr),
            "by_year_sent": emoji_by_year_sent_serializable,
            "by_year_received": emoji_by_year_received_serializable,
        },
        "msg_length": msg_length,
        "longest_messages": longest_messages,
        "game_pigeon": {
            "by_type": [[k, v] for k, v in gp_by_type.most_common()],
        },
        "person_profiles": person_profiles,
        "chat_profiles": chat_profiles,
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

    # Escape sequences that would break the <script type="text/plain"> data blob
    # if a message text happens to contain them (e.g. </script> in a Tableau embed).
    # JSON allows \/ for /, so JSON.parse on the receiving end handles this correctly.
    json_data = (
        json.dumps(data)
        .replace("</script>", r"<\/script>")
        .replace("<!--", r"<\!--")
    )

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__IMESSAGE_DATA__", json_data)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    # Generate the separate Lookup page if its template exists
    if os.path.exists(LOOKUP_TEMPLATE_FILE):
        with open(LOOKUP_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            lookup_html = f.read()
        lookup_html = lookup_html.replace("__IMESSAGE_DATA__", json_data)
        with open(LOOKUP_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(lookup_html)
        print(f"Done. Open: {OUTPUT_FILE}")
        print(f"      Lookup: {LOOKUP_OUTPUT_FILE}")
    else:
        print(f"\nDone. Open: {OUTPUT_FILE}")

    conn.close()
    shutil.rmtree(os.path.dirname(tmp_db), ignore_errors=True)


if __name__ == "__main__":
    main()
