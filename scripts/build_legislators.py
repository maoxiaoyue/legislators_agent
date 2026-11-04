#!/usr/bin/env python3
"""Build data/legislators/legislators.json from the Legislative Yuan's roster PDF.

The PDF (https://data.ly.gov.tw/legislator.pdf) is the source of record. Its table
has no rules, wraps long names across lines, and shows rare characters as empty
boxes, so ly.govapi.tw is used to validate each row and restore those characters.
A name is only replaced when it matches the API apart from spacing and punctuation;
real disagreements keep the PDF text and record the API value in `note`.

Run from legagent/ (needs PyMuPDF):
    python scripts/build_legislators.py
"""

import collections
import json
import pathlib
import re
import sys
import urllib.request

import fitz

PDF_URL = "https://data.ly.gov.tw/legislator.pdf"
API_URL = "https://ly.govapi.tw/v2/legislators?limit=2000"
RAW_DIR = pathlib.Path("data/raw")
OUT_PATH = pathlib.Path("data/legislators/legislators.json")
USER_AGENT = "legagent-ingest/0.1"

PUA = re.compile("[-]")
HEADER = re.compile("屆期|編號|姓名|立法院開放|歷屆立法")


def fetch(url, path):
    """Download url once; delete the cached file in data/raw to refresh it."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            path.write_bytes(resp.read())
    return path


def normalize(name):
    return re.sub(r"[\s．‧·•]", "", name)


def pdf_pattern(pdf_text):
    """Regex for PDF text ignoring layout; a private-use character matches any one character."""
    return "".join("." if PUA.fullmatch(c) else re.escape(c) for c in normalize(pdf_text))


def matches(pdf_text, api_name):
    return re.fullmatch(pdf_pattern(pdf_text), normalize(api_name)) is not None


def is_prefix(pdf_text, api_name):
    """True when the PDF text is the start of the name, as in wrapped or truncated cells."""
    pattern = pdf_pattern(pdf_text)
    return bool(pattern) and re.match(pattern, normalize(api_name)) is not None


def page_words(page):
    return [
        {"x0": w[0], "top": w[1], "x1": w[2], "bottom": w[3], "text": w[4]}
        for w in page.get_text("words")
    ]


def middle(word):
    return (word["top"] + word["bottom"]) / 2


def parse_pdf(path, api_names):
    """Return rows of (term, no, text, page), using API names to place wrapped lines."""
    doc = fitz.open(path)
    term_columns = sorted(w["x0"] for w in page_words(doc[0]) if w["text"] == "屆期")
    width = doc[0].rect.width
    groups = [
        (x - 6, term_columns[i + 1] - 6 if i + 1 < len(term_columns) else width)
        for i, x in enumerate(term_columns)
    ]

    rows, leftovers = [], []
    for page_no, page in enumerate(doc, 1):
        words = page_words(page)
        footer = min(
            (w["top"] for w in words if w["text"].startswith("第") and w["top"] > page.rect.height * 0.8),
            default=page.rect.height,
        )
        words = [w for w in words if w["top"] < footer - 1 and not HEADER.search(w["text"])]
        for left, right in groups:
            group = [w for w in words if left <= (w["x0"] + w["x1"]) / 2 < right]
            numbers = sorted(
                (w for w in group if re.fullmatch(r"\d{4}", w["text"])), key=lambda w: w["top"]
            )
            if not numbers:
                continue
            lines = collections.defaultdict(list)
            terms = {}
            for w in group:
                if re.fullmatch(r"\d{4}", w["text"]):
                    continue
                if w["x1"] <= numbers[0]["x0"] + 1 and re.fullmatch(r"\d{1,2}", w["text"]):
                    nearest = min(numbers, key=lambda n: abs(middle(n) - middle(w)))
                    terms[nearest["text"]] = int(w["text"])
                elif w["x0"] >= numbers[0]["x1"] - 1:
                    lines[round(middle(w))].append(w)
            ordered = [
                "".join(w["text"] for w in sorted(lines[y], key=lambda w: w["x0"])) for y in sorted(lines)
            ]

            cursor = 0
            for number in numbers:
                no, term = number["text"], terms.get(number["text"])
                expected = api_names.get((term, no))
                used = 1
                if expected:
                    # Take every line that still fits the expected name, so a wrapped or
                    # truncated cell does not shift the rows after it.
                    for k in range(1, 6):
                        if cursor + k > len(ordered) or not is_prefix("".join(ordered[cursor : cursor + k]), expected):
                            break
                        used = k
                text = " ".join(ordered[cursor : cursor + used])
                rows.append({"term": term, "no": no, "text": text, "page": page_no})
                cursor += used
            leftovers.extend((page_no, line) for line in ordered[cursor:])
    return rows, leftovers


def build(rows, api_names):
    terms = collections.defaultdict(list)
    review = []
    for row in rows:
        key = (row["term"], row["no"])
        expected = api_names.get(key)
        member = {"no": row["no"]}
        if expected and matches(row["text"], expected):
            member["name"] = expected
        elif expected and is_prefix(row["text"], expected):
            member["name"] = expected
            member["note"] = f"PDF 第 {row['page']} 頁少字，顯示為「{row['text']}」"
        else:
            member["name"] = row["text"]
            member["note"] = f"API 記為「{expected}」" if expected else "API 無此筆資料"
        if "note" in member:
            review.append({"term": row["term"], **member})
        terms[row["term"]].append(member)
    for members in terms.values():
        members.sort(key=lambda m: m["no"])
    return terms, review


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    pdf_path = fetch(PDF_URL, RAW_DIR / "legislator.pdf")
    api_path = fetch(API_URL, RAW_DIR / "ly_legislators.json")

    api = json.loads(api_path.read_text(encoding="utf-8"))["legislators"]
    api_names, without_no = {}, []
    for record in api:
        if record["歷屆立法委員編號"] is None:
            without_no.append({"term": record["屆"], "name": record["委員姓名"], "on_duty": record["到職日"]})
        else:
            api_names[(record["屆"], str(record["歷屆立法委員編號"]).zfill(4))] = record["委員姓名"]

    rows, leftovers = parse_pdf(pdf_path, api_names)
    terms, review = build(rows, api_names)

    problems = [f"unassigned line: {item}" for item in leftovers]
    problems += [f"missing term: {r}" for r in rows if r["term"] is None or not r["text"]]
    problems += [f"unresolved character: {r}" for r in review if PUA.search(r["name"])]
    seen = collections.Counter((r["term"], r["no"]) for r in rows)
    problems += [f"duplicate row: {k}" for k, count in seen.items() if count > 1]
    if problems:
        print("\n".join(problems))
        return 1

    payload = {
        "source": PDF_URL,
        "title": "立法院開放資料服務平台 歷屆立法委員編號",
        "pdf_coverage": "第 2 屆至第 11 屆，PDF 建立於 2024-03-26；第 1 屆不在 PDF 內",
        "validated_against": "https://ly.govapi.tw/v2/legislators",
        "total_records": len(rows),
        "unique_legislators": len({r["no"] for r in rows}),
        "terms": [
            {"term": term, "count": len(terms[term]), "members": terms[term]} for term in sorted(terms)
        ],
        "needs_review": review,
        "in_api_not_in_pdf": without_no,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {OUT_PATH}: {payload['total_records']} records, {payload['unique_legislators']} legislators")
    print({term: len(terms[term]) for term in sorted(terms)})
    print("needs_review:", json.dumps(review, ensure_ascii=False))
    print("in_api_not_in_pdf:", json.dumps(without_no, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
