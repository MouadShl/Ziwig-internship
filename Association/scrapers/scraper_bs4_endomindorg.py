#!/usr/bin/env python3
import argparse
import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class EndomindScraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.base_url = self.config["base_url"].rstrip("/")
        self.language = self.config.get("language", "fr")
        self.source_name = self.config.get("source_name", "")

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

    @staticmethod
    def slugify(text: str) -> str:
        text = unicodedata.normalize("NFKD", text or "")
        text = text.encode("ascii", "ignore").decode("ascii")
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        return text or "item"

    def parse_date(self, text: str) -> tuple[str, str]:
        raw = self.clean_text(text)
        if not raw:
            return "", ""

        normalized = raw.lower()
        normalized = normalized.replace("1er", "1")
        normalized = normalized.replace("août", "aout")
        normalized = normalized.replace("février", "fevrier")
        normalized = normalized.replace("décembre", "decembre")
        normalized = normalized.replace("à", "a")

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
        }

        m = re.search(r"(?:^|\b)(\d{1,2})\s+([a-zéûôîàèùç\.]+)\s+(\d{4})(?:\b|$)", normalized, flags=re.I)
        if m:
            day = m.group(1).zfill(2)
            month_name = m.group(2).strip(".")
            year = m.group(3)
            month = months.get(month_name)
            if month:
                return raw, f"{year}-{month}-{day}"

        m = re.search(r"(?:^|\b)(\d{4})-(\d{2})-(\d{2})(?:\b|$)", normalized)
        if m:
            return raw, f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return raw, ""

    def looks_like_date(self, text: str) -> bool:
        _, iso = self.parse_date(text)
        return bool(iso)

    def remove_noise(self, soup: BeautifulSoup) -> BeautifulSoup:
        for selector in self.config.get("remove_selectors", []):
            for tag in soup.select(selector):
                tag.decompose()

        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        return soup

    def get_content_root(self, soup: BeautifulSoup) -> Tag:
        root = soup.find("main")
        if root is None:
            root = soup.body
        if root is None:
            root = soup
        return root

    def heading_text(self, tag: Tag) -> str:
        return self.normalize_ws(tag.get_text(" ", strip=True))

    def find_section_heading(self, root: Tag, section_title: str) -> Tag | None:
        target = self.normalize_ws(section_title).lower()
        for tag in root.find_all(re.compile(r"^h[1-6]$")):
            if self.heading_text(tag).lower() == target:
                return tag
        return None

    def should_skip_title(self, title: str, section_title: str) -> bool:
        if not title:
            return True
        normalized = self.normalize_ws(title)
        if normalized.lower() == self.normalize_ws(section_title).lower():
            return True
        for skip in self.config.get("skip_titles", []):
            if normalized.lower() == self.normalize_ws(skip).lower():
                return True
        return False

    def text_and_links_between(self, start_tag: Tag, stop_tag: Tag | None) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        links: list[str] = []
        last_line = ""

        skip_parent_names = {"script", "style", "noscript", "svg", "button", "iframe"}
        heading_names = {"h1", "h2", "h3", "h4", "h5", "h6"}

        for el in start_tag.next_elements:
            if stop_tag is not None and el is stop_tag:
                break

            if isinstance(el, Tag):
                if el.name in {"a"}:
                    href = self.normalize_ws(el.get("href", ""))
                    if href:
                        links.append(href)
                continue

            if not isinstance(el, NavigableString):
                continue

            parent = el.parent
            if parent is None:
                continue
            if getattr(parent, "name", None) in skip_parent_names:
                continue
            if getattr(parent, "name", None) in heading_names:
                continue

            text = self.normalize_ws(str(el))
            if not text:
                continue
            if text == last_line:
                continue
            last_line = text
            lines.append(text)

        return lines, links

    def strip_footer_lines(self, lines: list[str]) -> list[str]:
        footer_markers = [self.normalize_ws(x).lower() for x in self.config.get("footer_markers", [])]
        body_noise = [self.normalize_ws(x).lower() for x in self.config.get("body_noise_prefixes", [])]

        result = []
        for line in lines:
            low = self.normalize_ws(line).lower()
            if low in body_noise:
                continue
            if any(marker in low for marker in footer_markers):
                break
            if low == "bottom of page":
                break
            result.append(line)
        return result

    def extract_hashtags(self, lines: list[str], category: str) -> list[str]:
        tags = []
        seen = set()
        for line in lines:
            for match in re.findall(r"#([A-Za-zÀ-ÿ0-9_\-]+)", line):
                tag = self.clean_text(match)
                if not tag:
                    continue
                key = tag.lower()
                if key in seen:
                    continue
                seen.add(key)
                tags.append(tag)
        if not tags and category:
            tags = [category]
        return tags

    def pick_attachment(self, links: list[str], page_url: str) -> tuple[bool, str, str]:
        for href in links:
            full = urljoin(page_url, href)
            if re.search(r"\.pdf(?:$|\?)", full, flags=re.I):
                return True, full, "pdf"
        return False, "", ""

    def refine_title_and_body(self, title: str, lines: list[str]) -> tuple[str, list[str]]:
        lines = [self.clean_text(x) for x in lines if self.clean_text(x)]
        title = self.clean_text(title)
        generic_titles = {self.normalize_ws(x).lower() for x in self.config.get("generic_titles", [])}

        while lines and self.normalize_ws(lines[0]).lower() == title.lower():
            lines.pop(0)

        if self.normalize_ws(title).lower() in generic_titles and lines:
            first = lines[0]
            if first and not self.should_skip_title(first, "") and not self.looks_like_date(first):
                if first.startswith("«") or first.startswith('"'):
                    title = first
                else:
                    title = f"Étude - {first}"
                lines.pop(0)

        if lines:
            first = lines[0]
            short_sub = (
                len(first) <= 60
                and len(first.split()) <= 8
                and not self.looks_like_date(first)
                and not first.endswith((".", "!", "?", ":"))
                and self.normalize_ws(first).lower() not in generic_titles
                and self.normalize_ws(first).lower() not in {
                    self.normalize_ws(x).lower() for x in self.config.get("skip_titles", [])
                }
            )
            if short_sub and first.lower() not in title.lower():
                title = f"{title} - {first}"
                lines.pop(0)

        return self.clean_text(title), lines

    def clean_body_lines(self, lines: list[str], title: str) -> list[str]:
        cleaned = []
        low_title = self.normalize_ws(title).lower()
        for line in lines:
            text = self.clean_text(line)
            if not text:
                continue
            low = self.normalize_ws(text).lower()
            if low == low_title:
                continue
            if self.should_skip_title(text, ""):
                continue
            cleaned.append(text)

        while cleaned and cleaned[0].lower() == "web":
            cleaned.pop(0)

        return self.strip_footer_lines(cleaned)

    def article_id_from_title(self, section_name: str, title: str) -> str:
        return f"{self.slugify(section_name)}-{self.slugify(title)}"

    def extract_section_items(self, html: str, section_cfg: dict, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        soup = self.remove_noise(soup)
        root = self.get_content_root(soup)

        section_title = section_cfg.get("section_title", section_cfg.get("name", ""))
        section_heading = self.find_section_heading(root, section_title)
        if section_heading is None:
            raise ValueError(f"Could not find section heading: {section_title}")

        headings = root.find_all(re.compile(r"^h[1-6]$"))
        try:
            start_idx = headings.index(section_heading)
        except ValueError as exc:
            raise ValueError(f"Section heading not found in heading list: {section_title}") from exc

        candidate_headings: list[Tag] = []
        for h in headings[start_idx + 1:]:
            title = self.heading_text(h)
            if self.should_skip_title(title, section_title):
                continue
            candidate_headings.append(h)

        items = []
        for idx, heading in enumerate(candidate_headings):
            next_heading = candidate_headings[idx + 1] if idx + 1 < len(candidate_headings) else None
            raw_title = self.heading_text(heading)
            lines, links = self.text_and_links_between(heading, next_heading)
            title, lines = self.refine_title_and_body(raw_title, lines)
            lines = self.clean_body_lines(lines, title)

            if not title:
                continue
            if not lines:
                continue

            body = "\n\n".join(lines).strip()
            if not body:
                continue

            publish_date = ""
            date_iso = ""
            for line in lines[:5]:
                raw_date, iso = self.parse_date(line)
                if iso:
                    publish_date = raw_date
                    date_iso = iso
                    break

            tags = self.extract_hashtags(lines, section_cfg.get("category", section_cfg.get("name", "")))
            has_attachment, attachment_url, attachment_type = self.pick_attachment(links, page_url)

            article_id = self.article_id_from_title(section_cfg.get("name", "section"), title)
            article_url = f"{page_url}#{self.slugify(title)}"
            excerpt = body[:220].strip()
            if len(body) > 220:
                excerpt += "..."

            item = {
                "source_id": self.source_id,
                "source_mode": "association",
                "source_name": self.source_name,
                "source_country": self.config.get("country", ""),
                "source_language": self.language,
                "source_type": self.config.get("source_type", "news_blog"),
                "article_id": article_id,
                "article_url": article_url,
                "article_type": "article",
                "title": title,
                "excerpt": excerpt,
                "body": body,
                "word_count": len(body.split()) if body else 0,
                "author": self.source_name,
                "author_type": "organization",
                "user_id": self.slugify(self.source_name),
                "category": section_cfg.get("category", section_cfg.get("name", "")),
                "tags": tags,
                "publish_date": publish_date,
                "date_iso": date_iso,
                "has_attachment": has_attachment,
                "attachment_url": attachment_url,
                "attachment_type": attachment_type,
                "scraped_at": datetime.now().isoformat(),
            }
            items.append(item)

        return items

    def scrape(self) -> None:
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.posts_file}")
        print("=" * 60)

        for section_cfg in self.config.get("sections", []):
            page_url = urljoin(self.base_url + "/", section_cfg["path"].lstrip("/"))
            print(f"📄 section={section_cfg.get('name', '')} url={page_url}")

            html = self.fetch(page_url)
            if not html:
                continue

            try:
                items = self.extract_section_items(html, section_cfg, page_url)
            except Exception as exc:
                print(f"   ❌ parse failed: {page_url} -> {exc}")
                self.log_error(url=page_url, error=str(exc), stage="parse")
                continue

            page_threads = len(items)
            new_items = [x for x in items if x["article_id"] not in self.seen_ids]
            new_threads = len(new_items)
            skipped_existing = page_threads - new_threads

            print(
                f"   page_threads={page_threads} "
                f"new_threads={new_threads} "
                f"skipped_existing={skipped_existing}"
            )

            for idx, item in enumerate(new_items, start=1):
                self.append_jsonl(self.posts_file, item)
                self.seen_ids.add(item["article_id"])
                self.stats["articles_scraped"] += 1
                print(
                    f"   [{idx}/{len(new_items)}] {item['title'][:80]}\n"
                    f"      saved_words={item['word_count']} publish_date={item['publish_date'][:30]}"
                )

                if self.stats["articles_scraped"] % 10 == 0:
                    self._save_resume()
                    print(f"   💾 Progress saved: {self.stats['articles_scraped']} articles")

            self.stats["skipped"] += skipped_existing

        self._save_resume()
        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped']}")
        print(f"errors={self.stats['errors']}")
        print(f"posts_file={self.posts_file}")
        print(f"errors_file={self.errors_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ENDOmind section scraper")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    scraper = EndomindScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()
