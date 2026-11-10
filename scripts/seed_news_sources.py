#!/usr/bin/env python3
"""Build news.sources: one row per domain seen in news.articles, plus the hand-labelled list.

Re-run after each import to pick up new domains and refresh counts. Rows in
scripts/news_sources.csv are marked curated and always win over the automatic rules;
edit the CSV, not the table, so the labels survive a rebuild.
Run from legagent/ after `go run . init-news`:

    python scripts/seed_news_sources.py
"""

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from import_proceedings import connect, log  # noqa: E402

CURATED = pathlib.Path(__file__).resolve().parent / "news_sources.csv"
# Big News Network sites share this URL shape; 20 matches is enough to tell them from a stray hit.
BNN_URL = r"/news/[0-9]{9}/"
BNN_MIN = 20

COUNT_SQL = """INSERT INTO news.sources (domain, source_region, article_count, first_seen_at, last_seen_at)
    SELECT domain, min(source_region), count(*), min(first_seen_at), max(first_seen_at)
    FROM news.articles GROUP BY domain
    ON CONFLICT (domain) DO UPDATE SET article_count = EXCLUDED.article_count,
        first_seen_at = EXCLUDED.first_seen_at, last_seen_at = EXCLUDED.last_seen_at, updated_at = now()
    WHERE (news.sources.article_count, news.sources.last_seen_at)
        IS DISTINCT FROM (EXCLUDED.article_count, EXCLUDED.last_seen_at)"""
GOV_SQL = r"""UPDATE news.sources SET kind = 'government', updated_at = now()
    WHERE NOT curated AND kind IS NULL AND domain ~ '(^|\.)gov(\.[a-z]{2})?$'"""
BNN_SQL = """UPDATE news.sources s SET kind = 'content_farm', excluded = true,
        notes = 'Big News Network 轉貼站', updated_at = now()
    FROM (SELECT domain FROM news.articles WHERE url ~ %s GROUP BY domain HAVING count(*) >= %s) b
    WHERE s.domain = b.domain AND NOT s.curated AND s.kind IS DISTINCT FROM 'content_farm'"""
CURATED_SQL = """INSERT INTO news.sources (domain, name, source_region, country, kind, stance, excluded, notes, curated)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)
    ON CONFLICT (domain) DO UPDATE SET name = EXCLUDED.name, source_region = EXCLUDED.source_region,
        country = EXCLUDED.country, kind = EXCLUDED.kind, stance = EXCLUDED.stance, excluded = EXCLUDED.excluded,
        notes = EXCLUDED.notes, curated = true, updated_at = now()
    WHERE (news.sources.name, news.sources.source_region, news.sources.country, news.sources.kind,
           news.sources.stance, news.sources.excluded, news.sources.notes, news.sources.curated)
        IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.source_region, EXCLUDED.country, EXCLUDED.kind,
           EXCLUDED.stance, EXCLUDED.excluded, EXCLUDED.notes, true)"""
# The import scripts classify by regex; a hand-labelled region corrects the articles too.
REGION_SQL = """UPDATE news.articles a SET source_region = s.source_region, updated_at = now()
    FROM news.sources s WHERE s.curated AND a.domain = s.domain AND a.source_region <> s.source_region"""


def curated_rows():
    rows = []
    with CURATED.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            blank = lambda k: (r[k] or "").strip() or None  # noqa: E731
            rows.append((r["domain"].strip().lower(), blank("name"), r["source_region"].strip(), blank("country"),
                         blank("kind"), blank("stance"), (r["excluded"] or "").strip().lower() == "true", blank("notes")))
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = curated_rows()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(COUNT_SQL)
        log("sources counted", cur.rowcount)
        cur.execute(GOV_SQL)
        log("sources government", cur.rowcount)
        cur.execute(BNN_SQL, (BNN_URL, BNN_MIN))
        log("sources content farms", cur.rowcount)
        cur.executemany(CURATED_SQL, rows)
        log("sources curated", len(rows))
        cur.execute(REGION_SQL)
        log("articles region corrected", cur.rowcount)
        conn.commit()
        cur.execute("""SELECT source_region, count(*), count(*) FILTER (WHERE curated),
            count(*) FILTER (WHERE excluded), sum(article_count) FILTER (WHERE excluded)
            FROM news.sources GROUP BY 1 ORDER BY 1""")
        for region, total, curated, excluded, excluded_articles in cur.fetchall():
            log("sources", region, "domains", total, "curated", curated, "excluded", excluded,
                "excluded articles", excluded_articles or 0)


if __name__ == "__main__":
    main()
