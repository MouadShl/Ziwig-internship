#!/usr/bin/env python3
"""
API-first + BeautifulSoup fallback scraper for https://endodailynews.com/

What it collects
----------------
1. Articles/posts
   - title
   - publication date (formatted like "September 27, 2024" when parseable)
   - author / doctor name
   - featured image URL
   - excerpt
   - full article URL
   - categories
   - section label (Featured / Editorial picks / Latest Articles / Uncategorized)

2. EndoLife / Endo Daily magazine metadata
   - issue label
   - date label
   - cover image URL
   - featured topics extracted from nearby captions / alt text / filenames

Strategy
--------
- Try the WordPress REST API first.
- If the API is unavailable or incomplete, scrape HTML archives and post pages.
- Also scan the homepage to mark hero / featured / editorial / latest sections.
- Save progress every 10 articles into JSONL files under outputs/SRC023/.

Default output files
--------------------
- outputs/SRC023/SRC023_articles_final.jsonl
- outputs/SRC023/SRC023_magazine_covers_final.jsonl
- outputs/SRC023/SRC023_summary.json

Usage
-----
python scrapers/scraper_bs4_endodailynews.py
python scrapers/scraper_bs4_endodailynews.py --output-dir outputs/SRC023
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


SOURCE_ID = "SRC023"
SOURCE_MODE = "association"
SOURCE_NAME = "Endo Daily News"
SOURCE_COUNTRY = "International"
SOURCE_LANGUAGE = "en"

BASE_URL = "https://endodailynews.com/"
API_POSTS = urljoin(BASE_URL, "wp-json/wp/v2/posts")
API_CATEGORIES = urljoin(BASE_URL, "wp-json/wp/v2/categories")
API_MEDIA = urljoin(BASE_URL, "wp-json/wp/v2/media")
MAGAZINE_URL = urljoin(BASE_URL, "endolife-magazine/")
REQUEST_DELAY_SECONDS = 1.0
SAVE_EVERY = 10
TIMEOUT = 30

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ARTICLE_URL_EXCLUDES = (
    "/category/",
    "/tag/",
    "/author/",
    "/wp-json/",
    "/wp-content/",
    "/feed/",
    "/comments/",
    "/search/",
    "/page/",
    "#respond",
)

SECTION_PATTERNS = OrderedDict(
    [
        ("Featured", re.compile(r"\b(featured|hero|top stories?)\b", re.I)),
        ("Editorial picks", re.compile(r"\b(editorial\s*picks?)\b", re.I)),
        ("Latest Articles", re.compile(r"\b(latest\s*articles?|latest|recent)\b", re.I)),
    ]
)

MONTH_NAMES = {
    "january": "January",
    "february": "February",
    "march": "March",
    "april": "April",
    "may": "May",
    "june": "June",
    "july": "July",
    "august": "August",
    "september": "September",
    "october": "October",
    "november": "November",
    "december": "December",
}


@dataclass
class ArticleRecord:
    title: str
    author: str
    date: str
    excerpt: str
    image_url: str
    article_url: str
    categories: List[str] = field(default_factory=list)
    section: str = "Uncategorized"
    id: Optional[int] = None

    def as_output_dict(self) -> Dict[str, object]:
        return {
            "source_id": SOURCE_ID,
            "source_mode": SOURCE_MODE,
            "source_name": SOURCE_NAME,
            "source_country": SOURCE_COUNTRY,
            "source_language": SOURCE_LANGUAGE,
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "date": self.date,
            "excerpt": self.excerpt,
            "image_url": self.image_url,
            "article_url": self.article_url,
            "categories": self.categories,
            "section": self.section,
        }


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def sleep_briefly() -> None:
    time.sleep(REQUEST_DELAY_SECONDS)


def fetch_response(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, object]] = None,
    allow_redirects: bool = True,
) -> requests.Response:
    response = session.get(
        url,
        params=params,
        timeout=TIMEOUT,
        allow_redirects=allow_redirects,
    )
    response.raise_for_status()
    sleep_briefly()
    return response


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, object]] = None,
) -> Optional[object]:
    try:
        response = fetch_response(session, url, params=params)
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type and not response.text.strip().startswith(("{", "[")):
            return None
        return response.json()
    except Exception:
        return None


def fetch_soup(session: requests.Session, url: str) -> Optional[BeautifulSoup]:
    try:
        response = fetch_response(session, url)
        return BeautifulSoup(response.text, "html.parser")
    except Exception:
        return None


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_to_text(html: Optional[str]) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return clean_text(soup.get_text(" ", strip=True))


def normalize_url(url: Optional[str], base_url: str = BASE_URL) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url, url)


def is_http_url(url: Optional[str]) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


def valid_article_url(url: str) -> bool:
    if not is_http_url(url):
        return False
    parsed = urlparse(url)
    base_netloc = urlparse(BASE_URL).netloc.replace("www.", "")
    netloc = parsed.netloc.replace("www.", "")
    if netloc != base_netloc:
        return False
    if parsed.path in ("", "/"):
        return False
    if parsed.path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf")):
        return False
    lowered = url.lower()
    if any(piece in lowered for piece in ARTICLE_URL_EXCLUDES):
        return False
    return True


def parse_wp_datetime(date_str: Optional[str]) -> str:
    if not date_str:
        return ""
    date_str = date_str.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        pass
    if dt is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%B %d, %Y",
            "%d %B %Y",
        ):
            try:
                dt = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return clean_text(date_str)
    return dt.strftime("%B %d, %Y").replace(" 0", " ")


def first_non_empty(values: Iterable[Optional[str]]) -> str:
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return ""


def get_meta_content(soup: BeautifulSoup, selectors: List[str]) -> str:
    for selector in selectors:
        tag = soup.select_one(selector)
        if not tag:
            continue
        if tag.name == "meta":
            value = tag.get("content")
        else:
            value = tag.get_text(" ", strip=True)
        value = clean_text(value)
        if value:
            return value
    return ""


def extract_image_from_tag(tag: Optional[Tag]) -> str:
    if not tag:
        return ""
    for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-srcset", "srcset"):
        raw = tag.get(attr)
        if not raw:
            continue
        candidate = str(raw).split(",")[0].strip().split()[0].strip()
        candidate = normalize_url(candidate)
        if is_http_url(candidate):
            return candidate
    return ""


def infer_author_from_title(title: str) -> str:
    title = clean_text(title)
    if not title:
        return ""
    match = re.match(
        r"^((?:Dr|Prof|Professor|Mr|Mrs|Ms|Miss)\.?\s+[^:–\-]{2,120})(?:\s*[:\-–])",
        title,
        flags=re.I,
    )
    if match:
        return clean_text(match.group(1))
    return ""


def derive_issue_from_text(text: str) -> Tuple[str, str]:
    text = clean_text(text)
    if not text:
        return "", ""

    lowered = text.lower()
    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
        lowered,
        flags=re.I,
    )
    year_match = re.search(r"\b(20\d{2})\b", text)
    issue_match = re.search(r"\bissue\s*([\w-]+)\b", text, flags=re.I)

    date_label = ""
    if month_match and year_match:
        date_label = f"{MONTH_NAMES[month_match.group(1).lower()]} {year_match.group(1)}"
    elif month_match:
        date_label = MONTH_NAMES[month_match.group(1).lower()]

    issue_label = ""
    if issue_match:
        token = issue_match.group(1).upper()
        issue_label = f"ISSUE {token}"
    elif date_label:
        issue_label = date_label.upper()

    return issue_label, date_label


def append_jsonl(path: Path, record: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rewrite_jsonl(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_output_dir(custom_output_dir: Optional[str]) -> Path:
    if custom_output_dir:
        return Path(custom_output_dir)

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent if script_dir.name.lower() == "scrapers" else Path.cwd()
    return project_root / "outputs" / SOURCE_ID


def get_output_paths(output_dir: Path) -> Dict[str, Path]:
    return {
        "articles": output_dir / f"{SOURCE_ID}_articles_final.jsonl",
        "magazines": output_dir / f"{SOURCE_ID}_magazine_covers_final.jsonl",
        "summary": output_dir / f"{SOURCE_ID}_summary.json",
    }


def save_outputs(output_dir: Path, articles: List[ArticleRecord], magazines: List[Dict[str, object]]) -> None:
    paths = get_output_paths(output_dir)
    article_rows = [article.as_output_dict() for article in articles]
    rewrite_jsonl(paths["articles"], article_rows)
    rewrite_jsonl(paths["magazines"], magazines)

    payload = {
        "site": SOURCE_NAME,
        "source_id": SOURCE_ID,
        "total_articles": len(article_rows),
        "articles_file": str(paths["articles"]),
        "magazine_covers_file": str(paths["magazines"]),
        "magazine_covers_count": len(magazines),
    }
    paths["summary"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_taxonomy_map(session: requests.Session, endpoint: str) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    page = 1
    while True:
        data = fetch_json(session, endpoint, params={"per_page": 100, "page": page})
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if isinstance(item, dict) and "id" in item and "name" in item:
                try:
                    mapping[int(item["id"])] = clean_text(str(item["name"]))
                except Exception:
                    continue
        if len(data) < 100:
            break
        page += 1
    return mapping


def get_featured_media_url(post_obj: Dict[str, object], session: requests.Session) -> str:
    embedded = post_obj.get("_embedded")
    if isinstance(embedded, dict):
        media_items = embedded.get("wp:featuredmedia")
        if isinstance(media_items, list) and media_items:
            item = media_items[0]
            if isinstance(item, dict):
                value = item.get("source_url")
                if isinstance(value, str) and is_http_url(value):
                    return value
    media_id = post_obj.get("featured_media")
    if isinstance(media_id, int) and media_id > 0:
        media_obj = fetch_json(session, f"{API_MEDIA}/{media_id}")
        if isinstance(media_obj, dict):
            value = media_obj.get("source_url")
            if isinstance(value, str) and is_http_url(value):
                return value
    return ""


def infer_section_from_categories(categories: List[str]) -> str:
    normalized = {c.lower() for c in categories}
    if any("featured" in c for c in normalized):
        return "Featured"
    if any("editorial" in c for c in normalized):
        return "Editorial picks"
    return "Latest Articles"


def scrape_homepage_section_map(session: requests.Session) -> Dict[str, str]:
    section_map: Dict[str, str] = {}
    soup = fetch_soup(session, BASE_URL)
    if not soup:
        return section_map

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        heading_text = clean_text(heading.get_text(" ", strip=True))
        if not heading_text:
            continue
        matched_section = ""
        for section_name, pattern in SECTION_PATTERNS.items():
            if pattern.search(heading_text):
                matched_section = section_name
                break
        if not matched_section:
            continue

        parent = heading.parent if isinstance(heading.parent, Tag) else None
        containers: List[Tag] = []
        if parent:
            containers.append(parent)
        sibling = heading.find_next_sibling()
        traversed = 0
        while isinstance(sibling, Tag) and traversed < 3:
            containers.append(sibling)
            sibling = sibling.find_next_sibling()
            traversed += 1

        for container in containers:
            for link in container.select("a[href]"):
                url = normalize_url(link.get("href"))
                if valid_article_url(url):
                    section_map.setdefault(url, matched_section)

    hero_candidates: List[str] = []
    for container in soup.select("article, .post, .elementor-post, .jeg_post, .featured, .hero"):
        for link in container.select("a[href]"):
            url = normalize_url(link.get("href"))
            if valid_article_url(url):
                hero_candidates.append(url)
    for url in hero_candidates[:4]:
        section_map.setdefault(url, "Featured")

    return section_map


def scrape_posts_via_api(
    session: requests.Session,
    section_map: Dict[str, str],
    output_dir: Path,
) -> List[ArticleRecord]:
    articles: List[ArticleRecord] = []
    seen_urls: Set[str] = set()

    category_map = load_taxonomy_map(session, API_CATEGORIES)
    page = 1
    total_pages = None

    while True:
        params = {
            "per_page": 100,
            "page": page,
            "_embed": "author,wp:featuredmedia,wp:term",
            "status": "publish",
        }
        try:
            response = fetch_response(session, API_POSTS, params=params)
            if total_pages is None:
                try:
                    total_pages = int(response.headers.get("X-WP-TotalPages", "0") or 0)
                except ValueError:
                    total_pages = 0
            data = response.json()
        except Exception:
            break

        if not isinstance(data, list) or not data:
            break

        for post in data:
            if not isinstance(post, dict):
                continue
            url = normalize_url(str(post.get("link") or ""))
            if not valid_article_url(url) or url in seen_urls:
                continue

            categories = [
                category_map[cid]
                for cid in post.get("categories", [])
                if isinstance(cid, int) and cid in category_map
            ]
            categories = [c for c in categories if c]

            author = ""
            embedded = post.get("_embedded")
            if isinstance(embedded, dict):
                author_items = embedded.get("author")
                if isinstance(author_items, list) and author_items:
                    item = author_items[0]
                    if isinstance(item, dict):
                        author = clean_text(str(item.get("name") or ""))

            title = html_to_text(post.get("title", {}).get("rendered") if isinstance(post.get("title"), dict) else "")
            excerpt = html_to_text(post.get("excerpt", {}).get("rendered") if isinstance(post.get("excerpt"), dict) else "")
            if not author:
                author = infer_author_from_title(title)

            article = ArticleRecord(
                id=int(post.get("id")) if isinstance(post.get("id"), int) else None,
                title=title,
                author=author,
                date=parse_wp_datetime(str(post.get("date") or "")),
                excerpt=excerpt,
                image_url=get_featured_media_url(post, session),
                article_url=url,
                categories=categories,
                section=section_map.get(url, infer_section_from_categories(categories)),
            )
            articles.append(article)
            seen_urls.add(url)

            if len(articles) % SAVE_EVERY == 0:
                save_outputs(output_dir, articles, [])

        if total_pages and page >= total_pages:
            break
        if len(data) < 100:
            break
        page += 1

    return articles


def discover_category_urls(session: requests.Session) -> List[str]:
    discovered: List[str] = []
    seen: Set[str] = set()

    homepage = fetch_soup(session, BASE_URL)
    if homepage:
        for link in homepage.select("a[href]"):
            href = normalize_url(link.get("href"))
            if "/category/" in href and href not in seen:
                seen.add(href)
                discovered.append(href)

    guessed_slugs = [
        "global-news",
        "endometriosis",
        "endometriosis-specialists-interviews",
        "specialist-information",
        "testimonials",
        "pregnancy-and-endometriosis",
        "adenomyosis",
        "research",
        "awareness",
        "health-and-wellbeing",
        "nutrition",
        "mental-health",
        "sport",
        "endometriosis-advocacy",
        "teenagers-endometriosis",
    ]
    for slug in guessed_slugs:
        href = urljoin(BASE_URL, f"category/{slug}/")
        if href not in seen:
            seen.add(href)
            discovered.append(href)

    return discovered


def discover_article_urls_on_page(soup: BeautifulSoup) -> List[str]:
    candidates: List[str] = []

    selector_groups = [
        "article a[href]",
        ".post a[href]",
        ".entry-title a[href]",
        "h1 a[href], h2 a[href], h3 a[href], h4 a[href]",
        "main a[href]",
    ]
    for selector in selector_groups:
        for link in soup.select(selector):
            href = normalize_url(link.get("href"))
            if valid_article_url(href):
                candidates.append(href)

    return list(OrderedDict.fromkeys(candidates))


def extract_article_date_from_soup(soup: BeautifulSoup) -> str:
    for selector in [
        "time[datetime]",
        "time",
        ".entry-date",
        ".post-date",
        ".published",
        "article .date",
        ".posted-on",
    ]:
        tag = soup.select_one(selector)
        if not tag:
            continue
        raw = tag.get("datetime") or tag.get_text(" ", strip=True)
        date_text = parse_wp_datetime(raw)
        if date_text:
            return date_text
    return ""


def extract_categories_from_soup(soup: BeautifulSoup) -> List[str]:
    categories: List[str] = []
    selectors = [
        'a[rel="category tag"]',
        ".cat-links a",
        ".post-categories a",
        'a[href*="/category/"]',
    ]
    for selector in selectors:
        for tag in soup.select(selector):
            value = clean_text(tag.get_text(" ", strip=True))
            if value and value.lower() not in {"home", "read more"}:
                categories.append(value)
    return list(OrderedDict.fromkeys(categories))


def extract_article(session: requests.Session, url: str, section_map: Dict[str, str]) -> Optional[ArticleRecord]:
    soup = fetch_soup(session, url)
    if not soup:
        return None

    title = first_non_empty(
        [
            tag.get_text(" ", strip=True)
            for tag in [
                soup.select_one("h1.entry-title"),
                soup.select_one("article h1"),
                soup.select_one("main h1"),
                soup.select_one("h1"),
            ]
            if tag
        ]
    )
    if not title:
        return None

    author = first_non_empty(
        [
            get_meta_content(soup, ['meta[name="author"]', 'meta[property="article:author"]']),
            soup.select_one('[rel="author"]').get_text(" ", strip=True) if soup.select_one('[rel="author"]') else "",
            soup.select_one(".author a").get_text(" ", strip=True) if soup.select_one(".author a") else "",
            soup.select_one(".author").get_text(" ", strip=True) if soup.select_one(".author") else "",
            soup.select_one(".byline a").get_text(" ", strip=True) if soup.select_one(".byline a") else "",
            soup.select_one(".byline").get_text(" ", strip=True) if soup.select_one(".byline") else "",
        ]
    )
    if not author:
        author = infer_author_from_title(title)

    date_text = extract_article_date_from_soup(soup)

    excerpt = first_non_empty(
        [
            get_meta_content(soup, ['meta[name="description"]', 'meta[property="og:description"]']),
            soup.select_one("article p").get_text(" ", strip=True) if soup.select_one("article p") else "",
            soup.select_one("main p").get_text(" ", strip=True) if soup.select_one("main p") else "",
        ]
    )

    image_url = first_non_empty(
        [
            get_meta_content(soup, ['meta[property="og:image"]']),
            extract_image_from_tag(soup.select_one("article img")),
            extract_image_from_tag(soup.select_one("main img")),
        ]
    )
    image_url = normalize_url(image_url)
    if not is_http_url(image_url):
        image_url = ""

    categories = extract_categories_from_soup(soup)
    section = section_map.get(url, infer_section_from_categories(categories))

    return ArticleRecord(
        id=None,
        title=title,
        author=author,
        date=date_text,
        excerpt=excerpt,
        image_url=image_url,
        article_url=url,
        categories=categories,
        section=section,
    )


def scrape_posts_via_html(
    session: requests.Session,
    section_map: Dict[str, str],
    output_dir: Path,
) -> List[ArticleRecord]:
    articles: List[ArticleRecord] = []
    seen_urls: Set[str] = set()

    category_urls = discover_category_urls(session)
    for category_url in category_urls:
        page_number = 1
        consecutive_empty_pages = 0

        while True:
            paged_url = category_url if page_number == 1 else urljoin(category_url, f"page/{page_number}/")
            soup = fetch_soup(session, paged_url)
            if not soup:
                break

            page_urls = discover_article_urls_on_page(soup)
            new_urls = [url for url in page_urls if url not in seen_urls]
            if not new_urls:
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    break
            else:
                consecutive_empty_pages = 0

            for url in new_urls:
                article = extract_article(session, url, section_map)
                if article is None:
                    continue
                if article.article_url in seen_urls:
                    continue
                seen_urls.add(article.article_url)
                article.id = len(articles) + 1
                articles.append(article)
                if len(articles) % SAVE_EVERY == 0:
                    save_outputs(output_dir, articles, [])

            next_link = soup.select_one('a.next, a[rel="next"], .nav-next a, .pagination .next a')
            if page_number > 1 and not next_link and not new_urls:
                break
            page_number += 1

    return articles


def text_near_element(tag: Tag, limit: int = 250) -> str:
    snippets: List[str] = []
    current: Optional[Tag] = tag
    steps = 0
    while current is not None and steps < 3:
        snippet = clean_text(current.get_text(" ", strip=True))
        if snippet:
            snippets.append(snippet)
        current = current.parent if isinstance(current.parent, Tag) else None
        steps += 1
    combined = " | ".join(snippets)
    return combined[:limit]


def extract_topics_from_text(text: str) -> List[str]:
    text = clean_text(text)
    if not text:
        return []
    pieces = re.split(r"\s*[|,;•·]+\s*", text)
    topics: List[str] = []
    for piece in pieces:
        cleaned = clean_text(piece)
        if not cleaned:
            continue
        if cleaned.lower().startswith(("download", "subscribe", "read more", "copyright", "company")):
            continue
        if cleaned not in topics:
            topics.append(cleaned)
    return topics[:8]


def derive_issue_from_url(url: str) -> Tuple[str, str]:
    filename = Path(unquote(urlparse(url).path)).name
    text = filename.replace("-", " ").replace("_", " ").replace(".pdf", " ")
    return derive_issue_from_text(text)


def scrape_magazine_covers(session: requests.Session) -> List[Dict[str, object]]:
    soup = fetch_soup(session, MAGAZINE_URL)
    if not soup:
        return []

    results: List[Dict[str, object]] = []
    seen_pdf_urls: Set[str] = set()

    for link in soup.select('a[href$=".pdf"], a[href*=".pdf?"]'):
        pdf_url = normalize_url(link.get("href"))
        if not is_http_url(pdf_url) or pdf_url in seen_pdf_urls:
            continue
        seen_pdf_urls.add(pdf_url)

        nearby_text = text_near_element(link)
        issue, date_label = derive_issue_from_text(nearby_text)
        if not issue:
            issue, date_label = derive_issue_from_url(pdf_url)

        image_url = ""
        nearby_image = None
        candidate_parent = link.parent if isinstance(link.parent, Tag) else None
        candidate_grandparent = candidate_parent.parent if isinstance(candidate_parent, Tag) and isinstance(candidate_parent.parent, Tag) else None
        for candidate in [candidate_parent, candidate_grandparent]:
            if isinstance(candidate, Tag):
                nearby_image = candidate.find("img")
                if nearby_image:
                    break
        if nearby_image:
            image_url = extract_image_from_tag(nearby_image)
        if not image_url:
            for img in soup.select("img"):
                parent = img.parent
                if isinstance(parent, Tag) and parent.get("href") == link.get("href"):
                    image_url = extract_image_from_tag(img)
                    if image_url:
                        break

        topics = extract_topics_from_text(nearby_text)
        if not topics and nearby_image and nearby_image.get("alt"):
            topics = extract_topics_from_text(str(nearby_image.get("alt")))

        results.append(
            {
                "source_id": SOURCE_ID,
                "source_mode": SOURCE_MODE,
                "source_name": SOURCE_NAME,
                "source_country": SOURCE_COUNTRY,
                "source_language": SOURCE_LANGUAGE,
                "issue": issue,
                "date": date_label,
                "cover_image_url": image_url if is_http_url(image_url) else "",
                "featured_topics": topics,
                "pdf_url": pdf_url,
            }
        )

    deduped: List[Dict[str, object]] = []
    seen_keys: Set[Tuple[str, str, str]] = set()
    for item in results:
        key = (
            str(item.get("issue") or ""),
            str(item.get("date") or ""),
            str(item.get("cover_image_url") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)

    return deduped


def merge_articles(api_articles: List[ArticleRecord], html_articles: List[ArticleRecord]) -> List[ArticleRecord]:
    merged: "OrderedDict[str, ArticleRecord]" = OrderedDict()

    for article in api_articles + html_articles:
        key = article.article_url
        if key not in merged:
            merged[key] = article
            continue

        existing = merged[key]
        if not existing.author and article.author:
            existing.author = article.author
        if not existing.date and article.date:
            existing.date = article.date
        if not existing.excerpt and article.excerpt:
            existing.excerpt = article.excerpt
        if not existing.image_url and article.image_url:
            existing.image_url = article.image_url
        if not existing.categories and article.categories:
            existing.categories = article.categories
        if existing.section in ("Uncategorized", "Latest Articles") and article.section:
            existing.section = article.section

    final_articles = list(merged.values())
    for idx, article in enumerate(final_articles, start=1):
        article.id = idx
    return final_articles


def run_scraper(output_dir: Path) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = build_session()
    section_map = scrape_homepage_section_map(session)

    api_articles = scrape_posts_via_api(session, section_map, output_dir)
    if not api_articles:
        html_articles = scrape_posts_via_html(session, section_map, output_dir)
    else:
        html_articles = scrape_posts_via_html(session, section_map, output_dir)

    magazines = scrape_magazine_covers(session)
    articles = merge_articles(api_articles, html_articles)
    save_outputs(output_dir, articles, magazines)

    return {
        "site": SOURCE_NAME,
        "source_id": SOURCE_ID,
        "total_articles": len(articles),
        "articles_file": str(get_output_paths(output_dir)["articles"]),
        "magazine_covers_file": str(get_output_paths(output_dir)["magazines"]),
        "magazine_covers_count": len(magazines),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Endo Daily News into outputs/SRC023 JSONL files")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: project_root/outputs/SRC023)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    output_dir = get_output_dir(args.output_dir)

    try:
        payload = run_scraper(output_dir)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Scraper failed: {exc}", file=sys.stderr)
        return 1

    print(f"Saved {payload['total_articles']} articles to {payload['articles_file']}")
    print(f"Saved {payload['magazine_covers_count']} magazine cover records to {payload['magazine_covers_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
