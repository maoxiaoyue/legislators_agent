#!/usr/bin/env python3
"""Load council meetings, documents, bills and votes into the proceedings schema.

One-off importer, kept here so it can become a scheduled job later. Every step upserts
on source_key and checkpoints by page, so it can be stopped and re-run safely.
Run from legagent/ after `go run . init-proceedings`:

    pip install "psycopg[binary]" pyyaml requests pymupdf
    python scripts/import_proceedings.py ly-meets
    python scripts/import_proceedings.py ly-votes ...

Downloaded files are converted to text and discarded, never stored on disk.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import psycopg
import requests
import yaml
from psycopg.types.json import Jsonb

CONFIG_PATH = "config/config.yaml"
STATE_DIR = pathlib.Path("data/raw/import_state")
LY_API = "https://ly.govapi.tw/v2"
LY_DOC = "https://v2.ly.govapi.tw/gazette_agenda_doc"
TAICHUNG_API = "https://yishi.tccc.gov.tw/api"
USER_AGENT = "legagent-import/0.1"
ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
VOTE_METHODS = {"記名表決": "記名", "記名": "記名", "無記名": "無記名", "無記名表決": "無記名", "唱名表決": "唱名",
                "舉手表決": "舉手", "舉手": "舉手", "表決器": "表決器"}


def log(*parts):
    print(dt.datetime.now().strftime("%H:%M:%S"), *parts, flush=True)


def connect():
    raw = pathlib.Path(CONFIG_PATH).read_text(encoding="utf-8")
    expanded = ENV_VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), raw)
    return psycopg.connect(yaml.safe_load(expanded)["database"]["dsn"])


class Fetcher:
    """Paces requests per instance and backs off on rate limits and transient failures."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, url, attempts=8, **kwargs):
        delay = 15
        for attempt in range(1, attempts + 1):
            wait = self.min_interval - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                resp = self.session.get(url, timeout=120, **kwargs)
            except requests.RequestException as err:
                log("request error", attempt, type(err).__name__, url[:100])
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {resp.status_code} for {url[:120]}")
                log("retryable status", resp.status_code, attempt, url[:100])
            time.sleep(delay)
            delay = min(delay * 2, 600)
        raise RuntimeError(f"gave up after {attempts} attempts: {url[:120]}")


def load_page(step):
    path = STATE_DIR / f"{step}.json"
    return json.loads(path.read_text())["next_page"] if path.exists() else 1


def save_page(step, page):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{step}.json").write_text(json.dumps({"next_page": page}))


def clean(value):
    """Strip NUL characters, which PostgreSQL text and jsonb reject."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>|<br\s*/?>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t　]+", " ", text)).strip()


def split_names(text):
    return [n.strip() for n in re.split(r"[、,，;；\n]+", text or "") if n.strip() and n.strip() != "-"]


def as_names(value):
    """Sources give names as a list or a delimited string."""
    return [str(n).strip() for n in value if str(n).strip()] if isinstance(value, list) else split_names(value)


def meeting_ids(conn, council):
    with conn.cursor() as cur:
        cur.execute("SELECT source_key, meeting_id FROM proceedings.meetings WHERE council_code = %s", (council,))
        return dict(cur.fetchall())


# ---------- Legislative Yuan ----------

def ly_pages(step, resource, key, limit, filters=None):
    """Yield (page, rows) from a ly.govapi.tw list endpoint, resuming from the checkpoint."""
    fetcher = Fetcher(min_interval=4)
    page = load_page(step)
    while True:
        data = fetcher.get(f"{LY_API}/{resource}", params={"limit": limit, "page": page, **(filters or {})}).json()
        rows = data.get(key) or []
        if not rows:
            return
        yield page, rows, data.get("total_page"), data.get("total")
        save_page(step, page + 1)
        page += 1


def ly_total(resource, filters):
    time.sleep(4)
    return Fetcher(min_interval=0).get(f"{LY_API}/{resource}", params={"limit": 1, **filters}).json().get("total") or 0


LY_WINDOW = 10000  # the API rejects requests past this offset (HTTP 413), so large sets are read in segments


def ly_segments(resource, key, limit, step, terms):
    """Read by term, or by term and session when a term alone exceeds the window; warn if segments miss rows."""
    grand = 0
    for term in terms:
        total = ly_total(resource, {"屆": term})
        if not total:
            continue
        if total <= LY_WINDOW:
            segments = [{"屆": term}]
        else:
            segments = [{"屆": term, "會期": s} for s in range(1, 16)]
        covered = 0
        for filters in segments:
            name = f"{step}-" + "-".join(str(v) for v in filters.values())
            for page, rows, total_page, seg_total in ly_pages(name, resource, key, limit, filters):
                yield page, rows, f"{name}:{total_page}", seg_total
            if load_page(name) > 1:
                covered += ly_total(resource, filters)
        grand += total
        if covered != total:
            log("WARNING segment gap", resource, "term", term, "term total", total, "segment total", covered)
    log(resource, "term totals sum", grand)


def step_ly_meets(conn):
    sql = """INSERT INTO proceedings.meetings (council_code, source_key, term, session_no, session_type, meeting_no,
             meeting_kind, committee, title, meeting_dates, source_url, source_data)
             VALUES ('ly', %s, %s, %s, %s, %s, %s, %s, %s, %s::date[], %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET term = EXCLUDED.term, session_no = EXCLUDED.session_no,
             session_type = EXCLUDED.session_type, meeting_no = EXCLUDED.meeting_no, meeting_kind = EXCLUDED.meeting_kind,
             committee = EXCLUDED.committee, title = EXCLUDED.title, meeting_dates = EXCLUDED.meeting_dates,
             source_url = EXCLUDED.source_url, source_data = EXCLUDED.source_data, updated_at = now()"""
    for page, rows, total_page, total in ly_pages("ly-meets", "meets", "meets", 100):
        with conn.cursor() as cur:
            for m in rows:
                info = (m.get("會議資料") or [{}])[0]
                extra = info.get("臨時會會次")
                session_type = "臨時會" if extra not in (None, "null", "") else None
                dates = [d for d in (m.get("日期") or []) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d))]
                cur.execute(sql, (m["會議代碼"], to_int(m.get("屆")), to_int(m.get("會期")), session_type,
                                  to_int(m.get("會次")), m.get("會議種類"), "、".join(m.get("委員會代號:str") or []) or None,
                                  clean(m.get("會議標題") or m.get("name") or m["會議代碼"]), dates,
                                  info.get("ppg_url"), Jsonb(clean(m))))
        conn.commit()
        log("ly-meets page", page, "/", total_page, "total", total)


def legislator_lookup(conn):
    """Map (term, name) to legislator_no from public.legislator_terms, ignoring spacing and name punctuation."""
    with conn.cursor() as cur:
        cur.execute("SELECT term, name, legislator_no FROM public.legislator_terms")
        return {(t, name_key(n)): no for t, n, no in cur.fetchall()}


def name_key(name):
    return re.sub(r"[\s．‧·•.]", "", name)


def parse_vote_time(text, dates):
    """Return a timestamp only when the date is certain; a missing year is filled solely from a matching meeting date."""
    m = re.search(r"(\d*)年(\d+)月(\d+)日\s*(上午|下午)?\s*(\d+)時(\d+)分(\d+)秒", text or "")
    if not m:
        return None
    year_text, month, day, noon, hour, minute, second = m.groups()
    month, day, hour, minute, second = int(month), int(day), int(hour), int(minute), int(second)
    if noon == "下午" and hour < 12:
        hour += 12
    if noon == "上午" and hour == 12:
        hour = 0
    if year_text:
        year = int(year_text) + 1911
    else:
        years = {d.year for d in dates if d.month == month and d.day == day}
        if len(years) != 1:
            return None
        year = years.pop()
    try:
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        return None


def step_ly_votes(conn):
    meetings = meeting_ids(conn, "ly")
    with conn.cursor() as cur:
        cur.execute("SELECT source_key, meeting_dates FROM proceedings.meetings WHERE council_code = 'ly'")
        meeting_dates = dict(cur.fetchall())
    names = legislator_lookup(conn)
    unmatched, conflicts, mismatches, repeats = set(), 0, 0, 0
    sql = """INSERT INTO proceedings.votes (council_code, source_key, meeting_id, voted_at, voted_at_text, vote_method, subject,
             present_count, yes_count, no_count, abstain_count, source_data)
             VALUES ('ly', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id, voted_at = EXCLUDED.voted_at,
             voted_at_text = EXCLUDED.voted_at_text, vote_method = EXCLUDED.vote_method, subject = EXCLUDED.subject,
             present_count = EXCLUDED.present_count, yes_count = EXCLUDED.yes_count, no_count = EXCLUDED.no_count,
             abstain_count = EXCLUDED.abstain_count, source_data = EXCLUDED.source_data, updated_at = now()
             RETURNING vote_id"""
    for page, rows, total_page, total in ly_segments("votes", "votes", 500, "ly-votes", [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]):
        with conn.cursor() as cur:
            for v in rows:
                result = v.get("表決結果") or {}
                code = v.get("會議代碼")
                extra = {k: v.get(k) for k in ("公報文件代碼", "行號", "會議名稱", "agenda_id", "session_period", "表決型態")}
                term = to_int(v.get("屆"))
                # A name listed under two positions cannot be trusted; record neither rather than guess.
                lists = {p: [str(n) for n in (v.get(p) or [])] for p in ("贊成", "反對", "棄權")}
                ambiguous = sorted(n for n in set(sum(lists.values(), []))
                                   if sum(n in names_ for names_ in lists.values()) > 1)
                count_mismatch = (result.get("贊成人數") != len(lists["贊成"]) or result.get("反對人數") != len(lists["反對"])
                                  or (result.get("棄權人數") or 0) != len(lists["棄權"]))
                if ambiguous:
                    extra["ballot_conflicts"] = {n: [p for p, ns in lists.items() if n in ns] for n in ambiguous}
                    conflicts += 1
                if count_mismatch:
                    extra["count_mismatch"] = {"reported": result, "listed": {p: len(ns) for p, ns in lists.items()}}
                    mismatches += 1
                cur.execute(sql, (v["表決代碼"], meetings.get(code),
                                  parse_vote_time(v.get("表決時間"), meeting_dates.get(code) or []), v.get("表決時間"),
                                  VOTE_METHODS.get(v.get("表決型態"), "其他"),
                                  re.sub(r"[\s　]+", " ", clean(v.get("表決議題") or "")).strip() or "（來源未記載）",
                                  result.get("出席人數"), result.get("贊成人數"), result.get("反對人數"), result.get("棄權人數"),
                                  Jsonb(clean(extra))))
                vote_id = cur.fetchone()[0]
                cur.execute("DELETE FROM proceedings.vote_ballots WHERE vote_id = %s", (vote_id,))
                seen = set()
                for position, listed in lists.items():
                    for name in listed:
                        if name in ambiguous:
                            continue
                        if name in seen:
                            repeats += 1
                            continue
                        seen.add(name)
                        no = names.get((term, name_key(name)))
                        if not no:
                            unmatched.add((term, name))
                        cur.execute("INSERT INTO proceedings.vote_ballots VALUES (%s, %s, %s, %s)", (vote_id, clean(name), position, no))
        conn.commit()
        log("ly-votes page", page, "/", total_page, "total", total, "unmatched", len(unmatched),
            "votes-with-ambiguous-ballots", conflicts, "same-list-repeats", repeats, "count-mismatch", mismatches)
    log("ly-votes unmatched names:", sorted(unmatched, key=str)[:80])


def step_ly_bills(conn):
    meetings = meeting_ids(conn, "ly")
    sql = """INSERT INTO proceedings.bills (council_code, source_key, meeting_id, term, session_no, bill_no, bill_type, subject,
             sponsors, cosigners, status, source_url, source_data)
             VALUES ('ly', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id, term = EXCLUDED.term,
             session_no = EXCLUDED.session_no, bill_no = EXCLUDED.bill_no, bill_type = EXCLUDED.bill_type, subject = EXCLUDED.subject,
             sponsors = EXCLUDED.sponsors, cosigners = EXCLUDED.cosigners, status = EXCLUDED.status,
             source_url = EXCLUDED.source_url, source_data = EXCLUDED.source_data, updated_at = now()"""
    for page, rows, total_page, total in ly_segments("bills", "bills", 1000, "ly-bills", [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]):
        with conn.cursor() as cur:
            for b in rows:
                sponsors = as_names(b.get("提案人")) or split_names(b.get("提案單位/提案委員"))
                cur.execute(sql, (b["議案編號"], meetings.get(b.get("會議代碼")), to_int(b.get("屆")), to_int(b.get("會期")),
                                  b.get("字號"), b.get("議案類別"), clean(b.get("議案名稱") or b["議案編號"]), clean(sponsors),
                                  clean(as_names(b.get("連署人"))), b.get("議案狀態"), b.get("url"), Jsonb(clean(b))))
        conn.commit()
        log("ly-bills page", page, "/", total_page, "total", total)


def upsert_ly_bills(conn, rows, meetings):
    sql = """INSERT INTO proceedings.bills (council_code, source_key, meeting_id, term, session_no, bill_no, bill_type, subject,
             sponsors, cosigners, status, source_url, source_data)
             VALUES ('ly', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO NOTHING"""
    with conn.cursor() as cur:
        for b in rows:
            cur.execute(sql, (b["議案編號"], meetings.get(b.get("會議代碼")), to_int(b.get("屆")), to_int(b.get("會期")),
                              b.get("字號"), b.get("議案類別"), clean(b.get("議案名稱") or b["議案編號"]),
                              clean(as_names(b.get("提案人")) or split_names(b.get("提案單位/提案委員"))),
                              clean(as_names(b.get("連署人"))), b.get("議案狀態"), b.get("url"), Jsonb(clean(b))))
    conn.commit()


def step_ly_bills_gaps(conn):
    """Bills without a session cannot be reached by session; re-read gap terms split by bill type, then by source."""
    meetings = meeting_ids(conn, "ly")
    for term in range(11, 0, -1):
        total = ly_total("bills", {"屆": term})
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM proceedings.bills WHERE council_code = 'ly' AND term = %s", (term,))
            have = cur.fetchone()[0]
            cur.execute("""SELECT DISTINCT bill_type FROM proceedings.bills WHERE council_code = 'ly' AND term = %s
                           AND bill_type IS NOT NULL""", (term,))
            types = [r[0] for r in cur.fetchall()]
        log("ly-bills-gaps term", term, "source", total, "database", have)
        if have >= total:
            continue
        for bill_type in types:
            filters = {"屆": term, "議案類別": bill_type}
            count = ly_total("bills", filters)
            segments = [filters] if count <= LY_WINDOW else [{**filters, "提案來源": s} for s in ("委員提案", "政府提案", "審查報告", "請願案")]
            if count > LY_WINDOW:
                log("ly-bills-gaps large type", term, bill_type, count)
            for segment in segments:
                name = "ly-bills-gap-" + hashlib.sha1(json.dumps(segment, ensure_ascii=False).encode()).hexdigest()[:10]
                try:
                    for _, rows, _, _ in ly_pages(name, "bills", "bills", 1000, segment):
                        upsert_ly_bills(conn, rows, meetings)
                except RuntimeError as err:
                    if "HTTP 413" not in str(err):
                        raise
                    log("ly-bills-gaps window limit reached", term, bill_type, segment.get("提案來源"))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM proceedings.bills WHERE council_code = 'ly' AND term = %s", (term,))
            log("ly-bills-gaps term", term, "after", cur.fetchone()[0], "source", total)


def step_ly_bills_status(conn):
    """Second pass for remaining gaps: split each gap term by status, then by type, so every segment fits the window."""
    meetings = meeting_ids(conn, "ly")
    for term in range(11, 0, -1):
        total = ly_total("bills", {"屆": term})
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM proceedings.bills WHERE council_code = 'ly' AND term = %s", (term,))
            have = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT status FROM proceedings.bills WHERE council_code = 'ly' AND term = %s AND status IS NOT NULL", (term,))
            statuses = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT bill_type FROM proceedings.bills WHERE council_code = 'ly' AND term = %s AND bill_type IS NOT NULL", (term,))
            types = [r[0] for r in cur.fetchall()]
        if have >= total:
            continue
        for status in statuses:
            base = {"屆": term, "議案狀態": status}
            count = ly_total("bills", base)
            segments = [base] if count <= LY_WINDOW else [{**base, "議案類別": t} for t in types]
            for segment in segments:
                name = "ly-bills-st-" + hashlib.sha1(json.dumps(segment, ensure_ascii=False).encode()).hexdigest()[:10]
                try:
                    for _, rows, _, _ in ly_pages(name, "bills", "bills", 1000, segment):
                        upsert_ly_bills(conn, rows, meetings)
                except RuntimeError as err:
                    if "HTTP 413" not in str(err):
                        raise
                    log("ly-bills-status window limit reached", segment)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM proceedings.bills WHERE council_code = 'ly' AND term = %s", (term,))
            log("ly-bills-status term", term, "after", cur.fetchone()[0], "source", total)


def step_ly_gazette(conn):
    """Catalog gazette agendas as documents; agendas that are a meeting's minutes are typed 議事錄 and linked to it."""
    with conn.cursor() as cur:
        cur.execute("""SELECT source_data #>> '{議事錄,agenda_lcidc_id}', meeting_id FROM proceedings.meetings
                       WHERE council_code = 'ly' AND source_data #>> '{議事錄,agenda_lcidc_id}' IS NOT NULL""")
        minutes = dict(cur.fetchall())
        cur.execute("""SELECT r ->> 'agenda_id', meeting_id FROM proceedings.meetings,
                       jsonb_array_elements(CASE WHEN jsonb_typeof(source_data -> '公報發言紀錄') = 'array'
                                                 THEN source_data -> '公報發言紀錄' ELSE '[]'::jsonb END) r
                       WHERE council_code = 'ly'""")
        records = {k: v for k, v in cur.fetchall() if k}
    sql = """INSERT INTO proceedings.documents (council_code, source_key, meeting_id, doc_type, title, document_date, source_url,
             file_format, source_data)
             VALUES ('ly', %s, %s, %s, %s, %s, %s, 'txt', %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id, doc_type = EXCLUDED.doc_type,
             title = EXCLUDED.title, document_date = EXCLUDED.document_date, source_url = EXCLUDED.source_url,
             source_data = EXCLUDED.source_data, updated_at = now()"""
    for page, rows, total_page, total in ly_segments("gazette_agendas", "gazetteagendas", 1000, "ly-gazette", [11, 10, 9, 8]):
        with conn.cursor() as cur:
            for g in rows:
                key = g["公報議程編號"]
                dates = g.get("會議日期") or []
                date = dates[0] if dates and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dates[0])) else None
                cur.execute(sql, (key, minutes.get(key) or records.get(key), "議事錄" if key in minutes else "公報",
                                  clean(g.get("案由") or key), date, f"{LY_DOC}/{key}/txt", Jsonb(clean(g))))
        conn.commit()
        log("ly-gazette page", page, "/", total_page, "total", total)
    with conn.cursor() as cur:
        cur.execute("""UPDATE proceedings.votes v SET document_id = d.document_id, updated_at = now()
                       FROM proceedings.documents d
                       WHERE v.council_code = 'ly' AND d.council_code = 'ly' AND d.source_key = v.source_data ->> 'agenda_id'
                       AND v.document_id IS DISTINCT FROM d.document_id""")
        log("linked votes to documents", cur.rowcount)
    conn.commit()


def step_ly_gazette_files(conn):
    """Agendas share gazette files, so each official file becomes one document and its agendas link to it."""
    with conn.cursor() as cur:
        cur.execute("""SELECT document_id, source_key, meeting_id, title, document_date, source_data FROM proceedings.documents
                       WHERE council_code = 'ly' AND source_key NOT LIKE 'file:%' ORDER BY source_key""")
        agendas = cur.fetchall()
    files, links = {}, []
    for document_id, key, meeting_id, title, date, data in agendas:
        keys = []
        for url in data.get("doc檔案下載位置") or []:
            name = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            file_key = f"file:{name}"
            keys.append(file_key)
            entry = files.setdefault(file_key, {"url": url, "meeting": None, "date": date, "agendas": [], "name": name,
                                                "gazette": data.get("公報網址")})
            entry["agendas"].append(key)
            entry["meeting"] = entry["meeting"] or meeting_id
        links.append((document_id, keys))
    with conn.cursor() as cur:
        for file_key, f in files.items():
            cur.execute("""INSERT INTO proceedings.documents (council_code, source_key, meeting_id, doc_type, title, document_date,
                           source_url, file_format, source_data)
                           VALUES ('ly', %s, %s, '公報', %s, %s, %s, 'doc', %s)
                           ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id,
                           source_url = EXCLUDED.source_url, source_data = EXCLUDED.source_data, updated_at = now()""",
                        (file_key, f["meeting"], f"立法院公報 {f['name']}", f["date"], f["url"],
                         Jsonb({"agendas": f["agendas"], "gazette_url": f["gazette"]})))
        for document_id, keys in links:
            cur.execute("""UPDATE proceedings.documents SET parse_status = %s, content_text = NULL, content_sha256 = NULL,
                           source_data = source_data || %s, updated_at = now() WHERE document_id = %s""",
                        ("linked" if keys else "failed", Jsonb({"text_documents": keys} if keys else {"fetch_error": "no file listed"}), document_id))
    conn.commit()
    log("ly gazette files", len(files), "agendas", len(links), "without file", sum(1 for _, k in links if not k))


def step_ly_backfill(conn):
    """Link ballots whose names differ from the roster only by punctuation, and votes to meetings through their document."""
    names = legislator_lookup(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT (m.source_data ->> '屆')::int, b.member_name FROM proceedings.vote_ballots b
                       JOIN proceedings.votes v USING (vote_id) LEFT JOIN proceedings.meetings m USING (meeting_id)
                       WHERE b.legislator_no IS NULL""")
        fixed = 0
        for term, name in cur.fetchall():
            no = names.get((term, name_key(name)))
            if no:
                cur.execute("""UPDATE proceedings.vote_ballots b SET legislator_no = %s FROM proceedings.votes v
                               JOIN proceedings.meetings m USING (meeting_id)
                               WHERE b.vote_id = v.vote_id AND b.member_name = %s AND (m.source_data ->> '屆')::int = %s
                               AND b.legislator_no IS NULL""", (no, name, term))
                fixed += cur.rowcount
        log("ballots linked", fixed)
        cur.execute("""UPDATE proceedings.votes v SET meeting_id = d.meeting_id, updated_at = now() FROM proceedings.documents d
                       WHERE v.document_id = d.document_id AND v.meeting_id IS NULL AND d.meeting_id IS NOT NULL""")
        log("votes linked to meetings", cur.rowcount)
    conn.commit()


# ---------- Taichung ----------

SESSION = re.compile(r"第\s*(\d+)\s*屆\s*(?:第\s*(\d+)\s*次)?\s*(\S+)?")


def parse_session(text):
    m = SESSION.search(text or "")
    return (int(m.group(1)), to_int(m.group(2)), m.group(3)) if m else (None, None, None)


def step_taichung_bills(conn):
    fetcher = Fetcher(min_interval=1.5)
    page = load_page("taichung-bills")
    sql = """INSERT INTO proceedings.bills (council_code, source_key, term, session_no, session_type, bill_no, bill_type, committee,
             subject, sponsors, cosigners, resolution, status, source_url, source_data)
             VALUES ('taichung', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'https://yishi.tccc.gov.tw/proposals', %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET term = EXCLUDED.term, session_no = EXCLUDED.session_no,
             session_type = EXCLUDED.session_type, bill_no = EXCLUDED.bill_no, bill_type = EXCLUDED.bill_type,
             committee = EXCLUDED.committee, subject = EXCLUDED.subject, sponsors = EXCLUDED.sponsors, cosigners = EXCLUDED.cosigners,
             resolution = EXCLUDED.resolution, status = EXCLUDED.status, source_data = EXCLUDED.source_data, updated_at = now()"""
    while True:
        data = fetcher.get(f"{TAICHUNG_API}/Proposal/FrontList", params={"pageNumber": page, "pageSize": 200}).json()["data"]
        rows = data.get("data") or []
        if not rows:
            break
        with conn.cursor() as cur:
            for b in rows:
                term, session_no, session_type = parse_session(b.get("session"))
                subject = strip_html(b.get("proposalRationaleSort")) or b.get("proposalRationale") or b["billId"]
                cur.execute(sql, (b["billId"], term, session_no, session_type, b.get("billNumber"), b.get("billType"),
                                  b.get("committee"), clean(subject), clean(split_names(b.get("sponsor"))),
                                  clean(split_names(b.get("jointSignatory"))),
                                  clean(strip_html(b.get("resolutionSort")) or b.get("resolution")) or None,
                                  b.get("closureType"), Jsonb(clean(b))))
        conn.commit()
        log("taichung-bills page", page, "/", data.get("totalPages"), "total", data.get("totalCount"))
        save_page("taichung-bills", page + 1)
        page += 1


ROC_DATE = re.compile(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日")


def roc_date(text):
    m = ROC_DATE.search(text or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def taichung_doc_type(chapter):
    if chapter in ("會議紀錄", "會議記錄"):
        return "會議紀錄"
    if chapter in ("目錄", "總目錄", "附錄", "總附錄"):
        return "其他"
    return "議事錄"


def step_taichung_minutes(conn):
    """Catalog the minutes index; attachment text is fetched later by the fetch-text step."""
    fetcher = Fetcher(min_interval=1.5)
    # Keep existing rows untouched: their source_data holds fetch errors from earlier text runs.
    sql = """INSERT INTO proceedings.documents (council_code, source_key, doc_type, title, document_date, source_url, source_data)
             VALUES ('taichung', %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO NOTHING"""
    url = f"{TAICHUNG_API}/OldMinutesSearch/List"
    for agency in ("臺中市議會", "合併前臺中市議會", "合併前臺中縣議會"):
        # The list has no stable order, so paging skips rows; read each agency in a single response.
        total = fetcher.get(url, params={"pageNumber": 1, "pageSize": 1, "agencyType": agency}).json()["data"]["totalCount"]
        rows = fetcher.get(url, params={"pageNumber": 1, "pageSize": total, "agencyType": agency}).json()["data"]["data"]
        if len(rows) != total:
            raise RuntimeError(f"{agency}: expected {total} rows in one response, got {len(rows)}")
        added = 0
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (r["recordOldId"], taichung_doc_type(r.get("chapter")),
                                  clean(f"{r.get('session') or ''} {r.get('section') or r.get('chapter') or ''}".strip() or r["recordOldId"]),
                                  roc_date(r.get("section")),
                                  f"{TAICHUNG_API}/Attachment/AttachmentDownload?attachmentId={r.get('attachmentId')}",
                                  Jsonb(clean(r))))
                added += cur.rowcount
        conn.commit()
        log("taichung-minutes", agency, "total", total, "added", added)


# ---------- Taipei ----------

TAIPEI = "https://www.tcc.gov.tw"
TAIPEI_KINDS = [(2, 13534, "會議紀錄"), (3, 13535, "速記錄"), (1, 13537, "其他")]
TAIPEI_TERMS = ["14", "13", "12", "11", "10", "09"]
TAIPEI_ROW = re.compile(
    r'data-title="屆次會別"[^>]*>\s*<span>(.*?)</span>.*?data-title="類別"[^>]*>\s*<span>(.*?)</span>.*?'
    r'data-title="資料名稱"[^>]*>\s*<span><a href="(MeetingMinutesDetail[^"]+)"[^>]*>(.*?)</a>.*?'
    r'data-title="資料日期"[^>]*>\s*<span>(.*?)</span>', re.S)
TAIPEI_FILE = re.compile(r'https://obasfront\.tcc\.gov\.tw/Agenda/DownloadFile\.aspx\?FileName=([^"&]+)&(?:amp;)?FilePath=([^"&]*)&(?:amp;)?FileGrpKind=(\d+)')


def taipei_query_url(fetcher, kind, n, term):
    """Submit the term filter; the site answers with a redirect to a paged result URL."""
    base = f"{TAIPEI}/MeetingMinutes.aspx?n={n}&kind={kind}"
    page = fetcher.get(base).text
    form = {name: html.unescape(value) for name, value in
            re.findall(r'<input[^>]*type="hidden"[^>]*name="(__[A-Z]+)"[^>]*value="([^"]*)"', page)}
    form.update({"MeetingMinSearch_MeetExpire": term, "MeetingMinSearch_MeetSession": "", "MeetingMinSearch_FileName": "",
                 "MeetingMinSearch_Submit": "送出查詢", "OrderingCondition1": "FileGrpDate", "OrderingDirection1": "DESC"})
    time.sleep(fetcher.min_interval)
    resp = fetcher.session.post(base, data=form, timeout=120, allow_redirects=True)
    resp.raise_for_status()
    if "_qry=" not in resp.url:
        raise RuntimeError(f"term filter was not applied for term {term}")
    return resp.url


def taipei_date(text):
    m = re.fullmatch(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*", text or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def step_taipei_docs(conn):
    fetcher = Fetcher(min_interval=1.0)
    meeting_sql = """INSERT INTO proceedings.meetings (council_code, source_key, term, session_no, session_type, meeting_kind, committee,
                     title, meeting_dates, source_url, source_data)
                     VALUES ('taipei', %s, %s, %s, %s, %s, %s, %s, %s::date[], %s, %s)
                     ON CONFLICT (council_code, source_key) DO UPDATE SET updated_at = now() RETURNING meeting_id"""
    doc_sql = """INSERT INTO proceedings.documents (council_code, source_key, meeting_id, doc_type, title, document_date, source_url,
                 file_format, source_data)
                 VALUES ('taipei', %s, %s, %s, %s, %s, %s, %s, %s)
                 ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id, title = EXCLUDED.title,
                 document_date = EXCLUDED.document_date, source_url = EXCLUDED.source_url, source_data = EXCLUDED.source_data,
                 updated_at = now()"""
    for kind, n, doc_type in TAIPEI_KINDS:
        for term in TAIPEI_TERMS:
            step = f"taipei-docs-{kind}-{term}"
            page = load_page(step)
            url = taipei_query_url(fetcher, kind, n, term)
            while True:
                listing = fetcher.get(f"{url}&page={page}&PageSize=200").text
                rows = TAIPEI_ROW.findall(listing)
                if not rows:
                    break
                for session, category, href, name, date_text in rows:
                    session, category, name = (strip_html(session), strip_html(category), strip_html(name))
                    href = html.unescape(href)
                    try:
                        detail = fetcher.get(f"{TAIPEI}/{href}").text
                    except RuntimeError as err:
                        log("taipei-docs detail skipped", session, name, str(err)[:120])
                        continue
                    files = {}
                    for file_name, path, group in TAIPEI_FILE.findall(detail):
                        stem, _, ext = file_name.rpartition(".")
                        if ext.lower() == "pdf" or stem not in files:
                            files[stem] = (file_name, path, group, ext.lower())
                    date = taipei_date(date_text)
                    t, session_no, session_type = parse_session(session)
                    info = {"session": session, "category": category, "name": name, "date_text": date_text, "kind": kind, "detail": href}
                    key = f"{session}|{category}|{name}|{date_text}".strip()
                    with conn.cursor() as cur:
                        cur.execute(meeting_sql, (key, t, session_no, session_type, "大會" if category == "大會" else "委員會",
                                                  None if category == "大會" else category, clean(f"{session} {category} {name}"),
                                                  [date] if date else [], f"{TAIPEI}/{href}", Jsonb(clean(info))))
                        meeting_id = cur.fetchone()[0]
                        if not files:
                            log("no file", key)
                        for stem, (file_name, path, group, ext) in files.items():
                            source = f"https://obasfront.tcc.gov.tw/Agenda/DownloadFile.aspx?FileName={file_name}&FilePath={path}&FileGrpKind={group}"
                            cur.execute(doc_sql, (f"{kind}:{stem}", meeting_id, doc_type, clean(f"{session} {category} {name}"), date,
                                                  source, ext, Jsonb(clean(info))))
                    conn.commit()
                log(step, "page", page, "rows", len(rows))
                # The site keeps returning the last page for any higher page number.
                if len(rows) < 200:
                    break
                save_page(step, page + 1)
                page += 1


TAIPEI_MOTION = re.compile(r'<table[^>]*class="table-content[^"]*"[^>]*>(.*?)</table>', re.S)
TAIPEI_MOTION_HEAD = re.compile(r'<span>(.*?)</span>\s*<a href="MotionContent\.aspx\?n=\d+&(?:amp;)?sysn=(\d+)"', re.S)
TAIPEI_MOTION_ROW = re.compile(r'class="td-title"[^>]*>(.*?)</td>\s*<td[^>]*class="td-text"[^>]*>(.*?)</td>', re.S)


def step_taipei_bills(conn):
    """Read each term's resolved motions session by session; term selection is an ASP.NET postback."""
    fetcher = Fetcher(min_interval=1.0)
    base = f"{TAIPEI}/MotionList.aspx?n=13548&sms=0"
    field = "ctl00$ContentPlaceHolder1$DDL_Session"
    sql = """INSERT INTO proceedings.bills (council_code, source_key, term, session_no, session_type, bill_no, bill_type, committee,
             subject, sponsors, resolution, decided_on, source_url, source_data)
             VALUES ('taipei', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET term = EXCLUDED.term, session_no = EXCLUDED.session_no,
             session_type = EXCLUDED.session_type, bill_no = EXCLUDED.bill_no, bill_type = EXCLUDED.bill_type,
             committee = EXCLUDED.committee, subject = EXCLUDED.subject, sponsors = EXCLUDED.sponsors, resolution = EXCLUDED.resolution,
             decided_on = EXCLUDED.decided_on, source_data = EXCLUDED.source_data, updated_at = now()"""
    for term in [f"{t:02d}" for t in range(14, 0, -1)]:
        step = f"taipei-bills-{term}"
        if load_page(step) > 1:
            continue
        page = fetcher.get(base).text
        form = {name: html.unescape(value) for name, value in
                re.findall(r'<input[^>]*name="(__[A-Z]+)"[^>]*value="([^"]*)"', page)}
        form.update({"__EVENTTARGET": field, "__EVENTARGUMENT": "", field: term})
        time.sleep(fetcher.min_interval)
        resp = fetcher.session.post(base, data=form, timeout=120)
        resp.raise_for_status()
        links = list(dict.fromkeys(html.unescape(h) for h in re.findall(r'href="(MotionView\.aspx\?[^"]*PageType=4[^"]*)"', resp.text)))
        log("taipei-bills term", term, "sessions", len(links))
        for link in links:
            number = 1
            seen = set()
            while True:
                body = fetcher.get(f"{TAIPEI}/{link}&page={number}&PageSize=200").text
                tables = TAIPEI_MOTION.findall(body)
                fresh = 0
                with conn.cursor() as cur:
                    for table in tables:
                        head = TAIPEI_MOTION_HEAD.search(table)
                        if not head:
                            continue
                        sysn = head.group(2)
                        if sysn in seen:
                            continue
                        seen.add(sysn)
                        fresh += 1
                        title = strip_html(head.group(1))
                        fields = {strip_html(k): strip_html(v) for k, v in TAIPEI_MOTION_ROW.findall(table)}
                        parts = title.split()
                        committee = re.search(r"\[(.*?)\]", title)
                        decision = fields.get("三讀議決") or fields.get("二讀議決") or fields.get("議決會次") or ""
                        date = re.search(r"(\d{2,3})\.(\d{1,2})\.(\d{1,2})", decision)
                        decided = None
                        if date:
                            try:
                                decided = dt.date(int(date.group(1)) + 1911, int(date.group(2)), int(date.group(3)))
                            except ValueError:
                                decided = None
                        t, session_no, session_type = parse_session(decision)
                        cur.execute(sql, (sysn, t or int(term), session_no, session_type, parts[1] if len(parts) > 1 else None,
                                          parts[0] if parts else None, committee.group(1) if committee else None,
                                          clean(fields.get("案由") or title), clean(split_names(fields.get("提案者"))),
                                          clean(decision) or None, decided,
                                          f"{TAIPEI}/MotionContent.aspx?n=13548&sysn={sysn}", Jsonb(clean({"title": title, **fields}))))
                conn.commit()
                if not fresh:
                    break
                log("taipei-bills term", term, "page", number, "new", fresh)
                number += 1
        save_page(step, 2)


# ---------- New Taipei ----------

NTP = "https://www.ntp.gov.tw"


def step_new_taipei_bills(conn):
    """The bill search only renders in a browser (Blazor), so a headless browser pages through the results."""
    from playwright.sync_api import sync_playwright
    sql = """INSERT INTO proceedings.bills (council_code, source_key, term, session_no, session_type, bill_no, bill_type, committee,
             subject, sponsors, source_url, source_data)
             VALUES ('new_taipei', %s, %s, %s, %s, %s, %s, %s, %s, %s, 'https://bms1.ntp.gov.tw/billsystem/councilbillquery', %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET subject = EXCLUDED.subject, sponsors = EXCLUDED.sponsors,
             source_data = EXCLUDED.source_data, updated_at = now()"""
    columns = ["會期", "審查會", "案別", "案號", "提案類別", "提案人", "發文日期", "案由"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(60000)
        page.goto("https://bms1.ntp.gov.tw/billsystem/councilbillquery", wait_until="networkidle")
        page.get_by_role("button", name="搜尋").click()
        page.wait_for_selector("tr.k-master-row")
        number = 0
        while True:
            number += 1
            rows = page.eval_on_selector_all("tr.k-master-row", "rs => rs.map(r => [...r.children].map(c => c.innerText.trim()))")
            with conn.cursor() as cur:
                for cells in rows:
                    start = next((i for i, c in enumerate(cells) if re.match(r"第\s*\d+\s*屆", c)), None)
                    if start is None:
                        raise RuntimeError(f"unexpected row layout: {cells}")
                    record = dict(zip(columns, cells[start:]))
                    subject = record.get("案由") or ""
                    digest = hashlib.sha1(subject.encode()).hexdigest()[:12]
                    key = "|".join(record.get(c, "") for c in ("會期", "審查會", "案別", "案號", "提案類別")) + "|" + digest
                    term, session_no, session_type = parse_session(record.get("會期"))
                    cur.execute(sql, (key, term, session_no, session_type, record.get("案號"), record.get("案別"), record.get("審查會"),
                                      clean(subject or key), clean(split_names(record.get("提案人"))), Jsonb(clean(record))))
            conn.commit()
            if number % 25 == 1:
                info = page.inner_text(".k-pager-info") if page.query_selector(".k-pager-info") else ""
                log("new-taipei-bills page", number, "rows", len(rows), info)
            first = rows[0] if rows else None
            nxt = page.query_selector("button.k-pager-nav[title='下一頁']:not([disabled]):not(.k-disabled)")
            if not nxt:
                total = re.search(r"共\s*([\d,]+)\s*項", page.inner_text(".k-pager-info"))
                log("new-taipei-bills last page", number, "source total", total.group(1) if total else "unknown")
                break
            nxt.click()
            for _ in range(120):
                page.wait_for_timeout(500)
                now = page.eval_on_selector("tr.k-master-row", "r => [...r.children].map(c => c.innerText.trim())")
                if now != first:
                    break
            else:
                raise RuntimeError(f"page {number + 1} did not load")
        browser.close()
    log("new-taipei-bills pages", number)


def step_new_taipei_books(conn):
    """Catalog the past-term minutes; each session is published as one PDF book."""
    fetcher = Fetcher(min_interval=1.5)
    headers = {"X-Requested-With": "XMLHttpRequest"}
    sql = """INSERT INTO proceedings.documents (council_code, source_key, doc_type, title, source_url, file_format, source_data)
             VALUES ('new_taipei', %s, '議事錄', %s, %s, 'pdf', %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET title = EXCLUDED.title, source_url = EXCLUDED.source_url,
             source_data = EXCLUDED.source_data, updated_at = now()"""
    for term in (3, 2, 1):
        time.sleep(fetcher.min_interval)
        options = json.loads(fetcher.session.post(f"{NTP}/discussion-info05_query", data={"if_P": 1, "Proceedings_Type": term},
                                                  headers=headers, timeout=60).content.decode("utf-8-sig"))["main"]
        for book_id, title in re.findall(r'<option value="(\d+)">(.*?)</option>', options):
            time.sleep(fetcher.min_interval)
            link = json.loads(fetcher.session.post(f"{NTP}/discussion-info05_search",
                                                   data={"if_P": 1, "Proceedings": book_id, "Proceedings_Type": term},
                                                   headers=headers, timeout=60).content.decode("utf-8-sig")).get("link") or ""
            if not link:
                log("new-taipei no link", book_id, title)
                continue
            pdf = f"{NTP}/{link.lstrip('/')}"
            if not link.lower().endswith(".pdf"):
                page = fetcher.get(pdf).text
                found = re.search(r'["\']([^"\']+\.pdf)["\']', page, re.I)
                if not found:
                    log("new-taipei no pdf", book_id, link)
                    continue
                pdf = requests.compat.urljoin(pdf, found.group(1))
            with conn.cursor() as cur:
                cur.execute(sql, (f"book:{book_id}", clean(html.unescape(title)), pdf, Jsonb({"term": term, "book_id": book_id, "link": link})))
            conn.commit()
            log("new-taipei book", book_id, pdf[-60:])


def decode_page(body):
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return body.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace")


def step_new_taipei_html(conn):
    """Walk the session books published as static HTML; every page is stored as its own document."""
    fetcher = Fetcher(min_interval=1.0)
    headers = {"X-Requested-With": "XMLHttpRequest"}
    sql = """INSERT INTO proceedings.documents (council_code, source_key, doc_type, title, source_url, file_format, content_text,
             content_sha256, parse_status, source_data)
             VALUES ('new_taipei', %s, '議事錄', %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET title = EXCLUDED.title, source_url = EXCLUDED.source_url,
             content_text = coalesce(EXCLUDED.content_text, proceedings.documents.content_text),
             content_sha256 = coalesce(EXCLUDED.content_sha256, proceedings.documents.content_sha256),
             parse_status = CASE WHEN EXCLUDED.content_text IS NOT NULL THEN 'parsed' ELSE proceedings.documents.parse_status END,
             source_data = EXCLUDED.source_data, updated_at = now()"""
    for term in (3, 2, 1):
        time.sleep(fetcher.min_interval)
        options = json.loads(fetcher.session.post(f"{NTP}/discussion-info05_query", data={"if_P": 1, "Proceedings_Type": term},
                                                  headers=headers, timeout=60).content.decode("utf-8-sig"))["main"]
        for book_id, book_title in re.findall(r'<option value="(\d+)">(.*?)</option>', options):
            step = f"new-taipei-html-{book_id}"
            if load_page(step) > 1:
                continue
            time.sleep(fetcher.min_interval)
            link = json.loads(fetcher.session.post(f"{NTP}/discussion-info05_search",
                                                   data={"if_P": 1, "Proceedings": book_id, "Proceedings_Type": term},
                                                   headers=headers, timeout=60).content.decode("utf-8-sig")).get("link") or ""
            if not link.lower().endswith((".htm", ".html")) or "index.htm" not in link.lower():
                continue
            book_title = html.unescape(book_title)
            root = requests.compat.urljoin(f"{NTP}/", link)
            prefix = root.rsplit("/", 1)[0] + "/"
            queue, seen, pages, files = [root], {root}, 0, 0
            while queue:
                url = queue.pop(0)
                try:
                    body = fetcher.get(url, attempts=3).content
                except RuntimeError as err:
                    log("new-taipei-html skip", url[:120], str(err)[:80])
                    continue
                page = decode_page(body)
                for target in re.findall(r"""(?:href|src)\s*=\s*['"]([^'"#]+)""", page, re.I):
                    absolute = requests.compat.urljoin(url, html.unescape(target)).split("?")[0]
                    lower = absolute.lower()
                    if not absolute.startswith(prefix) or absolute in seen:
                        continue
                    if lower.endswith((".htm", ".html")):
                        seen.add(absolute)
                        queue.append(absolute)
                    elif lower.endswith((".pdf", ".doc", ".docx", ".odt")):
                        seen.add(absolute)
                        files += 1
                        relative = absolute[len(prefix):]
                        with conn.cursor() as cur:
                            cur.execute(sql, (f"book:{book_id}:{relative}", clean(f"{book_title} {relative}"), absolute,
                                              lower.rsplit(".", 1)[1], None, None, "pending",
                                              Jsonb({"term": term, "book_id": book_id, "path": relative})))
                relative = url[len(prefix):]
                text = clean(strip_html(page)).strip()
                title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
                name = clean(f"{book_title} {relative.rsplit('.', 1)[0]}")
                if text:
                    with conn.cursor() as cur:
                        cur.execute(sql, (f"book:{book_id}:{relative}", name, url, "html", text,
                                          hashlib.sha256(text.encode()).hexdigest(), "parsed",
                                          Jsonb({"term": term, "book_id": book_id, "path": relative,
                                                 "page_title": strip_html(title.group(1)) if title else None})))
                conn.commit()
                pages += 1
            log("new-taipei-html book", book_id, "pages", pages, "files", files, book_title[:40])
            save_page(step, 2)


NTPBOOK = "https://ntpbook.ntp.gov.tw"
NTPBOOK_LISTS = ("/Home/BookAgenda/ViewExamAnnex", "/Home/BookAgenda/ViewSummaryAnnex",
                 "/Home/BookAgenda/ViewNonSummaryAnnex", "/Home/BookAnnex")


def ntpbook_anchors(page):
    """Yield (roc_date, label, href) in page order; a date applies to the links in its table row."""
    date = None
    for m in re.finditer(r"""<tr\b|>\s*(\d{3}-\d{2}-\d{2})\s*<|<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>""", page, re.S | re.I):
        if m.group(0).lower().startswith("<tr"):
            date = None
        elif m.group(1):
            date = m.group(1)
        else:
            yield date, clean(strip_html(m.group(3))).strip(), html.unescape(m.group(2))


def ntpbook_body(page):
    """Cut the site navigation and footer so stored text starts at the record itself."""
    text = clean(strip_html(re.sub(r"<select\b.*?</select>", " ", page, flags=re.S | re.I)))
    start, end = text.find("進階檢索"), text.rfind("新北市議會，機關地址")
    return text[start + 4 if start >= 0 else 0:end if end >= 0 else None].strip()


def ntpbook_doc_type(label):
    if "紀錄" in label or "記錄" in label:
        return "會議紀錄"
    return "議事錄" if "議事錄" in label else "其他"


def step_new_taipei_current(conn):
    """Import the current-term books, which are published only on ntpbook and not as past-minutes files."""
    fetcher = Fetcher(min_interval=1.0)
    sql = """INSERT INTO proceedings.documents (council_code, source_key, doc_type, title, document_date, source_url, file_format,
             content_text, content_sha256, parse_status, source_data)
             VALUES ('new_taipei', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO NOTHING"""

    def save(key, doc_type, title, date, url, file_format, text, data):
        digest = hashlib.sha256(text.encode()).hexdigest() if text else None
        parsed = None
        if date:
            year, month, day = date.split("-")
            parsed = f"{int(year) + 1911}-{month}-{day}"
        with conn.cursor() as cur:
            cur.execute(sql, (key, doc_type, title, parsed, url, file_format, text, digest, "parsed" if text else "pending", Jsonb(data)))

    home = fetcher.get(f"{NTPBOOK}/Home").content.decode("utf-8", "replace")
    books = [(href, label) for _, label, href in ntpbook_anchors(home) if href.startswith("/Home/BookAgenda?") and label.startswith("第4屆")]
    log("new-taipei-current books", len(books))
    for href, book in books:
        book_id = href.rsplit("=", 1)[1]
        step = f"new-taipei-current-{book_id}"
        if load_page(step) > 1:
            continue
        base = {"term": "4", "book": book, "book_id": book_id}
        page = fetcher.get(f"{NTPBOOK}{href}&ShowMode=PC").content.decode("utf-8", "replace")
        records = files = 0
        for date, label, target in ntpbook_anchors(page):
            title = clean(f"{book} {date or ''} {label}")
            path = target.split("?")[0]
            data = {**base, "label": label, "roc_date": date}
            try:
                if "/Mam/" in target:
                    url = requests.compat.urljoin(NTPBOOK, target)
                    save("ntpbook:" + path.rsplit("/", 1)[1], ntpbook_doc_type(label), title, date, url,
                         path.rsplit(".", 1)[-1].lower(), None, data)
                    files += 1
                elif path in ("/Home/BookAgenda/ViewRecord", "/Home/BookAgenda/ViewExamRecord"):
                    url = requests.compat.urljoin(NTPBOOK, target)
                    text = ntpbook_body(fetcher.get(url, attempts=3).content.decode("utf-8", "replace"))
                    # A scheduled day with no published record has only the heading.
                    if len(text) >= 200:
                        save("ntpbook:" + hashlib.sha1(target.encode()).hexdigest()[:20], "議事錄", title, date, url, "html", text, data)
                        records += 1
                elif path in NTPBOOK_LISTS or path == "/Home/BookAnnex/DisplayedInHomeWithList":
                    listing = fetcher.get(requests.compat.urljoin(NTPBOOK, target), attempts=3).content.decode("utf-8", "replace")
                    for _, name, file_url in ntpbook_anchors(listing):
                        if "/Mam/" not in file_url:
                            continue
                        file_path = file_url.split("?")[0]
                        save("ntpbook:" + file_path.rsplit("/", 1)[1], ntpbook_doc_type(name), clean(f"{book} {date or ''} {label} {name}"), date,
                             requests.compat.urljoin(NTPBOOK, file_url), file_path.rsplit(".", 1)[-1].lower(), None,
                             {**data, "file_label": name})
                        files += 1
            except RuntimeError as err:
                log("new-taipei-current skip", target[:120], str(err)[:80])
        conn.commit()
        log("new-taipei-current book", book, "records", records, "files", files)
        save_page(step, 2)


# ---------- Document text ----------

def text_from_bytes(body, url):
    """Convert a downloaded file to text; the file itself is discarded."""
    # One source answers HTTP 200 with this body when rate limiting; never store it as content.
    if body[:200].lstrip().lower().startswith((b"too many requests", b'{"ok": false', b'{"ok":false')):
        raise RuntimeError("rate limited or error body returned with HTTP 200")
    head = body[:8]
    if head.startswith(b"%PDF"):
        import pymupdf
        with pymupdf.open(stream=body, filetype="pdf") as pdf:
            return "\n".join(page.get_text() for page in pdf), "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
            tmp.write(body)
        try:
            out = subprocess.run(["antiword", "-m", "UTF-8.txt", tmp.name], capture_output=True, timeout=180)
            if out.returncode != 0:
                raise RuntimeError(f"antiword failed: {out.stderr[:200]!r}")
            return out.stdout.decode("utf-8", "replace"), "doc"
        finally:
            os.unlink(tmp.name)
    if head.startswith(b"PK"):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            # Taipei committee reports are often slide decks; slide order is numeric, not lexical.
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"\d+", n).group()))
            if slides:
                xml = "\n".join(archive.read(n).decode("utf-8", "replace") for n in slides)
                return strip_html(re.sub(r"</a:p>", "\n", xml)), "pptx"
            if "xl/sharedStrings.xml" in names:
                xml = archive.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                return strip_html(re.sub(r"</si>", "\n", xml)), "xlsx"
            name = "word/document.xml" if "word/document.xml" in names else "content.xml"
            xml = archive.read(name).decode("utf-8", "replace")
        return strip_html(re.sub(r"</w:p>|</text:p>", "\n", xml)), "docx" if name.startswith("word") else "odt"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"II*\x00", b"MM\x00*", b"BM")):
        raise RuntimeError("image file; needs OCR")
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        decoded = body.decode("utf-16")
        return strip_html(decoded), "html"
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = body.decode("cp950")
        except UnicodeDecodeError as err:
            raise RuntimeError(f"unrecognized binary file, starts {head!r}") from err
    return strip_html(decoded) if "<" in decoded[:400] else decoded, "txt"


def step_fetch_text(conn, council, batch, interval, doc_types, newest_first=True, limit=None, shard=(0, 1), max_db_gb=None, agency=None):
    fetcher = Fetcher(min_interval=interval)
    done = failed = 0
    order = "document_date DESC NULLS LAST, document_id DESC" if newest_first else "document_id"
    while limit is None or done + failed < limit:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT document_id, source_url FROM proceedings.documents
                            WHERE council_code = %s AND parse_status = 'pending' AND doc_type = ANY(%s)
                            AND document_id %% %s = %s AND (%s::text IS NULL OR source_data ->> 'agencyType' = %s)
                            ORDER BY {order} LIMIT %s""", (council, doc_types, shard[1], shard[0], agency, agency, batch))
            rows = cur.fetchall()
        conn.commit()
        if not rows:
            break
        if max_db_gb:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                size_gb = cur.fetchone()[0] / 1024 ** 3
            conn.commit()
            if size_gb >= max_db_gb:
                log("fetch-text paused: database size limit reached", round(size_gb, 1), "GB")
                break
        for document_id, url in rows:
            try:
                body = fetcher.get(url, attempts=4).content
                # Taipei lists a PDF that is sometimes missing (an HTML error page comes back) while its Word copy exists.
                if "obasfront.tcc.gov.tw" in url and ".pdf&" in url and not body.startswith(b"%PDF"):
                    url = url.replace(".pdf&", ".doc&").replace("FilePath=pdf/", "FilePath=")
                    body = fetcher.get(url, attempts=4).content
                if b"<html" in body[:600].lower() and "obasfront.tcc.gov.tw" in url:
                    raise RuntimeError("error page instead of a file")
                text, file_format = text_from_bytes(body, url)
                text = clean(text).strip()
                if not text:
                    raise RuntimeError("empty text")
                with conn.cursor() as cur:
                    cur.execute("""UPDATE proceedings.documents SET content_text = %s, content_sha256 = %s, file_format = %s,
                                   parse_status = 'parsed', updated_at = now() WHERE document_id = %s""",
                                (text, hashlib.sha256(text.encode()).hexdigest(), file_format, document_id))
                done += 1
            except Exception as err:  # one bad document must not stop a long run
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("""UPDATE proceedings.documents SET parse_status = 'failed', updated_at = now(),
                                   source_data = coalesce(source_data, '{}'::jsonb) || %s WHERE document_id = %s""",
                                (Jsonb({"fetch_error": str(err)[:300]}), document_id))
                failed += 1
                log("failed", council, document_id, str(err)[:160])
            conn.commit()
            if (done + failed) % 100 == 0:
                log("fetch-text", council, "parsed", done, "failed", failed)
    log("fetch-text finished", council, "parsed", done, "failed", failed)


# Minutes only: shorthand records repeat the same votes in speech, and the word 無記名 must not count as named.
TAIPEI_NAMED_DOCS = r"""council_code = 'taipei' AND parse_status = 'parsed' AND doc_type = '會議紀錄'
    AND content_text ~ '(^|[^無])記\s*名\s*表\s*決|唱\s*名\s*表\s*決'
    AND content_text ~ '贊\s*成\s*(議\s*員|者)\s*[：:（(]|反\s*對\s*(議\s*員|者)\s*[：:（(]'"""
# Named lists in wording the filter above misses: 提付表決 with no 記名, and 贊成者計有 with no opposing list.
TAIPEI_VOTE_DOCS = f"""({TAIPEI_NAMED_DOCS}) OR (council_code = 'taipei' AND parse_status = 'parsed'
    AND source_key IN ('2:201700010003', '2:0910710re'))"""
TP_PAGE_NO = re.compile(r"\n[ \t　]*\n[ \t　]*\d{1,3}[ \t　]*(?=\n)")
TP_YES = re.compile(r"贊成(?:議員|者|委員會意見[^：:。]{0,30}?者)?(?:計有|計|共)?(?:[：:]|(?=\d+[位人]))")
TP_NO = re.compile(r"[；;，,。）)]?反對(?:議員|者)?(?:計有|計|共)?(?:[：:，,、]|(?=\d+[位人]))")
TP_ABSTAIN = re.compile(r"[；;，,。）)]?棄權(?:議員|者)?(?:計有|計|共)?(?:[：:，,、]|(?=\d+[位人]))")
TP_RESULT = re.compile(r"再?表決結果[：:]?((?:審查意見)?(?:不通過|未通過|不同意|通過|否決|同意))?")
TP_COUNT = re.compile(r"[，,、]?(?:計有|計|共|等)?[（(]?(\d+)[位人][）)]?$")
TP_NAME = re.compile(r"[一-鿿]{2,5}")


def tp_ballot_list(body):
    """Split one position's text into names and its stated count; count is None when the source states none."""
    body = body.strip("，,；;。、 ")
    found = TP_COUNT.search(body)
    count = int(found.group(1)) if found else None
    tokens = re.split(r"[、，,]", body[:found.start()] if found else body)
    # Committee minutes write 張三議員 inside the list, while plenary minutes write 張三.
    return [re.sub(r"議員$", "", t) for t in tokens if t and t != "無"], count


def tp_method(context):
    """Name the method from the words nearest the vote, because one passage can mention several."""
    hits = list(re.finditer(r"無記名|不記名|唱名|記名|舉手", context))
    word = hits[-1].group() if hits else ""
    return {"無記名": "無記名", "不記名": "無記名", "唱名": "唱名", "記名": "記名", "舉手": "舉手"}.get(word, "其他")


def tp_subject(context):
    """Keep the item and its motion; the full wording stays in the linked document."""
    context = re.split(r"在場議員", context)[0] if "在場議員" in context[-160:] else context
    quoted = re.search(r"「[^「」]*」[^「」]{0,24}提[議案][：:]?「[^「」]*」[^「」]*$", context)
    if quoted:
        return quoted.group()[:500]
    tail = re.split(r"議決[：:][^。]*。", context)[-1]
    # Older minutes put the vote in a note after its resolution, leaving nothing useful behind the split.
    return (tail if len(tail) >= 30 else context)[-300:]


def parse_taipei_votes(text):
    """Find each stated vote in Taipei minutes; whitespace is dropped first because names wrap across lines."""
    flat = re.sub(r"\s+", "", TP_PAGE_NO.sub("\n", text))
    votes, previous_end = [], 0
    for yes in TP_YES.finditer(flat):
        if yes.start() < previous_end:
            continue
        window = flat[yes.end():yes.end() + 2500]
        result = TP_RESULT.search(window)
        stop = result.start() if result else len(window)
        no = TP_NO.search(window, 0, stop)
        abstain = TP_ABSTAIN.search(window, no.end() if no else 0, stop)
        tail_from = abstain.end() if abstain else no.end() if no else 0
        ended = re.search(r"議決[：:]|。", window[tail_from:stop])
        tail_end = tail_from + ended.start() if ended else stop
        yes_body = window[:no.start() if no else abstain.start() if abstain else tail_end]
        # With no opposing list to bound it, trailing words such as 通過 would otherwise read as a name.
        counted = None if no or abstain else re.search(r"\d+[位人][）)]?", yes_body)
        yes_names, yes_count = tp_ballot_list(yes_body[:counted.end()] if counted else yes_body)
        # Unanimous votes omit the opposing list entirely, so an absent list is unstated rather than a parse failure.
        if not no and not yes_names and yes_count is None:
            continue
        no_names, no_count = tp_ballot_list(window[no.end():abstain.start() if abstain else tail_end]) if no else ([], None)
        abstain_names, abstain_count = tp_ballot_list(window[abstain.end():tail_end]) if abstain else ([], None)
        close_to = result.end() if result and result.start() - tail_end < 8 else tail_end
        context = flat[max(previous_end, yes.start() - 900):yes.start()]
        present = re.search(r"在場議員(?:含主席)?(?:計|共)?(\d+)[位人]", context[-120:])
        outcome = result.group(1) if result and close_to == result.end() else None
        if outcome is None:
            noted = re.search(r"議決[：:](不通過|未通過|通過|否決)[^。]{0,10}。（案經[^。]*$", context)
            outcome = noted.group(1) if noted else None
        named = bool(yes_names or no_names or abstain_names)
        method = tp_method(context[-160:])
        votes.append({"subject": tp_subject(context), "method": "記名" if method == "其他" and named else method,
                      "method_inferred": method == "其他" and named, "result_text": outcome,
                      "present": int(present.group(1)) if present else None,
                      "passed": None if outcome is None else outcome.endswith(("通過", "同意")) and not outcome.endswith(("不通過", "未通過", "不同意")),
                      "revote": bool(re.search(r"再表決|重行表決|再提付", context[-160:])),
                      "lists": {"贊成": yes_names, "反對": no_names, "棄權": abstain_names},
                      "counts": {"贊成": yes_count, "反對": no_count, "棄權": abstain_count}})
        previous_end = yes.end() + close_to
    return votes


def tp_split_names(token, roster):
    """Split names the source separated only by a line break, and only when every part is a known member."""
    if token in roster and roster[token] >= 3:
        return [token]
    for size in (2, 3, 4):
        head, rest = token[:size], token[size:]
        if roster.get(head, 0) >= 3 and rest:
            parts = tp_split_names(rest, roster)
            if parts and all(roster.get(p, 0) >= 3 for p in parts):
                return [head, *parts]
    return [token]


def step_taipei_votes(conn, dry_run=False):
    """Load votes stated in Taipei minutes; suspect votes are flagged in source_data rather than corrected."""
    with conn.cursor() as cur:
        cur.execute(f"""SELECT document_id, source_key, meeting_id, document_date, source_url, title, content_text
                        FROM proceedings.documents WHERE {TAIPEI_VOTE_DOCS} ORDER BY document_date, document_id""")
        docs = cur.fetchall()
    parsed = [(doc, parse_taipei_votes(doc[6])) for doc in docs]
    roster = {}
    for _, votes in parsed:
        for vote in votes:
            for names in vote["lists"].values():
                for name in names:
                    if TP_NAME.fullmatch(name) and len(name) <= 4:
                        roster[name] = roster.get(name, 0) + 1
    sql = """INSERT INTO proceedings.votes (council_code, source_key, meeting_id, document_id, voted_at, voted_at_text, vote_method,
             subject, passed, present_count, yes_count, no_count, abstain_count, source_url, source_data)
             VALUES ('taipei', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (council_code, source_key) DO UPDATE SET meeting_id = EXCLUDED.meeting_id, document_id = EXCLUDED.document_id,
             voted_at = EXCLUDED.voted_at, voted_at_text = EXCLUDED.voted_at_text, vote_method = EXCLUDED.vote_method,
             subject = EXCLUDED.subject, passed = EXCLUDED.passed, present_count = EXCLUDED.present_count, yes_count = EXCLUDED.yes_count,
             no_count = EXCLUDED.no_count, abstain_count = EXCLUDED.abstain_count, source_url = EXCLUDED.source_url,
             source_data = EXCLUDED.source_data, updated_at = now()
             RETURNING vote_id"""
    totals = {"votes": 0, "ballots": 0, "mismatch": 0, "conflict": 0, "bad_names": 0, "no_outcome": 0, "no_present": 0, "over_present": 0}
    for doc, votes in parsed:
        document_id, source_key, meeting_id, document_date, source_url, title, _ = doc
        keys = []
        with conn.cursor() as cur:
            for number, vote in enumerate(votes, 1):
                lists = {p: [part for name in names for part in tp_split_names(name, roster)] for p, names in vote["lists"].items()}
                bad = sorted({n for names in lists.values() for n in names if not TP_NAME.fullmatch(n)})
                ambiguous = sorted(n for n in set(sum(lists.values(), [])) if sum(n in ns for ns in lists.values()) > 1)
                listed = {p: len(ns) for p, ns in lists.items()}
                counts = vote["counts"]
                # A list-free vote legitimately states counts only, so only compare when names were given.
                mismatch = any(lists[p] and counts[p] is not None and counts[p] != listed[p] for p in lists)
                extra = {"parser": "taipei-minutes", "time_precision": "date", "document_title": title}
                if vote["revote"]:
                    extra["revote"] = True
                if vote["method_inferred"]:
                    extra["method_inferred"] = True
                if vote["result_text"]:
                    extra["result_text"] = vote["result_text"]
                if bad:
                    extra["unparsed_names"] = bad
                    totals["bad_names"] += 1
                if ambiguous:
                    extra["ballot_conflicts"] = {n: [p for p, ns in lists.items() if n in ns] for n in ambiguous}
                    totals["conflict"] += 1
                if mismatch:
                    extra["count_mismatch"] = {"reported": counts, "listed": listed}
                    totals["mismatch"] += 1
                totals["no_outcome"] += vote["passed"] is None
                totals["no_present"] += vote["present"] is None
                stated = [counts[p] if counts[p] is not None else listed[p] for p in lists]
                if vote["present"] is not None and sum(stated) > vote["present"]:
                    extra["over_present"] = True
                    totals["over_present"] += 1
                totals["votes"] += 1
                seats = [(n, p) for p, names in lists.items() for n in dict.fromkeys(names)
                         if n not in ambiguous and TP_NAME.fullmatch(n)]
                totals["ballots"] += len(seats)
                key = f"minutes:{source_key}:{number}"
                keys.append(key)
                if dry_run:
                    vote["checked"] = {"lists": lists, "extra": extra}
                    continue
                cur.execute(sql, (key, meeting_id, document_id, document_date, document_date.isoformat(), vote["method"], vote["subject"],
                                  vote["passed"], vote["present"], counts["贊成"], counts["反對"], counts["棄權"], source_url, Jsonb(extra)))
                vote_id = cur.fetchone()[0]
                cur.execute("DELETE FROM proceedings.vote_ballots WHERE vote_id = %s", (vote_id,))
                cur.executemany("INSERT INTO proceedings.vote_ballots (vote_id, member_name, position) VALUES (%s, %s, %s)",
                                [(vote_id, n, p) for n, p in seats])
            if not dry_run:
                # A re-parse that finds fewer votes must not leave the old surplus behind.
                cur.execute("DELETE FROM proceedings.votes WHERE council_code = 'taipei' AND document_id = %s AND NOT source_key = ANY(%s)",
                            (document_id, keys))
        if not dry_run:
            conn.commit()
    log("taipei-votes docs", len(docs), totals)
    return parsed, roster


STEPS = {"ly-meets": step_ly_meets, "ly-votes": step_ly_votes, "ly-bills": step_ly_bills, "ly-gazette": step_ly_gazette,
         "taichung-bills": step_taichung_bills, "taichung-minutes": step_taichung_minutes,
         "taipei-docs": step_taipei_docs, "taipei-bills": step_taipei_bills, "taipei-votes": step_taipei_votes,
         "new-taipei-books": step_new_taipei_books, "new-taipei-bills": step_new_taipei_bills,
         "new-taipei-html": step_new_taipei_html, "new-taipei-current": step_new_taipei_current, "ly-gazette-files": step_ly_gazette_files, "ly-backfill": step_ly_backfill,
         "ly-bills-gaps": step_ly_bills_gaps, "ly-bills-status": step_ly_bills_status}


# Old scans stay un-OCRed on purpose; undated New Taipei annexes are placed by ROC year or session.
OCR_ENGINE = "RapidOCR PP-OCRv5 mobile"
OCR_SCOPE = r"""council_code IN ('new_taipei', 'taipei') AND file_format = 'pdf' AND NOT coalesce(source_data ? 'ocr', false)
    AND (parse_status = 'parsed' OR (parse_status = 'failed' AND source_data ->> 'fetch_error' = 'empty text'))
    AND (document_date >= '2025-01-01' OR (document_date IS NULL AND source_key LIKE 'ntpbook:%%'
         AND title ~ '11[45]年|11[45][01][0-9]{3}|第4屆第([6-9]|10)次臨時會|第4屆第[5-8]次定期會'))"""


def step_ocr_text(conn, shard=(0, 1), limit=None):
    """OCR image-only pages of 2025-2026 PDFs and keep embedded text for the other pages."""
    import numpy as np
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    engine = RapidOCR(params={"Rec.lang_type": LangRec.CH, "Rec.ocr_version": OCRVersion.PPOCRV5,
                              "Rec.model_type": ModelType.MOBILE, "Global.log_level": "error"})
    fetcher = Fetcher(min_interval=1.0)
    with conn.cursor() as cur:
        cur.execute(f"""SELECT document_id, source_url FROM proceedings.documents WHERE {OCR_SCOPE} AND document_id %% %s = %s
                        ORDER BY council_code, document_id LIMIT %s""", (shard[1], shard[0], limit))
        rows = cur.fetchall()
    conn.commit()
    log("ocr-text documents", len(rows))
    changed = pages_done = 0
    for done, (document_id, url) in enumerate(rows, 1):
        try:
            body = fetcher.get(url, attempts=3).content
            parts, scores, ocr_pages = [], [], 0
            with pymupdf_open(body) as pdf:
                total = pdf.page_count
                for page in pdf:
                    text = page.get_text()
                    # Real text layers run to hundreds of characters; a few stray ones mean a scanned page.
                    if len(text.strip()) >= 20 or not page.get_images():
                        parts.append(text)
                        continue
                    pix = page.get_pixmap(dpi=200)
                    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
                    result = engine(image[:, :, ::-1])
                    parts.append("\n".join(result.txts or []))
                    scores.extend(float(s) for s in (result.scores or []))
                    ocr_pages += 1
            marker = {"ocr": {"engine": OCR_ENGINE, "pages": ocr_pages, "of": total,
                              "avg_score": round(sum(scores) / len(scores), 3) if scores else None}}
            text = clean("\n".join(parts)).strip()
            with conn.cursor() as cur:
                if ocr_pages and text:
                    cur.execute("""UPDATE proceedings.documents SET content_text = %s, content_sha256 = %s, parse_status = 'parsed',
                                   source_data = (coalesce(source_data, '{}'::jsonb) - 'fetch_error') || %s, updated_at = now()
                                   WHERE document_id = %s""", (text, hashlib.sha256(text.encode()).hexdigest(), Jsonb(marker), document_id))
                    changed += 1
                    pages_done += ocr_pages
                else:
                    # Remember the check, without touching updated_at, so re-runs and the sync skip it.
                    cur.execute("UPDATE proceedings.documents SET source_data = coalesce(source_data, '{}'::jsonb) || %s WHERE document_id = %s",
                                (Jsonb(marker), document_id))
            conn.commit()
        except Exception as err:  # one bad document must not stop a long run
            conn.rollback()
            log("ocr-text failed", document_id, str(err)[:160])
        if done % 25 == 0:
            log("ocr-text progress", done, "/", len(rows), "changed", changed, "ocr pages", pages_done)
    log("ocr-text finished changed", changed, "ocr pages", pages_done)


def pymupdf_open(body):
    import pymupdf
    return pymupdf.open(stream=body, filetype="pdf")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=[*STEPS, "fetch-text", "ocr-text"])
    parser.add_argument("--council")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--doc-types", default="議事錄,會議紀錄,速記錄,公報,議程,其他")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--agency", help="only documents with this source_data.agencyType")
    parser.add_argument("--max-db-gb", type=float, help="pause before the database grows past this size")
    parser.add_argument("--shard", default="0/1", help="k/n: only process documents whose id modulo n equals k")
    parser.add_argument("--dry-run", action="store_true", help="taipei-votes only: parse and report without writing")
    args = parser.parse_args()
    with connect() as conn:
        if args.step == "fetch-text":
            step_fetch_text(conn, args.council, 200, args.interval, args.doc_types.split(","), limit=args.limit,
                            shard=tuple(int(x) for x in args.shard.split("/")), max_db_gb=args.max_db_gb, agency=args.agency)
        elif args.step == "ocr-text":
            step_ocr_text(conn, shard=tuple(int(x) for x in args.shard.split("/")), limit=args.limit)
        elif args.step == "taipei-votes":
            step_taipei_votes(conn, dry_run=args.dry_run)
        else:
            STEPS[args.step](conn)
    log("step complete", args.step)


if __name__ == "__main__":
    main()
