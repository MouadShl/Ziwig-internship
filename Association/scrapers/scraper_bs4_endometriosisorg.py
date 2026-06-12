#!/usr/bin/env python3
"""
Scrape all 31 articles from:
https://endometriosis.org/topic/resources/articles/

- 4 listing pages
- 31 total articles
- full article content included
- saves to outputs/SRC026/SRC026.json

Run:
python scrapers/scraper_bs4_endometriosisorg.py --config configs/SRC026.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Set, Dict, Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 30


@dataclass
class Article:
    title: Optional[str]
    url: Optional[str]
    date: Optional[str]
    summary: Optional[str]
    full_content: Optional[str]


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def normalize(url: Optional[str], base: str) -> Optional[str]:
    if not url:
        return None
    return urljoin(base, url)


def fetch(session: requests.Session, url: str) -> Optional[str]:
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}")
        return None


def extract_articles_from_listing(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Optional[str]]]:
    articles = []
    seen_urls = set()

    # This site uses div.post on listing pages
    containers = soup.find_all("div", class_=re.compile(r"\bpost\b"))

    for container in containers:
        link = container.find("a", class_="title", href=True)
        if not link:
            continue

        url = normalize(link.get("href"), base_url)
        if not url:
            continue

        if "/resources/articles/" not in url or "/topic/" in url:
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = clean(link.get_text())

        date_elem = container.find("div", class_="date")
        date = clean(date_elem.get_text()) if date_elem else None

        summary_elem = container.find("p")
        summary = clean(summary_elem.get_text()) if summary_elem else None

        articles.append({
            "title": title,
            "url": url,
            "date": date,
            "summary": summary,
        })

    return articles


def extract_full_content(soup: BeautifulSoup) -> Optional[str]:
    # Main target on article pages
    content_div = soup.find("div", class_="entry-content")

    # Fallbacks
    if not content_div:
        content_div = (
            soup.find("div", class_="post")
            or soup.find("article")
            or soup.find("main")
            or soup.find("div", id="canvas")
        )

    if not content_div:
        return None

    # Remove junk
    for elem in content_div.find_all(["script", "style", "nav", "header", "footer", "iframe", "form"]):
        elem.decompose()

    for elem in content_div.find_all(class_=re.compile(r"(sharedaddy|related|navigation|meta|comment|cookie|banner|advert)")):
        elem.decompose()

    # Keep readable structure
    chunks = []
    for elem in content_div.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        text = clean(elem.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) < 2:
            continue
        chunks.append(text)

    if not chunks:
        text = clean(content_div.get_text("\n", strip=True))
        return text

    full_text = "\n\n".join(chunks)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    return full_text.strip()


def scrape_article_content(session: requests.Session, url: str) -> Optional[str]:
    html = fetch(session, url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    return extract_full_content(soup)


def scrape_all_articles(start_url: str, sleep_time: float = 1.0) -> List[Article]:
    if not is_allowed(start_url):
        raise Exception("Blocked by robots.txt")

    session = build_session()
    all_articles: List[Article] = []
    seen_urls: Set[str] = set()

    # exactly 4 pages
    base = start_url.rstrip("/")
    page_urls = [
        f"{base}/",
        f"{base}/page/2/",
        f"{base}/page/3/",
        f"{base}/page/4/",
    ]

    print(f"Will scrape {len(page_urls)} listing pages")
    print("=" * 60)

    for page_num, page_url in enumerate(page_urls, start=1):
        print(f"\n[LISTING PAGE {page_num}] {page_url}")

        html = fetch(session, page_url)
        if not html:
            print("  Failed to fetch page, skipping")
            continue

        soup = BeautifulSoup(html, "html.parser")
        page_articles = extract_articles_from_listing(soup, page_url)

        print(f"  Found {len(page_articles)} articles on page {page_num}")

        for item in page_articles:
            url = item["url"]
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)

            print(f"  Scraping full content: {item['title']}")
            full_content = scrape_article_content(session, url)

            if full_content:
                print(f"    OK - {len(full_content)} chars")
            else:
                print("    NO CONTENT EXTRACTED")

            all_articles.append(
                Article(
                    title=item["title"],
                    url=item["url"],
                    date=item["date"],
                    summary=item["summary"],
                    full_content=full_content,
                )
            )

            time.sleep(sleep_time)

    print("\n" + "=" * 60)
    print(f"TOTAL ARTICLES SCRAPED: {len(all_articles)}")
    print(f"WITH FULL CONTENT: {sum(1 for a in all_articles if a.full_content)}")
    return all_articles


def save_articles(articles: List[Article], output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([asdict(a) for a in articles], f, ensure_ascii=False, indent=2)

    print(f"\nSaved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape endometriosis.org article listings + full content")
    parser.add_argument("--config", required=True, help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)

    start_url = config.get("start_url", "https://endometriosis.org/topic/resources/articles/")
    output = config.get("output_file", "outputs/SRC026/SRC026.json")
    sleep_time = float(config.get("sleep_seconds", 1.0))

    articles = scrape_all_articles(start_url, sleep_time)
    save_articles(articles, output)

    print(f"\nDONE: scraped {len(articles)} articles")


if __name__ == "__main__":
    main()