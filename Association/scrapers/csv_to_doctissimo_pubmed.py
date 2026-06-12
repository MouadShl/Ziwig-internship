import csv
import json
from pathlib import Path

# === PATH SETUP ===
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = PROJECT_DIR / "csv-endometrio-set.csv"

OUTPUT_JSON = PROJECT_DIR / "configs" / "SRC038.json"
OUTPUT_JSONL = PROJECT_DIR / "configs" / "SRC038.jsonl"

OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_date(date_str):
    if not date_str:
        return ""

    date_str = str(date_str).strip()

    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            m, d, y = parts
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    if date_str.isdigit() and len(date_str) == 4:
        return f"{date_str}-01-01"

    return date_str


def build_thread(row):
    pmid = clean(row.get("PMID"))
    title = clean(row.get("Title"))
    authors = clean(row.get("Authors"))
    journal = clean(row.get("Journal/Book"))
    year = clean(row.get("Publication Year"))
    date = parse_date(row.get("Create Date"))
    doi = clean(row.get("DOI"))
    citation = clean(row.get("Citation"))

    if not pmid:
        return None

    thread_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    author = authors.split(",")[0] if authors else "Unknown"

    body_parts = [
        f"Title: {title}",
        f"PMID: {pmid}",
        f"Authors: {authors}",
        f"Journal: {journal}",
        f"Year: {year}",
    ]

    if citation:
        body_parts += ["", "=== CITATION ===", citation]

    if doi:
        body_parts.append(f"DOI: {doi}")

    body = "\n".join(body_parts).strip()

    return {
        "thread_id": pmid,
        "thread_title": title,
        "thread_title_detail": title,
        "thread_url": thread_url,
        "thread_starter": author,
        "thread_starter_url": "",
        "listing_author": author,
        "listing_author_url": "",
        "opening_post_date": date,
        "opening_post_body": body,
        "comments_count": 0,
        "replies_count": 0,
        "posts": [
            {
                "post_id": f"{pmid}_1",
                "post_author": author,
                "post_date": date,
                "post_body": body,
            }
        ],
    }


def main():
    threads = []

    with open(INPUT_FILE, "r", encoding="latin-1") as f:
        reader = csv.DictReader(f)

        for row in reader:
            thread = build_thread(row)
            if thread:
                threads.append(thread)

    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(threads, f, ensure_ascii=False, indent=2)

    # JSONL
    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        for t in threads:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    print(f"✅ DONE: {len(threads)} threads saved")
    print(f"📁 {OUTPUT_JSON}")
    print(f"📁 {OUTPUT_JSONL}")


if __name__ == "__main__":
    main()