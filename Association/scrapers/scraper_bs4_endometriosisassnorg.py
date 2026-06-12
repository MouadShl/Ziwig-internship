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

        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return ""

    @staticmethod
    def first_match(soup, selectors):
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors or []:
            try:
                el = soup.select_one(sel)
            except Exception:
                continue
            if el:
                return el
        return None

    def all_matches(self, soup, selectors):
        seen = []
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors or []:
            try:
                matches = soup.select(sel)
            except Exception:
                continue
            for el in matches:
                if el not in seen:
                    seen.append(el)
        return seen

    def clean_body_container(self, node):
        node = BeautifulSoup(str(node), "lxml")
        for selector in self.config.get("remove_selectors", []):
            try:
                matches = node.select(selector)
            except Exception:
                matches = []
            for el in matches:
                el.decompose()
        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        for el in node.select("img, figure, source, picture, video, audio, button"):
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

        m = re.search(r"by\s+(.+?)\s*\|\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})\s*\|\s*(.+)", meta_text, re.I)
        if m:
            author = self.clean_text(m.group(1))
            publish_date = self.clean_text(m.group(2))
            date_iso = self.parse_date(publish_date)
            category = self.clean_text(m.group(3))
        else:
            page_text = self.clean_text(soup.get_text("\n", strip=True))
            title_pat = re.escape(title) if title else r".+?"
            m = re.search(
                title_pat + r"\s+by\s+(.+?)\s*\|\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})\s*\|\s*([^\n]+)",
                page_text,
                re.I | re.S,
            )
            if m:
                author = self.clean_text(m.group(1))
                publish_date = self.clean_text(m.group(2))
                date_iso = self.parse_date(publish_date)
                category = self.clean_text(m.group(3))

        return author, publish_date, date_iso, category

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

    def score_text_blocks(self, node):
        texts = []
        for el in node.find_all(["p", "h2", "h3", "h4", "li", "blockquote"]):
            text = self.clean_text(el.get_text(" ", strip=True))
            if not text:
                continue
            texts.append(text)
        body = "\n\n".join(texts).strip()
        return body, len(body.split()) if body else 0

    def extract_body(self, soup, title):
        selectors = self.config.get("selectors", {})
        best_body = ""
        best_words = 0

        for body_el in self.all_matches(soup, selectors.get("body", [])):
            cleaned = self.clean_body_container(body_el)
            body, words = self.score_text_blocks(cleaned)
            if words > best_words:
                best_body = body
                best_words = words

        if not best_body:
            page_text = self.clean_text(soup.get_text("\n", strip=True))
            if title and title in page_text:
                after = page_text.split(title, 1)[-1].strip()
            else:
                after = page_text
            lines = [x.strip() for x in after.split("\n") if x.strip()]
            keep = []
            for line in lines:
                if line in {"Privacy Policy", "Older Entries", "Next Entries", "Donate"}:
                    break
                keep.append(line)
            best_body = "\n\n".join(keep).strip()

        if title and best_body.startswith(title):
            best_body = best_body[len(title):].strip()

        stop_markers = [
            "The Endometriosis Association is a self-help organization",
            "Privacy Policy",
            "Designed by Milwaukee Digital Marketing"
        ]
        for marker in stop_markers:
            idx = best_body.find(marker)
            if idx > 0:
                best_body = best_body[:idx].strip()

        # Trim leading meta noise like "by X | Date | Category"
        best_body = re.sub(r"^by\s+.+?\|\s+[A-Za-z]+\s+\d{1,2},\s*\d{4}\s*\|\s*.+?(\n\n|$)", "", best_body, flags=re.I | re.S).strip()
        return best_body

    def extract_article(self, article_url):
        html = self.fetch(article_url, listing_page=False)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        selectors = self.config.get("selectors", {})

        title_el = self.first_match(soup, selectors.get("title", []))
        title = self.clean_text(title_el.get_text(" ", strip=True)) if title_el else ""

        author, publish_date, date_iso, category = self.extract_meta(soup, title)
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

    def extract_listing_links(self, soup):
        selectors = self.config.get("selectors", {})
        links = []
        seen = set()
        for el in self.all_matches(soup, selectors.get("article_list", [])):
            href = el.get("href")
            if not href:
                continue
            full = urljoin(self.base_url + "/", href)
            parsed = urlparse(full)
            host = f"{parsed.scheme}://{parsed.netloc}"
            if parsed.netloc and host != self.base_url:
                continue
            path = parsed.path.rstrip("/")
            if not path:
                continue
            if path == "/blog-2" or path.startswith("/blog-2/page/"):
                continue
            if full not in seen:
                seen.add(full)
                links.append(full)
        return links

    def extract_next_listing_url(self, soup, current_url):
        for a in soup.find_all("a", href=True):
            text = self.clean_text(a.get_text(" ", strip=True)).lower()
            href = urljoin(self.base_url + "/", a["href"])
            if "/blog-2/page/" in href and href != current_url:
                if "older entries" in text or "next entries" in text or a.get("rel") == ["next"]:
                    return href
        return None

    def listing_urls(self):
        start_url = urljoin(self.base_url + "/", self.config.get("start_url", "/"))
        max_pages = int(self.config.get("pagination", {}).get("max_pages", 20))
        current_url = start_url
        seen = set()
        page_number = 1

        while current_url and current_url not in seen and page_number <= max_pages:
            seen.add(current_url)
            yield page_number, current_url
            html = self.fetch(current_url, listing_page=True)
            if html is None:
                break
            soup = BeautifulSoup(html, "lxml")
            next_url = self.extract_next_listing_url(soup, current_url)
            if not next_url:
                break
            current_url = next_url
            page_number += 1

    def run(self):
        print(f"Starting {self.source_id} scraper")
        print(f"URL: {self.base_url}")
        print(f"Output: {self.articles_file}")
        print("=" * 60)

        for page_number, listing_url in self.listing_urls():
            print(f"listing_page_number={page_number} url={listing_url}")
            html = self.fetch(listing_url, listing_page=True)
            if html is None:
                break
            soup = BeautifulSoup(html, "lxml")
            page_links = self.extract_listing_links(soup)
            page_threads = len(page_links)
            new_links = [u for u in page_links if self.slug_from_url(u) not in self.existing_ids]
            print(
                f"page_threads={page_threads} "
                f"new_threads={len(new_links)} "
                f"skipped_existing={page_threads - len(new_links)}"
            )
            if page_threads == 0:
                break

            for idx, article_url in enumerate(new_links, start=1):
                article_id = self.slug_from_url(article_url)
                if article_id in self.existing_ids:
                    self.stats["skipped_existing"] += 1
                    continue
                print(f"[{idx}/{len(new_links)}] {article_url}")
                try:
                    row = self.extract_article(article_url)
                    if not row:
                        continue
                    self.write_article(row)
                    self.existing_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(f"saved_words={row['word_count']} title={row['title'][:80]}")
                except Exception as e:
                    self.log_error({
                        "source_id": self.source_id,
                        "url": article_url,
                        "article_id": article_id,
                        "error": str(e)
                    })
                    print(f"parse failed: {e}")

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
