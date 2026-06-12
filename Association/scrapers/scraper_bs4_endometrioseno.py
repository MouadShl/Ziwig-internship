#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://endometriose.no"
LISTING_URL = f"{BASE_URL}/aktuelt/"
SOURCE_ID = "SRC021"
SOURCE_NAME = "Endometrioseforeningen"
SOURCE_COUNTRY = "Norway"
SOURCE_LANGUAGE = "no"
USER_ID = "endometrioseforeningen"
SLEEP_SECONDS = 1.0
TIMEOUT = 30
MAX_PAGES = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DATE_DMY_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2,4})\b")


class Src021Scraper:
    def __init__(self, sleep_seconds: float = SLEEP_SECONDS) -> None:
        self.sleep_seconds = sleep_seconds
        self.session = self._build_session()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.output_dir = Path("outputs") / SOURCE_ID
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.articles_file = self.output_dir / f"{SOURCE_ID}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{SOURCE_ID}_errors_final.jsonl"
        self.existing_ids = self._load_existing_ids()
        self.seen_listing_urls: set[str] = set()
        self.stats = {
            "listing_pages_fetched": 0,
            "listing_posts_found": 0,
            "articles_saved": 0,
            "articles_skipped_existing": 0,
            "errors": 0,
        }

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.7",
                "Referer": LISTING_URL,
            }
        )
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _sleep(self) -> None:
        time.sleep(self.sleep_seconds)

    def _load_existing_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.articles_file.exists():
            return ids
        with open(self.articles_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                article_id = row.get("article_id")
                if article_id:
                    ids.add(article_id)
        return ids

    def log_error(self, payload: dict) -> None:
        self.stats["errors"] += 1
        with open(self.errors_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def write_row(self, payload: dict) -> None:
        with open(self.articles_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def fetch_text(self, url: str) -> str | None:
        self._sleep()
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            self.log_error({"source_id": SOURCE_ID, "url": url, "error": str(exc)})
            self.logger.warning("GET failed: %s -> %s", url, exc)
            return None

    @staticmethod
    def clean_text(text: str | None) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ").replace("\ufeff", "")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def normalize_url(url: str, base: str = BASE_URL) -> str:
        if not url:
            return ""
        full = urljoin(base, url)
        full, _ = urldefrag(full)
        parsed = urlparse(full)
        if parsed.scheme not in {"http", "https"}:
            return ""
        return full

    @staticmethod
    def slug_from_url(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        slug = path.split("/")[-1] if path else "home"
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug)
        return slug.strip("-") or "home"

    @staticmethod
    def parse_dmy_to_iso(text: str) -> str:
        m = DATE_DMY_RE.search(text or "")
        if not m:
            return ""
        dd, mm, yy = m.groups()
        year = int(yy)
        if year < 100:
            year += 2000
        return f"{year:04d}-{int(mm):02d}-{int(dd):02d}"

    @staticmethod
    def clean_container(node) -> BeautifulSoup:
        soup = BeautifulSoup(str(node), "lxml")
        remove_selectors = [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
            "form",
            "nav",
            "header",
            "footer",
            ".share",
            ".social",
            ".newsletter",
            ".related",
            ".comments",
            ".comment",
            ".back-to-top-wrapper",
            ".anchor-menu",
            ".overlay-menu",
            ".header",
            ".footer",
        ]
        for sel in remove_selectors:
            try:
                for el in soup.select(sel):
                    el.decompose()
            except Exception:
                continue
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        return soup

    def extract_listing_cards(self, html_text: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("article.post-card")
        results: list[dict] = []
        for card in cards:
            a = card.select_one("a.wrapper-link[href]") or card.select_one("a[href]")
            if not a:
                continue
            article_url = self.normalize_url(a.get("href", ""), page_url)
            if not article_url:
                continue
            parsed = urlparse(article_url)
            if parsed.netloc != urlparse(BASE_URL).netloc:
                continue
            if parsed.path.rstrip("/") == "/aktuelt":
                continue
            if article_url in self.seen_listing_urls:
                continue

            title = self.clean_text((card.select_one("h2") or a).get_text(" ", strip=True))
            date = self.clean_text((card.select_one(".date") or card.select_one(".time")).get_text(" ", strip=True) if (card.select_one(".date") or card.select_one(".time")) else "")
            category = self.clean_text(card.select_one(".cat").get_text(" ", strip=True) if card.select_one(".cat") else "")
            image_url = ""
            img = card.select_one("img")
            if img:
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
                if not src and img.get("srcset"):
                    src = img.get("srcset", "").split(",")[0].split()[0]
                image_url = self.normalize_url(src, page_url)
            teaser = self.clean_text(card.select_one("p").get_text(" ", strip=True) if card.select_one("p") else "")

            self.seen_listing_urls.add(article_url)
            results.append(
                {
                    "article_url": article_url,
                    "listing_title": title,
                    "listing_date": date,
                    "listing_date_iso": self.parse_dmy_to_iso(date),
                    "listing_category": category,
                    "featured_image": image_url,
                    "listing_excerpt": teaser,
                }
            )
        return results

    def read_listing_source(self, listing_html_path: str | None) -> list[dict]:
        items: list[dict] = []
        if listing_html_path:
            path = Path(listing_html_path)
            text = path.read_text(encoding="utf-8")
            batch = self.extract_listing_cards(text, LISTING_URL)
            self.stats["listing_pages_fetched"] += 1
            items.extend(batch)
            self.logger.info("Loaded %s listing posts from local HTML", len(batch))

        empty_streak = 0
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page == 1 else f"{LISTING_URL}page/{page}/"
            html_text = self.fetch_text(url)
            if html_text is None:
                break
            self.stats["listing_pages_fetched"] += 1
            batch = self.extract_listing_cards(html_text, url)
            if batch:
                items.extend(batch)
                empty_streak = 0
            else:
                empty_streak += 1
            if empty_streak >= 2:
                break
        self.stats["listing_posts_found"] = len(items)
        return items

    def extract_article(self, listing_row: dict) -> dict | None:
        url = listing_row["article_url"]
        html_text = self.fetch_text(url)
        if html_text is None:
            return None
        soup = BeautifulSoup(html_text, "lxml")

        title = ""
        for sel in ["main h1", "article h1", "h1"]:
            el = soup.select_one(sel)
            if el:
                title = self.clean_text(el.get_text(" ", strip=True))
                break
        if not title:
            title = listing_row.get("listing_title", "")

        date_text = ""
        for sel in ["time", ".date", ".post-date", ".published", ".time"]:
            el = soup.select_one(sel)
            if el:
                date_text = self.clean_text(el.get_text(" ", strip=True) or el.get("datetime", ""))
                if date_text:
                    break
        if not date_text:
            date_text = listing_row.get("listing_date", "")
        date_iso = self.parse_dmy_to_iso(date_text)
        if not date_iso:
            dt = soup.select_one("time")
            if dt and dt.get("datetime"):
                m = re.match(r"(\d{4}-\d{2}-\d{2})", dt.get("datetime", ""))
                if m:
                    date_iso = m.group(1)

        category = ""
        for sel in [".cat", ".categories a", ".breadcrumb a", ".post-categories a"]:
            els = soup.select(sel)
            for el in els:
                txt = self.clean_text(el.get_text(" ", strip=True))
                if txt and txt.lower() not in {"hjem", "aktuelt"}:
                    category = txt
                    break
            if category:
                break
        if not category:
            category = listing_row.get("listing_category", "")

        container = None
        for sel in [
            "main article",
            "article",
            ".the-content",
            ".entry-content",
            ".content",
            ".post-content",
            "main",
        ]:
            el = soup.select_one(sel)
            if el:
                container = el
                break
        if container is None:
            container = soup
        cleaned = self.clean_container(container)
        body = self.clean_text(cleaned.get_text("\n", strip=True))
        excerpt = body[:200] + "..." if len(body) > 200 else body
        word_count = len(body.split()) if body else 0

        featured_image = listing_row.get("featured_image", "")
        if not featured_image:
            og = soup.select_one('meta[property="og:image"]')
            if og and og.get("content"):
                featured_image = self.normalize_url(og.get("content", ""), url)

        attachment_url = ""
        attachment_type = ""
        for a in cleaned.select("a[href]"):
            href = self.normalize_url(a.get("href", ""), url)
            if href.lower().endswith(".pdf"):
                attachment_url = href
                attachment_type = "pdf"
                break

        article_id = self.slug_from_url(url)
        return {
            "source_id": SOURCE_ID,
            "source_mode": "association",
            "source_name": SOURCE_NAME,
            "source_country": SOURCE_COUNTRY,
            "source_language": SOURCE_LANGUAGE,
            "source_type": "news_blog",
            "article_id": article_id,
            "article_url": url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": word_count,
            "author": SOURCE_NAME,
            "author_type": "organization",
            "user_id": USER_ID,
            "category": category,
            "tags": [],
            "publish_date": date_text,
            "date_iso": date_iso,
            "has_attachment": bool(attachment_url),
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
            "featured_image": featured_image,
            "listing_title": listing_row.get("listing_title", ""),
            "listing_date": listing_row.get("listing_date", ""),
            "listing_category": listing_row.get("listing_category", ""),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    def run(self, listing_html_path: str | None = None) -> None:
        self.logger.info("Starting %s scraper", SOURCE_ID)
        self.logger.info("Listing URL: %s", LISTING_URL)
        self.logger.info("Output: %s", self.articles_file)

        listings = self.read_listing_source(listing_html_path)
        self.logger.info("Discovered %s unique listing posts", len(listings))

        for row in listings:
            article_id = self.slug_from_url(row["article_url"])
            if article_id in self.existing_ids:
                self.stats["articles_skipped_existing"] += 1
                continue
            try:
                item = self.extract_article(row)
                if item:
                    self.write_row(item)
                    self.existing_ids.add(article_id)
                    self.stats["articles_saved"] += 1
            except Exception as exc:
                self.log_error({"source_id": SOURCE_ID, "article_url": row["article_url"], "error": str(exc)})

        self.logger.info("Done")
        for key, value in self.stats.items():
            self.logger.info("%s=%s", key, value)
        self.logger.info("articles_file=%s", self.articles_file)
        self.logger.info("errors_file=%s", self.errors_file)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape endometriose.no/aktuelt into outputs/SRC021 JSONL")
    parser.add_argument("--listing-html", help="Optional local HTML file containing all listing cards/links")
    parser.add_argument("--sleep", type=float, default=SLEEP_SECONDS)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    scraper = Src021Scraper(sleep_seconds=args.sleep)
    scraper.run(listing_html_path=args.listing_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
