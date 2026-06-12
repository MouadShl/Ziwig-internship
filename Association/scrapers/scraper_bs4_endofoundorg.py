#!/usr/bin/env python3
"""
Site-specific scraper for https://www.endofound.org/endometriosis-stories
- requests + BeautifulSoup only
- no Selenium
- resume mode from existing JSONL output and resume file
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


class EndoFoundStoriesScraper:
    def __init__(self, config_path: str):
        self.config = self._load_json(config_path)
        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.start_url = self.config["start_url"]
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.2))
        self.request_timeout = int(self.config.get("request_timeout", 30))
        self.selectors = self.config.get("selectors", {})
        self.remove_selectors = self.config.get("remove_selectors", [])
        self.listing_link_allow = self.config.get("listing_link_allow", ["/"])
        self.listing_link_deny = self.config.get("listing_link_deny", [])
        self.body_noise_prefixes = self.config.get("body_noise_prefixes", [])
        self.end_markers = self.config.get("end_markers", [])
        self.fallback_category = self.config.get("listing_fallback_category", "")

        self.output_file = Path(self.config["output_file"])
        self.error_file = Path(self.config["error_file"])
        self.resume_file = Path(self.config["resume_file"])
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.error_file.parent.mkdir(parents=True, exist_ok=True)

        self.session = self._build_session()
        self.seen_article_ids: Set[str] = set()
        self._load_resume_file()
        self._load_existing_output_ids()

        self.stats = {
            "listing_pages_checked": 0,
            "page_threads": 0,
            "new_threads": 0,
            "skipped_existing": 0,
            "articles_scraped": 0,
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
                "Accept-Language": "en-US,en;q=0.9",
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

    def _load_resume_file(self) -> None:
        if not self.resume_file.exists():
            return
        try:
            with open(self.resume_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("seen_ids", []):
                if item:
                    self.seen_article_ids.add(str(item))
            print(f"📂 Resume file loaded: {len(self.seen_article_ids)} known article_ids")
        except Exception as exc:
            print(f"⚠️ Could not read resume file: {exc}")

    def _load_existing_output_ids(self) -> None:
        if not self.output_file.exists():
            return
        loaded = 0
        with open(self.output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                article_id = row.get("article_id")
                if article_id:
                    self.seen_article_ids.add(str(article_id))
                    loaded += 1
        if loaded:
            print(f"📂 Existing output loaded: {loaded} article_ids")

    def _save_resume(self) -> None:
        payload = {
            "source_id": self.source_id,
            "seen_ids": sorted(self.seen_article_ids),
            "stats": self.stats,
            "last_update": datetime.now().isoformat(),
        }
        with open(self.resume_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def append_jsonl(path: Path, data: Dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def log_error(self, **kwargs) -> None:
        kwargs["timestamp"] = datetime.now().isoformat()
        self.append_jsonl(self.error_file, kwargs)
        self.stats["errors"] += 1

    def fetch(self, url: str) -> Optional[str]:
        try:
            time.sleep(self.sleep_seconds)
            resp = self.session.get(url, timeout=self.request_timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            print(f"    ❌ fetch failed: {url} -> {exc}")
            self.log_error(url=url, error=str(exc), stage="fetch")
            return None

    @staticmethod
    def clean_text(text: Optional[str]) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = text.replace("\r", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def normalize_ws(text: Optional[str]) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def article_id_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        slug = path.split("/")[-1] if path else ""
        slug = re.sub(r"\.(html|php|aspx?)$", "", slug, flags=re.I)
        return slug or url

    def is_valid_article_url(self, full_url: str) -> bool:
        parsed = urlparse(full_url)
        base_netloc = urlparse(self.base_url).netloc
        if parsed.netloc and parsed.netloc != base_netloc:
            return False

        path = parsed.path or ""
        if not path or path == "/":
            return False

        if not any(part in path for part in self.listing_link_allow):
            return False

        for deny in self.listing_link_deny:
            if deny and deny in full_url:
                return False

        article_id = self.article_id_from_url(full_url)
        if not article_id:
            return False
        if article_id in {"endometriosis-stories", "share-your-story", "login", "register"}:
            return False

        return True

    def extract_listing_urls(self, soup: BeautifulSoup) -> List[str]:
        selector = self.selectors.get("article_cards", "h3 a, h2 a, a[href^='/']")
        urls: List[str] = []
        seen: Set[str] = set()

        for link in soup.select(selector):
            href = link.get("href")
            if not href:
                continue
            full_url = urljoin(self.base_url + "/", href)
            if not self.is_valid_article_url(full_url):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            urls.append(full_url)

        return urls

    def parse_date_to_iso(self, text: str) -> Tuple[str, str]:
        raw = self.clean_text(text)
        if not raw:
            return "", ""

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

        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", raw)
        if m:
            month = months.get(m.group(1).lower())
            if month:
                return raw, f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"

        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", raw)
        if m:
            return raw, m.group(0)

        return raw, ""

    def clean_body_soup(self, node: BeautifulSoup) -> BeautifulSoup:
        for selector in self.remove_selectors:
            for tag in node.select(selector):
                tag.decompose()

        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        for tag in node.find_all(["button", "svg", "iframe"]):
            tag.decompose()

        return node

    def truncate_body_text(self, text: str) -> str:
        text = self.clean_text(text)
        if not text:
            return ""

        cut_positions = []
        for marker in self.end_markers:
            pos = text.find(marker)
            if pos > 0:
                cut_positions.append(pos)
        if cut_positions:
            text = text[: min(cut_positions)].strip()

        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

    def extract_title(self, soup: BeautifulSoup) -> str:
        selector = self.selectors.get("title", "h1")
        el = soup.select_one(selector)
        if el:
            return self.normalize_ws(el.get_text(" ", strip=True))
        return ""

    def extract_author(self, soup: BeautifulSoup) -> str:
        selector = self.selectors.get("author_links", "")
        if selector:
            for el in soup.select(selector):
                txt = self.normalize_ws(el.get_text(" ", strip=True)).rstrip(",")
                if txt:
                    return txt

        text = soup.get_text("\n", strip=True)
        m = re.search(r"by\s+([A-Za-z0-9 .,'’\-]+?)\s+Posted on\s+[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}", text, flags=re.I)
        if m:
            return self.clean_text(m.group(1)).rstrip(",")

        return self.config.get("source_name", "")

    def extract_date(self, soup: BeautifulSoup) -> Tuple[str, str]:
        for time_el in soup.select("time"):
            dt = self.normalize_ws(time_el.get("datetime", ""))
            if re.match(r"\d{4}-\d{2}-\d{2}", dt):
                return dt[:10], dt[:10]
            txt = self.normalize_ws(time_el.get_text(" ", strip=True))
            if re.search(r"\d{4}", txt):
                return self.parse_date_to_iso(txt)

        text = soup.get_text("\n", strip=True)
        m = re.search(r"Posted on\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", text, flags=re.I)
        if m:
            return self.parse_date_to_iso(m.group(1))

        return "", ""

    def extract_categories(self, soup: BeautifulSoup) -> List[str]:
        categories: List[str] = []
        seen: Set[str] = set()
        selector = self.selectors.get("category_links", "")
        if selector:
            for el in soup.select(selector):
                txt = self.normalize_ws(el.get_text(" ", strip=True))
                href = el.get("href", "")
                if not txt:
                    continue
                if href and urlparse(href).netloc and urlparse(href).netloc != urlparse(self.base_url).netloc:
                    continue
                if txt.lower() in {"previous", "next", "posted on", "by"}:
                    continue
                if txt not in seen:
                    seen.add(txt)
                    categories.append(txt)

        if categories:
            return categories

        if self.fallback_category:
            return [self.fallback_category]

        return []

    def extract_tags(self, soup: BeautifulSoup, categories: List[str]) -> List[str]:
        tags: List[str] = []
        seen: Set[str] = set()
        for el in soup.select("a[rel='tag'], .tags a"):
            txt = self.normalize_ws(el.get_text(" ", strip=True))
            if not txt or txt in seen:
                continue
            seen.add(txt)
            tags.append(txt)

        if not tags and categories:
            tags = list(categories)

        return tags

    def extract_attachment(self, soup: BeautifulSoup, url: str) -> Tuple[bool, str, str]:
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if re.search(r"\.pdf(?:$|\?)", href, flags=re.I):
                return True, urljoin(url, href), "pdf"
        return False, "", ""

    def _body_from_candidates(self, soup: BeautifulSoup) -> str:
        selector = self.selectors.get("body", "article, main article, main")
        candidates = soup.select(selector)
        best = ""
        for candidate in candidates:
            candidate_soup = BeautifulSoup(str(candidate), "lxml")
            candidate_soup = self.clean_body_soup(candidate_soup)
            text = candidate_soup.get_text("\n", strip=True)
            text = self.truncate_body_text(text)
            if len(text) > len(best):
                best = text
        return best

    def _body_from_page_text(self, soup: BeautifulSoup, title: str) -> str:
        text = soup.get_text("\n", strip=True)
        text = self.clean_text(text)
        if not text:
            return ""

        if title:
            idx = text.find(title)
            if idx >= 0:
                text = text[idx + len(title):].lstrip()

        text = re.sub(
            r"^by\s+[A-Za-z0-9 .,'’\-]+\s+Posted on\s+[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\s+",
            "",
            text,
            count=1,
            flags=re.I,
        )
        return self.truncate_body_text(text)

    def _clean_body_lines(self, text: str, title: str, author: str, publish_date: str, category: str) -> str:
        lines = [self.clean_text(line) for line in text.splitlines()]
        lines = [line for line in lines if line]

        noise = set(self.body_noise_prefixes)
        cleaned: List[str] = []
        for line in lines:
            if line == title:
                continue
            if author and line == author:
                continue
            if publish_date and line == publish_date:
                continue
            if category and line == category:
                continue
            if line in noise:
                continue
            if line.lower() in {"by", "posted on"}:
                continue
            if re.fullmatch(r"Posted on\s+[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}", line, flags=re.I):
                continue
            if cleaned and cleaned[-1] == line:
                continue
            cleaned.append(line)

        while cleaned and cleaned[0] in noise:
            cleaned.pop(0)

        return "\n\n".join(cleaned).strip()

    def extract_body(self, soup: BeautifulSoup, title: str, author: str, publish_date: str, category: str) -> str:
        body = self._body_from_candidates(soup)
        body = self._clean_body_lines(body, title, author, publish_date, category)

        if not body or len(body.split()) < 40:
            fallback = self._body_from_page_text(soup, title)
            fallback = self._clean_body_lines(fallback, title, author, publish_date, category)
            if len(fallback) > len(body):
                body = fallback

        return self.truncate_body_text(body)

    def extract_article(self, html: str, url: str) -> Dict:
        soup = BeautifulSoup(html, "lxml")
        title = self.extract_title(soup)
        author = self.extract_author(soup)
        publish_date, date_iso = self.extract_date(soup)
        categories = self.extract_categories(soup)
        category = categories[0] if categories else ""
        tags = self.extract_tags(soup, categories)
        body = self.extract_body(soup, title, author, publish_date, category)
        excerpt = body[:220].strip()
        if len(body) > 220:
            excerpt += "..."

        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, url)
        article_id = self.article_id_from_url(url)

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": article_id,
            "article_url": url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": author,
            "author_type": "organization",
            "user_id": author.lower().replace(" ", "-") if author else "",
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
            "scraped_at": datetime.now().isoformat(),
        }

    def scrape(self) -> None:
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.start_url}")
        print(f"💾 Output: {self.output_file}")
        print("=" * 60)

        print(f"📄 listing_page_number=1 url={self.start_url}")
        listing_html = self.fetch(self.start_url)
        self.stats["listing_pages_checked"] += 1
        if not listing_html:
            self._save_resume()
            print("=" * 60)
            print("❌ Could not fetch listing page")
            return

        soup = BeautifulSoup(listing_html, "lxml")
        page_links = self.extract_listing_urls(soup)
        self.stats["page_threads"] = len(page_links)

        new_links: List[str] = []
        skipped_existing = 0
        for article_url in page_links:
            article_id = self.article_id_from_url(article_url)
            if article_id in self.seen_article_ids:
                skipped_existing += 1
                self.stats["skipped_existing"] += 1
                continue
            new_links.append(article_url)

        self.stats["new_threads"] = len(new_links)

        print(
            f"   page_threads={len(page_links)} "
            f"new_threads={len(new_links)} "
            f"skipped_existing={skipped_existing}"
        )

        for idx, article_url in enumerate(new_links, start=1):
            article_id = self.article_id_from_url(article_url)
            print(f"   [{idx}/{len(new_links)}] {article_url}")
            article_html = self.fetch(article_url)
            if not article_html:
                continue

            try:
                item = self.extract_article(article_html, article_url)
                if not item.get("title") or not item.get("body"):
                    raise ValueError("missing title/body after parsing")

                self.append_jsonl(self.output_file, item)
                self.seen_article_ids.add(article_id)
                self.stats["articles_scraped"] += 1
                print(
                    f"      saved_words={item['word_count']} "
                    f"date={item['date_iso'] or item['publish_date'] or '-'} "
                    f"category={item['category'] or '-'} "
                    f"title={item['title'][:70]}"
                )

                if self.stats["articles_scraped"] % 10 == 0:
                    self._save_resume()
                    print(f"   💾 Progress saved: {self.stats['articles_scraped']} articles")
            except Exception as exc:
                print(f"      ❌ parse failed: {exc}")
                self.log_error(url=article_url, article_id=article_id, error=str(exc), stage="parse")

        self._save_resume()
        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.output_file}")
        print(f"errors_file={self.error_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="EndoFound Endometriosis Stories scraper")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    args = parser.parse_args()

    scraper = EndoFoundStoriesScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()
