#!/usr/bin/env python3
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class EndoFranceScraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.language = self.config.get("language", "fr")

        self.output_dir = Path("outputs") / self.source_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.posts_file = self.output_dir / f"{self.source_id}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{self.source_id}_errors_final.jsonl"
        self.resume_file = self.output_dir / f"{self.source_id}_resume.json"

        self.session = self._build_session()
        self.seen_ids = set()
        self.stats = {
            "articles_scraped": 0,
            "errors": 0,
            "skipped": 0,
        }

        self._load_resume()
        self._load_existing_output_ids()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
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

    def _load_resume(self) -> None:
        if not self.resume_file.exists():
            return
        try:
            with open(self.resume_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.seen_ids.update(x for x in data.get("seen_ids", []) if x)
            print(f"📂 Resume file loaded: {len(self.seen_ids)} known article_ids")
        except Exception as exc:
            print(f"⚠️ Could not read resume file: {exc}")

    def _load_existing_output_ids(self) -> None:
        if not self.posts_file.exists():
            return
        loaded = 0
        with open(self.posts_file, "r", encoding="utf-8") as f:
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
                    self.seen_ids.add(article_id)
                    loaded += 1
        if loaded:
            print(f"📂 Existing output loaded: {loaded} article_ids")

    def _save_resume(self) -> None:
        payload = {
            "source_id": self.source_id,
            "seen_ids": sorted(self.seen_ids),
            "stats": self.stats,
            "last_update": datetime.now().isoformat(),
        }
        with open(self.resume_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @staticmethod
    def append_jsonl(path: Path, data: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def log_error(self, **kwargs) -> None:
        kwargs["timestamp"] = datetime.now().isoformat()
        self.append_jsonl(self.errors_file, kwargs)
        self.stats["errors"] += 1

    def fetch(self, url: str) -> str | None:
        try:
            time.sleep(float(self.config.get("sleep_seconds", 1.0)))
            resp = self.session.get(url, timeout=int(self.config.get("request_timeout", 30)))
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            print(f"    ❌ fetch failed: {url} -> {exc}")
            self.log_error(url=url, error=str(exc), stage="fetch")
            return None

    @staticmethod
    def clean_text(text: str | None) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def normalize_ws(text: str | None) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def article_id_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1]
        slug = re.sub(r"\.(html|php|aspx?)$", "", slug, flags=re.I)
        return slug or path.strip("/").replace("/", "_")

    def listing_url_for_page(self, page: int) -> str:
        blog_path = self.config.get("sections", {}).get("blog", "/blog/")
        if page <= 1:
            return urljoin(self.base_url + "/", blog_path.lstrip("/"))
        template = self.config.get("pagination", {}).get("template", "/blog/page/{page}/")
        return urljoin(self.base_url + "/", template.format(page=page).lstrip("/"))

    def is_valid_article_link(self, href: str | None) -> bool:
        if not href:
            return False
        full_url = urljoin(self.base_url + "/", href)
        parsed = urlparse(full_url)
        if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
            return False

        path = parsed.path or ""
        allow_parts = self.config.get("listing_link_allow", ["/blog/"])
        deny_parts = self.config.get("listing_link_deny", [])

        if not any(part in path for part in allow_parts):
            return False
        if any(part in full_url for part in deny_parts):
            return False

        article_id = self.article_id_from_url(full_url)
        if not article_id or article_id in {"blog", "page"}:
            return False
        return True

    def extract_listing_links(self, soup: BeautifulSoup) -> list[str]:
        selector = self.config.get("selectors", {}).get("article_list", "h2 a, h3 a, article a[href*='/blog/']")
        links = []
        seen_on_page = set()

        for a in soup.select(selector):
            href = a.get("href")
            if not self.is_valid_article_link(href):
                continue
            full_url = urljoin(self.base_url + "/", href)
            article_id = self.article_id_from_url(full_url)
            if article_id in seen_on_page:
                continue
            seen_on_page.add(article_id)
            links.append(full_url)

        return links

    def parse_date(self, text: str) -> tuple[str, str]:
        raw = self.clean_text(text)
        if not raw:
            return "", ""

        normalized = raw.lower().replace("1er", "1")
        normalized = normalized.replace("août", "aout")
        normalized = normalized.replace("février", "fevrier")
        normalized = normalized.replace("décembre", "decembre")

        months = {
            "janvier": "01", "janv": "01", "jan": "01",
            "fevrier": "02", "février": "02", "fevr": "02", "févr": "02", "feb": "02",
            "mars": "03",
            "avril": "04", "avr": "04",
            "mai": "05",
            "juin": "06",
            "juillet": "07", "juil": "07",
            "aout": "08", "août": "08",
            "septembre": "09", "sept": "09", "sep": "09",
            "octobre": "10", "oct": "10",
            "novembre": "11", "nov": "11",
            "decembre": "12", "décembre": "12", "dec": "12",
            "january": "01", "jan": "01",
            "february": "02", "march": "03", "mar": "03",
            "april": "04", "apr": "04",
            "may": "05",
            "june": "06", "jun": "06",
            "july": "07", "jul": "07",
            "august": "08", "aug": "08",
            "september": "09",
            "october": "10",
            "november": "11",
            "december": "12",
        }

        m = re.search(r"(\d{1,2})\s+([a-zéûôîàèùç\.]+)\s+(\d{4})", normalized, flags=re.I)
        if m:
            day = m.group(1).zfill(2)
            month_name = m.group(2).strip(".")
            year = m.group(3)
            month = months.get(month_name)
            if month:
                return raw, f"{year}-{month}-{day}"

        m = re.search(r"([a-z\.]+)\s+(\d{1,2}),?\s+(\d{4})", normalized, flags=re.I)
        if m:
            month_name = m.group(1).strip(".")
            day = m.group(2).zfill(2)
            year = m.group(3)
            month = months.get(month_name)
            if month:
                return raw, f"{year}-{month}-{day}"

        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", normalized)
        if m:
            return raw, f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return raw, ""

    def clean_body_soup(self, node: BeautifulSoup) -> BeautifulSoup:
        for selector in self.config.get("remove_selectors", []):
            for tag in node.select(selector):
                tag.decompose()

        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        for tag in node.find_all(["button", "svg", "iframe"]):
            tag.decompose()

        return node

    def truncate_body_text(self, text: str) -> str:
        text = self.clean_text(text)
        if not text:
            return ""

        cut_positions = []
        for marker in self.config.get("end_markers", []):
            pos = text.find(marker)
            if pos > 0:
                cut_positions.append(pos)
        if cut_positions:
            text = text[: min(cut_positions)].strip()

        text = re.sub(r"(?:\n\s*\*\s*\*\s*\*\s*)+$", "", text).strip()
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

    def extract_title(self, soup: BeautifulSoup) -> str:
        selector = self.config.get("selectors", {}).get("title", "h1")
        el = soup.select_one(selector)
        if el:
            return self.normalize_ws(el.get_text(" ", strip=True))
        return ""

    def extract_author(self, soup: BeautifulSoup) -> str:
        selector = self.config.get("selectors", {}).get("author", "")
        if selector:
            for el in soup.select(selector):
                txt = self.normalize_ws(el.get_text(" ", strip=True))
                if txt:
                    return txt

        text = self.normalize_ws(soup.get_text("\n", strip=True))
        m = re.search(r"\b(web)\b\s+\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+\d{4}", text, flags=re.I)
        if m:
            return m.group(1)

        return self.config.get("source_name", "")

    def extract_date(self, soup: BeautifulSoup) -> tuple[str, str]:
        selector = self.config.get("selectors", {}).get("date", "time")
        for el in soup.select(selector):
            dt_attr = self.normalize_ws(el.get("datetime", ""))
            if re.match(r"\d{4}-\d{2}-\d{2}", dt_attr):
                return dt_attr[:10], dt_attr[:10]

            txt = self.normalize_ws(el.get_text(" ", strip=True))
            if re.search(r"\d{4}", txt):
                raw, iso = self.parse_date(txt)
                if iso:
                    return raw, iso

        text = self.normalize_ws(soup.get_text("\n", strip=True))
        m = re.search(r"\b(\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+\d{4})\b", text, flags=re.I)
        if m:
            raw, iso = self.parse_date(m.group(1))
            return raw, iso

        return "", ""

    def extract_categories(self, soup: BeautifulSoup) -> list[str]:
        selector = self.config.get("selectors", {}).get("category", "")
        results = []
        seen = set()

        if selector:
            for el in soup.select(selector):
                txt = self.normalize_ws(el.get_text(" ", strip=True))
                href = el.get("href", "")
                if not txt:
                    continue
                if href and "/blog/" not in href:
                    continue
                if txt.lower() in {"web", "lire l'article", "previous article", "next article"}:
                    continue
                if re.search(r"\d{4}", txt):
                    continue
                if re.fullmatch(r"\d+\s+min\s+read", txt, flags=re.I):
                    continue
                if txt not in seen:
                    seen.add(txt)
                    results.append(txt)

        if results:
            return results

        text = soup.get_text("\n", strip=True)
        title = self.extract_title(soup)
        if title:
            m = re.search(
                re.escape(title)
                + r"\s+([^\n]+)\s+(\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+\d{4})\s+([^\n]+)\s+\d+\s+min\s+read",
                text,
                flags=re.I,
            )
            if m:
                cat = self.clean_text(m.group(3))
                if cat:
                    return [cat]

        return []

    def extract_tags(self, soup: BeautifulSoup, categories: list[str]) -> list[str]:
        selector = self.config.get("selectors", {}).get("tags", "")
        tags = []
        seen = set()

        if selector:
            for el in soup.select(selector):
                txt = self.normalize_ws(el.get_text(" ", strip=True))
                if not txt or txt in seen:
                    continue
                seen.add(txt)
                tags.append(txt)

        if not tags and categories:
            tags = list(categories)

        return tags

    def extract_attachment(self, soup: BeautifulSoup, url: str) -> tuple[bool, str, str]:
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if re.search(r"\.pdf(?:$|\?)", href, flags=re.I):
                return True, urljoin(url, href), "pdf"
        return False, "", ""

    def _body_from_main_candidates(self, soup: BeautifulSoup) -> str:
        selector = self.config.get("selectors", {}).get("body", "article")
        candidates = soup.select(selector) or []
        best_text = ""

        for candidate in candidates:
            candidate_soup = BeautifulSoup(str(candidate), "lxml")
            candidate_soup = self.clean_body_soup(candidate_soup)
            text = candidate_soup.get_text("\n", strip=True)
            text = self.truncate_body_text(text)
            if len(text) > len(best_text):
                best_text = text

        return best_text

    def _body_from_page_text(self, soup: BeautifulSoup, title: str) -> str:
        page_text = self.clean_text(soup.get_text("\n", strip=True))
        if not page_text or not title:
            return ""

        start = page_text.find(title)
        if start >= 0:
            page_text = page_text[start + len(title):].lstrip()

        # Remove meta block: author, date, category, min read
        meta_pattern = r"^[^\n]{1,80}\n+\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+\d{4}\n+[^\n]{1,120}\n+\d+\s+min\s+read\n+"
        page_text = re.sub(meta_pattern, "", page_text, count=1, flags=re.I)

        page_text = self.truncate_body_text(page_text)
        return page_text.strip()

    def _clean_body_lines(self, text: str, title: str, category: str, publish_date: str) -> str:
        lines = [self.clean_text(line) for line in text.splitlines()]
        lines = [x for x in lines if x]

        noise_prefixes = set(self.config.get("body_noise_prefixes", []))
        minread_re = re.compile(r"^\d+\s+min\s+read$", flags=re.I)
        date_re = re.compile(r"^\d{1,2}\s+[A-Za-zÀ-ÿ\.]+\s+\d{4}$")

        cleaned = []
        for line in lines:
            if line == title:
                continue
            if line == publish_date:
                continue
            if category and line == category:
                continue
            if line in noise_prefixes:
                continue
            if line.lower() == "web":
                continue
            if minread_re.match(line):
                continue
            if date_re.match(line):
                continue
            cleaned.append(line)

        while cleaned and cleaned[0] in noise_prefixes:
            cleaned.pop(0)

        return "\n\n".join(cleaned).strip()

    def extract_body(self, soup: BeautifulSoup, title: str, category: str, publish_date: str) -> str:
        body = self._body_from_main_candidates(soup)
        body = self._clean_body_lines(body, title, category, publish_date)

        if not body or body.startswith("Contacter nos bénévoles"):
            fallback = self._body_from_page_text(soup, title)
            fallback = self._clean_body_lines(fallback, title, category, publish_date)
            if len(fallback) > len(body):
                body = fallback

        return self.truncate_body_text(body)

    def extract_article(self, html: str, url: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        title = self.extract_title(soup)
        author = self.extract_author(soup)
        publish_date, date_iso = self.extract_date(soup)
        categories = self.extract_categories(soup)
        category = categories[0] if categories else ""
        tags = self.extract_tags(soup, categories)
        body = self.extract_body(soup, title, category, publish_date)
        excerpt = body[:220].strip()
        if len(body) > 220:
            excerpt += "..."

        has_attachment, attachment_url, attachment_type = self.extract_attachment(soup, url)
        article_id = self.article_id_from_url(url)

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.language,
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": article_id,
            "article_url": url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": author,
            "author_type": "organization",
            "user_id": author.lower().replace(" ", "-") if author else "",
            "category": category,
            "tags": tags,
            "publish_date": publish_date,
            "date_iso": date_iso,
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
            "scraped_at": datetime.now().isoformat(),
        }

    def scrape(self) -> None:
        max_pages = int(self.config.get("pagination", {}).get("max_pages", 50))
        empty_pages = 0

        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)

        for page_number in range(1, max_pages + 1):
            listing_url = self.listing_url_for_page(page_number)
            print(f"📄 listing_page_number={page_number} url={listing_url}")

            html = self.fetch(listing_url)
            if not html:
                empty_pages += 1
                if empty_pages >= 1:
                    break
                continue

            soup = BeautifulSoup(html, "lxml")
            page_links = self.extract_listing_links(soup)
            page_threads = len(page_links)

            if page_threads == 0:
                print("   page_threads=0 new_threads=0 skipped_existing=0")
                empty_pages += 1
                if empty_pages >= int(self.config.get("stop_when_empty_pages", 1)):
                    break
                continue

            empty_pages = 0

            new_links = []
            skipped_existing = 0
            for article_url in page_links:
                article_id = self.article_id_from_url(article_url)
                if article_id in self.seen_ids:
                    skipped_existing += 1
                    self.stats["skipped"] += 1
                    continue
                new_links.append(article_url)

            print(
                f"   page_threads={page_threads} "
                f"new_threads={len(new_links)} "
                f"skipped_existing={skipped_existing}"
            )

            for idx, article_url in enumerate(new_links, start=1):
                article_id = self.article_id_from_url(article_url)
                print(f"   [{idx}/{len(new_links)}] {article_url}")
                article_html = self.fetch(article_url)
                if not article_html:
                    continue

                try:
                    item = self.extract_article(article_html, article_url)
                    if not item.get("title") or not item.get("body"):
                        raise ValueError("missing title/body after parsing")

                    self.append_jsonl(self.posts_file, item)
                    self.seen_ids.add(article_id)
                    self.stats["articles_scraped"] += 1
                    print(
                        f"      saved_words={item['word_count']} "
                        f"date={item['date_iso'] or item['publish_date'] or '-'} "
                        f"category={item['category'] or '-'} "
                        f"title={item['title'][:60]}"
                    )

                    if self.stats["articles_scraped"] % 10 == 0:
                        self._save_resume()
                        print(f"   💾 Progress saved: {self.stats['articles_scraped']} articles")
                except Exception as exc:
                    print(f"      ❌ parse failed: {exc}")
                    self.log_error(url=article_url, article_id=article_id, error=str(exc), stage="parse")

        self._save_resume()
        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.posts_file}")
        print(f"errors_file={self.errors_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="EndoFrance BS4 scraper")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    args = parser.parse_args()

    scraper = EndoFranceScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()
