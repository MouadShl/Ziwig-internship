#!/usr/bin/env python3
import argparse
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class InsightEndoScraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.source_name = self.config.get("source_name", "")
        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.posts_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"

        self.session = self._build_session()
        self.seen_ids = self._load_existing_ids()
        self.visited_urls = set()

    def _build_session(self):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-NZ,en;q=0.9",
        })
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _load_existing_ids(self):
        seen = set()
        if self.posts_file.exists():
            with open(self.posts_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        aid = obj.get("article_id")
                        if aid:
                            seen.add(aid)
                    except Exception:
                        continue
        return seen

    def append_jsonl(self, path: Path, item: dict):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def fetch(self, url: str):
        try:
            time.sleep(float(self.config.get("sleep_seconds", 1.0)))
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"   fetch_failed url={url} error={e}")
            self.append_jsonl(self.errors_file, {
                "source_id": self.source_id,
                "url": url,
                "error": str(e)
            })
            return None

    def clean_text(self, text: str):
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def normalize_url(self, url: str):
        absolute = urljoin(self.base_url + "/", url)
        parsed = urlparse(absolute)
        normalized = parsed._replace(fragment="")
        # keep query only for true content pages; strip common tracking
        query = normalized.query
        if query:
            kept = []
            for part in query.split("&"):
                key = part.split("=", 1)[0].lower()
                if key not in {"utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}:
                    kept.append(part)
            query = "&".join([x for x in kept if x])
        normalized = normalized._replace(query=query)
        return urlunparse(normalized)

    def is_allowed_content_url(self, url: str):
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
            return False
        path = parsed.path or "/"
        exclude_patterns = self.config.get("exclude_url_patterns", [])
        full = url.lower()
        for pat in exclude_patterns:
            if pat.lower() in full:
                return False
        if re.search(r"\.(jpg|jpeg|png|gif|webp|svg|css|js|xml|zip|mp4|mp3)$", path, re.I):
            return False
        if path == "/":
            return True
        for prefix in self.config.get("allow_prefixes", []):
            if path == prefix or path.startswith(prefix):
                return True
        return False

    def extract_article_id(self, url: str):
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return "home"
        aid = re.sub(r"[^a-zA-Z0-9/_-]+", "-", path)
        aid = aid.replace("/", "__")
        return aid[:250]

    def extract_pdf_links(self, soup: BeautifulSoup, page_url: str):
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            full = self.normalize_url(urljoin(page_url, href))
            if "/_files/" in full or re.search(r"\.pdf($|\?)", full, re.I):
                label = self.clean_text(a.get_text(" ", strip=True))
                pdf_links.append({
                    "label": label,
                    "url": full
                })
        dedup = []
        seen = set()
        for item in pdf_links:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            dedup.append(item)
        return dedup

    def parse_date(self, text: str):
        if not text:
            return ""
        text = self.clean_text(text)
        m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", text)
        if m:
            raw = m.group(1)
            months = {
                "January": "01", "February": "02", "March": "03", "April": "04",
                "May": "05", "June": "06", "July": "07", "August": "08",
                "September": "09", "October": "10", "November": "11", "December": "12"
            }
            parts = raw.replace(",", "").split()
            return f"{parts[2]}-{months.get(parts[0], '01')}-{parts[1].zfill(2)}"
        m2 = re.search(r"\b(\d{1,2} [A-Z][a-z]{2} \d{4})\b", text)
        if m2:
            raw = m2.group(1)
            months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
            parts = raw.split()
            return f"{parts[2]}-{months.get(parts[1], '01')}-{parts[0].zfill(2)}"
        return ""

    def clean_body_soup(self, soup: BeautifulSoup):
        for selector in self.config.get("remove_selectors", []):
            for node in soup.select(selector):
                node.decompose()
        for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
            node.extract()
        # remove common page chrome by text
        for bad in ["CONTACT US", "CONNECT WITH US", "SUBSCRIBE", "REGISTERED CHARITY NO.", "Request Support", "Donate Now", "Visit Info Hub"]:
            for tag in soup.find_all(["p", "div", "span", "h1", "h2", "h3", "h4"]):
                txt = self.clean_text(tag.get_text(" ", strip=True))
                if txt == bad:
                    tag.decompose()
        return soup

    def extract_category(self, url: str):
        path = urlparse(url).path.strip("/")
        if path.startswith("events-1/"):
            return "events"
        if not path:
            return "home"
        return path.split("/")[0].replace("-", " ")

    def extract_page(self, page_url: str, html: str):
        soup = BeautifulSoup(html, "lxml")
        title = ""
        for sel in ["h1", "main h1", "title"]:
            el = soup.select_one(sel)
            if el:
                title = self.clean_text(el.get_text(" ", strip=True))
                if title:
                    break
        if title.lower().endswith("| insightendometriosis"):
            title = title.rsplit("|", 1)[0].strip()

        pdf_links = self.extract_pdf_links(soup, page_url)

        body_root = soup.select_one("main") or soup.select_one("body") or soup
        body_clone = BeautifulSoup(str(body_root), "lxml")
        body_clone = self.clean_body_soup(body_clone)
        body_text = self.clean_text(body_clone.get_text("\n", strip=True))

        if title and body_text.startswith(title):
            body_text = self.clean_text(body_text[len(title):])

        lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]
        filtered = []
        for line in lines:
            if line in {"Read More >", "More...", "See other events", "Register Now", "Checkout"}:
                continue
            if re.fullmatch(r"\$?\d+[\d.,]*", line):
                continue
            filtered.append(line)
        body_text = "\n\n".join(filtered).strip()

        date_iso = self.parse_date(body_text)
        publish_date = ""
        m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", body_text)
        if m:
            publish_date = m.group(1)
        else:
            m2 = re.search(r"\b(\d{1,2} [A-Z][a-z]{2} \d{4})\b", body_text)
            if m2:
                publish_date = m2.group(1)

        excerpt = body_text[:220] + ("..." if len(body_text) > 220 else "")
        article_id = self.extract_article_id(page_url)
        category = self.extract_category(page_url)
        tags = []
        if category and category not in tags:
            tags.append(category)
        if pdf_links and "pdf" not in tags:
            tags.append("pdf")

        item = {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.source_name,
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": article_id,
            "article_url": page_url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body_text,
            "word_count": len(body_text.split()) if body_text else 0,
            "author": self.source_name,
            "author_type": "organization",
            "user_id": re.sub(r"\s+", "-", self.source_name.strip().lower()),
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": bool(pdf_links),
            "attachment_url": pdf_links[0]["url"] if pdf_links else "",
            "attachment_type": "pdf" if pdf_links else "",
            "attachments": pdf_links,
            "comments_count": 0,
            "comments": []
        }
        return item

    def discover_links(self, page_url: str, html: str):
        soup = BeautifulSoup(html, "lxml")
        discovered = []
        for a in soup.find_all("a", href=True):
            full = self.normalize_url(a["href"])
            if self.is_allowed_content_url(full):
                discovered.append(full)
        out = []
        seen = set()
        for link in discovered:
            if link not in seen:
                seen.add(link)
                out.append(link)
        return out

    def run(self):
        queue = deque()
        for path in self.config.get("seed_urls", []):
            queue.append(self.normalize_url(path))

        total_saved = 0
        total_scanned = 0
        max_pages = int(self.config.get("max_pages", 250))

        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)

        while queue and total_scanned < max_pages:
            url = queue.popleft()
            if url in self.visited_urls:
                continue
            self.visited_urls.add(url)
            total_scanned += 1
            print(f"📄 page_number={total_scanned} url={url}")

            html = self.fetch(url)
            if not html:
                continue

            for link in self.discover_links(url, html):
                if link not in self.visited_urls:
                    queue.append(link)

            item = self.extract_page(url, html)
            article_id = item["article_id"]
            if article_id in self.seen_ids:
                print(f"   skipped_existing=1 article_id={article_id}")
                continue

            if not item["title"] or not item["body"]:
                self.append_jsonl(self.errors_file, {
                    "source_id": self.source_id,
                    "url": url,
                    "error": "empty_title_or_body"
                })
                print("   skipped_empty=1")
                continue

            self.append_jsonl(self.posts_file, item)
            self.seen_ids.add(article_id)
            total_saved += 1
            print(f"   saved_words={item['word_count']} title={item['title'][:80]}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={total_saved}")
        print(f"pages_scanned={total_scanned}")
        print(f"posts_file={self.posts_file}")
        print(f"errors_file={self.errors_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    scraper = InsightEndoScraper(args.config)
    scraper.run()


if __name__ == "__main__":
    main()
