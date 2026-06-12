#!/usr/bin/env python3
import argparse
import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path, item):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def clean_text(value):
    if value is None:
        return ""
    value = str(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_multiline_text(value):
    if value is None:
        return ""
    value = str(value).replace("\xa0", " ")
    value = value.replace("\r", "")
    value = re.sub(r"\n[ \t]*\n+", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def month_to_num_fr(month_name):
    month_name = month_name.lower().strip().strip(".")
    months = {
        "janvier": "01", "janv": "01", "jan": "01",
        "février": "02", "fevrier": "02", "févr": "02", "fevr": "02", "fév": "02", "fev": "02",
        "mars": "03",
        "avril": "04", "avr": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07", "juil": "07",
        "août": "08", "aout": "08", "aoû": "08",
        "septembre": "09", "sept": "09",
        "octobre": "10", "oct": "10",
        "novembre": "11", "nov": "11",
        "décembre": "12", "decembre": "12", "déc": "12", "dec": "12"
    }
    return months.get(month_name, "")


def parse_date_to_iso(date_str):
    date_str = clean_text(date_str)
    if not date_str:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    m = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ.]+)\s+(\d{4})", date_str)
    if m:
        day = m.group(1).zfill(2)
        month = month_to_num_fr(m.group(2))
        year = m.group(3)
        if month:
            return f"{year}-{month}-{day}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m:
        return m.group(1)
    return ""


class ResendoScraper:
    def __init__(self, config_path):
        self.config = load_json(config_path)
        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.2))
        self.timeout_seconds = int(self.config.get("timeout_seconds", 30))
        self.selectors = self.config.get("selectors", {})
        self.listing = self.config.get("listing", {})

        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.articles_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"

        self.session = self._build_session()
        self.existing_ids = self._load_existing_ids()

        self.stats = {
            "articles_scraped": 0,
            "skipped_existing": 0,
            "errors": 0
        }

    def _build_session(self):
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr,en;q=0.9",
            "Referer": self.base_url + "/"
        })
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "HEAD"])
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _load_existing_ids(self):
        ids = set()
        if self.articles_file.exists():
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

    def fetch_html(self, url):
        time.sleep(self.sleep_seconds)
        resp = self.session.get(url, timeout=self.timeout_seconds)
        resp.raise_for_status()
        return resp.text

    def listing_url(self, page_number):
        if page_number == 1:
            return urljoin(self.base_url + "/", self.listing.get("path", "/endometriose-actualites"))
        template = self.listing.get("page_template", "/endometriose-actualites/page/{page}")
        return urljoin(self.base_url + "/", template.format(page=page_number))

    def article_id_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        return path.split("/")[-1]

    def parse_listing_links(self, html):
        soup = BeautifulSoup(html, "lxml")
        links = []
        seen = set()
        selector = self.selectors.get("listing_links", "a[href*='/single-post/']")
        for a in soup.select(selector):
            href = a.get("href")
            if not href:
                continue
            full = urljoin(self.base_url + "/", href)
            parsed = urlparse(full)
            if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if "/single-post/" not in parsed.path:
                continue
            article_id = self.article_id_from_url(full)
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            links.append(full)
        return links

    def extract_title(self, soup):
        title_elem = soup.select_one(self.selectors.get("title", "h1"))
        if title_elem:
            title = clean_text(title_elem.get_text(" ", strip=True))
            if title:
                return title
        text = soup.get_text("\n", strip=True)
        m = re.search(r"Actualités\s+(.+?)\s+\d{1,2}\s+[A-Za-zÀ-ÿ.]+(?:\s+\d{4})?\s+\d+\s+min", text, re.S)
        if m:
            return clean_text(m.group(1))
        return ""

    def extract_publish_date(self, soup, page_text):
        for elem in soup.select("time,[datetime]"):
            dt = clean_text(elem.get("datetime", ""))
            if dt:
                return dt[:10], parse_date_to_iso(dt[:10])
        m = re.search(r"\n(\d{1,2}\s+[A-Za-zÀ-ÿ.]+(?:\s+\d{4})?)\n\s*(\d+\s+min(?:ute)?s?\s+de\s+lecture|\d+\s+min)", page_text, re.I)
        if m:
            publish_date = clean_text(m.group(1))
            return publish_date, parse_date_to_iso(publish_date)
        return "", ""

    def extract_pdf_links(self, soup):
        pdfs = []
        seen = set()
        for a in soup.select("a[href]"):
            href = a.get("href")
            if not href:
                continue
            full = urljoin(self.base_url + "/", href)
            text = clean_text(a.get_text(" ", strip=True)).lower()
            parsed = urlparse(full)
            domain = parsed.netloc.lower()
            href_lower = full.lower()
            is_pdf = href_lower.endswith(".pdf") or "pdf" in text or "usrfiles.com" in domain
            if not is_pdf:
                continue
            if full in seen:
                continue
            seen.add(full)
            pdfs.append(full)
        return pdfs

    def parse_comments_from_text(self, page_text):
        lines = [clean_multiline_text(x) for x in page_text.split("\n")]
        lines = [x for x in lines if x]

        comments_count = 0
        comments = []
        comments_more_available = False

        header_idx = None
        for i, line in enumerate(lines):
            m = re.match(r"^(\d+)\s+commentaires?$", line, re.I)
            if m:
                comments_count = int(m.group(1))
                header_idx = i
                break

        if header_idx is None:
            return comments_count, 0, comments_more_available, comments

        for line in lines[header_idx + 1:]:
            if "Voir plus de commentaires" in line:
                comments_more_available = True
                break

        section = []
        footer_markers = (
            "Centre de l’endométriose",
            "Centre de l'endométriose",
            "Groupe Hospitalier Paris Saint-Joseph",
            "bottom of page"
        )
        for line in lines[header_idx + 1:]:
            if any(line.startswith(marker) for marker in footer_markers):
                break
            section.append(line)

        blocks = []
        current = []
        for line in section:
            if line == "* * *":
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)

        noise_starts = {
            "Rédigez un commentaire...Rédigez un commentaire...",
            "Rédigez un commentaire...",
            "Trier par :",
            "Les plus récents"
        }
        reserved = {
            "J'aime",
            "Répondre",
            "Modifié",
            "Afficher plus"
        }

        for block in blocks:
            block = [x for x in block if x]
            if not block:
                continue
            if all(x in noise_starts or x in reserved for x in block):
                continue
            if block[0] in noise_starts:
                continue

            like_button_visible = "J'aime" in block
            reply_button_visible = "Répondre" in block

            author = block[0] if len(block) >= 1 else ""
            date = block[1] if len(block) >= 2 else ""

            if author in noise_starts:
                continue

            body_lines = []
            for part in block[2:]:
                if part in reserved or part in noise_starts:
                    continue
                body_lines.append(part)

            body = clean_multiline_text("\n".join(body_lines))
            if not author and not body:
                continue

            comments.append({
                "author": author,
                "date": date,
                "body": body,
                "like_button_visible": like_button_visible,
                "reply_button_visible": reply_button_visible
            })

        return comments_count, len(comments), comments_more_available, comments

    def parse_article(self, url, html):
        soup = BeautifulSoup(html, "lxml")
        page_text = soup.get_text("\n", strip=True)

        title = self.extract_title(soup)
        publish_date, date_iso = self.extract_publish_date(soup, page_text)
        pdf_links = self.extract_pdf_links(soup)
        comments_count, comments_visible_count, comments_more_available, comments = self.parse_comments_from_text(page_text)

        article_id = self.article_id_from_url(url)

        return OrderedDict([
            ("source_id", self.source_id),
            ("source_mode", "association"),
            ("source_name", self.config.get("source_name", "")),
            ("source_country", self.config.get("country", "")),
            ("source_language", self.config.get("language", "")),
            ("source_type", self.config.get("source_type", "")),
            ("article_id", article_id),
            ("article_url", url),
            ("title", title),
            ("publish_date", publish_date),
            ("date_iso", date_iso),
            ("pdf_links_count", len(pdf_links)),
            ("pdf_links", pdf_links),
            ("comments_count", comments_count),
            ("comments_visible_count", comments_visible_count),
            ("comments_more_available", comments_more_available),
            ("comments", comments)
        ])

    def run(self):
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.articles_file}")
        print("=" * 60)

        start_page = int(self.listing.get("start_page", 1))
        end_page = int(self.listing.get("end_page", 1))

        for page_number in range(start_page, end_page + 1):
            page_url = self.listing_url(page_number)
            try:
                print(f"📄 listing_page_number={page_number} url={page_url}")
                listing_html = self.fetch_html(page_url)
                page_links = self.parse_listing_links(listing_html)

                new_links = []
                for article_url in page_links:
                    article_id = self.article_id_from_url(article_url)
                    if article_id in self.existing_ids:
                        continue
                    new_links.append(article_url)

                print(
                    f"   page_threads={len(page_links)} "
                    f"new_threads={len(new_links)} "
                    f"skipped_existing={len(page_links) - len(new_links)}"
                )

                for idx, article_url in enumerate(new_links, start=1):
                    article_id = self.article_id_from_url(article_url)
                    try:
                        article_html = self.fetch_html(article_url)
                        item = self.parse_article(article_url, article_html)
                        append_jsonl(self.articles_file, item)
                        self.existing_ids.add(article_id)
                        self.stats["articles_scraped"] += 1
                        print(
                            f"   [{idx}/{len(new_links)}] "
                            f"comments={item['comments_visible_count']}/{item['comments_count']} "
                            f"pdf_links={item['pdf_links_count']} "
                            f"title={item['title'][:80]}"
                        )
                    except Exception as e:
                        self.stats["errors"] += 1
                        append_jsonl(self.errors_file, {
                            "source_id": self.source_id,
                            "article_url": article_url,
                            "article_id": article_id,
                            "error": str(e)
                        })
                        print(f"   ❌ article_failed: {article_url} -> {e}")

                self.stats["skipped_existing"] += len(page_links) - len(new_links)

            except Exception as e:
                self.stats["errors"] += 1
                append_jsonl(self.errors_file, {
                    "source_id": self.source_id,
                    "listing_url": page_url,
                    "page_number": page_number,
                    "error": str(e)
                })
                print(f"   ❌ listing_failed: {page_url} -> {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"articles_file={self.articles_file}")
        print(f"errors_file={self.errors_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    scraper = ResendoScraper(args.config)
    scraper.run()


if __name__ == "__main__":
    main()
