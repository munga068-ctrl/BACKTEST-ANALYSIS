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
# The classic parent database page ID (distinct from the data source ID used
# elsewhere) — the data_sources/2025-09-03 preview endpoint was returning
# empty relation arrays for cross-database relations despite correct sharing,
# so we use the older, well-established databases/query endpoint instead.
# Hardcoded directly (not overridable via env var) since a stale
# BACKTESTING_DB_ID secret from an earlier setup step would otherwise
# silently reintroduce the broken data-source-ID endpoint.
DB_ID = "207f7bb7-7d6d-8083-b3e9-fe13b9253159"
NOTION_VERSION = "2022-06-28"
API_URL = f"https://api.notion.com/v1/databases/{DB_ID}/query"

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


# Entry model relation IDs -> display names (from the ENTRY MODELS data source).
# Hardcoded because resolving these dynamically would mean one extra Notion API
# call per unique model; update this map if models are renamed or added.
ENTRY_MODEL_NAMES = {
    "269f7bb7-7d6d-8083-9a48-e3fae950184f": "2022 Model",
    "269f7bb7-7d6d-8001-9a9e-de24422e97ae": "SB",
    "269f7bb7-7d6d-8058-a69e-f3ad37c52e15": "OTE",
    "269f7bb7-7d6d-8060-8554-fa7394e1bc3f": "BREAKER",
    "269f7bb7-7d6d-8059-ab77-d9c05c6077b0": "IFVG in Breaker",
    "288f7bb7-7d6d-804d-bce7-c2e8a2ed1889": "1st PFVG",
    "28df7bb7-7d6d-8028-9dcc-c60bc4ae8d7c": "None",
}


def build_stats(pages):
    total = len(pages)
    direction = Counter()
    rr = Counter()
    monthly = Counter()
    entry_models = Counter()
    am_count = 0
    pm_count = 0
    entry_buckets_am = Counter()
    entry_buckets_pm = Counter()
    durations = []
    durations_am = []
    durations_pm = []

    for p in pages:
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
        is_am = bool(am_fw)
        is_pm = bool(pm_fw)
        if is_am:
            am_count += 1
        if is_pm:
            pm_count += 1
        for m in models:
            name = ENTRY_MODEL_NAMES.get(m.get("id", ""), m.get("id", "unknown"))
            entry_models[name] += 1

        start_et = to_et(start_iso)
        if start_et:
            monthly[start_et.strftime("%Y-%m")] += 1
            bucket = bucket_5min(start_et)
            if is_am:
                entry_buckets_am[bucket] += 1
            elif is_pm:
                entry_buckets_pm[bucket] += 1

        if start_iso and end_iso:
            s = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            mins = round((e - s).total_seconds() / 60)
            if mins >= 0:
                durations.append(mins)
                if is_am:
                    durations_am.append(mins)
                elif is_pm:
                    durations_pm.append(mins)

    def mean_median(vals):
        if not vals:
            return None, None
        vals = sorted(vals)
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        return round(sum(vals) / n, 1), med

    durations.sort()
    n = len(durations)
    mean, median = mean_median(durations)
    am_mean, am_median = mean_median(durations_am)
    pm_mean, pm_median = mean_median(durations_pm)

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "total_trades": total,
        "direction": dict(direction),
        "rr_distribution": dict(rr),
        "monthly_volume": dict(sorted(monthly.items())),
        "session_split": {"AM": am_count, "PM": pm_count},
        "entry_model_usage": dict(entry_models.most_common()),
        "entry_time_buckets_am": dict(sorted(entry_buckets_am.items())),
        "entry_time_buckets_pm": dict(sorted(entry_buckets_pm.items())),
        "duration": {
            "count": n,
            "mean_minutes": mean,
            "median_minutes": median,
            "min_minutes": durations[0] if n else None,
            "max_minutes": durations[-1] if n else None,
            "am_mean": am_mean,
            "am_median": am_median,
            "pm_mean": pm_mean,
            "pm_median": pm_median,
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
