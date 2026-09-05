#!/usr/bin/env python3
"""
Syncs the Backtesting database from Notion and computes summary stats
for the dashboard (direction bias, R:R distribution, entry models,
session split, entry-time-of-day, and hold-duration analysis).

Requires env vars:
  NOTION_TOKEN        - Notion integration token
  BACKTESTING_DB_ID   - data source / database ID for the Backtesting DB
"""
import os
import sys
import json
import datetime as dt
from collections import Counter, defaultdict
import urllib.request
import urllib.error

NOTION_TOKEN = os.environ.get("NOTION_TOKEN") or None
DB_ID = os.environ.get("BACKTESTING_DB_ID") or "207f7bb7-7d6d-80d7-b4f0-000bec43a2e3"
# This workspace uses Notion's multi-source database model, so the ID above is
# a *data source* ID (not a classic database ID) and needs the newer
# data_sources endpoint + API version rather than /v1/databases/{id}/query.
NOTION_VERSION = "2025-09-03"
API_URL = f"https://api.notion.com/v1/data_sources/{DB_ID}/query"

# US Eastern Time DST transition dates (2nd Sun in March / 1st Sun in Nov)
# Extend this table as years roll forward.
DST_RANGES = [
    (dt.date(2025, 3, 9), dt.date(2025, 11, 2)),
    (dt.date(2026, 3, 8), dt.date(2026, 11, 1)),
    (dt.date(2027, 3, 14), dt.date(2027, 11, 7)),
]


def is_edt(d: dt.date) -> bool:
    for start, end in DST_RANGES:
        if start <= d < end:
            return True
    return False


def to_et(iso_str):
    """Convert a Notion UTC ISO datetime string to US Eastern local time."""
    if not iso_str:
        return None
    ts = iso_str.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(ts)
    offset = 4 if is_edt(d.date()) else 5
    return d - dt.timedelta(hours=offset)


def notion_request(payload, cursor=None):
    body = dict(payload)
    if cursor:
        body["start_cursor"] = cursor
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_all_pages():
    results = []
    cursor = None
    while True:
        data = notion_request({"page_size": 100}, cursor)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def get_prop(page, name, kind):
    prop = page.get("properties", {}).get(name)
    if not prop:
        return None
    if kind == "select":
        sel = prop.get("select")
        return sel["name"] if sel else None
    if kind == "date_start":
        d = prop.get("date")
        return d["start"] if d else None
    if kind == "date_end":
        d = prop.get("date")
        return d["end"] if d else None
    if kind == "relation":
        return prop.get("relation", [])
    if kind == "title":
        t = prop.get("title", [])
        return t[0]["plain_text"] if t else None
    return None


def bucket_5min(t: dt.datetime) -> str:
    minute = (t.minute // 5) * 5
    return f"{t.hour:02d}:{minute:02d}"


def build_stats(pages):
    total = len(pages)
    direction = Counter()
    rr = Counter()
    monthly = Counter()
    entry_models = Counter()
    am_count = 0
    pm_count = 0
    entry_buckets = Counter()
    durations = []

    for p in pages:
        name = get_prop(p, "Name", "title") or "Untitled"
        position = get_prop(p, "Position", "select")
        r_r = get_prop(p, "R R", "select")
        start_iso = get_prop(p, "Date", "date_start")
        end_iso = get_prop(p, "Date", "date_end")
        am_fw = get_prop(p, "AM FRAMEWORK", "relation") or []
        pm_fw = get_prop(p, "PM FRAMEWORK", "relation") or []
        models = get_prop(p, "ENTRY MODELS", "relation") or []

        direction[position or "Unset"] += 1
        if r_r:
            rr[r_r] += 1
        if am_fw:
            am_count += 1
        if pm_fw:
            pm_count += 1
        for m in models:
            entry_models[m.get("id", "unknown")] += 1

        start_et = to_et(start_iso)
        if start_et:
            monthly[start_et.strftime("%Y-%m")] += 1
            entry_buckets[bucket_5min(start_et)] += 1

        if start_iso and end_iso:
            s = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            mins = round((e - s).total_seconds() / 60)
            if mins >= 0:
                durations.append(mins)

    durations.sort()
    n = len(durations)
    median = (
        durations[n // 2]
        if n % 2
        else (durations[n // 2 - 1] + durations[n // 2]) / 2
    ) if n else None
    mean = round(sum(durations) / n, 1) if n else None

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "total_trades": total,
        "direction": dict(direction),
        "rr_distribution": dict(rr),
        "monthly_volume": dict(sorted(monthly.items())),
        "session_split": {"AM": am_count, "PM": pm_count},
        "entry_time_buckets": dict(sorted(entry_buckets.items())),
        "duration": {
            "count": n,
            "mean_minutes": mean,
            "median_minutes": median,
            "min_minutes": durations[0] if n else None,
            "max_minutes": durations[-1] if n else None,
            "raw": durations,
        },
    }


def write_debug(message):
    os.makedirs("data", exist_ok=True)
    with open("data/debug.log", "w") as f:
        f.write(message + "\n")


def main():
    if not NOTION_TOKEN:
        write_debug("NOTION_TOKEN not set")
        print("NOTION_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    try:
        pages = fetch_all_pages()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        write_debug(f"Notion API error: {e.code}\nURL: {API_URL}\nBody: {body}")
        print(f"Notion API error: {e.code} {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        write_debug(f"Unexpected error: {type(e).__name__}: {e}")
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

    stats = build_stats(pages)
    os.makedirs("data", exist_ok=True)
    with open("data/backtest.json", "w") as f:
        json.dump(stats, f, indent=2)
    # Clear any stale debug log from a previous failed run
    if os.path.exists("data/debug.log"):
        os.remove("data/debug.log")
    print(f"Synced {stats['total_trades']} trades -> data/backtest.json")


if __name__ == "__main__":
    main()
