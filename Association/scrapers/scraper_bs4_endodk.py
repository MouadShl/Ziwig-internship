#!/usr/bin/env python3
import argparse
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MONTHS = {
    "da": {
        "januar": "01", "jan": "01",
        "februar": "02", "feb": "02",
        "marts": "03", "mar": "03",
        "april": "04", "apr": "04",
        "maj": "05",
        "juni": "06", "jun": "06",
        "juli": "07", "jul": "07",
        "august": "08", "aug": "08",
        "september": "09", "sep": "09",
        "oktober": "10", "okt": "10",
        "november": "11", "nov": "11",
        "december": "12", "dec": "12",
    },
    "en": {
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
    },
}

BODY_CANDIDATES = [
    "main article",
    "article",
    "main",
    ".entry-content",
    ".post-content",
    ".elementor-widget-theme-post-content",
    ".elementor-location-single",
    ".site-main",
    ".content-area",
    ".page-content",
]

TITLE_CANDIDATES = [
    "main h1",
    "article h1",
    "h1.entry-title",
    "h1.page-title",
    "h1",
]

DATE_CANDIDATES = [
    "time",
    ".post-date",
    ".entry-date",
    ".published",
    ".meta-date",
    ".elementor-post-info__item--type-date",
]

TAG_CANDIDATES = [
    "a[rel='tag']",
    ".tags a",
    ".post-tags a",
]

STOP_PATTERNS = [
    r"Tilmeld nyhedsbrev",
    r"Vi kunne ikke tilmelde dig",
    r"Tak for din tilmelding",
    r"Endometriose F[æa]llesskabet\s*$",
    r"Gravh[øo]jen\s+34",
    r"Privatlivspolitik",
    r"Handelsbetingelser",
    r"Web:\s*Mercatus",
]


class Scraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.base_netloc = urlparse(self.base_url).netloc.lower()
        self.language = self.config.get("language", "da")
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.0))
        self.max_pages = int(self.config.get("max_pages", 250))
        self.allowed_prefixes = tuple(self.config.get("allowed_prefixes", []))
        self.deny_prefixes = tuple(self.config.get("deny_prefixes", []))

        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.articles_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"

        self.session = self._build_session()
        self.existing_ids = self._load_existing_ids()
        self.stats = {"articles_scraped": 0, "skipped_existing": 0, "errors": 0}

    def _build_session(self):
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "da,en;q=0.9",
            "Referer": self.base_url + "/",
        })
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
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
                article_id = row.get("article_id")
                if article_id:
                    ids.add(article_id)
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
            return response.text
        except Exception as e:
            self.log_error({"source_id": self.source_id, "url": url, "error": str(e)})
            print(f"    ❌ fetch failed: {url} -> {e}")
            return None

    @staticmethod
    def clean_text(text):
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = text.replace("\u200b", "")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    @staticmethod
    def slug_from_url(url):
        path = urlparse(url).path.rstrip("/")
        if not path:
            return "home"
        slug = path.strip("/").replace("/", "__")
        return slug

    def canonicalize(self, href, base=None):
        full = urljoin(base or self.base_url + "/", href)
        parsed = urlparse(full)
        if parsed.scheme not in {"http", "https"}:
            return ""
        if parsed.netloc.lower() != self.base_netloc:
            return ""
        path = re.sub(r"/+", "/", parsed.path or "/")
        if path != "/" and path.endswith("/"):
            path = path[:-1] + "/"
        full = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        return full

    def is_allowed_url(self, url):
        parsed = urlparse(url)
        if parsed.netloc.lower() != self.base_netloc:
            return False
        path = parsed.path or "/"
        if re.search(r"\.(jpg|jpeg|png|gif|webp|svg|mp4|mp3|zip|xml|css|js)$", path, re.I):
            return False
        if any(path.startswith(x) for x in self.deny_prefixes):
            return False
        if path == "/":
            return True
        return any(path.startswith(x) for x in self.allowed_prefixes)

    def all_matches(self, soup, selectors):
        seen = []
        for sel in selectors:
            for el in soup.select(sel):
                if el not in seen:
                    seen.append(el)
        return seen

    def first_match(self, soup, selectors):
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el
        return None

    def clean_body_container(self, node):
        node = BeautifulSoup(str(node), "lxml")
        for selector in self.config.get("remove_selectors", []):
            for el in node.select(selector):
                el.decompose()
        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        for el in node.select("img, picture, source, video, audio, button, input, select, textarea"):
            el.decompose()
        return node

    def parse_date(self, text):
        text = self.clean_text(text).lower()
        if not text:
            return ""
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        month_map = MONTHS.get(self.language, MONTHS["en"])
        m = re.search(r"\b(\d{1,2})[.\- ]+([a-zæøå]+)[.]?\s+(\d{4})\b", text)
        if m:
            day = m.group(1).zfill(2)
            mon = month_map.get(m.group(2), "")
            year = m.group(3)
            if mon:
                return f"{year}-{mon}-{day}"
        m = re.search(r"\b([a-zæøå]+)\s+(\d{1,2}),?\s+(\d{4})\b", text)
        if m:
            mon = month_map.get(m.group(1), "")
            day = m.group(2).zfill(2)
            year = m.group(3)
            if mon:
                return f"{year}-{mon}-{day}"
        return ""

    def extract_publish_date(self, soup):
        for el in self.all_matches(soup, DATE_CANDIDATES):
            dt = el.get("datetime", "")
            if dt:
                m = re.search(r"\d{4}-\d{2}-\d{2}", dt)
                if m:
                    return self.clean_text(el.get_text(" ", strip=True)) or m.group(0), m.group(0)
            txt = self.clean_text(el.get_text(" ", strip=True))
            iso = self.parse_date(txt)
            if iso:
                return txt, iso
        page_text = self.clean_text(soup.get_text("\n", strip=True))
        m = re.search(r"\b(\d{1,2}[.]?\s+[A-Za-zÆØÅæøå]+\s+\d{4})\b", page_text)
        if m:
            txt = self.clean_text(m.group(1))
            return txt, self.parse_date(txt)
        return "", ""

    def derive_category(self, url):
        path = urlparse(url).path.strip("/")
        if not path:
            return "home"
        first = path.split("/")[0]
        mapping = {
            "endometriose": "endometriose",
            "adenomyose": "adenomyose",
            "dysmenore": "dysmenore",
            "mere-viden": "mere_viden",
            "arbejdslivet": "arbejdslivet",
            "nyheder": "nyheder",
            "om-os": "om_os",
        }
        return mapping.get(first, first)

    def extract_tags(self, soup):
        tags = []
        for el in self.all_matches(soup, TAG_CANDIDATES):
            t = self.clean_text(el.get_text(" ", strip=True))
            if t and t not in tags:
                tags.append(t)
        return tags

    def extract_attachment(self, soup, page_url):
        for link in soup.find_all("a", href=True):
            full = self.canonicalize(link.get("href"), page_url) or urljoin(page_url, link.get("href"))
            if re.search(r"\.pdf($|[?#])", full, re.I):
                return True, full, "pdf"
        return False, "", ""

    def extract_body(self, soup):
        body = ""
        for sel in BODY_CANDIDATES:
            node = soup.select_one(sel)
            if not node:
                continue
            cleaned = self.clean_body_container(node)
            text = self.clean_text(cleaned.get_text("\n", strip=True))
            if len(text.split()) >= 40:
                body = text
                break
        if not body:
            cleaned = self.clean_body_container(soup)
            body = self.clean_text(cleaned.get_text("\n", strip=True))

        for pat in STOP_PATTERNS:
            m = re.search(pat, body, re.I)
            if m and m.start() > 0:
                body = body[:m.start()].strip()
        return body

    def extract_title(self, soup):
        el = self.first_match(soup, TITLE_CANDIDATES)
        return self.clean_text(el.get_text(" ", strip=True)) if el else ""

    def extract_internal_links(self, soup, page_url):
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            full = self.canonicalize(href, page_url)
            if not full:
                continue
            if not self.is_allowed_url(full):
                continue
            parsed = urlparse(full)
            if re.search(r"/(wp-content|wp-json|cdn-cgi)/", parsed.path):
                continue
            if full not in seen:
                seen.add(full)
                links.append(full)
        return links

    def extract_page(self, page_url):
        html = self.fetch(page_url)
        if not html:
            return None, []
        soup = BeautifulSoup(html, "lxml")
        title = self.extract_title(soup)
        body = self.extract_body(soup)
        publish_date, date_iso = self.extract_publish_date(soup)
        tags = self.extract_tags(soup)
        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, page_url)
        links = self.extract_internal_links(soup, page_url)
        article_id = self.slug_from_url(page_url)
        excerpt = body[:200] + "..." if len(body) > 200 else body

        row = {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": "site_crawl",
            "article_id": article_id,
            "article_url": page_url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": self.config.get("source_name", ""),
            "author_type": "organization",
            "user_id": self.config.get("source_name", "").lower().replace(" ", "-") if self.config.get("source_name") else "",
            "category": self.derive_category(page_url),
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
        }
        return row, links

    def run(self):
        queue = deque()
        visited = set()
        queued = set()

        for seed in self.config.get("seed_urls", []):
            url = self.canonicalize(seed)
            if url and url not in queued and self.is_allowed_url(url):
                queue.append(url)
                queued.add(url)

        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.articles_file}")
        print("=" * 60)

        page_number = 0
        while queue and len(visited) < self.max_pages:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            page_number += 1

            article_id = self.slug_from_url(current)
            if article_id in self.existing_ids:
                self.stats["skipped_existing"] += 1
                print(f"📄 page_number={page_number} SKIP existing {current}")
                continue

            print(f"📄 page_number={page_number} url={current}")
            try:
                row, discovered = self.extract_page(current)
                if row and (row["title"] or row["body"]):
                    self.write_article(row)
                    self.existing_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(f"   saved_words={row['word_count']} title={row['title'][:90]}")
                for url in discovered:
                    if url not in queued and url not in visited:
                        queue.append(url)
                        queued.add(url)
            except Exception as e:
                self.log_error({
                    "source_id": self.source_id,
                    "url": current,
                    "article_id": article_id,
                    "error": str(e),
                })
                print(f"   ❌ parse failed: {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"articles_file={self.articles_file}")
        print(f"errors_file={self.errors_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    Scraper(args.config).run()
