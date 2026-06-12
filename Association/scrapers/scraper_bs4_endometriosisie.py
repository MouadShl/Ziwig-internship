#!/usr/bin/env python3
import argparse
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MONTHS_EN = {
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
    "december": "12", "dec": "12"
}


class Scraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.base_netloc = urlparse(self.base_url).netloc.lower()
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.0))
        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.articles_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"
        self.session = self._build_session()
        self.existing_ids = self._load_existing_ids()
        self.stats = {"pages_scraped": 0, "skipped_existing": 0, "errors": 0, "discovered": 0}

    def _build_session(self):
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base_url + "/"
        })
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _load_existing_ids(self):
        ids = set()
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
                page_id = row.get("article_id")
                if page_id:
                    ids.add(page_id)
        return ids

    def log_error(self, payload):
        self.stats["errors"] += 1
        with open(self.errors_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def write_article(self, payload):
        with open(self.articles_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def fetch(self, url):
        time.sleep(self.sleep_seconds)
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response
        except Exception as e:
            self.log_error({"source_id": self.source_id, "url": url, "error": str(e)})
            print(f"    ❌ fetch failed: {url} -> {e}")
            return None

    @staticmethod
    def clean_text(text):
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def normalize_url(self, url):
        if not url:
            return ""
        full = urljoin(self.base_url + "/", url)
        parsed = urlparse(full)
        if parsed.scheme not in {"http", "https"}:
            return ""
        if parsed.netloc.lower() != self.base_netloc:
            return ""
        normalized = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", "", ""))
        if normalized.endswith("/") and parsed.path not in {"/", ""}:
            normalized = normalized.rstrip("/")
        return normalized

    def should_skip_url(self, url):
        lower = url.lower()
        for patt in self.config.get("skip_url_patterns", []):
            if patt.lower() in lower:
                return True
        path = urlparse(url).path.lower()
        for ext in self.config.get("skip_extensions", []):
            if path.endswith(ext.lower()):
                return True
        return False

    def slug_from_url(self, url):
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return "home"
        slug = path.replace("/", "__")
        slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug or "page"

    def first_match(self, soup, selectors):
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors or []:
            el = soup.select_one(sel)
            if el:
                return el
        return None

    def all_matches(self, soup, selectors):
        seen = []
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors or []:
            for el in soup.select(sel):
                if el not in seen:
                    seen.append(el)
        return seen

    def parse_date(self, text):
        if not text:
            return ""
        text = self.clean_text(text)
        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", text)
        if m:
            month = MONTHS_EN.get(m.group(1).lower())
            if month:
                day = m.group(2).zfill(2)
                year = m.group(3)
                return f"{year}-{month}-{day}"
        m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", text)
        if m:
            month = MONTHS_EN.get(m.group(2).lower())
            if month:
                day = m.group(1).zfill(2)
                year = m.group(3)
                return f"{year}-{month}-{day}"
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""

    def extract_sitemap_urls(self):
        urls = []
        seen_sitemaps = set()
        queue = deque([urljoin(self.base_url + "/", p) for p in self.config.get("sitemap_candidates", [])])

        while queue:
            sitemap_url = queue.popleft()
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            resp = self.fetch(sitemap_url)
            if not resp:
                continue
            ctype = resp.headers.get("Content-Type", "")
            text = resp.text
            if "xml" not in ctype and not text.lstrip().startswith("<?xml"):
                continue
            try:
                root = ET.fromstring(text)
            except Exception:
                continue
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            if root.tag.endswith("sitemapindex"):
                for loc in root.findall(".//sm:sitemap/sm:loc", ns):
                    if loc.text:
                        queue.append(loc.text.strip())
            else:
                for loc in root.findall(".//sm:url/sm:loc", ns):
                    if loc.text:
                        u = self.normalize_url(loc.text.strip())
                        if u and not self.should_skip_url(u):
                            urls.append(u)
        return urls

    def discover_urls(self):
        seeds = [self.normalize_url(self.base_url)]
        for seed in self.config.get("seeds", []):
            u = self.normalize_url(seed)
            if u:
                seeds.append(u)

        discovered = []
        seen = set()

        # First, pull from sitemap when available.
        for u in self.extract_sitemap_urls():
            if u not in seen:
                seen.add(u)
                discovered.append(u)

        # Then, crawl same-domain links breadth-first.
        queue = deque()
        for u in seeds + discovered:
            if u and u not in seen:
                seen.add(u)
                discovered.append(u)
            if u:
                queue.append(u)

        crawled = set()
        max_pages = int(self.config.get("crawl_max_pages", 500))

        while queue and len(crawled) < max_pages:
            url = queue.popleft()
            if url in crawled or self.should_skip_url(url):
                continue
            crawled.add(url)
            resp = self.fetch(url)
            if not resp:
                continue
            if "html" not in resp.headers.get("Content-Type", ""):
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                nxt = self.normalize_url(a.get("href"))
                if not nxt or self.should_skip_url(nxt):
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    discovered.append(nxt)
                    queue.append(nxt)

        self.stats["discovered"] = len(discovered)
        return discovered[:max_pages]

    def clean_body_container(self, node):
        node = BeautifulSoup(str(node), "lxml")
        for selector in self.config.get("remove_selectors", []):
            for el in node.select(selector):
                el.decompose()
        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        for el in node.select("img, figure, source, picture, video, audio, button, aside"):
            el.decompose()
        return node

    def extract_meta(self, soup, title):
        selectors = self.config.get("selectors", {})
        meta_el = self.first_match(soup, selectors.get("meta", []))
        meta_text = self.clean_text(meta_el.get_text(" ", strip=True)) if meta_el else ""

        author = ""
        publish_date = ""
        date_iso = ""
        category = ""

        # Common WP patterns.
        m = re.search(r"by\s+(.+?)\s+[|·]\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})", meta_text, re.I)
        if m:
            author = self.clean_text(m.group(1))
            publish_date = self.clean_text(m.group(2))
            date_iso = self.parse_date(publish_date)

        if not publish_date:
            m = re.search(r"([A-Za-z]+\s+\d{1,2},\s*\d{4})", meta_text)
            if m:
                publish_date = self.clean_text(m.group(1))
                date_iso = self.parse_date(publish_date)

        cat_el = self.first_match(soup, selectors.get("category", []))
        if cat_el:
            category = self.clean_text(cat_el.get_text(" ", strip=True))

        page_text = self.clean_text(soup.get_text("\n", strip=True))
        if title and not publish_date:
            m = re.search(re.escape(title) + r"\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})", page_text, re.I | re.S)
            if m:
                publish_date = self.clean_text(m.group(1))
                date_iso = self.parse_date(publish_date)

        return author, publish_date, date_iso, category

    def extract_tags(self, soup):
        selectors = self.config.get("selectors", {})
        tags = []
        for el in self.all_matches(soup, selectors.get("tags", [])):
            text = self.clean_text(el.get_text(" ", strip=True))
            if text and text not in tags:
                tags.append(text)
        return tags

    def extract_attachment(self, soup, page_url):
        for link in soup.find_all("a", href=True):
            full = urljoin(page_url, link.get("href", ""))
            if re.search(r"\.(pdf|doc|docx|xls|xlsx)($|[?#])", full, re.I):
                ext = re.search(r"\.([a-z0-9]+)($|[?#])", full, re.I)
                return True, full, ext.group(1).lower() if ext else ""
        return False, "", ""

    def extract_body(self, soup):
        selectors = self.config.get("selectors", {})
        body_el = self.first_match(soup, selectors.get("body", []))
        if not body_el:
            return ""
        cleaned = self.clean_body_container(body_el)
        body = self.clean_text(cleaned.get_text("\n", strip=True))
        for marker in self.config.get("stop_text_markers", []):
            idx = body.find(marker)
            if idx > 0:
                body = body[:idx].strip()
        return body

    def extract_page(self, page_url):
        resp = self.fetch(page_url)
        if not resp or "html" not in resp.headers.get("Content-Type", ""):
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        selectors = self.config.get("selectors", {})

        title_el = self.first_match(soup, selectors.get("title", []))
        title = self.clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
        author, publish_date, date_iso, category = self.extract_meta(soup, title)
        body = self.extract_body(soup)
        tags = self.extract_tags(soup)
        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, page_url)
        page_id = self.slug_from_url(page_url)
        excerpt = body[:200] + "..." if len(body) > 200 else body

        page_type = "article"
        path = urlparse(page_url).path.lower()
        if path in {"", "/"}:
            page_type = "homepage"
        elif "/category/" in path:
            page_type = "category"
        elif path.count("/") <= 2 and publish_date == "":
            page_type = "page"

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "website"),
            "article_id": page_id,
            "article_url": page_url,
            "article_type": page_type,
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": author,
            "author_type": "organization" if author else "",
            "user_id": author.lower().replace(" ", "-") if author else "",
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type
        }

    def run(self):
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.articles_file}")
        print("=" * 60)

        urls = self.discover_urls()
        print(f"discovered_urls={len(urls)}")

        new_urls = [u for u in urls if self.slug_from_url(u) not in self.existing_ids]
        print(f"new_urls={len(new_urls)} skipped_existing={len(urls) - len(new_urls)}")

        for idx, page_url in enumerate(new_urls, start=1):
            page_id = self.slug_from_url(page_url)
            if page_id in self.existing_ids:
                self.stats["skipped_existing"] += 1
                continue
            print(f"[{idx}/{len(new_urls)}] {page_url}")
            try:
                row = self.extract_page(page_url)
                if not row:
                    continue
                # Skip near-empty utility pages.
                if row["word_count"] < 20 and row["article_type"] == "page" and row["article_url"] != self.base_url:
                    continue
                self.write_article(row)
                self.existing_ids.add(page_id)
                self.stats["pages_scraped"] += 1
                print(f"   saved_words={row['word_count']} type={row['article_type']} title={row['title'][:80]}")
            except Exception as e:
                self.log_error({
                    "source_id": self.source_id,
                    "url": page_url,
                    "article_id": page_id,
                    "error": str(e)
                })
                print(f"   ❌ parse failed: {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"discovered={self.stats['discovered']}")
        print(f"pages_scraped={self.stats['pages_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.articles_file}")
        print(f"errors_file={self.errors_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    Scraper(args.config).run()
