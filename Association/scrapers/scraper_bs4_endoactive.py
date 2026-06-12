#!/usr/bin/env python3
"""
EndoActive blog-only scraper
Scrapes only the Endo Blog listing and each article's full content.
"""

import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class EndoActiveBlogScraper:
    BASE_URL = "https://endoactive.org.au"
    BLOG_URL = "https://endoactive.org.au/blog/"
    OUTPUT_FILE = "SRC028_blog.json"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            }
        )
        self.seen_urls = set()
        self.data = {
            "source": "endoactive.org.au",
            "scraped_at": datetime.now().isoformat(),
            "url": self.BLOG_URL,
            "section": "blog",
            "posts": [],
        }

    def fetch_page(self, url):
        try:
            time.sleep(1)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            print(f"Error fetching {url}: {exc}")
            return None

    def clean_text(self, text):
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def is_blog_post_url(self, url):
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.netloc and "endoactive.org.au" not in parsed.netloc:
            return False
        bad_paths = {
            "/blog/",
            "/",
            "/contact/",
            "/donate/",
            "/free-videos/",
            "/video-news/",
            "/research-paper/",
            "/cost-of-endo/",
            "/your-stories/",
        }
        path = parsed.path.rstrip("/") + "/"
        if path in bad_paths:
            return False
        return "/" in parsed.path.strip("/")

    def extract_listing_posts(self, soup):
        posts = []
        seen = set()

        for a in soup.select("h1 a, h2 a, h3 a, article a, .post a, .entry a"):
            href = a.get("href")
            if not href:
                continue
            full_url = urljoin(self.BASE_URL, href)
            if full_url in seen or not self.is_blog_post_url(full_url):
                continue

            title = self.clean_text(a.get_text(" ", strip=True))
            if not title or len(title) < 8:
                continue

            container = a.find_parent(["article", "div", "section"])
            date = ""
            author = ""
            excerpt = ""
            featured_image = ""

            if container:
                date_el = container.select_one("time, .date, .post-date, .entry-date, .meta-date")
                author_el = container.select_one(".author, .post-author, .byline, .meta-author")
                excerpt_el = container.select_one("p")
                img_el = container.select_one("img")

                if date_el:
                    date = self.clean_text(date_el.get_text(" ", strip=True))
                if author_el:
                    author = self.clean_text(author_el.get_text(" ", strip=True))
                if excerpt_el:
                    excerpt = self.clean_text(excerpt_el.get_text(" ", strip=True))
                if img_el and img_el.get("src"):
                    featured_image = urljoin(self.BASE_URL, img_el["src"])

            posts.append(
                {
                    "title": title,
                    "url": full_url,
                    "date": date,
                    "author": author,
                    "excerpt": excerpt,
                    "featured_image": featured_image,
                }
            )
            seen.add(full_url)

        return posts

    def find_next_page(self, soup, current_url):
        for a in soup.select("a.next, a[rel='next'], .pagination a, .nav-links a"):
            href = a.get("href")
            text = self.clean_text(a.get_text(" ", strip=True)).lower()
            if not href:
                continue
            if "next" in text or "older" in text or a.get("rel") == ["next"]:
                return urljoin(current_url, href)
        return None

    def scrape_article(self, url, listing_meta=None):
        html = self.fetch_page(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        title = ""
        title_el = soup.select_one("h1")
        if title_el:
            title = self.clean_text(title_el.get_text(" ", strip=True))
        elif listing_meta:
            title = listing_meta.get("title", "")

        date = ""
        author = ""
        featured_image = ""

        date_el = soup.select_one("time, .date, .post-date, .entry-date, .meta-date")
        if date_el:
            date = self.clean_text(date_el.get_text(" ", strip=True))
        elif listing_meta:
            date = listing_meta.get("date", "")

        author_el = soup.select_one(".author, .post-author, .byline, .meta-author")
        if author_el:
            author = self.clean_text(author_el.get_text(" ", strip=True))
        elif listing_meta:
            author = listing_meta.get("author", "")

        img_el = soup.select_one("article img, .post img, .entry-content img, .featured img, img.wp-post-image")
        if img_el and img_el.get("src"):
            featured_image = urljoin(self.BASE_URL, img_el["src"])
        elif listing_meta:
            featured_image = listing_meta.get("featured_image", "")

        body_parts = []
        body_container = None
        for selector in [
            "article",
            ".post",
            ".entry-content",
            ".post-content",
            ".article-content",
            "main",
        ]:
            body_container = soup.select_one(selector)
            if body_container:
                break

        if body_container:
            for tag in body_container.select(
                "script, style, nav, footer, form, .sharedaddy, .share, .social, .related, .comments, .comment-respond"
            ):
                tag.decompose()

            for p in body_container.select("p, li, h2, h3, h4"):
                text = self.clean_text(p.get_text(" ", strip=True))
                if text:
                    body_parts.append(text)

        body = "\n\n".join(body_parts).strip()
        excerpt = listing_meta.get("excerpt", "") if listing_meta else ""
        if not excerpt and body:
            excerpt = body[:300]

        return {
            "title": title,
            "date": date,
            "author": author,
            "featured_image": featured_image,
            "excerpt": excerpt,
            "content": body,
            "word_count": len(body.split()) if body else 0,
            "url": url,
        }

    def scrape_blogs(self):
        print("Scraping blog only...")
        next_url = self.BLOG_URL
        page_number = 1
        listing_posts = []

        while next_url and next_url not in self.seen_urls:
            print(f"Listing page {page_number}: {next_url}")
            self.seen_urls.add(next_url)
            html = self.fetch_page(next_url)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            page_posts = self.extract_listing_posts(soup)

            for post in page_posts:
                if post["url"] not in {x["url"] for x in listing_posts}:
                    listing_posts.append(post)

            next_url = self.find_next_page(soup, next_url)
            page_number += 1

            if page_number > 20:
                break

        print(f"Found {len(listing_posts)} blog post URLs")

        for idx, post in enumerate(listing_posts, start=1):
            print(f"[{idx}/{len(listing_posts)}] {post['url']}")
            article = self.scrape_article(post["url"], listing_meta=post)
            if article and article.get("title"):
                self.data["posts"].append(article)

    def save_to_json(self):
        with open(self.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        print(f"Data saved to {self.OUTPUT_FILE}")

    def run(self):
        self.scrape_blogs()
        self.save_to_json()
        print("Done.")


if __name__ == "__main__":
    scraper = EndoActiveBlogScraper()
    scraper.run()
