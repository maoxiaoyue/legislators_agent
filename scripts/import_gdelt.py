#!/usr/bin/env python3
"""Load GDELT 2.0 events about Taiwan, and the articles that mention them, into the news schema.

Reads the 15-minute Events and Mentions files of both the English and the translated stream,
so Taiwanese Chinese-language media are included. Articles from mainland China domains are
skipped. Files are streamed and discarded; only rows about Taiwan are stored.
Run from legagent/ after `go run . init-news`:

    python scripts/import_gdelt.py --start 2016-01-01
    python scripts/import_gdelt.py --start 2026-10-01 --end 2026-10-07

Each finished day is checkpointed, so the run can be stopped and resumed; a day re-run is a no-op.
"""

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from import_proceedings import USER_AGENT, connect, log  # noqa: E402

csv.field_size_limit(1 << 30)
BASE = "https://data.gdeltproject.org/gdeltv2/"
LISTS = {"en": "masterfilelist.txt", "tr": "masterfilelist-translation.txt"}
LIST_DIR = pathlib.Path("data/raw/gdelt")
STATE = pathlib.Path("data/raw/import_state/gdelt.json")

# Mainland outlets are out of scope; the list covers state media and the portals GDELT sees most often.
PRC_DOMAINS = re.compile(
    r"(^|\.)(cn|xinhuanet\.com|chinadaily\.com|globaltimes\.com|cgtn\.com|chinanews\.com|huanqiu\.com|cctv\.com|"
    r"163\.com|sohu\.com|qq\.com|ifeng\.com|thepaper\.cn|guancha\.cn|ycwb\.com|yangtse\.com|kaixian\.tv|cnfol\.com|"
    r"eastday\.com|hexun\.com|caixin\.com|jiemian\.com|sina\.com\.cn|people\.com|xinmin\.cn|cri\.cn|china\.com|"
    r"chinaxiaokang\.com|21jingji\.com|nbd\.com\.cn|bjnews\.com|takungpao\.com|wenweipo\.com|crntt\.com|chinaz\.com)$")
TW_DOMAINS = re.compile(
    r"(^|\.)(tw|udn\.com|chinatimes\.com|ettoday\.net|setn\.com|storm\.mg|thenewslens\.com|yam\.com|ctwant\.com|"
    r"nownews\.com|newtalk\.tw|upmedia\.mg|mirrormedia\.mg|cnyes\.com|taiwannews\.com|twreporter\.org|cw\.com\.tw|"
    r"businesstoday\.com|tvbs\.com|ftvnews\.com|ltn\.com|pts\.org|rti\.org|hinet\.net|money-link\.com|"
    r"taipeitimes\.com|focustaiwan\.tw|cna\.com|ctee\.com|wealth\.com|bnext\.com|technews\.tw|inside\.com)$")
HK_DOMAINS = re.compile(r"(^|\.)(hk|scmp\.com|hk01\.com|mingpao\.com|hket\.com|thestandard\.com|inmediahk\.net)$")

EVENT_SQL = """INSERT INTO news.gdelt_events (global_event_id, event_date, actor1_code, actor1_name, actor1_country,
    actor2_code, actor2_name, actor2_country, event_code, event_root_code, quad_class, goldstein, avg_tone,
    num_mentions, num_sources, num_articles, action_geo_name, action_geo_country, action_lat, action_lon,
    taiwan_match, translated, date_added)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (global_event_id) DO NOTHING"""
ARTICLE_SQL = """INSERT INTO news.articles (url, domain, source_region, language, first_seen_at, doc_tone, doc_length)
    VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING"""
LINK_SQL = """INSERT INTO news.article_events (article_id, global_event_id, mentioned_at, confidence, in_raw_text, sentence_id)
    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (article_id, global_event_id) DO NOTHING"""

_local = threading.local()


def region_of(domain):
    """Classify the outlet by domain; None means mainland China, which is not stored."""
    if PRC_DOMAINS.search(domain):
        return None
    if TW_DOMAINS.search(domain):
        return "TW"
    if HK_DOMAINS.search(domain):
        return "HK"
    return "foreign"


def file_lists(refresh):
    """Map (slot timestamp, stream) to its export and mentions URLs, taken from GDELT's own file lists."""
    LIST_DIR.mkdir(parents=True, exist_ok=True)
    slots = {}
    for stream, name in LISTS.items():
        path = LIST_DIR / name
        if refresh or not path.exists():
            resp = requests.get(BASE + name, timeout=600, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            path.write_bytes(resp.content)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            found = re.search(r"/(\d{14})\.(?:translation\.)?(export|mentions)\.CSV\.zip$", parts[2])
            if found:
                slots.setdefault((found.group(1), stream), {})[found.group(2)] = parts[2].replace("http://", "https://")
    return slots


def fetch(url):
    """Download and unzip one file; a missing file returns None so the gap is logged, not fatal."""
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
    for attempt in range(5):
        try:
            resp = session.get(url, timeout=120)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
            return archive.read(archive.namelist()[0]).decode("utf-8", errors="replace")
        except (requests.RequestException, zipfile.BadZipFile):
            time.sleep(2 ** attempt * 3)
    raise RuntimeError(f"gave up after 5 attempts: {url}")


def fetch_slot(job):
    stamp, stream, urls = job
    return stamp, stream, (fetch(urls["export"]) if "export" in urls else None), (fetch(urls["mentions"]) if "mentions" in urls else None)


def rows(text):
    return csv.reader(io.StringIO(text or ""), delimiter="\t", quoting=csv.QUOTE_NONE)


def num(value, kind=float):
    try:
        return kind(value) if value not in ("", None) else None
    except ValueError:
        return None


def stamp_time(value):
    try:
        return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def taiwan_events(text, translated):
    """Keep events where Taiwan is an actor or a location, recording which field matched."""
    found = []
    for r in rows(text):
        if len(r) < 61:
            continue
        match = [name for name, hit in (("actor1", r[7] == "TWN" or r[5].startswith("TWN")),
                                        ("actor2", r[17] == "TWN" or r[15].startswith("TWN")),
                                        ("action_geo", r[53] == "TW"), ("actor1_geo", r[37] == "TW"),
                                        ("actor2_geo", r[45] == "TW")) if hit]
        if not match:
            continue
        try:
            day = dt.datetime.strptime(r[1], "%Y%m%d").date()
        except ValueError:
            continue
        found.append((int(r[0]), day, r[5] or None, r[6] or None, r[7] or None, r[15] or None, r[16] or None, r[17] or None,
                      r[26], r[28], num(r[29], int), num(r[30]), num(r[34]), num(r[31], int), num(r[32], int), num(r[33], int),
                      r[52] or None, r[53] or None, num(r[56]), num(r[57]), match, translated, stamp_time(r[59])))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", help="last day to load, inclusive; default is the newest file")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--refresh-lists", action="store_true", help="download GDELT's file lists again")
    parser.add_argument("--no-resume", action="store_true", help="ignore the checkpoint and start at --start")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    start = args.start.replace("-", "")
    end = (args.end or "9999-12-31").replace("-", "")
    state = json.loads(STATE.read_text()) if STATE.exists() and not args.no_resume else {}
    if state.get("done_through", "") >= start:
        start = (dt.datetime.strptime(state["done_through"], "%Y%m%d") + dt.timedelta(days=1)).strftime("%Y%m%d")
    slots = file_lists(args.refresh_lists)
    # English before translated within a slot, so the set of Taiwan events grows in time order.
    jobs = sorted(((s, st, urls) for (s, st), urls in slots.items() if start <= s[:8] <= end), key=lambda j: (j[0], j[1]))
    log("gdelt slots", len(jobs), "from", start, "to", end)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT global_event_id FROM news.gdelt_events")
            known = {r[0] for r in cur.fetchall()}
        log("gdelt known Taiwan events", len(known))
        day, totals, started = None, None, time.time()

        def flush(finished_day):
            conn.commit()
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"done_through": finished_day}))
            log("gdelt day", finished_day, totals, f"{time.time() - started:.0f}s")

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for stamp, stream, export, mentions in pool.map(fetch_slot, jobs):
                if stamp[:8] != day:
                    if day is not None:
                        flush(day)
                    day = stamp[:8]
                    totals = {"events": 0, "articles": 0, "links": 0, "skipped_prc": 0, "missing_files": 0}
                totals["missing_files"] += (export is None) + (mentions is None)
                events = taiwan_events(export, stream == "tr")
                with conn.cursor() as cur:
                    if events:
                        cur.executemany(EVENT_SQL, events)
                        known.update(e[0] for e in events)
                        totals["events"] += len(events)
                    articles, links = {}, []
                    for r in rows(mentions):
                        if len(r) < 15 or r[3] != "1":
                            continue
                        event_id = num(r[0], int)
                        if event_id not in known:
                            continue
                        url = r[5].strip()
                        domain = (r[4] or urlsplit(url).hostname or "").lower().removeprefix("www.")
                        region = region_of(domain)
                        if region is None:
                            totals["skipped_prc"] += 1
                            continue
                        seen = stamp_time(r[2])
                        lang = re.search(r"srclc:(\w+)", r[14] or "")
                        articles.setdefault(url, (url, domain, region, lang.group(1) if lang else "eng", seen,
                                                  num(r[13]), num(r[12], int)))
                        links.append((url, event_id, seen, num(r[11], int), r[10] == "1", num(r[6], int)))
                    if articles:
                        cur.executemany(ARTICLE_SQL, list(articles.values()))
                        cur.execute("SELECT url, article_id FROM news.articles WHERE url = ANY(%s)", (list(articles),))
                        ids = dict(cur.fetchall())
                        cur.executemany(LINK_SQL, [(ids[u], *rest) for u, *rest in links if u in ids])
                        totals["articles"] += len(articles)
                        totals["links"] += len(links)
            if day is not None:
                flush(day)
    log("gdelt finished")


if __name__ == "__main__":
    main()
