#!/usr/bin/env python3
"""
Endometriozis Magyarorszag blog scraper
Scrapes all blog archive pages and full article details into SRC031.json
"""

import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://endometriozismagyarorszag.hu"
BLOG_URL = f"{BASE_URL}/blog/"
OUTPUT_FILE = "SRC031.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
}
REQUEST_DELAY_SECONDS = 0.8
TIMEOUT = 30


class MagyarorszagBlogScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.seen_urls = set()

    def get_soup(self, url: str) -> BeautifulSoup:
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return BeautifulSoup(response.text, "html.parser")

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def make_page_url(self, page_number: int) -> str:
        if page_number == 1:
            return BLOG_URL
        return f"{BLOG_URL}?e-page-edcb056={page_number}"

    def discover_total_pages(self, soup: BeautifulSoup) -> int:
        max_page = 1
        for a in soup.select('a[href*="e-page-edcb056="]'):
            href = a.get("href", "")
            match = re.search(r"[?&]e-page-edcb056=(\d+)", href)
            if match:
                max_page = max(max_page, int(match.group(1)))
            text_match = re.search(r"(\d+)", self.clean_text(a.get_text()))
            if text_match:
                max_page = max(max_page, int(text_match.group(1)))
        return max_page

    def is_probable_article_url(self, href: str) -> bool:
        if not href:
            return False
        full = urljoin(BASE_URL, href)
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
            return False
        blocked_parts = [
            "/blog/",
            "/category/",
            "/tag/",
            "/author/",
            "/shop/",
            "/rolunk",
            "/kapcsolat",
            "/intezmenyek",
            "/kiegeszito-terapiak",
            "/cart/",
            "/checkout/",
            "/my-account/",
            "/feed/",
            "/wp-content/",
            "/wp-json/",
            "/page/",
            "#",
        ]
        lower = full.lower()
        if any(part in lower for part in blocked_parts):
            return False
        path = parsed.path.rstrip("/")
        if not path or path.count("/") != 1:
            return False
        slug = path.split("/")[-1]
        if not slug or slug in {"blog", "shop"}:
            return False
        return True

    def extract_article_links_from_page(self, soup: BeautifulSoup) -> list[str]:
        urls: list[str] = []

        selectors = [
            "h3 a[href]",
            "h2 a[href]",
            ".elementor-post__title a[href]",
            "article a[href]",
            "main a[href]",
        ]

        for selector in selectors:
            for a in soup.select(selector):
                href = a.get("href", "")
                full_url = urljoin(BASE_URL, href)
                if self.is_probable_article_url(full_url) and full_url not in urls:
                    text = self.clean_text(a.get_text(" ", strip=True))
                    if text or selector in {"article a[href]", "main a[href]"}:
                        urls.append(full_url)

        return urls

    def extract_meta_content(self, soup: BeautifulSoup, *keys: str) -> str:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return self.clean_text(tag["content"])
        return ""

    def extract_date(self, soup: BeautifulSoup) -> tuple[str, str]:
        date_text = ""
        date_iso = ""

        time_tag = soup.find("time")
        if time_tag:
            date_text = self.clean_text(time_tag.get_text(" ", strip=True))
            date_iso = self.clean_text(time_tag.get("datetime", ""))

        if not date_text:
            candidates = soup.select(
                ".elementor-icon-list-text, .post-info, .entry-meta, .elementor-post-info, .meta-date"
            )
            for node in candidates:
                text = self.clean_text(node.get_text(" ", strip=True))
                if re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{4}\.\d{2}\.\d{2}\.", text):
                    date_text = text
                    break

        if not date_iso and date_text:
            match = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", date_text)
            if match:
                yyyy, mm, dd = match.groups()
                date_iso = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"

        return date_text, date_iso

    def extract_author(self, soup: BeautifulSoup) -> str:
        selectors = [
            'a[rel="author"]',
            ".author a",
            ".entry-author a",
            ".byline a",
            ".elementor-post-info__item--type-author a",
        ]
        for selector in selectors:
            tag = soup.select_one(selector)
            if tag:
                return self.clean_text(tag.get_text(" ", strip=True))
        return ""

    def extract_categories(self, soup: BeautifulSoup) -> list[str]:
        categories: list[str] = []
        selectors = [
            'a[href*="/category/"]',
            ".cat-links a",
            ".post-categories a",
            ".elementor-post-info__item--type-terms a",
        ]
        for selector in selectors:
            for tag in soup.select(selector):
                text = self.clean_text(tag.get_text(" ", strip=True))
                if text and text not in categories:
                    categories.append(text)
        return categories

    def extract_featured_image(self, soup: BeautifulSoup) -> str:
        meta_image = self.extract_meta_content(soup, "og:image", "twitter:image")
        if meta_image:
            return meta_image

        selectors = [
            "article img[src]",
            ".elementor-widget-theme-post-featured-image img[src]",
            ".post-thumbnail img[src]",
            "main img[src]",
        ]
        for selector in selectors:
            img = soup.select_one(selector)
            if img:
                src = img.get("src") or img.get("data-src")
                if src and not src.startswith("data:"):
                    return urljoin(BASE_URL, src)
        return ""

    def extract_content(self, soup: BeautifulSoup) -> str:
        selectors = [
            ".elementor-widget-theme-post-content",
            ".entry-content",
            "article .elementor",
            "article",
            "main",
        ]

        for selector in selectors:
            container = soup.select_one(selector)
            if not container:
                continue

            cloned = BeautifulSoup(str(container), "html.parser")
            for unwanted in cloned.select(
                "script, style, noscript, svg, form, iframe, .sharedaddy, .jp-relatedposts, .elementor-widget-theme-post-title, .elementor-widget-theme-post-featured-image, .elementor-widget-theme-post-info, .elementor-widget-sidebar"
            ):
                unwanted.decompose()

            text_blocks: list[str] = []
            for node in cloned.select("h2, h3, h4, p, li, blockquote"):
                text = self.clean_text(node.get_text(" ", strip=True))
                if text and text not in text_blocks:
                    text_blocks.append(text)

            if len(" ".join(text_blocks)) >= 120:
                return "\n\n".join(text_blocks)

        return ""

    def parse_article(self, article_url: str) -> dict:
        soup = self.get_soup(article_url)

        title = ""
        for selector in ["h1", ".entry-title", ".elementor-heading-title"]:
            tag = soup.select_one(selector)
            if tag:
                title = self.clean_text(tag.get_text(" ", strip=True))
                if title:
                    break

        date_text, date_iso = self.extract_date(soup)
        author = self.extract_author(soup)
        categories = self.extract_categories(soup)
        featured_image = self.extract_featured_image(soup)
        excerpt = self.extract_meta_content(soup, "description", "og:description")
        content = self.extract_content(soup)

        if not excerpt and content:
            excerpt = self.clean_text(content[:280])

        slug = urlparse(article_url).path.rstrip("/").split("/")[-1]

        return {
            "id": slug,
            "url": article_url,
            "title": title,
            "date": date_text,
            "date_iso": date_iso,
            "author": author,
            "categories": categories,
            "featured_image": featured_image,
            "excerpt": excerpt,
            "content": content,
            "word_count": len(content.split()) if content else 0,
        }

    def run(self):
        first_page_soup = self.get_soup(BLOG_URL)
        total_pages = self.discover_total_pages(first_page_soup)
        print(f"Detected {total_pages} blog pages.")

        all_article_urls: list[str] = []

        for page_number in range(1, total_pages + 1):
            page_url = self.make_page_url(page_number)
            print(f"Scraping page {page_number}: {page_url}")
            try:
                soup = first_page_soup if page_number == 1 else self.get_soup(page_url)
                page_urls = self.extract_article_links_from_page(soup)
                print(f"  Found {len(page_urls)} article links.")
                for url in page_urls:
                    if url not in self.seen_urls:
                        self.seen_urls.add(url)
                        all_article_urls.append(url)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:
                print(f"  Error on page {page_number}: {exc}")

        print(f"\nUnique article URLs collected: {len(all_article_urls)}")

        articles = []
        for index, article_url in enumerate(all_article_urls, start=1):
            print(f"[{index}/{len(all_article_urls)}] {article_url}")
            try:
                article = self.parse_article(article_url)
                articles.append(article)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:
                print(f"  Failed to parse article: {exc}")

        output = {
            "metadata": {
                "source": "endometriozismagyarorszag.hu",
                "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "base_url": BASE_URL,
                "blog_url": BLOG_URL,
                "pages_scraped": total_pages,
                "total_articles": len(articles),
            },
            "articles": articles,
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\nSaved {len(articles)} articles to {OUTPUT_FILE}")


if __name__ == "__main__":
    scraper = MagyarorszagBlogScraper()
    scraper.run()
