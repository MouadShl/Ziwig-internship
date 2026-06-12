#!/usr/bin/env python3
import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MONTHS_RO = {
    "ianuarie": "01", "ian": "01",
    "februarie": "02", "feb": "02",
    "martie": "03", "mar": "03",
    "aprilie": "04", "apr": "04",
    "mai": "05",
    "iunie": "06", "iun": "06",
    "iulie": "07", "iul": "07",
    "august": "08", "aug": "08",
    "septembrie": "09", "sep": "09", "sept": "09",
    "octombrie": "10", "oct": "10",
    "noiembrie": "11", "nov": "11",
    "decembrie": "12", "dec": "12"
}


class Scraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.0))
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
            "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
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

    def fetch(self, url, listing_page=False):
        time.sleep(self.sleep_seconds)
        try:
            response = self.session.get(url, timeout=30)
            if listing_page and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.text
        except Exception as e:
            if listing_page:
                return None
            self.log_error({"source_id": self.source_id, "url": url, "error": str(e)})
            print(f"    fetch failed: {url} -> {e}")
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

    @staticmethod
    def slug_from_url(url):
        path = urlparse(url).path.rstrip("/")
        if not path:
            return ""
        slug = path.split("/")[-1]
        slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
        return slug

    @staticmethod
    def normalize_url(url):
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", parsed.query, ""))

    def parse_date(self, text):
        if not text:
            return ""
        text = self.clean_text(text)

        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
        if m:
            day = m.group(1).zfill(2)
            month = m.group(2).zfill(2)
            year = m.group(3)
            return f"{year}-{month}-{day}"

        m = re.search(r"\b(\d{1,2})\s+([A-Za-zĂÂÎȘŞȚŢăâîșşțţ]+)\s+(\d{4})\b", text, re.I)
        if m:
            day = m.group(1).zfill(2)
            month = MONTHS_RO.get(m.group(2).lower(), "")
            year = m.group(3)
            if month:
                return f"{year}-{month}-{day}"

        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return ""

    @staticmethod
    def first_match(soup, selectors):
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

    def clean_page_soup(self, soup):
        soup = BeautifulSoup(str(soup), "lxml")
        for selector in self.config.get("remove_selectors", []):
            for el in soup.select(selector):
                el.decompose()
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        for el in soup.select("img, figure, source, picture, video, audio, svg"):
            el.decompose()
        return soup

    def is_article_url(self, url):
        parsed = urlparse(url)
        if not parsed.netloc.endswith(urlparse(self.base_url).netloc):
            return False
        path = parsed.path.rstrip("/")
        if not path or path == "":
            return False
        banned_prefixes = [
            "/category/", "/tag/", "/author/", "/wp-content/", "/wp-json/", "/feed/",
            "/shop", "/contact", "/despre-noi", "/proiecte", "/echipa-noastra", "/despre-endometrioza",
            "/informatii-medicale", "/nutritie", "/bunastare-emotionala", "/vindecare-holistica",
            "/testimonialele-pacientilor", "/vino-in-comunitate", "/doneaza", "/redirecteaza-3-5-gratuit",
            "/devino-voluntar"
        ]
        for prefix in banned_prefixes:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return False
        if path.startswith("/page/"):
            return False
        slug = self.slug_from_url(url)
        if not slug:
            return False
        return True

    def extract_listing_items(self, soup):
        selectors = self.config.get("selectors", {})
        items = []
        seen = set()
        for el in self.all_matches(soup, selectors.get("article_list", [])):
            href = el.get("href")
            if not href:
                continue
            full = self.normalize_url(urljoin(self.base_url + "/", href))
            if not self.is_article_url(full):
                continue
            if full in seen:
                continue
            seen.add(full)

            title = self.clean_text(el.get_text(" ", strip=True))
            context_text = ""
            for ancestor in [el.parent, getattr(el.parent, 'parent', None), getattr(getattr(el.parent, 'parent', None), 'parent', None)]:
                if ancestor is not None:
                    context_text += "\n" + self.clean_text(ancestor.get_text(" ", strip=True))
            listing_publish_date = ""
            m = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", context_text)
            if m:
                listing_publish_date = m.group(0)
            else:
                m = re.search(r"\b\d{1,2}\s+[A-Za-zĂÂÎȘŞȚŢăâîșşțţ]+\s+\d{4}\b", context_text, re.I)
                if m:
                    listing_publish_date = m.group(0)

            items.append({
                "url": full,
                "title": title,
                "listing_publish_date": listing_publish_date
            })
        return items

    def next_listing_url(self, soup, current_url):
        selectors = self.config.get("selectors", {})
        for el in self.all_matches(soup, selectors.get("next_page", [])):
            href = el.get("href")
            if not href:
                continue
            full = self.normalize_url(urljoin(current_url, href))
            if full != self.normalize_url(current_url):
                return full
        return None

    def extract_author(self, soup):
        selectors = self.config.get("selectors", {})
        author_el = self.first_match(soup, selectors.get("author", []))
        if author_el:
            return self.clean_text(author_el.get_text(" ", strip=True))

        meta_el = self.first_match(soup, selectors.get("meta", []))
        meta_text = self.clean_text(meta_el.get_text(" ", strip=True)) if meta_el else ""
        m = re.search(r"\bBy\s+([^|/]+)", meta_text, re.I)
        if m:
            return self.clean_text(m.group(1))
        return ""

    def extract_category(self, soup):
        selectors = self.config.get("selectors", {})
        cat_el = self.first_match(soup, selectors.get("category", []))
        if cat_el:
            return self.clean_text(cat_el.get_text(" ", strip=True))

        meta_el = self.first_match(soup, selectors.get("meta", []))
        meta_text = self.clean_text(meta_el.get_text(" ", strip=True)) if meta_el else ""
        m = re.search(r"Leave a Comment\s*/\s*([^/|]+)\s*/\s*By", meta_text, re.I)
        if m:
            return self.clean_text(m.group(1))
        return ""

    def extract_date(self, soup, listing_publish_date=""):
        selectors = self.config.get("selectors", {})
        publish_date = ""
        date_iso = ""

        date_el = self.first_match(soup, selectors.get("date", []))
        if date_el:
            dt_attr = date_el.get("datetime", "")
            if dt_attr:
                date_iso = dt_attr[:10]
            date_text = self.clean_text(date_el.get_text(" ", strip=True))
            m = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", date_text)
            if m:
                publish_date = m.group(0)
            else:
                m = re.search(r"\b\d{1,2}\s+[A-Za-zĂÂÎȘŞȚŢăâîșşțţ]+\s+\d{4}\b", date_text, re.I)
                if m:
                    publish_date = m.group(0)
                elif date_text:
                    publish_date = date_text
            if not date_iso and publish_date:
                date_iso = self.parse_date(publish_date)

        if not publish_date:
            meta_el = self.first_match(soup, selectors.get("meta", []))
            meta_text = self.clean_text(meta_el.get_text(" ", strip=True)) if meta_el else ""
            m = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", meta_text)
            if m:
                publish_date = m.group(0)
                date_iso = self.parse_date(publish_date)
            else:
                m = re.search(r"\b\d{1,2}\s+[A-Za-zĂÂÎȘŞȚŢăâîșşțţ]+\s+\d{4}\b", meta_text, re.I)
                if m:
                    publish_date = m.group(0)
                    date_iso = self.parse_date(publish_date)

        if not publish_date and listing_publish_date:
            publish_date = listing_publish_date
            date_iso = self.parse_date(publish_date)

        return publish_date, date_iso

    def extract_tags(self, soup):
        selectors = self.config.get("selectors", {})
        tags = []
        for el in self.all_matches(soup, selectors.get("tags", [])):
            text = self.clean_text(el.get_text(" ", strip=True))
            if text and text not in tags:
                tags.append(text)
        return tags

    def extract_attachment(self, soup, article_url):
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            full = urljoin(article_url, href)
            if re.search(r"\.pdf($|[?#])", full, re.I):
                return True, full, "pdf"
        return False, "", ""

    def extract_body(self, soup, title):
        selectors = self.config.get("selectors", {})
        body_el = self.first_match(soup, selectors.get("body", []))
        if not body_el:
            return ""

        cleaned = self.clean_page_soup(body_el)
        body = self.clean_text(cleaned.get_text("\n", strip=True))

        stop_markers = [
            "Copyright ©",
            "Powered by",
            "Follow Us",
            "What We Do",
            "Scroll to Top",
            "VREI SĂ FACI O DIFERENȚĂ",
            "DONEAZĂ ACUM",
            "DONEAZĂ acum"
        ]
        for marker in stop_markers:
            idx = body.find(marker)
            if idx > 0:
                body = body[:idx].strip()

        if title and body.startswith(title):
            body = body[len(title):].strip()

        return body

    def extract_article(self, article_url, listing_info=None):
        listing_info = listing_info or {}
        html = self.fetch(article_url, listing_page=False)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        selectors = self.config.get("selectors", {})

        title_el = self.first_match(soup, selectors.get("title", []))
        title = self.clean_text(title_el.get_text(" ", strip=True)) if title_el else listing_info.get("title", "")
        author = self.extract_author(soup)
        category = self.extract_category(soup)
        publish_date, date_iso = self.extract_date(soup, listing_info.get("listing_publish_date", ""))
        body = self.extract_body(soup, title)
        tags = self.extract_tags(soup)
        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, article_url)
        article_id = self.slug_from_url(article_url)
        excerpt = body[:200] + "..." if len(body) > 200 else body

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": article_id,
            "article_url": article_url,
            "article_type": "article",
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

    def crawl_section(self, start_url):
        current_url = self.normalize_url(start_url)
        seen_pages = set()
        page_number = 0

        while current_url and current_url not in seen_pages:
            seen_pages.add(current_url)
            page_number += 1
            print(f"📄 listing_page_number={page_number} url={current_url}")
            html = self.fetch(current_url, listing_page=True)
            if html is None:
                break
            soup = BeautifulSoup(html, "lxml")
            items = self.extract_listing_items(soup)
            page_threads = len(items)
            new_items = [x for x in items if self.slug_from_url(x["url"]) not in self.existing_ids]
            print(
                f"   page_threads={page_threads} "
                f"new_threads={len(new_items)} "
                f"skipped_existing={page_threads - len(new_items)}"
            )
            if page_threads == 0:
                break

            for idx, item in enumerate(new_items, start=1):
                article_url = item["url"]
                article_id = self.slug_from_url(article_url)
                if article_id in self.existing_ids:
                    self.stats["skipped_existing"] += 1
                    continue
                print(f"   [{idx}/{len(new_items)}] {article_url}")
                try:
                    row = self.extract_article(article_url, item)
                    if not row:
                        continue
                    self.write_article(row)
                    self.existing_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(f"      saved_words={row['word_count']} title={row['title'][:80]}")
                except Exception as e:
                    self.log_error({
                        "source_id": self.source_id,
                        "url": article_url,
                        "article_id": article_id,
                        "error": str(e)
                    })
                    print(f"      parse failed: {e}")

            next_url = self.next_listing_url(soup, current_url)
            current_url = next_url

    def run(self):
        print(f"Starting {self.source_id} scraper")
        print(f"URL: {self.base_url}")
        print(f"Output: {self.articles_file}")
        print("=" * 60)

        for section in self.config.get("sections", []):
            full_url = self.normalize_url(urljoin(self.base_url + "/", section))
            print(f"Section: {full_url}")
            self.crawl_section(full_url)

        print("=" * 60)
        print("Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.articles_file}")
        print(f"errors_file={self.errors_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    Scraper(args.config).run()
