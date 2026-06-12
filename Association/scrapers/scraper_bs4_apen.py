import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from bs4 import BeautifulSoup, Tag

INPUT_FILE = Path("SRC030.json")
OUTPUT_FILE = Path("SRC030.json")
TIMEOUT = 30
SLEEP_SECONDS = 0.8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

DATE_RE = re.compile(
    r"\b(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)\s+"
    r"\d{1,2}\s+[a-zàéìòù]+\s+\d{4}\b",
    re.IGNORECASE,
)

STOP_TEXTS = {
    "documenti allegati",
    "condividi questa pagina",
    "share",
    "categorie",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()



def is_probably_article_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    if "/it/" not in url:
        return False
    if url.rstrip("/").endswith("/it"):
        return False
    return True



def read_input_urls(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    items: List[Dict[str, Any]] = []
    for row in data:
        if isinstance(row, dict) and is_probably_article_url(row.get("url", "")):
            items.append(dict(row))
    return items



def detect_date(main_root: Tag) -> str:
    time_tag = main_root.find("time")
    if time_tag:
        time_text = clean_text(time_tag.get_text(" ", strip=True))
        if time_text:
            return time_text

    full_text = clean_text(main_root.get_text(" ", strip=True))
    match = DATE_RE.search(full_text)
    return match.group(0) if match else ""



def find_title(soup: BeautifulSoup) -> str:
    for selector in ["main h1", "article h1", "h1", "main h2", "article h2"]:
        tag = soup.select_one(selector)
        if tag:
            text = clean_text(tag.get_text(" ", strip=True))
            if text:
                return text
    return ""



def should_skip_text(text: str) -> bool:
    low = text.lower()
    return any(stop in low for stop in STOP_TEXTS)



def find_main_root(soup: BeautifulSoup, title: str) -> Tag:
    if title:
        for selector in ["main", "article", "body"]:
            root = soup.select_one(selector)
            if root and title.lower() in clean_text(root.get_text(" ", strip=True)).lower():
                return root

    return soup.select_one("main") or soup.select_one("article") or soup.body or soup



def extract_attachments(main_root: Tag) -> List[Dict[str, str]]:
    attachments: List[Dict[str, str]] = []
    seen = set()
    attached_section_found = False

    for tag in main_root.find_all(["h2", "h3", "h4", "a"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if not text:
            continue

        if tag.name in {"h2", "h3", "h4"} and "documenti allegati" in text.lower():
            attached_section_found = True
            continue

        if attached_section_found and tag.name == "a":
            href = clean_text(tag.get("href", ""))
            if href and href not in seen:
                attachments.append({"label": text, "url": href})
                seen.add(href)

    return attachments



def extract_content(main_root: Tag, title: str) -> str:
    paragraphs: List[str] = []
    seen = set()

    for p in main_root.find_all("p"):
        text = clean_text(p.get_text(" ", strip=True))
        if not text:
            continue
        if title and text == title:
            continue
        if DATE_RE.fullmatch(text):
            continue
        if should_skip_text(text):
            continue
        if len(text) < 20:
            continue
        if text not in seen:
            paragraphs.append(text)
            seen.add(text)

    if paragraphs:
        return "\n\n".join(paragraphs)

    text_blocks: List[str] = []
    for tag in main_root.find_all(["div", "section"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if not text or should_skip_text(text):
            continue
        if title and title.lower() not in text.lower() and len(text) > 200:
            text_blocks.append(text)

    return "\n\n".join(text_blocks[:3])



def extract_category(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"property": "article:section"})
    if meta and meta.get("content"):
        return clean_text(meta["content"])
    return ""



def parse_article(session: requests.Session, url: str) -> Dict[str, Any]:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title = find_title(soup)
    main_root = find_main_root(soup, title)
    date_text = detect_date(main_root)
    content = extract_content(main_root, title)
    attachments = extract_attachments(main_root)
    category = extract_category(soup)

    return {
        "url": url,
        "title": title,
        "date": date_text,
        "category": category,
        "content": content,
        "attachments": attachments,
        "word_count": len(content.split()) if content else 0,
    }



def merge_row(original: Dict[str, Any], scraped: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(original)
    merged.update(scraped)
    return merged



def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE.resolve()}\n"
            "Put this script in the same folder as SRC030.json."
        )

    rows = read_input_urls(INPUT_FILE)
    print(f"Loaded {len(rows)} URLs from {INPUT_FILE}")

    session = requests.Session()
    session.headers.update(HEADERS)

    fixed_rows: List[Dict[str, Any]] = []

    for i, row in enumerate(rows, start=1):
        url = row.get("url", "").strip()
        print(f"[{i}/{len(rows)}] Scraping: {url}")
        try:
            scraped = parse_article(session, url)
            fixed_rows.append(merge_row(row, scraped))
            print(
                f"    OK | title={scraped['title'][:60]!r} | "
                f"date={scraped['date']!r} | words={scraped['word_count']}"
            )
        except Exception as exc:
            print(f"    ERROR: {exc}")
            failed = dict(row)
            failed["scrape_error"] = str(exc)
            fixed_rows.append(failed)
        time.sleep(SLEEP_SECONDS)

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(fixed_rows, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(fixed_rows)} rows to {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
