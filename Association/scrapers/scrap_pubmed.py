import csv
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_CSV = PROJECT_DIR / "ctg-studies.csv"
OUTPUT_JSON = PROJECT_DIR / "pubmed_scraped.json"
OUTPUT_JSONL = PROJECT_DIR / "pubmed_scraped.jsonl"
OUTPUT_ERRORS = PROJECT_DIR / "pubmed_scraped_errors.json"

EMAIL = "your_email@example.com"
TOOL_NAME = "scrap_pubmed"
SLEEP_SECONDS = 0.35
TIMEOUT = 30

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def clean(value: Optional[str]) -> str:
    return (value or "").strip()


def load_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def build_query(row: Dict[str, str]) -> str:
    nct = clean(row.get("NCT Number"))
    title = clean(row.get("Study Title"))

    parts = []
    if nct:
        parts.append(f'"{nct}"[All Fields]')
    if title:
        parts.append(f'"{title}"[Title]')

    return " OR ".join(parts)


def esearch(session: requests.Session, query: str) -> List[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": 5,
        "tool": TOOL_NAME,
        "email": EMAIL,
    }
    r = session.get(ESEARCH_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])


def esummary(session: requests.Session, pmid: str) -> Dict:
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "json",
        "tool": TOOL_NAME,
        "email": EMAIL,
    }
    r = session.get(ESUMMARY_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("result", {}).get(str(pmid), {})


def efetch_abstract(session: requests.Session, pmid: str) -> str:
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": EMAIL,
    }
    r = session.get(EFETCH_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text

    abstract_parts = []
    start = 0
    open_tag = "<AbstractText"
    close_tag = "</AbstractText>"

    while True:
        i = text.find(open_tag, start)
        if i == -1:
            break
        j = text.find(">", i)
        if j == -1:
            break
        k = text.find(close_tag, j)
        if k == -1:
            break
        part = text[j + 1:k].strip()
        if part:
            abstract_parts.append(part)
        start = k + len(close_tag)

    return "\n\n".join(abstract_parts).strip()


def build_pubmed_url(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def parse_authors(summary: Dict) -> List[str]:
    authors = []
    for author in summary.get("authors", []) or []:
        name = clean(author.get("name"))
        if name:
            authors.append(name)
    return authors


def parse_pubdate(summary: Dict) -> str:
    return clean(summary.get("pubdate")) or clean(summary.get("epubdate"))


def scrape_pubmed_for_row(session: requests.Session, row: Dict[str, str]) -> Dict:
    nct = clean(row.get("NCT Number"))
    title = clean(row.get("Study Title"))
    study_url = clean(row.get("Study URL"))
    status = clean(row.get("Study Status"))

    query = build_query(row)
    pmids = esearch(session, query) if query else []

    result = {
        "nct_number": nct,
        "study_title": title,
        "study_url": study_url,
        "study_status": status,
        "pubmed_query": query,
        "pubmed_match_count": len(pmids),
        "pubmed_matches": [],
    }

    for pmid in pmids:
        summary = esummary(session, pmid)
        abstract = efetch_abstract(session, pmid)

        result["pubmed_matches"].append({
            "pmid": pmid,
            "pubmed_url": build_pubmed_url(pmid),
            "title": clean(summary.get("title")),
            "authors": parse_authors(summary),
            "journal": clean(summary.get("fulljournalname")) or clean(summary.get("source")),
            "publication_date": parse_pubdate(summary),
            "doi": clean(summary.get("elocationid")),
            "article_ids": summary.get("articleids", []),
            "abstract": abstract,
        })

        time.sleep(SLEEP_SECONDS)

    return result


def save_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_jsonl(path: Path, rows: List[Dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    if not INPUT_CSV.exists():
        raise FileNotFoundError(
            f"CSV not found: {INPUT_CSV}\n"
            f"Expected it in: {PROJECT_DIR}"
        )

    rows = load_csv_rows(INPUT_CSV)
    print(f"Loaded {len(rows)} studies from: {INPUT_CSV}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": f"{TOOL_NAME}/1.0 ({EMAIL})",
        "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
    })

    scraped = []
    errors = []

    for idx, row in enumerate(rows, start=1):
        nct = clean(row.get("NCT Number"))
        title = clean(row.get("Study Title"))
        print(f"[{idx}/{len(rows)}] {nct} | {title[:80]}")

        try:
            item = scrape_pubmed_for_row(session, row)
            scraped.append(item)
            print(f"    matches: {item['pubmed_match_count']}")
        except Exception as e:
            errors.append({
                "row_number": idx,
                "nct_number": nct,
                "study_title": title,
                "error": str(e),
            })
            print(f"    ERROR: {e}")

        time.sleep(SLEEP_SECONDS)

    save_json(OUTPUT_JSON, scraped)
    save_jsonl(OUTPUT_JSONL, scraped)
    save_json(OUTPUT_ERRORS, errors)

    print("\nDone.")
    print(f"Saved JSON:   {OUTPUT_JSON}")
    print(f"Saved JSONL:  {OUTPUT_JSONL}")
    print(f"Saved errors: {OUTPUT_ERRORS}")


if __name__ == "__main__":
    main()