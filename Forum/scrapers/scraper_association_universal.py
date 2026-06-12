#!/usr/bin/env python3
"""
UNIVERSAL ASSOCIATION SCRAPER
Scrapes news, blogs, articles, directories, and resources from association websites
Works with: WordPress, Drupal, custom CMS sites

Usage:
    python scraper_association_universal.py --config configs/SRC004.json
    python scraper_association_universal.py --config configs/SRC010.json --resume
"""

import os
import re
import json
import time
import argparse
from datetime import datetime
from urllib.parse import urljoin, urlparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class AssociationScraper:
    """Universal scraper for association websites"""

    def __init__(self, config_path):
        self.config = self.load_config(config_path)
        self.source_id = self.config["source_id"]
        self.source_type = self.config.get("source_type", "news_blog")
        self.base_url = self.config["base_url"]
        self.language = self.config.get("language", "en")
        self.country = self.config.get("country", "")

        # Setup session
        self.session = self._build_session()

        # Track progress
        self.seen_ids = set()
        self.stats = {
            "articles_scraped": 0,
            "errors": 0,
            "skipped": 0
        }

        # Setup output
        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.posts_file, self.errors_file, self.resume_file = self._build_output_paths()

        # Load resume data
        self._load_resume()

    def load_config(self, path):
        """Load JSON config"""
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_session(self):
        """Build requests session with retries"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{self.language},en;q=0.9",
        })

        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def _build_output_paths(self):
        """Build output file paths"""
        tag = datetime.now().strftime("%d%m%Y_%Hh%M")
        posts = self.output_dir / f"{self.source_id}_articles_{tag}.jsonl"
        errors = self.output_dir / f"{self.source_id}_errors_{tag}.jsonl"
        resume = self.output_dir / f"{self.source_id}_resume.json"
        return posts, errors, resume

    def _load_resume(self):
        """Load previously scraped IDs"""
        if self.resume_file.exists():
            with open(self.resume_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.seen_ids = set(data.get("seen_ids", []))
                print(f"📂 Resuming: {len(self.seen_ids)} articles already scraped")

    def _save_resume(self):
        """Save progress"""
        with open(self.resume_file, "w", encoding="utf-8") as f:
            json.dump({
                "seen_ids": list(self.seen_ids),
                "stats": self.stats,
                "last_update": datetime.now().isoformat()
            }, f)

    def write_jsonl(self, path, data):
        """Append data to JSONL file"""
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def fetch(self, url):
        """Fetch URL with error handling"""
        try:
            time.sleep(self.config.get("sleep_seconds", 1.0))
            r = self.session.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"    ❌ Error fetching {url}: {e}")
            self.write_jsonl(self.errors_file, {
                "url": url,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            })
            self.stats["errors"] += 1
            return None

    def clean_text(self, text):
        """Clean extracted text"""
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n\s*\n", "\n", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    def extract_id_from_url(self, url):
        """Extract article ID from URL"""
        path = urlparse(url).path
        # Try to get last path component
        slug = path.strip("/").split("/")[-1]
        # Remove file extensions
        slug = re.sub(r"\.(html|php|aspx)$", "", slug, flags=re.I)
        return slug

    def parse_date(self, date_str):
        """Parse date to ISO format based on language"""
        if not date_str:
            return ""

        # Language-specific month mappings
        months = {
            "en": {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12"
            },
            "fr": {
                "janvier": "01", "février": "02", "mars": "03", "avril": "04",
                "mai": "05", "juin": "06", "juillet": "07", "août": "08",
                "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12"
            },
            # Add more languages as needed
        }

        date_str = date_str.lower().strip()

        # Try ISO format first
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            return date_str[:10]

        # Try common formats
        # DD Month YYYY
        match = re.search(r"(\d{1,2})\s+([a-zéû]+)\s+(\d{4})", date_str)
        if match:
            day = match.group(1).zfill(2)
            month_name = match.group(2)
            year = match.group(3)

            lang_months = months.get(self.language, months["en"])
            month = lang_months.get(month_name, "01")
            return f"{year}-{month}-{day}"

        # Month DD, YYYY (US format)
        match = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", date_str)
        if match:
            month_name = match.group(1)
            day = match.group(2).zfill(2)
            year = match.group(3)

            lang_months = months.get(self.language, months["en"])
            month = lang_months.get(month_name, "01")
            return f"{year}-{month}-{day}"

        return ""

    def clean_body(self, soup):
        """Clean body content by removing UI elements"""
        # Remove unwanted elements
        remove_selectors = self.config.get("remove_selectors", [
            "script", "style", "nav", "header", "footer",
            ".share-buttons", ".social-share",
            ".related-posts", ".recommended",
            ".comments", ".comment-section",
            ".sidebar", ".widget",
            ".advertisement", ".ads",
            ".newsletter-signup", ".subscribe"
        ])

        for selector in remove_selectors:
            for elem in soup.select(selector):
                elem.decompose()

        # Remove HTML comments
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        return soup

    def extract_article(self, soup, url):
        """Extract article data from page"""
        selectors = self.config.get("selectors", {})

        # Extract title
        title = ""
        title_sel = selectors.get("title", "h1")
        title_elem = soup.select_one(title_sel)
        if title_elem:
            title = self.clean_text(title_elem.get_text())

        # Extract author
        author = ""
        author_sel = selectors.get("author", ".author")
        author_elem = soup.select_one(author_sel)
        if author_elem:
            author = self.clean_text(author_elem.get_text())
        if not author:
            author = self.config.get("source_name", "Unknown")

        # Extract date
        date_str = ""
        date_iso = ""
        date_sel = selectors.get("date", "time")
        date_elem = soup.select_one(date_sel)
        if date_elem:
            # Try datetime attribute first
            date_iso = date_elem.get("datetime", "")
            if not date_iso:
                date_str = self.clean_text(date_elem.get_text())
                date_iso = self.parse_date(date_str)

        # Extract body
        body = ""
        body_sel = selectors.get("body", ".content")
        body_elem = soup.select_one(body_sel)
        if body_elem:
            # Clean before extracting text
            body_elem = self.clean_body(BeautifulSoup(str(body_elem), "lxml"))
            body = self.clean_text(body_elem.get_text("\n", strip=True))

        # Extract excerpt (first 200 chars of body)
        excerpt = body[:200] + "..." if len(body) > 200 else body

        # Check for attachments/PDFs
        has_attachment = False
        attachment_url = ""
        pdf_links = soup.find_all("a", href=re.compile(r"\.pdf$", re.I))
        if pdf_links:
            has_attachment = True
            attachment_url = urljoin(url, pdf_links[0]["href"])

        # Extract category
        category = ""
        cat_sel = selectors.get("category", ".category")
        cat_elem = soup.select_one(cat_sel)
        if cat_elem:
            category = self.clean_text(cat_elem.get_text())

        article_id = self.extract_id_from_url(url)

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.country,
            "source_language": self.language,
            "source_type": self.source_type,

            "article_id": article_id,
            "article_url": url,
            "article_type": "article",

            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,

            "author": author,
            "author_type": "organization",
            "user_id": author.lower().replace(" ", "-"),

            "category": category,
            "tags": [],

            "publish_date": date_str,
            "date_iso": date_iso,

            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": "pdf" if has_attachment else "",

            "scraped_at": datetime.now().isoformat()
        }

    def extract_directory_entry(self, soup, url):
        """Extract directory/professional listing"""
        selectors = self.config.get("selectors", {})

        name = ""
        name_sel = selectors.get("name", "h2")
        name_elem = soup.select_one(name_sel)
        if name_elem:
            name = self.clean_text(name_elem.get_text())

        specialty = ""
        spec_sel = selectors.get("specialty", ".specialty")
        spec_elem = soup.select_one(spec_sel)
        if spec_elem:
            specialty = self.clean_text(spec_elem.get_text())

        address = ""
        addr_sel = selectors.get("address", ".address")
        addr_elem = soup.select_one(addr_sel)
        if addr_elem:
            address = self.clean_text(addr_elem.get_text("\n"))

        return {
            "source_id": self.source_id,
            "source_mode": "association_directory",
            "professional_id": self.extract_id_from_url(url),
            "name": name,
            "professional_type": "doctor",
            "specialty": specialty,
            "address": address,
            "listing_url": url
        }

    def scrape_news_blog(self):
        """Scrape news/blog type site"""
        sections = self.config.get("sections", {})

        for section_name, section_path in sections.items():
            print(f"\n📂 Scraping section: {section_name}")

            section_url = urljoin(self.base_url, section_path)
            html = self.fetch(section_url)
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")

            # Find article links
            article_links = []
            list_sel = self.config.get("selectors", {}).get("article_list", "article h2 a")

            for link in soup.select(list_sel):
                href = link.get("href")
                if href:
                    full_url = urljoin(self.base_url, href)
                    article_id = self.extract_id_from_url(full_url)

                    if article_id not in self.seen_ids:
                        article_links.append(full_url)

            print(f"   Found {len(article_links)} new articles")

            # Scrape each article
            for idx, article_url in enumerate(article_links, 1):
                article_id = self.extract_id_from_url(article_url)

                if article_id in self.seen_ids:
                    print(f"   [{idx}/{len(article_links)}] SKIP: {article_id}")
                    self.stats["skipped"] += 1
                    continue

                print(f"   [{idx}/{len(article_links)}] {article_url[:60]}...")

                html = self.fetch(article_url)
                if not html:
                    continue

                soup = BeautifulSoup(html, "lxml")
                data = self.extract_article(soup, article_url)

                self.write_jsonl(self.posts_file, data)
                self.seen_ids.add(article_id)
                self.stats["articles_scraped"] += 1

                # Save progress every 10 articles
                if self.stats["articles_scraped"] % 10 == 0:
                    self._save_resume()
                    print(f"   💾 Progress saved: {self.stats['articles_scraped']} articles")

    def scrape_directory(self):
        """Scrape professional directory"""
        start_url = self.config.get("start_url", "")
        pagination = self.config.get("pagination", {})

        page = 1
        has_more = True

        while has_more:
            if pagination.get("type") == "query_param":
                url = urljoin(self.base_url, f"{start_url}?page={page}")
            else:
                url = urljoin(self.base_url, start_url)

            print(f"\n📄 Directory page {page}: {url}")

            html = self.fetch(url)
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")

            # Find professional listings
            list_sel = self.config.get("selectors", {}).get("listing_item", ".views-row")
            items = soup.select(list_sel)

            if not items:
                print("   No items found")
                break

            print(f"   Found {len(items)} professionals")

            for item in items:
                # Extract from item element
                data = self.extract_directory_entry(item, url)

                if data["name"]:
                    self.write_jsonl(self.posts_file, data)
                    self.stats["articles_scraped"] += 1

            # Check for next page
            next_link = soup.select_one(self.config.get("selectors", {}).get("pagination", ".next"))
            if not next_link or "disabled" in str(next_link):
                has_more = False
            else:
                page += 1

    def run(self):
        """Main entry point"""
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"📁 Type: {self.source_type}")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)

        if self.source_type == "news_blog":
            self.scrape_news_blog()
        elif self.source_type == "directory":
            self.scrape_directory()
        else:
            # Default to news/blog
            self.scrape_news_blog()

        # Final save
        self._save_resume()

        print("\n" + "=" * 60)
        print(f"🎉 SCRAPING COMPLETE")
        print(f"📊 Articles scraped: {self.stats['articles_scraped']}")
        print(f"📊 Skipped (already seen): {self.stats['skipped']}")
        print(f"📊 Errors: {self.stats['errors']}")
        print(f"📁 Output: {self.posts_file}")


def main():
    parser = argparse.ArgumentParser(description="Universal Association Scraper")
    parser.add_argument("--config", required=True, help="Path to config JSON file")
    parser.add_argument("--resume", action="store_true", help="Resume from previous run")
    args = parser.parse_args()

    scraper = AssociationScraper(args.config)
    scraper.run()


if __name__ == "__main__":
    main()
