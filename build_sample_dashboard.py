#!/usr/bin/env python3
"""Regenerate iMessage_Dashboard_SAMPLE.html from dashboard_template.html."""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_FILE = os.path.join(SCRIPT_DIR, "iMessage_Dashboard_SAMPLE.html")
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "dashboard_template.html")
OUTPUT_FILE = SAMPLE_FILE

# Reuse reply-bucket logic from the main builder.
from build_dashboard import _burst_responses, _response_buckets  # noqa: E402


def _extract_data_blob(html: str) -> dict:
    match = re.search(r'id="data-blob">(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("Could not find data blob in sample file")
    return json.loads(match.group(1))


def _years_from_messages(messages) -> list[str]:
    years = sorted({str(datetime.fromtimestamp(m[0] / 1000).year) for m in messages})
    return years or [str(datetime.now().year)]


def _month_key(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000)
    return f"{d.year}-{d.month:02d}"


def _day_key(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def _top_emoji(n=12):
    emojis = ["😂", "❤️", "👍", "🔥", "😭", "✨", "🙏", "💀", "😊", "🥺", "😅", "🎉"]
    return [[e, 120 - i * 7] for i, e in enumerate(emojis[:n])]


def _len_stats(avg=42.0, count=1200):
    return {"avg_chars": avg, "median_chars": max(1, avg - 6), "count": count}


def _sample_longest(chat_name: str, side: str, idx: int, year: int):
    text = (
        f"This is a sample long message in {chat_name}. "
        f"It shows how the longest-message section looks when someone sends "
        f"a wall of text instead of a quick reply. ({side} #{idx + 1})"
    )
    if idx == 0:
        text += " " + ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 8)
    return {
        "chat": chat_name,
        "ts": int(datetime(year, 6, 15 + idx, 14, 30).timestamp() * 1000),
        "chars": len(text),
        "preview": text[:120] + ("…" if len(text) > 120 else ""),
        "full": text,
    }


def _vocab_by_year(word_freq: dict, years: list[str]):
    def slice_pairs(items, scale=1.0):
        out = []
        for word, count in items[:40]:
            out.append([word, max(1, int(count * scale))])
        return out

    by_year = {}
    for gram in ("unigrams", "bigrams", "messages"):
        sent, received = {}, {}
        for i, year in enumerate(years):
            scale = 0.75 + (i % 3) * 0.1
            sent[year] = slice_pairs(word_freq.get(gram, {}).get("sent", []), scale)
            received[year] = slice_pairs(word_freq.get(gram, {}).get("received", []), scale * 0.9)
        by_year[gram] = {"sent": sent, "received": received}
    return by_year


def _build_person_profiles(data: dict) -> dict:
    handles = data["handles"]
    chats = data["chats"]
    messages = data["messages"]
    person_to_1on1 = {}
    for cid, chat in chats.items():
        if not chat.get("group") and len(chat.get("participants", [])) == 1:
            person_to_1on1[chat["participants"][0]] = cid

    profiles = {}
    for pid, name in handles.items():
        one_on_one = []
        hour_counts = [0] * 24
        monthly_sent = Counter()
        monthly_received = Counter()
        daily_sent = Counter()
        daily_received = Counter()

        for ts, chat_id, sender, is_from_me in messages:
            chat = chats.get(chat_id, {})
            if chat.get("group"):
                continue
            if chat.get("participants") != [pid]:
                continue
            one_on_one.append((ts, bool(is_from_me)))
            h = datetime.fromtimestamp(ts / 1000).hour
            hour_counts[h] += 1
            mk = _month_key(ts)
            dk = _day_key(ts)
            if is_from_me:
                monthly_sent[mk] += 1
                daily_sent[dk] += 1
            else:
                monthly_received[mk] += 1
                daily_received[dk] += 1

        one_on_one.sort(key=lambda x: x[0])
        bursts = _burst_responses(one_on_one)
        sent = sum(1 for _, me in one_on_one if me)
        received = sum(1 for _, me in one_on_one if not me)

        profiles[pid] = {
            "sent": sent,
            "received": received,
            "your_response_ms": 8 * 60_000 if bursts["your"] else None,
            "their_response_ms": 22 * 60_000 if bursts["their"] else None,
            "your_response_pcts": _response_buckets(bursts["your"]) or _response_buckets([90_000, 240_000, 600_000, 1_800_000]),
            "their_response_pcts": _response_buckets(bursts["their"]) or _response_buckets([300_000, 900_000, 2_400_000, 7_200_000]),
            "hour_counts": hour_counts,
            "group_chats": [],
            "game_pigeon": 3 if pid == "p0" else 0,
            "game_pigeon_types": [["8 Ball", 2], ["Cup Pong", 1]] if pid == "p0" else [],
            "top_words": (data.get("word_freq", {}).get("unigrams", {}).get("overall", []))[:12],
            "top_phrases": (data.get("word_freq", {}).get("bigrams", {}).get("overall", []))[:8],
            "emojis_sent": _top_emoji(8),
            "emojis_received": _top_emoji(6),
            "avg_chars_sent": 36.5,
            "avg_chars_received": 44.2,
            "monthly_sent": dict(monthly_sent),
            "monthly_received": dict(monthly_received),
            "daily_sent": dict(daily_sent),
            "daily_received": dict(daily_received),
        }

    return profiles


def _build_chat_profiles(data: dict) -> dict:
    chats = data["chats"]
    messages = data["messages"]
    profiles = {}
    for cid, chat in chats.items():
        if not chat.get("group"):
            continue
        sender_counts = Counter()
        monthly = Counter()
        daily = Counter()
        hour_counts = [0] * 24
        by_sender_month = defaultdict(Counter)
        by_sender_day = defaultdict(Counter)

        for ts, chat_id, sender, is_from_me in messages:
            if chat_id != cid:
                continue
            who = "me" if is_from_me else str(sender)
            sender_counts[who] += 1
            mk = _month_key(ts)
            dk = _day_key(ts)
            monthly[mk] += 1
            daily[dk] += 1
            by_sender_month[who][mk] += 1
            by_sender_day[who][dk] += 1
            hour_counts[datetime.fromtimestamp(ts / 1000).hour] += 1

        word_freq = data.get("chat_word_freq", {}).get(cid, {})
        profiles[cid] = {
            "name": chat["name"],
            "total": sum(sender_counts.values()),
            "participants": chat.get("participants", []),
            "sender_counts": [[pid, cnt] for pid, cnt in sender_counts.most_common()],
            "top_words": word_freq.get("unigrams", [])[:15],
            "top_phrases": word_freq.get("bigrams", [])[:8],
            "top_emojis": _top_emoji(10),
            "hour_counts": hour_counts,
            "activity_by_month": dict(monthly),
            "activity_by_sender_month": {pid: dict(c) for pid, c in by_sender_month.items()},
            "activity_by_day": dict(daily),
            "activity_by_sender_day": {pid: dict(c) for pid, c in by_sender_day.items()},
        }
    return profiles


def augment_sample_data(data: dict) -> dict:
    messages = data["messages"]
    years = _years_from_messages(messages)
    chats = data["chats"]
    word_freq = data.setdefault("word_freq", {})

    word_freq["by_year"] = _vocab_by_year(word_freq, years)

    data["emoji_freq"] = {
        "sent": _top_emoji(),
        "received": _top_emoji(),
        "overall": _top_emoji(),
        "by_year_sent": {y: _top_emoji() for y in years},
        "by_year_received": {y: _top_emoji() for y in years},
    }

    data["msg_length"] = {
        "sent": _len_stats(38.4, len(messages) // 2),
        "received": _len_stats(45.1, len(messages) // 2),
        "by_year_sent": {y: _len_stats(35 + int(y) % 8) for y in years},
        "by_year_received": {y: _len_stats(42 + int(y) % 6) for y in years},
    }

    group_names = [c["name"] for c in chats.values() if c.get("group")]
    sent_msgs = [_sample_longest(group_names[i % len(group_names)] if group_names else "Sam", "sent", i, int(years[i % len(years)])) for i in range(3)]
    recv_msgs = [_sample_longest(data["handles"].get("p1", "Priya"), "received", i, int(years[i % len(years)])) for i in range(3)]
    data["longest_messages"] = {
        "all": {"sent": sent_msgs, "received": recv_msgs},
        "by_year": {
            y: {
                "sent": [_sample_longest("Sample Chat", "sent", 0, int(y))],
                "received": [_sample_longest("Sample Chat", "received", 0, int(y))],
            }
            for y in years
        },
    }

    data["game_pigeon"] = {"by_type": [["8 Ball", 14], ["Cup Pong", 9], ["Anagrams", 4]]}
    data["person_profiles"] = _build_person_profiles(data)
    data["chat_profiles"] = _build_chat_profiles(data)
    data["meta"]["generated_at"] = int(datetime.now().timestamp() * 1000)
    return data


def main():
    with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
        sample_html = f.read()
    data = augment_sample_data(_extract_data_blob(sample_html))

    json_data = (
        json.dumps(data, separators=(",", ":"))
        .replace("</script>", r"<\/script>")
        .replace("<!--", r"<\!--")
    )

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__IMESSAGE_DATA__", json_data)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Done. Open: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
