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


class EndoFoundationScraper:
    def __init__(self, config_path: str):
        self.config = self.load_config(config_path)
        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.language = self.config.get("language", "en")
        self.sleep_seconds = float(self.config.get("sleep_seconds", 1.0))
        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.posts_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"
        self.seen_ids = self.load_existing_ids()
        self.session = self.build_session()

    @staticmethod
    def load_config(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def build_session(self):
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        })
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
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
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                article_id = item.get("article_id") or item.get("thread_id")
                if article_id:
                    seen.add(str(article_id))
        return seen

    @staticmethod
    def append_jsonl(path: Path, item: dict):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def fetch(self, url: str, accept_json: bool = False):
        time.sleep(self.sleep_seconds)
        headers = {}
        if accept_json:
            headers["Accept"] = "application/json"
        resp = self.session.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp

    @staticmethod
    def clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def extract_id_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        if "/post/" in path:
            slug = path.split("/post/")[-1]
        else:
            slug = path.split("/")[-1]
        slug = re.sub(r"\.[A-Za-z0-9]+$", "", slug)
        return slug.strip()

    def parse_date(self, raw: str) -> str:
        if not raw:
            return ""
        raw = self.clean_text(raw)
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
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
            "december": "12", "dec": "12"
        }
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", raw)
        if m:
            month = months.get(m.group(1).lower(), "")
            if month:
                return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"
        m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
        if m:
            month = months.get(m.group(2).lower(), "")
            if month:
                return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"
        return ""

    def remove_noise(self, container: BeautifulSoup) -> BeautifulSoup:
        for selector in self.config.get("remove_selectors", []):
            for elem in container.select(selector):
                elem.decompose()
        for comment in container.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        return container

    def extract_article_links(self, soup: BeautifulSoup):
        selectors = self.config.get("selectors", {})
        list_selector = selectors.get("article_list", "a[href*='/post/']")
        links = []
        seen = set()
        for a in soup.select(list_selector):
            href = a.get("href")
            if not href:
                continue
            full_url = urljoin(self.base_url + "/", href)
            if "/post/" not in urlparse(full_url).path:
                continue
            article_id = self.extract_id_from_url(full_url)
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            links.append((article_id, full_url))
        return links

    def extract_meta_from_text(self, page_text: str, title: str):
        author = ""
        publish_date = ""
        date_iso = ""
        category = ""

        if title:
            pattern = (
                re.escape(title)
                + r"\s+([A-Za-z0-9_\- ]{2,100})\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})\s+\d+\s+min\s+read"
            )
            m = re.search(pattern, page_text, re.I)
            if m:
                author = self.clean_text(m.group(1))
                publish_date = self.clean_text(m.group(2))
                date_iso = self.parse_date(publish_date)

        category_matches = re.findall(r"\b(News|Blog|Events)\b", page_text, re.I)
        if category_matches:
            category = self.clean_text(category_matches[0])

        return author, publish_date, date_iso, category

    @staticmethod
    def parse_int(text: str):
        if text is None:
            return None
        m = re.search(r"(\d[\d,]*)", str(text))
        if not m:
            return None
        return int(m.group(1).replace(",", ""))

    def extract_counts_from_html(self, soup: BeautifulSoup, html: str):
        page_text = self.clean_text(soup.get_text("\n", strip=True))
        script_text = "\n".join(
            s.get_text(" ", strip=True)
            for s in soup.find_all("script")
            if s.get_text(" ", strip=True)
        )
        blob = page_text + "\n" + script_text + "\n" + html

        views_count = None
        comments_count = None

        text_patterns = [
            (r"\b(\d[\d,]*)\s+views?\b", "views"),
            (r"\b(\d[\d,]*)\s+comments?\b", "comments"),
            (r'"viewCount"\s*:\s*(\d+)', "views"),
            (r'"views"\s*:\s*(\d+)', "views"),
            (r'"commentCount"\s*:\s*(\d+)', "comments"),
            (r'"comments"\s*:\s*(\d+)', "comments"),
            (r'"totalComments"\s*:\s*(\d+)', "comments"),
            (r'"comment_count"\s*:\s*(\d+)', "comments")
        ]

        for pattern, kind in text_patterns:
            m = re.search(pattern, blob, re.I)
            if not m:
                continue
            value = self.parse_int(m.group(1))
            if value is None:
                continue
            if kind == "views" and views_count is None:
                views_count = value
            elif kind == "comments" and comments_count is None:
                comments_count = value

        return views_count, comments_count

    def extract_post_id(self, html: str, article_url: str):
        slug = re.escape(self.extract_id_from_url(article_url))
        patterns = [
            rf'"slug"\s*:\s*"{slug}"[^{{}}]{{0,400}}?"_id"\s*:\s*"([a-f0-9\-]{{24,36}})"',
            rf'"_id"\s*:\s*"([a-f0-9\-]{{24,36}})"[^{{}}]{{0,400}}?"slug"\s*:\s*"{slug}"',
            rf'"id"\s*:\s*"([a-f0-9\-]{{24,36}})"[^{{}}]{{0,400}}?"slug"\s*:\s*"{slug}"',
            rf'"postId"\s*:\s*"([a-f0-9\-]{{24,36}})"',
            rf'"post_id"\s*:\s*"([a-f0-9\-]{{24,36}})"'
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.I | re.S)
            if m:
                return m.group(1)
        return ""

    def try_wix_metrics_api(self, post_id: str, article_url: str):
        if not post_id:
            return None, None
        candidates = [
            f"https://www.wixapis.com/blog/v3/posts/{post_id}/metrics",
            f"https://www.wixapis.com/v3/posts/{post_id}/metrics"
        ]
        for api_url in candidates:
            try:
                time.sleep(self.sleep_seconds)
                resp = self.session.get(
                    api_url,
                    headers={
                        "Accept": "application/json",
                        "Referer": article_url,
                        "Origin": self.base_url
                    },
                    timeout=20
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                metrics = data.get("metrics") or data
                views = metrics.get("views")
                comments = metrics.get("comments")
                if isinstance(views, int) or isinstance(comments, int):
                    return views if isinstance(views, int) else None, comments if isinstance(comments, int) else None
            except Exception:
                continue
        return None, None

    def extract_visible_comments(self, soup: BeautifulSoup):
        selectors = [
            "[data-hook*='comment']",
            "[id*='comment']",
            "[class*='comment']"
        ]
        comments = []
        seen = set()

        def normalize_lines(text: str):
            return [self.clean_text(x) for x in text.split("\n") if self.clean_text(x)]

        for selector in selectors:
            for elem in soup.select(selector):
                raw_text = self.clean_text(elem.get_text("\n", strip=True))
                if not raw_text:
                    continue
                lowered = raw_text.lower()
                if "write a comment" in lowered:
                    continue
                if lowered in {"comments", "comment", "0 comments", "like", "reply"}:
                    continue
                if len(raw_text.split()) < 5:
                    continue
                if len(raw_text.split()) > 250:
                    continue
                if not any(token in lowered for token in ["ago", "reply", "like", "j'aime", "respond", "comment"]):
                    continue

                lines = normalize_lines(raw_text)
                if not lines:
                    continue

                author = ""
                date_text = ""
                body_lines = []
                likes_count = None
                has_like_button = any(
                    ("like" in x.lower() or "j'aime" in x.lower())
                    for x in lines
                )

                ui_tokens = {"like", "reply", "j'aime", "répondre", "edited", "modifier", "sort by"}
                stop_idx = len(lines)
                for i, line in enumerate(lines):
                    if line.lower() in ui_tokens or line.lower().startswith("sort by"):
                        stop_idx = i
                        break
                useful = lines[:stop_idx]
                if len(useful) < 2:
                    continue

                author = useful[0]
                if re.search(r"\b(ago|yesterday|today|day|days|hour|hours|week|weeks|month|months)\b", useful[1], re.I):
                    date_text = useful[1]
                    body_lines = useful[2:]
                else:
                    body_lines = useful[1:]

                body = self.clean_text("\n".join(body_lines))
                if not body or len(body.split()) < 3:
                    continue

                m_like = re.search(r"(\d+)\s*(?:likes?|j['’]?aime)", lowered, re.I)
                if m_like:
                    likes_count = int(m_like.group(1))

                key = (author.lower(), date_text.lower(), body.lower())
                if key in seen:
                    continue
                seen.add(key)
                comments.append({
                    "author": author,
                    "date": date_text,
                    "body": body,
                    "likes_count": likes_count,
                    "has_like_button": has_like_button
                })

        return comments

    def extract_article(self, url: str, html: str):
        soup = BeautifulSoup(html, "lxml")
        selectors = self.config.get("selectors", {})

        title = ""
        title_el = soup.select_one(selectors.get("title", "h1"))
        if title_el:
            title = self.clean_text(title_el.get_text(" ", strip=True))

        page_text = self.clean_text(soup.get_text("\n", strip=True))

        author = ""
        author_el = soup.select_one(selectors.get("author", "a[rel='author']"))
        if author_el:
            author = self.clean_text(author_el.get_text(" ", strip=True))

        publish_date = ""
        date_iso = ""
        date_el = soup.select_one(selectors.get("date", "time"))
        if date_el:
            publish_date = self.clean_text(date_el.get("datetime") or date_el.get_text(" ", strip=True))
            date_iso = self.parse_date(publish_date)
            dt_attr = date_el.get("datetime")
            if dt_attr and re.match(r"\d{4}-\d{2}-\d{2}", dt_attr):
                date_iso = dt_attr[:10]
                if not publish_date or publish_date == date_iso:
                    publish_date = date_iso

        category = ""
        category_el = soup.select_one(selectors.get("category", "a[href*='/blog/categories/']"))
        if category_el:
            category = self.clean_text(category_el.get_text(" ", strip=True))

        fallback_author, fallback_publish_date, fallback_date_iso, fallback_category = self.extract_meta_from_text(page_text, title)
        if not author:
            author = fallback_author
        if not publish_date:
            publish_date = fallback_publish_date
        if not date_iso:
            date_iso = fallback_date_iso
        if not category:
            category = fallback_category

        tags = []
        for el in soup.select(selectors.get("tags", "a[href*='/blog/categories/'], a[rel='tag']")):
            tag = self.clean_text(el.get_text(" ", strip=True))
            if tag and tag not in tags:
                tags.append(tag)
        if category and category not in tags:
            tags.insert(0, category)

        visible_comments = self.extract_visible_comments(soup)
        views_count, comments_count = self.extract_counts_from_html(soup, html)
        post_id = self.extract_post_id(html, url)
        api_views, api_comments = self.try_wix_metrics_api(post_id, url)
        if views_count is None and api_views is not None:
            views_count = api_views
        if comments_count is None and api_comments is not None:
            comments_count = api_comments
        if comments_count is None:
            comments_count = len(visible_comments) if visible_comments else 0

        body = ""
        body_container = None
        body_selectors = selectors.get("body", "article, main article, [data-hook='rich-content'], [itemprop='articleBody'], main")
        for selector in [s.strip() for s in body_selectors.split(",") if s.strip()]:
            found = soup.select_one(selector)
            if found:
                body_container = found
                break
        if body_container is not None:
            cloned = BeautifulSoup(str(body_container), "lxml")
            cloned = self.remove_noise(cloned)
            text = self.clean_text(cloned.get_text("\n", strip=True))
            if title and text.startswith(title):
                text = text[len(title):].strip()
            if author and text.startswith(author):
                text = text[len(author):].strip()
            if publish_date:
                text = text.replace(publish_date, "", 1).strip()
            text = re.sub(r"\b\d+\s+min\s+read\b", "", text, count=1, flags=re.I).strip()
            text = re.split(r"\n\s*Recent Posts\b", text, maxsplit=1, flags=re.I)[0].strip()
            text = re.split(r"\n\s*Comments\b", text, maxsplit=1, flags=re.I)[0].strip()
            text = re.split(r"\n\s*Write a comment", text, maxsplit=1, flags=re.I)[0].strip()
            body = text

        excerpt = body[:200] + "..." if len(body) > 200 else body

        attachment_url = ""
        attachment_type = ""
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if re.search(r"\.pdf($|\?)", href, re.I):
                attachment_url = urljoin(url, href)
                attachment_type = "pdf"
                break

        item = {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": self.extract_id_from_url(url),
            "article_url": url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": author,
            "author_type": "organization" if author == self.config.get("source_name", "") else "author",
            "user_id": re.sub(r"[^a-z0-9]+", "-", author.lower()).strip("-") if author else "",
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "views_count": views_count,
            "comments_count": comments_count,
            "comments_visible_count": len(visible_comments),
            "comments": visible_comments,
            "has_attachment": bool(attachment_url),
            "attachment_url": attachment_url,
            "attachment_type": attachment_type
        }
        return item

    def log_error(self, payload: dict):
        self.append_jsonl(self.errors_file, payload)

    def scrape(self):
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)
        total_saved = 0
        processed_pages = set()
        start_urls = self.config.get("start_urls", [self.config.get("start_url", "/blog")])

        for start_url in start_urls:
            for page in range(1, int(self.config.get("pagination", {}).get("max_pages", 50)) + 1):
                if start_url == "/blog" and page > 1:
                    path = self.config["pagination"]["template"].format(page=page)
                elif page > 1:
                    break
                else:
                    path = start_url
                url = urljoin(self.base_url + "/", path.lstrip("/"))
                if url in processed_pages:
                    continue
                processed_pages.add(url)
                print(f"📄 listing_page_number={page} url={url}")
                try:
                    html = self.fetch(url).text
                except Exception as e:
                    self.log_error({"source_id": self.source_id, "listing_url": url, "error": str(e)})
                    print(f"   ❌ fetch failed: {url} -> {e}")
                    if start_url == "/blog" and page > 1:
                        break
                    continue

                soup = BeautifulSoup(html, "lxml")
                links = self.extract_article_links(soup)
                page_threads = len(links)
                new_links = [(aid, link) for aid, link in links if aid not in self.seen_ids]
                print(
                    f"   page_threads={page_threads} "
                    f"new_threads={len(new_links)} "
                    f"skipped_existing={page_threads - len(new_links)}"
                )
                if page_threads == 0:
                    if start_url == "/blog":
                        break
                    continue

                for idx, (article_id, article_url) in enumerate(new_links, start=1):
                    print(f"   [{idx}/{len(new_links)}] {article_url}")
                    try:
                        article_html = self.fetch(article_url).text
                        item = self.extract_article(article_url, article_html)
                        self.append_jsonl(self.posts_file, item)
                        self.seen_ids.add(article_id)
                        total_saved += 1
                        print(
                            f"      saved_words={item['word_count']} "
                            f"views_count={item['views_count']} "
                            f"comments_count={item['comments_count']} "
                            f"visible_comments={item['comments_visible_count']}"
                        )
                    except Exception as e:
                        self.log_error({"source_id": self.source_id, "article_url": article_url, "error": str(e)})
                        print(f"      ❌ article failed: {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={total_saved}")
        print(f"posts_file={self.posts_file}")
        print(f"errors_file={self.errors_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    scraper = EndoFoundationScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()
