#!/usr/bin/env python3
"""
Site-specific scraper for https://endometriosisaustralia.org/
- requests + BeautifulSoup only
- no Selenium
- resume mode from existing JSONL output
- fixed output file names
- one JSON line per article
"""

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class EndoAustraliaScraper:
    def __init__(self, config_path: str):
        self.config = self._load_json(config_path)
        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.start_url = self.config["start_url"]
        self.listing_page_template = self.config["listing_page_template"]
        self.start_page = int(self.config.get("start_page", 1))
        self.max_pages = int(self.config.get("max_pages", 200))
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.5))
        self.request_timeout = int(self.config.get("request_timeout", 30))
        self.selectors = self.config.get("selectors", {})
        self.remove_selectors = self.config.get("remove_selectors", [])

        self.output_file = Path(self.config["output_file"])
        self.error_file = Path(self.config["error_file"])
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.error_file.parent.mkdir(parents=True, exist_ok=True)

        self.session = self._build_session()
        self.seen_article_ids = self._load_seen_article_ids()

        self.stats = {
            "pages_checked": 0,
            "page_articles": 0,
            "new_articles": 0,
            "skipped_existing": 0,
            "errors": 0,
        }

    @staticmethod
    def _load_json(path: str) -> Dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _load_seen_article_ids(self) -> Set[str]:
        seen: Set[str] = set()
        if not self.output_file.exists():
            return seen

        with open(self.output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                article_id = row.get("article_id") or row.get("thread_id")
                if article_id:
                    seen.add(str(article_id))
        return seen

    def append_jsonl(self, path: Path, item: Dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def log_error(self, payload: Dict) -> None:
        payload = dict(payload)
        payload["logged_at"] = datetime.utcnow().isoformat() + "Z"
        self.append_jsonl(self.error_file, payload)
        self.stats["errors"] += 1

    def fetch(self, url: str) -> Optional[str]:
        try:
            time.sleep(self.sleep_seconds)
            resp = self.session.get(url, timeout=self.request_timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            self.log_error({"url": url, "error": str(exc), "stage": "fetch"})
            return None

    @staticmethod
    def clean_text(text: Optional[str]) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = text.replace("\r", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def clean_multiline_text(text: Optional[str]) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = text.replace("\r", "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    @staticmethod
    def article_id_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1] if path else ""
        return slug or url

    def parse_date_to_iso(self, text: str) -> str:
        if not text:
            return ""

        text = self.clean_text(text)
        months = {
            "january": "01", "jan": "01",
            "february": "02", "feb": "02",
            "march": "03", "mar": "03",
            "april": "04", "apr": "04",
            "may": "05",
            "june": "06", "jun": "06",
            "july": "07", "jul": "07",
            "august": "08", "aug": "08",
            "september": "09", "sep": "09", "sept": "09",
            "october": "10", "oct": "10",
            "november": "11", "nov": "11",
            "december": "12", "dec": "12",
        }

        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", text)
        if m:
            month = months.get(m.group(1).lower(), "")
            if month:
                return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"

        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b", text)
        if m:
            month = months.get(m.group(2).lower(), "")
            if month:
                return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"

        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            return m.group(0)

        return ""

    def get_listing_url(self, page: int) -> str:
        if page <= 1:
            return self.start_url
        return self.listing_page_template.format(page=page)

    def extract_listing_urls(self, soup: BeautifulSoup) -> List[str]:
        urls: List[str] = []
        seen: Set[str] = set()
        selector = self.selectors.get("article_cards", "h2 a")

        for link in soup.select(selector):
            href = link.get("href")
            if not href:
                continue
            full_url = urljoin(self.base_url + "/", href)
            parsed = urlparse(full_url)
            if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if "/blog/page/" in parsed.path:
                continue
            if parsed.path.rstrip("/") == "/blog":
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            urls.append(full_url)

        return urls

    def clean_article_container(self, article_soup: BeautifulSoup) -> BeautifulSoup:
        for selector in self.remove_selectors:
            for node in article_soup.select(selector):
                node.decompose()

        for comment in article_soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        return article_soup

    def extract_author_date_category(self, soup: BeautifulSoup) -> Tuple[str, str, str, List[str]]:
        author = ""
        date_text = ""
        category_name = ""
        category_list: List[str] = []

        for link in soup.select(self.selectors.get("author_links", "a[rel*='author']")):
            value = self.clean_text(link.get_text(" ", strip=True))
            if value:
                author = re.sub(r"^by\s+", "", value, flags=re.I).strip()
                break

        for link in soup.select(self.selectors.get("category_links", ".cat-links a")):
            value = self.clean_text(link.get_text(" ", strip=True))
            if value and value not in category_list:
                category_list.append(value)
        if category_list:
            category_name = category_list[0]

        meta_selector = self.selectors.get("meta", ".post-meta")
        meta_elem = soup.select_one(meta_selector)
        if meta_elem:
            meta_text = self.clean_text(meta_elem.get_text(" ", strip=True))
            if not author:
                m_author = re.search(r"\bby\s+(.+?)\s+\|", meta_text, flags=re.I)
                if m_author:
                    author = self.clean_text(m_author.group(1))
            if not date_text:
                m_date = re.search(
                    r"\b([A-Za-z]+\s+\d{1,2},\s*\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\b",
                    meta_text,
                )
                if m_date:
                    date_text = self.clean_text(m_date.group(1))
            if not category_name and "|" in meta_text:
                parts = [self.clean_text(x) for x in meta_text.split("|") if self.clean_text(x)]
                if parts:
                    tail = parts[-1]
                    if not re.search(r"\d{4}", tail) and not re.search(r"^by\b", tail, flags=re.I):
                        category_name = tail
                        if tail not in category_list:
                            category_list.append(tail)

        time_elem = soup.select_one("time")
        if time_elem:
            dt = self.clean_text(time_elem.get("datetime") or time_elem.get_text(" ", strip=True))
            if dt and not date_text:
                date_text = dt

        return author, date_text, category_name, category_list

    def extract_body(self, soup: BeautifulSoup) -> str:
        candidates = [
            ".entry-content",
            ".post-content",
            ".et_pb_post_content",
            "article .et_pb_text_inner",
            "article",
            "main article",
            ".et_pb_post",
        ]

        container = None
        for selector in candidates:
            container = soup.select_one(selector)
            if container:
                break
        if not container:
            return ""

        cleaned = self.clean_article_container(BeautifulSoup(str(container), "lxml"))

        for bad in cleaned.find_all(["h1", "title"]):
            bad.decompose()

        text = cleaned.get_text("\n", strip=True)
        text = self.clean_multiline_text(text)

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        filtered: List[str] = []
        skip_patterns = [
            r"^Search$",
            r"^read more blogs",
            r"^Donate",
            r"^Donate now$",
            r"^Click Here$",
            r"^Home »",
            r"^Share your endometriosis story$",
        ]
        for line in lines:
            if any(re.search(pat, line, flags=re.I) for pat in skip_patterns):
                continue
            filtered.append(line)

        return self.clean_multiline_text("\n".join(filtered))

    def extract_article(self, article_url: str) -> Optional[Dict]:
        html = self.fetch(article_url)
        if not html:
            return None

        soup = BeautifulSoup(html, "lxml")

        title_elem = soup.select_one(self.selectors.get("title", "h1"))
        title = self.clean_text(title_elem.get_text(" ", strip=True) if title_elem else "")
        if not title:
            self.log_error({
                "url": article_url,
                "stage": "extract_article",
                "error": "missing_title"
            })
            return None

        author, date_text, category_name, category_list = self.extract_author_date_category(soup)
        body = self.extract_body(soup)
        if not body:
            self.log_error({
                "url": article_url,
                "stage": "extract_article",
                "error": "empty_body",
                "title": title,
            })

        article_id = self.article_id_from_url(article_url)
        date_iso = self.parse_date_to_iso(date_text)
        excerpt = self.clean_text(body[:240])
        if len(body) > 240:
            excerpt += "..."

        return {
            "source_id": self.source_id,
            "source_mode": "association_article",
            "source_name": self.config.get("source_name", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "country": self.config.get("country", ""),
            "language": self.config.get("language", "en"),
            "listing_category": "blog",
            "article_id": article_id,
            "thread_id": article_id,
            "thread_url_id": article_id,
            "article_url": article_url,
            "thread_url": article_url,
            "title": title,
            "thread_title": title,
            "thread_title_detail": category_name,
            "author": author,
            "thread_starter": author,
            "thread_starter_id": author,
            "publish_date": date_text,
            "date_iso": date_iso,
            "opening_post_date": date_text,
            "opening_post_body": body,
            "category_name": category_name,
            "category_slug": category_name.lower().replace(" ", "-") if category_name else "",
            "tags": category_list,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "views_count": None,
            "replies_count": 0,
            "posts_count": 1,
            "comments_count": 0,
            "likes_total": 0,
            "last_message_date": date_text,
            "last_message_author": author,
            "last_message_id": article_id,
            "thread_pages_count": 1,
            "last_page": 1,
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "post": {
                "author": author,
                "user_id": author,
                "native_user_id": author,
                "date": date_text,
                "date_iso": date_iso,
                "body": body,
                "likes_count": 0,
                "dislikes_count": 0,
                "thread_id": article_id,
                "message_id": article_id,
                "native_post_id": article_id,
                "anchor_id": article_id,
                "post_number": 1,
                "type": "post",
                "is_original_post": True,
                "post_id": article_id,
                "comment_id": "",
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": article_url,
            },
            "replies": []
        }

    def scrape(self) -> None:
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.start_url}")
        print(f"💾 Output: {self.output_file}")
        print(f"📂 Existing articles in output: {len(self.seen_article_ids)}")
        print("=" * 80)

        consecutive_empty_pages = 0

        for page in range(self.start_page, self.max_pages + 1):
            listing_url = self.get_listing_url(page)
            print(f"listing_page_number={page} url={listing_url}")

            html = self.fetch(listing_url)
            if not html:
                print("page_threads=0 new_threads=0 skipped_existing=0")
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    break
                continue

            soup = BeautifulSoup(html, "lxml")
            page_urls = self.extract_listing_urls(soup)
            page_ids = [self.article_id_from_url(u) for u in page_urls]
            unique_page_ids = []
            seen_local: Set[str] = set()
            for aid in page_ids:
                if aid in seen_local:
                    continue
                seen_local.add(aid)
                unique_page_ids.append(aid)

            new_urls = []
            seen_page_url_ids: Set[str] = set()
            for url in page_urls:
                article_id = self.article_id_from_url(url)
                if article_id in seen_page_url_ids:
                    continue
                seen_page_url_ids.add(article_id)
                if article_id in self.seen_article_ids:
                    continue
                new_urls.append(url)

            page_threads = len(seen_page_url_ids)
            new_threads = len(new_urls)
            skipped_existing = page_threads - new_threads
            self.stats["pages_checked"] += 1
            self.stats["page_articles"] += page_threads
            self.stats["skipped_existing"] += max(skipped_existing, 0)

            print(
                f"page_threads={page_threads} "
                f"new_threads={new_threads} "
                f"skipped_existing={skipped_existing}"
            )

            if page_threads == 0:
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    break
                continue

            consecutive_empty_pages = 0

            for idx, article_url in enumerate(new_urls, start=1):
                article_id = self.article_id_from_url(article_url)
                print(f"  [{idx}/{len(new_urls)}] {article_id} -> {article_url}")

                try:
                    item = self.extract_article(article_url)
                    if not item:
                        continue
                    self.append_jsonl(self.output_file, item)
                    self.seen_article_ids.add(article_id)
                    self.stats["new_articles"] += 1
                    print("     messages_comments_scraped=1")
                except Exception as exc:
                    self.log_error(
                        {
                            "url": article_url,
                            "stage": "thread_loop",
                            "error": str(exc),
                            "article_id": article_id,
                        }
                    )

            if new_threads == 0 and page >= 3:
                break

        print("=" * 80)
        print("Done")
        print(f"pages_checked={self.stats['pages_checked']}")
        print(f"page_threads_total={self.stats['page_articles']}")
        print(f"new_threads_total={self.stats['new_articles']}")
        print(f"skipped_existing_total={self.stats['skipped_existing']}")
        print(f"errors_total={self.stats['errors']}")
        print(f"output_file={self.output_file}")
        print(f"error_file={self.error_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Endometriosis Australia blog")
    parser.add_argument(
        "--config",
        default="configs/SRC001.json",
        help="Path to config JSON file"
    )
    args = parser.parse_args()

    scraper = EndoAustraliaScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()
