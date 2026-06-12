#!/usr/bin/env python3
"""
QENDO Australia Blog Scraper (SRC027)
Scrapes blog articles from qendo.org.au
Includes personal stories, medical info, and expert advice
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Set, Dict, Any
from urllib.parse import urljoin, urlparse

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
    author: Optional[str]
    category: Optional[str]
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
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
    })

    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) > 2 else None


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
        logging.error("Error fetching %s: %s", url, e)
        return None


def extract_articles(soup: BeautifulSoup, base_url: str) -> List[dict]:
    """Extract article listings from blog page"""
    articles = []
    seen = set()

    # QENDO uses article tags or divs with post classes
    containers = (
        soup.find_all("article", class_=re.compile("post|blog"))
        or soup.find_all("div", class_=re.compile("post|blog|entry"))
        or soup.select(".blog-posts article")
        or soup.select("[class*='post']")
    )

    for c in containers:
        # Find title link
        link = (
            c.select_one("h2.entry-title a")
            or c.select_one("h1 a")
            or c.select_one("h3 a")
            or c.find("a", rel="bookmark")
            or c.find("a", href=re.compile(r"/blog/\d{4}/\d{2}/"))
        )

        if not link:
            continue

        url = normalize(link.get("href"), base_url)
        if not url:
            continue

        # Skip pagination links and non-article URLs
        if "/blog/page/" in url or "/category/" in url or "/tag/" in url:
            continue

        if url in seen:
            continue
        seen.add(url)

        title = clean(link.get_text())

        # Extract date
        date_node = (
            c.select_one("time.entry-date")
            or c.select_one("time")
            or c.select_one(".date")
            or c.select_one(".published")
        )
        date = clean(date_node.get_text()) if date_node else None
        if date_node and not date:
            date = date_node.get("datetime", "")

        # Extract author
        author_node = (
            c.select_one(".author")
            or c.select_one(".byline")
            or c.select_one("[rel='author']")
        )
        author = clean(author_node.get_text()) if author_node else None

        # Extract category/tags
        category_node = c.select_one(".category") or c.find("a", href=re.compile(r"/category/"))
        category = clean(category_node.get_text()) if category_node else None

        # Extract summary/excerpt
        summary_node = (
            c.select_one(".entry-summary")
            or c.select_one(".excerpt")
            or c.select_one(".post-excerpt")
            or c.select_one("p")
        )
        summary = clean(summary_node.get_text()) if summary_node else None

        articles.append({
            "title": title,
            "url": url,
            "date": date,
            "author": author,
            "category": category,
            "summary": summary,
        })

    return articles


def get_next_page(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """Find next page URL for pagination"""
    # Look for next page link
    next_btn = (
        soup.select_one("a.next")
        or soup.select_one(".pagination .next")
        or soup.select_one("a[rel='next']")
        or soup.find("a", text=re.compile(r"next|older", re.I))
    )

    if next_btn and next_btn.get("href"):
        return normalize(next_btn["href"], current_url)

    # Try to construct next page URL
    if "/blog/page/" in current_url:
        current_match = re.search(r'/blog/page/(\d+)/', current_url)
        if current_match:
            current_num = int(current_match.group(1))
            next_num = current_num + 1
            return re.sub(r'/blog/page/\d+/', f'/blog/page/{next_num}/', current_url)
    else:
        # First page - construct page 2
        base = current_url.rstrip('/')
        return f"{base}/page/2/"

    return None


def extract_full_content(article_soup: BeautifulSoup) -> Optional[str]:
    """Extract full article content from individual blog post"""
    
    # Try multiple content selectors (QENDO likely uses WordPress)
    content_selectors = [
        "div.entry-content",
        "article .entry-content",
        "div.post-content",
        "div.blog-content",
        "article",
        "main article",
        ".site-main article",
        "[role='main']",
    ]
    
    content_node = None
    for selector in content_selectors:
        content_node = article_soup.select_one(selector)
        if content_node:
            break

    if not content_node:
        return None

    # Remove junk elements
    junk_selectors = [
        "script", "style", "nav", "header", "footer",
        ".sharedaddy", ".jp-relatedposts", ".related-posts",
        ".post-navigation", ".nav-links", ".pagination",
        ".comments", ".comment-form", ".reply",
        ".entry-meta", ".post-meta", ".author-box",
        ".sidebar", ".widget", ".tags", ".categories",
        "iframe", "noscript", "form",
        ".wp-block-button", ".gform"
    ]
    
    for selector in junk_selectors:
        for element in content_node.select(selector):
            element.decompose()

    # Extract content with structure
    # Get paragraphs, headers, and list items
    content_elements = content_node.find_all(["p", "h2", "h3", "h4", "li", "blockquote"])
    
    if content_elements:
        texts = []
        for elem in content_elements:
            text = elem.get_text(strip=True)
            # Filter navigation-like short text but keep headers
            if elem.name in ["h2", "h3", "h4"] or len(text) > 30:
                texts.append(text)
        
        if texts:
            full_text = "\n\n".join(texts)
            full_text = re.sub(r"\n{3,}", "\n\n", full_text)
            return full_text.strip()

    # Fallback: get all text
    full_text = content_node.get_text("\n", strip=True)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    
    return full_text.strip() if len(full_text) > 200 else None


def scrape_full_article(session: requests.Session, url: str) -> Optional[str]:
    """Fetch and extract content from individual article page"""
    html = fetch(session, url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    return extract_full_content(soup)


def scrape(start_url: str, sleep_time: float, max_pages: int = 10) -> List[Article]:
    """Main scraping function"""
    
    session = build_session()

    current = start_url
    visited_pages: Set[str] = set()
    seen_urls: Set[str] = set()
    all_articles: List[Article] = []
    page_count = 0

    print(f"Starting QENDO scraper from: {start_url}")
    print(f"Max pages: {max_pages}")
    print("=" * 70)

    while current and current not in visited_pages and page_count < max_pages:
        page_count += 1
        visited_pages.add(current)

        print(f"\n[Page {page_count}] Fetching: {current}")
        html = fetch(session, current)
        
        if not html:
            print(f"  ERROR: Failed to fetch page {page_count}")
            break

        soup = BeautifulSoup(html, "html.parser")
        page_articles = extract_articles(soup, current)

        print(f"  Found {len(page_articles)} articles")

        for item in page_articles:
            if item["url"] in seen_urls:
                print(f"  SKIP (duplicate): {item['title'][:50]}...")
                continue

            seen_urls.add(item["url"])

            print(f"  Scraping: {item['title'][:60]}...")
            full_content = scrape_full_article(session, item["url"])

            if full_content:
                preview = full_content[:70].replace("\n", " ")
                print(f"    ✓ Content: {len(full_content)} chars | {preview}...")
            else:
                print(f"    ✗ No content extracted")

            all_articles.append(
                Article(
                    title=item["title"],
                    url=item["url"],
                    date=item["date"],
                    author=item["author"],
                    category=item["category"],
                    summary=item["summary"],
                    full_content=full_content,
                )
            )

            time.sleep(sleep_time)

        # Get next page
        next_page = get_next_page(soup, current)

        if not next_page or next_page in visited_pages:
            print(f"\nNo more pages found")
            break

        print(f"  Next page: {next_page}")
        time.sleep(sleep_time)
        current = next_page

    print("\n" + "=" * 70)
    print(f"SCRAPING COMPLETE")
    print(f"Total pages visited: {page_count}")
    print(f"Total articles scraped: {len(all_articles)}")
    
    with_content = sum(1 for a in all_articles if a.full_content)
    print(f"Articles with full_content: {with_content}")

    return all_articles


def save(data: List[Article], path: str) -> None:
    """Save articles to JSON file"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in data], f, ensure_ascii=False, indent=2)
    print(f"\n✅ Saved to: {path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape QENDO Australia blog articles")
    parser.add_argument("--config", required=True, help="Path to config JSON file")
    args = parser.parse_args()

    config = load_config(args.config)

    start_url = config["start_url"]
    output = config["output_file"]
    sleep_time = float(config.get("sleep_seconds", 2))
    max_pages = int(config.get("max_pages", 10))

    data = scrape(start_url, sleep_time, max_pages)
    save(data, output)

    print(f"\n🎉 Completed! Scraped {len(data)} articles from QENDO")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    main()