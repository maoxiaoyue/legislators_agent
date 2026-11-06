#!/usr/bin/env python3
"""Load titles of Chinese-language articles from Taiwanese outlets out of GDELT's translated GKG stream.

GDELT's event files carry no Chinese articles, so Taiwanese Chinese media only appear in the
translated Global Knowledge Graph. Every article from a Taiwanese domain is kept, whatever its
topic; only the URL, title, time and tone are stored. Files are streamed and discarded.
Run from legagent/ after `go run . init-news`:

    python scripts/import_gdelt_titles.py --start 2024-01-01

Each finished day is checkpointed, so the run can be stopped and resumed.
"""

import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from import_gdelt import BASE, LIST_DIR, LISTS, fetch, file_lists, num, region_of, rows, stamp_time  # noqa: E402
from import_proceedings import connect, log  # noqa: E402

STATE = pathlib.Path("data/raw/import_state/gdelt_titles.json")
TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
# An article already stored from the event files keeps its row; the title is added if it had none.
ARTICLE_SQL = """INSERT INTO news.articles (url, domain, source_region, language, first_seen_at, doc_tone, doc_length, title, provider)
    VALUES (%s, %s, 'TW', 'zho', %s, %s, %s, %s, 'gdelt-gkg')
    ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title, updated_at = now()
    WHERE news.articles.title IS NULL"""


def gkg_slots(refresh):
    """List translated GKG files by timestamp, from GDELT's own file list."""
    if refresh or not (LIST_DIR / LISTS["tr"]).exists():
        file_lists(True)
    slots = {}
    for line in (LIST_DIR / LISTS["tr"]).read_text(encoding="utf-8", errors="replace").splitlines():
        found = re.search(r"/(\d{14})\.translation\.gkg\.csv\.zip$", line)
        if found:
            slots[found.group(1)] = BASE + found.group(0)[1:]
    return slots


def taiwan_titles(text):
    """Keep Chinese articles from Taiwanese domains; other Chinese outlets are left out until their origin is checked."""
    found = {}
    for r in rows(text):
        if len(r) < 27 or not (r[25] or "").startswith("srclc:zho"):
            continue
        domain = (r[3] or "").lower().removeprefix("www.")
        if region_of(domain) != "TW":
            continue
        title = TITLE.search(r[26] or "")
        title = re.sub(r"\s+", " ", html.unescape(title.group(1))).strip() if title else None
        tone = (r[15] or "").split(",")
        url = r[4].strip()
        found[url] = (url, domain, stamp_time(r[1]), num(tone[0]) if tone else None,
                      num(tone[6], int) if len(tone) > 6 else None, title or None)
    return list(found.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", help="last day to load, inclusive; default is the newest file")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--refresh-lists", action="store_true", help="download GDELT's file list again")
    parser.add_argument("--no-resume", action="store_true", help="ignore the checkpoint and start at --start")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    start = args.start.replace("-", "")
    end = (args.end or "9999-12-31").replace("-", "")
    state = json.loads(STATE.read_text()) if STATE.exists() and not args.no_resume else {}
    if state.get("done_through", "") >= start:
        start = (dt.datetime.strptime(state["done_through"], "%Y%m%d") + dt.timedelta(days=1)).strftime("%Y%m%d")
    jobs = sorted((s, u) for s, u in gkg_slots(args.refresh_lists).items() if start <= s[:8] <= end)
    log("gdelt-titles slots", len(jobs), "from", start, "to", end)

    with connect() as conn:
        day, totals, started = None, None, time.time()

        def flush(finished_day):
            conn.commit()
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"done_through": finished_day}))
            log("gdelt-titles day", finished_day, totals, f"{time.time() - started:.0f}s")

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for stamp, text in zip((s for s, _ in jobs), pool.map(lambda job: fetch(job[1]), jobs)):
                if stamp[:8] != day:
                    if day is not None:
                        flush(day)
                    day = stamp[:8]
                    totals = {"articles": 0, "no_title": 0, "missing_files": 0}
                if text is None:
                    totals["missing_files"] += 1
                    continue
                found = taiwan_titles(text)
                if found:
                    with conn.cursor() as cur:
                        cur.executemany(ARTICLE_SQL, found)
                    totals["articles"] += len(found)
                    totals["no_title"] += sum(f[5] is None for f in found)
            if day is not None:
                flush(day)
    log("gdelt-titles finished")


if __name__ == "__main__":
    main()
