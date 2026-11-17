#!/usr/bin/env python3
"""Free, rule-based Iraq news alerting over Google News RSS and Telegram."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "feeds.json"
BAGHDAD = ZoneInfo("Asia/Baghdad")
USER_AGENT = "IraqNewsMonitor/1.0 (RSS headlines; contact: repository owner)"
MAX_ALERTS_PER_DAY = 5
MAX_SEEN = 2500
MAX_AGE = timedelta(hours=36)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def item_key(url: str, title: str) -> str:
    normalized = (url.strip() or title.strip()).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode(
        {"q": query + " when:1d", "hl": "ar", "gl": "IQ", "ceid": "IQ:ar"}
    )
    return "https://news.google.com/rss/search?" + params


def fetch_feed(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = response.read(2_000_000)
    root = ET.fromstring(payload)
    items = []
    for node in root.findall(".//item"):
        title = clean_text(node.findtext("title", default=""))
        link = clean_text(node.findtext("link", default=""))
        description = clean_text(node.findtext("description", default=""))
        source_node = node.find("source")
        source = clean_text(source_node.text or "") if source_node is not None else ""
        raw_date = node.findtext("pubDate", default="")
        try:
            published = parsedate_to_datetime(raw_date).astimezone(BAGHDAD)
        except (TypeError, ValueError, OverflowError):
            published = None
        if title and link:
            items.append(
                {
                    "title": title,
                    "url": link,
                    "description": description,
                    "source": source or "Google News RSS",
                    "published": published,
                }
            )
    return items


def classify(item: dict, categories: dict, excluded_terms: list[str]):
    text = f"{item['title']} {item['description']}".casefold()
    if any(term.casefold() in text for term in excluded_terms):
        return None

    matches = []
    for name, terms in categories.items():
        if any(term.casefold() in text for term in terms):
            matches.append(name)
    if not matches:
        return None

    priority = {"social": 4, "humor": 3, "important": 2, "hawza": 1}
    category = sorted(matches, key=lambda name: priority.get(name, 0), reverse=True)[0]
    urgent_terms = ["عاجل", "فوري", "الآن", "وفاة", "استشهاد", "زلزال", "حريق", "فيضانات"]
    urgent = any(term.casefold() in text for term in urgent_terms)
    return category, urgent


def post_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID repository secret."
        )
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message; verify the bot and chat ID.")


def format_alert(item: dict, category: str, urgent: bool) -> str:
    labels = {
        "social": "موضوع اجتماعي",
        "humor": "فكاهة محترمة",
        "important": "خبر مهم",
        "hawza": "الحوزة في النجف",
    }
    prefix = "تنبيه عاجل" if urgent else "موضوع مرشّح"
    summary = item["description"] or "ما متوفر ملخص في موجز المصدر؛ راجع رابط الخبر للتفاصيل."
    summary = summary[:550].rstrip()
    published = item["published"].strftime("%Y-%m-%d %H:%M بتوقيت بغداد") if item["published"] else "وقت النشر غير متوفر"
    return (
        f"{prefix} | {labels[category]}\n\n"
        f"{item['title']}\n\n"
        f"{summary}\n\n"
        f"المصدر: {item['source']}\n"
        f"التوقيت: {published}\n"
        f"الرابط: {item['url']}\n\n"
        "هذا مقترح للرصد فقط؛ ماكو تصميم أو نشر تلقائي."
    )


def main() -> int:
    config = read_json(CONFIG_PATH, {})
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or not os.environ.get(
        "TELEGRAM_CHAT_ID", ""
    ).strip():
        print(
            "Setup required: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as GitHub Actions secrets."
        )
        return 0
    if os.environ.get("TEST_NOTIFICATION", "").casefold() == "true":
        post_telegram(
            "اختبار رصد سامي أفندي: اتصال Telegram يعمل. هذا اختبار فقط، وليس خبرًا أو منشورًا."
        )
        print("Telegram test message sent.")
        return 0

    now = datetime.now(BAGHDAD)
    today = now.date().isoformat()
    state = read_json(STATE_PATH, {"date": today, "sent_today": 0, "seen": []})
    if state.get("date") != today:
        state = {"date": today, "sent_today": 0, "seen": state.get("seen", [])}

    seen = set(state.get("seen", []))
    candidates = {}
    errors = []
    for query in config.get("queries", []):
        try:
            for item in fetch_feed(google_news_url(query)):
                published = item["published"]
                if published and now - published > MAX_AGE:
                    continue
                key = item_key(item["url"], item["title"])
                if key in seen or key in candidates:
                    continue
                classification = classify(
                    item, config.get("categories", {}), config.get("excluded_terms", [])
                )
                if classification:
                    category, urgent = classification
                    item["key"] = key
                    item["category"] = category
                    item["urgent"] = urgent
                    candidates[key] = item
        except (OSError, urllib.error.URLError, ET.ParseError, TimeoutError) as error:
            errors.append(f"RSS fetch issue: {type(error).__name__}")

    order = {"social": 0, "humor": 1, "important": 2, "hawza": 3}
    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            not item["urgent"],
            order.get(item["category"], 9),
            -(item["published"].timestamp() if item["published"] else 0),
        ),
    )
    available = max(0, MAX_ALERTS_PER_DAY - int(state.get("sent_today", 0)))
    selected = ordered[:available]

    send_errors = []
    sent_keys = set()
    for item in selected:
        try:
            post_telegram(format_alert(item, item["category"], item["urgent"]))
            state["sent_today"] = int(state.get("sent_today", 0)) + 1
            sent_keys.add(item["key"])
            time.sleep(1)
        except Exception as error:
            send_errors.append(f"Telegram send failed: {type(error).__name__}")

    # Count successfully delivered alerts only. Dropped alerts are marked seen
    # so a full daily cap does not release a stale backlog the next morning.
    seen.update(item["key"] for item in ordered if item not in selected)
    seen.update(sent_keys)
    state["date"] = today
    state["seen"] = list(seen)[-MAX_SEEN:]
    save_state(state)

    if send_errors:
        print("; ".join(send_errors), file=sys.stderr)
        return 1

    if errors and not candidates:
        print("No feed results; one or more RSS sources could not be reached.")
    else:
        print(f"Scanned feeds; sent {len(selected)} alert(s). Daily count: {state['sent_today']}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Monitor stopped: {error}", file=sys.stderr)
        raise
