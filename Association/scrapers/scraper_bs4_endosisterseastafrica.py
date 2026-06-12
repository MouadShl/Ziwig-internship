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


class EndoSistersEastAfricaScraper:
    def __init__(self, config_path: str):
        self.config = self.load_json(config_path)
        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.posts_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"
        self.session = self.build_session()
        self.seen_ids = self.load_existing_ids()
        self.stats = {"articles_scraped": 0, "skipped_existing": 0, "errors": 0}

    @staticmethod
    def load_json(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def build_session(self):
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
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
            allowed_methods=["HEAD", "GET", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def load_existing_ids(self):
        seen = set()
        if not self.posts_file.exists():
            return seen
        with open(self.posts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                article_id = obj.get("article_id") or obj.get("article_url")
                if article_id:
                    seen.add(article_id)
        return seen

    @staticmethod
    def append_jsonl(path: Path, record: dict):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_error(self, payload: dict):
        self.append_jsonl(self.errors_file, payload)
        self.stats["errors"] += 1

    def fetch(self, url: str):
        try:
            time.sleep(self.config.get("sleep_seconds", 1.0))
            response = self.session.get(url, timeout=self.config.get("request_timeout", 30))
            response.raise_for_status()
            return response.text
        except Exception as e:
            self.log_error({"source_id": self.source_id, "url": url, "error": str(e)})
            print(f"   ❌ fetch failed: {url} -> {e}")
            return None

    @staticmethod
    def clean_text(text: str):
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def clean_body_soup(self, soup: BeautifulSoup):
        for selector in self.config.get("remove_selectors", []):
            for node in soup.select(selector):
                node.decompose()
        for comment in soup.find_all(string=lambda x: isinstance(x, Comment)):
            comment.extract()
        return soup

    @staticmethod
    def normalize_url(base_url: str, href: str):
        return urljoin(base_url + "/", href)

    def extract_article_id(self, url: str):
        path = urlparse(url).path.strip("/")
        if not path:
            return ""
        slug = path.split("/")[-1]
        slug = re.sub(r"\.(html|php|aspx)$", "", slug, flags=re.I)
        return slug

    def is_valid_article_url(self, url: str):
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http"):
            return False
        allowed_domain = self.config.get("listing", {}).get("allowed_domain", "")
        if allowed_domain and allowed_domain not in parsed.netloc:
            return False
        path = parsed.path.rstrip("/") + "/"
        if path in ["/", "/blog/"]:
            return False
        for pattern in self.config.get("listing", {}).get("exclude_patterns", []):
            if pattern in parsed.path:
                return False
        slug = self.extract_article_id(url)
        if not slug:
            return False
        if slug.lower() in {"blog", "articles", "category", "page"}:
            return False
        return True

    def parse_listing_page(self, soup: BeautifulSoup):
        selectors = self.config.get("selectors", {})
        article_list_selector = selectors.get("article_list", "h2 a")
        urls = []
        seen = set()

        for link in soup.select(article_list_selector):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = self.normalize_url(self.base_url, href)
            if not self.is_valid_article_url(url):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)

        for heading in soup.select("h2, h3"):
            link = heading.find("a", href=True)
            if not link:
                continue
            url = self.normalize_url(self.base_url, link["href"])
            if not self.is_valid_article_url(url):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)

        return urls

    def parse_date(self, raw_text: str):
        if not raw_text:
            return "", ""
        txt = self.clean_text(raw_text)
        iso = ""
        # direct ISO in text
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", txt)
        if m:
            iso = m.group(1)
            return txt, iso
        # common date formats
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
        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\b", txt)
        if m:
            mm = months.get(m.group(1).lower())
            if mm:
                iso = f"{m.group(3)}-{mm}-{m.group(2).zfill(2)}"
        m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b", txt)
        if m and not iso:
            mm = months.get(m.group(2).lower())
            if mm:
                iso = f"{m.group(3)}-{mm}-{m.group(1).zfill(2)}"
        return txt, iso

    def extract_article(self, html: str, article_url: str):
        soup = BeautifulSoup(html, "lxml")
        selectors = self.config.get("selectors", {})

        title = ""
        title_el = soup.select_one(selectors.get("title", "h1"))
        if title_el:
            title = self.clean_text(title_el.get_text(" ", strip=True))
        if not title and soup.title:
            title = self.clean_text(re.sub(r"\s*[\-|–]\s*Endosisters East Africa.*$", "", soup.title.get_text(" ", strip=True), flags=re.I))

        author = ""
        author_el = soup.select_one(selectors.get("author", "a[rel='author']"))
        if author_el:
            author = self.clean_text(author_el.get_text(" ", strip=True))
        if not author:
            meta_text = self.clean_text(soup.get_text("\n", strip=True))
            m = re.search(r"\bBy\s+([^,\n]+)", meta_text, re.I)
            if m:
                author = self.clean_text(m.group(1))

        publish_date = ""
        date_iso = ""
        date_el = soup.select_one(selectors.get("date", "time"))
        if date_el:
            dt = (date_el.get("datetime") or "").strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", dt):
                date_iso = dt[:10]
            publish_date = self.clean_text(date_el.get_text(" ", strip=True)) or dt[:10]
        if not publish_date:
            meta_candidates = []
            for sel in [selectors.get("date", "time"), ".entry-meta", ".post-meta", ".byline"]:
                if not sel:
                    continue
                for el in soup.select(sel):
                    txt = self.clean_text(el.get_text(" ", strip=True))
                    if txt:
                        meta_candidates.append(txt)
            for txt in meta_candidates:
                parsed_text, parsed_iso = self.parse_date(txt)
                if parsed_text:
                    publish_date = parsed_text
                    if parsed_iso:
                        date_iso = parsed_iso
                    break

        category = ""
        cat_el = soup.select_one(selectors.get("category", "a[rel='category tag']"))
        if cat_el:
            category = self.clean_text(cat_el.get_text(" ", strip=True))
        if not category:
            meta_text = self.clean_text(soup.get_text("\n", strip=True))
            m = re.search(r"\bArticles\b", meta_text)
            if m:
                category = "Articles"

        tags = []
        tag_sel = selectors.get("tags", "a[rel='tag']")
        for el in soup.select(tag_sel):
            t = self.clean_text(el.get_text(" ", strip=True))
            if t and t not in tags:
                tags.append(t)

        body = ""
        body_el = None
        for sel in [x.strip() for x in selectors.get("body", ".entry-content, article").split(",") if x.strip()]:
            candidate = soup.select_one(sel)
            if candidate:
                body_el = candidate
                break
        if body_el is None:
            body_el = soup.find("article") or soup.body
        if body_el is not None:
            cleaned = self.clean_body_soup(BeautifulSoup(str(body_el), "lxml"))
            body = self.clean_text(cleaned.get_text("\n", strip=True))
            if title and body.startswith(title):
                body = body[len(title):].lstrip("\n ")
            if author and body.startswith(author):
                body = body[len(author):].lstrip("\n ")
            body = re.sub(r"^(By\s+[^\n]+\n?)", "", body, flags=re.I)
            body = re.sub(r"^Articles\n?", "", body)

        excerpt = body[:200] + "..." if len(body) > 200 else body

        attachment_url = ""
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r"\.pdf($|\?)", href, re.I):
                attachment_url = self.normalize_url(self.base_url, href)
                break
        has_attachment = bool(attachment_url)
        attachment_type = "pdf" if has_attachment else ""

        article_id = self.extract_article_id(article_url)
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
            "author": author or self.config.get("source_name", ""),
            "author_type": "organization" if not author else "author",
            "user_id": (author or self.config.get("source_name", "")).strip().lower().replace(" ", "-") if (author or self.config.get("source_name", "")) else "",
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
        }

    def iter_listing_urls(self):
        yielded = set()
        for url in self.config.get("start_urls", []):
            if url not in yielded:
                yielded.add(url)
                yield url

        pagination = self.config.get("pagination", {})
        template = pagination.get("template")
        if template:
            start_page = int(pagination.get("start_page", 1))
            max_pages = int(pagination.get("max_pages", 1))
            for page in range(start_page, max_pages + 1):
                url = self.base_url + template.format(page=page)
                if page == 1 and url in yielded:
                    continue
                if url not in yielded:
                    yielded.add(url)
                    yield url

    def run(self):
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)

        page_number = 0
        for listing_url in self.iter_listing_urls():
            page_number += 1
            print(f"📄 listing_page_number={page_number} url={listing_url}")
            html = self.fetch(listing_url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            page_links = self.parse_listing_page(soup)
            page_links = list(dict.fromkeys(page_links))
            new_links = [u for u in page_links if self.extract_article_id(u) not in self.seen_ids]
            skipped_existing = len(page_links) - len(new_links)
            print(
                f"   page_threads={len(page_links)} new_threads={len(new_links)} skipped_existing={skipped_existing}"
            )

            for idx, article_url in enumerate(new_links, start=1):
                article_id = self.extract_article_id(article_url)
                if article_id in self.seen_ids:
                    self.stats["skipped_existing"] += 1
                    continue
                print(f"   [{idx}/{len(new_links)}] {article_url}")
                article_html = self.fetch(article_url)
                if not article_html:
                    continue
                try:
                    item = self.extract_article(article_html, article_url)
                    self.append_jsonl(self.posts_file, item)
                    self.seen_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(f"      saved_words={item['word_count']} title={item['title'][:80]}")
                except Exception as e:
                    self.log_error(
                        {
                            "source_id": self.source_id,
                            "article_url": article_url,
                            "article_id": article_id,
                            "error": str(e),
                        }
                    )
                    print(f"      ❌ parse failed: {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.posts_file}")
        print(f"errors_file={self.errors_file}")


def main():
    parser = argparse.ArgumentParser(description="Endo Sisters East Africa scraper")
    parser.add_argument("--config", required=True, help="Path to config JSON file")
    args = parser.parse_args()

    scraper = EndoSistersEastAfricaScraper(args.config)
    scraper.run()


if __name__ == "__main__":
    main()
