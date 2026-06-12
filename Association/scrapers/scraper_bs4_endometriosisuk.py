#!/usr/bin/env python3
import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})",
    re.I
)


class Scraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.domain = urlparse(self.base_url).netloc
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
            "Accept-Language": "en-GB,en;q=0.9",
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
        text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
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
    def first_match(soup, selectors):
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors or []:
            el = soup.select_one(sel)
            if el:
                return el
        return None

    def parse_date(self, text):
        text = self.clean_text(text)
        m = DATE_RE.search(text)
        if not m:
            return ""
        day = m.group(2).zfill(2)
        month = MONTHS_EN.get(m.group(3).lower(), "")
        year = m.group(4)
        if not month:
            return ""
        return f"{year}-{month}-{day}"

    def clean_container(self, node):
        node = BeautifulSoup(str(node), "lxml")
        for selector in self.config.get("remove_selectors", []):
            for el in node.select(selector):
                el.decompose()
        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        for el in node.select("img, picture, source, video, audio, button, aside"):
            el.decompose()
        return node

    def listing_urls(self):
        base = urljoin(self.base_url + "/", self.config.get("start_url", "/"))
        pagination = self.config.get("pagination", {})
        start_index = int(pagination.get("start_index", 0))
        max_index = int(pagination.get("max_index", 0))

        for idx in range(start_index, max_index + 1):
            if idx == 0:
                yield idx + 1, base
            else:
                yield idx + 1, f"{base}?page={idx}"

    def infer_category(self, card_text):
        text = self.clean_text(card_text).upper()
        for label in ["INFORMATIVE", "PERSONAL STORY", "NEWS", "RESEARCH"]:
            if label in text:
                return label.title()
        return ""

    def anchor_context_text(self, anchor):
        current = anchor
        for _ in range(5):
            if not getattr(current, "parent", None):
                break
            current = current.parent
            try:
                text = self.clean_text(current.get_text(" ", strip=True))
            except Exception:
                text = ""
            if DATE_RE.search(text):
                return text
        return ""

    def extract_listing_links(self, soup):
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href:
                continue
            full = urljoin(self.base_url + "/", href)
            parsed = urlparse(full)

            if parsed.netloc != self.domain:
                continue
            if parsed.query and not parsed.path.rstrip("/") == "/blog":
                continue
            path = parsed.path.rstrip("/")

            if not path:
                continue
            if path == "/blog":
                continue
            if path.startswith("/system") or path.startswith("/user") or path.startswith("/search"):
                continue
            if path.startswith("/news") or path.startswith("/events") or path.startswith("/research"):
                pass

            # skip obvious navigation / pagination
            text = self.clean_text(a.get_text(" ", strip=True))
            if text.lower().startswith("page ") or text.lower() in {"next", "previous", "first", "last", "search"}:
                continue

            context = self.anchor_context_text(a)
            if not context:
                continue
            if "Displaying " in context:
                continue
            if "Pagination" in context:
                continue
            if not DATE_RE.search(context):
                continue

            slug = self.slug_from_url(full)
            if not slug:
                continue

            if full in seen:
                continue
            seen.add(full)

            date_match = DATE_RE.search(context)
            publish_date = self.clean_text(date_match.group(0)) if date_match else ""
            links.append({
                "url": full,
                "article_id": slug,
                "listing_category": self.infer_category(context),
                "listing_publish_date": publish_date,
                "listing_date_iso": self.parse_date(publish_date),
            })
        return links

    def extract_body_from_text(self, soup, title):
        selectors = self.config.get("selectors", {})
        container = self.first_match(soup, selectors.get("main_content", []))
        if not container:
            container = soup.body or soup

        cleaned = self.clean_container(container)
        text = cleaned.get_text("\n", strip=True)
        lines = [self.clean_text(x) for x in text.split("\n") if self.clean_text(x)]

        if not lines:
            return "", "", ""

        title_idx = 0
        if title:
            for i, line in enumerate(lines):
                if line == title:
                    title_idx = i
                    break

        date_idx = None
        publish_date = ""
        for i in range(len(lines) - 1, title_idx, -1):
            if DATE_RE.fullmatch(lines[i]):
                date_idx = i
                publish_date = lines[i]
                break

        footer_markers = [
            "© Endometriosis UK",
            "Footer menu",
            "Connect with us:",
            "Company number",
            "Registered Charity in England and Wales",
            "Scottish Charity Registration number",
            "WEO",
        ]

        start_idx = min(title_idx + 1, len(lines))
        end_idx = date_idx if date_idx is not None else len(lines)

        body_lines = []
        for line in lines[start_idx:end_idx]:
            if any(marker in line for marker in footer_markers):
                break
            if line == "Blog":
                continue
            if line.upper() in {"INFORMATIVE", "PERSONAL STORY", "NEWS", "RESEARCH"}:
                continue
            body_lines.append(line)

        body = "\n\n".join(body_lines).strip()
        return body, publish_date, self.parse_date(publish_date)

    def extract_attachment(self, soup, article_url):
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            full = urljoin(article_url, href)
            if re.search(r"\.pdf($|[?#])", full, re.I):
                return True, full, "pdf"
        return False, "", ""

    def extract_article(self, article_url, listing_meta):
        html = self.fetch(article_url, listing_page=False)
        if not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        title = ""
        h1 = soup.select_one("h1")
        if h1:
            title = self.clean_text(h1.get_text(" ", strip=True))

        body, publish_date, date_iso = self.extract_body_from_text(soup, title)
        if not publish_date:
            publish_date = listing_meta.get("listing_publish_date", "")
            date_iso = listing_meta.get("listing_date_iso", "")

        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, article_url)
        excerpt = body[:200] + "..." if len(body) > 200 else body
        category = listing_meta.get("listing_category", "")
        article_id = listing_meta.get("article_id") or self.slug_from_url(article_url)

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
            "author": self.config.get("source_name", ""),
            "author_type": "organization",
            "user_id": re.sub(r"[^a-z0-9]+", "-", self.config.get("source_name", "").lower()).strip("-"),
            "category": category,
            "tags": [],
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type
        }

    def run(self):
        print(f"Starting {self.source_id} scraper")
        print(f"URL: {self.base_url}")
        print(f"Output: {self.articles_file}")
        print("=" * 60)

        found_any_pages = False
        for page_number, listing_url in self.listing_urls():
            print(f"listing_page_number={page_number} url={listing_url}")
            html = self.fetch(listing_url, listing_page=True)
            if html is None:
                if found_any_pages:
                    break
                continue

            found_any_pages = True
            soup = BeautifulSoup(html, "lxml")
            page_rows = self.extract_listing_links(soup)
            page_threads = len(page_rows)
            new_rows = [row for row in page_rows if row["article_id"] not in self.existing_ids]
            print(
                f"  page_threads={page_threads} "
                f"new_threads={len(new_rows)} "
                f"skipped_existing={page_threads - len(new_rows)}"
            )

            if page_threads == 0:
                break

            for idx, row in enumerate(new_rows, start=1):
                article_id = row["article_id"]
                article_url = row["url"]
                print(f"  [{idx}/{len(new_rows)}] {article_url}")
                try:
                    payload = self.extract_article(article_url, row)
                    if not payload:
                        continue
                    self.write_article(payload)
                    self.existing_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(f"     saved_words={payload['word_count']} title={payload['title'][:80]}")
                except Exception as e:
                    self.log_error({
                        "source_id": self.source_id,
                        "url": article_url,
                        "article_id": article_id,
                        "error": str(e)
                    })
                    print(f"     parse failed: {e}")

        print("=" * 60)
        print("Done")
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
