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


SECTION_ORDER = [
    "Alimentation",
    "Gestion de la douleur",
    "Pratique d'une activité physique",
    "Le Yoga",
    "Le CBD",
]

SECTION_LINKS = {
    "Alimentation": "/alimentation/",
    "Gestion de la douleur": "/gestiondouleur/",
    "Pratique d'une activité physique": "",
    "Le Yoga": "/soulager-yoga/",
    "Le CBD": "/endometriose-cbd/",
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
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Referer": self.base_url + "/",
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

    def fetch(self, url):
        time.sleep(self.sleep_seconds)
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            self.log_error({"source_id": self.source_id, "url": url, "error": str(e)})
            print(f"   ❌ fetch failed: {url} -> {e}")
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
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if path in ("", "/"):
            return "home"
        slug = path.split("/")[-1]
        slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
        return slug

    def first_main(self, soup):
        for sel in ["main", ".site-main", ".elementor-location-single", ".elementor", "#content", "body"]:
            el = soup.select_one(sel)
            if el:
                return el
        return soup

    def clean_node(self, node):
        node = BeautifulSoup(str(node), "lxml")
        for sel in self.config.get("remove_selectors", []):
            for el in node.select(sel):
                el.decompose()
        for comment in node.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        return node

    def normalize_image(self, src):
        if not src:
            return ""
        return urljoin(self.base_url + "/", src)

    def visible_images(self, node):
        urls = []
        seen = set()
        for img in node.select("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            full = self.normalize_image(src)
            if not full:
                continue
            lower = full.lower()
            alt = (img.get("alt") or "").lower()
            if any(x in lower for x in ["/logo", "logo-", "cropped-", "favicon", "icon"]) and "yoga" not in lower:
                continue
            if any(x in alt for x in ["logo", "instagram", "facebook", "youtube", "marraine", "parrain", "partner"]):
                continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def extract_homepage_sections(self, soup, url):
        node = self.clean_node(self.first_main(soup))
        text = self.clean_text(node.get_text("\n", strip=True))

        start_marker = "Alimentation"
        end_marker = "### Nos événements"
        if start_marker in text:
            text = text[text.find(start_marker):]
        if end_marker in text:
            text = text[:text.find(end_marker)].strip()

        blocks = []
        for i, name in enumerate(SECTION_ORDER):
            pattern = re.escape(name) + r"\s*(.*?)\s*(?=" + (
                re.escape(SECTION_ORDER[i + 1]) if i + 1 < len(SECTION_ORDER) else r"$"
            ) + r")"
            m = re.search(pattern, text, re.S)
            desc = self.clean_text(m.group(1)) if m else ""
            blocks.append((name, desc))

        imgs = self.visible_images(node)
        content_imgs = []
        for img in imgs:
            low = img.lower()
            if any(x in low for x in ["food", "egg", "vegetable", "pain", "yoga", "cbd", "bottle", "cann", "sport", "kettle", "girl", "woman"]):
                content_imgs.append(img)
        if len(content_imgs) < 5:
            # fall back to first non-logo visuals from the main content area
            content_imgs = imgs[:10]

        image_map = {}
        for idx, name in enumerate(SECTION_ORDER):
            image_map[name] = content_imgs[idx] if idx < len(content_imgs) else ""

        page_sections = []
        body_parts = []
        for name, desc in blocks:
            link = SECTION_LINKS.get(name, "")
            full_link = urljoin(self.base_url + "/", link) if link else ""
            image_url = image_map.get(name, "")
            page_sections.append({
                "section_title": name,
                "description": desc,
                "en_savoir_plus_url": full_link,
                "image_url": image_url
            })
            line = name
            if desc:
                line += "\n" + desc
            if full_link:
                line += "\nEn savoir plus: " + full_link
            if image_url:
                line += "\nImage URL: " + image_url
            body_parts.append(line)

        body = "\n\n".join([x for x in body_parts if x.strip()]).strip()
        excerpt = body[:200] + "..." if len(body) > 200 else body

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": "home-mieux-vivre",
            "article_url": url,
            "article_type": "landing_page",
            "title": "Mieux vivre avec l'endométriose",
            "excerpt": excerpt,
            "body": body,
            "word_count": len(body.split()) if body else 0,
            "author": "",
            "author_type": "",
            "user_id": "",
            "category": "Nos conseils",
            "tags": [],
            "publish_date": "",
            "date_iso": "",
            "has_attachment": False,
            "attachment_url": "",
            "attachment_type": "",
            "section_titles": [x["section_title"] for x in page_sections],
            "en_savoir_plus_links": [x["en_savoir_plus_url"] for x in page_sections if x["en_savoir_plus_url"]],
            "image_urls": [x["image_url"] for x in page_sections if x["image_url"]],
            "page_sections": page_sections
        }

    def extract_page(self, soup, url):
        node = self.clean_node(self.first_main(soup))
        title = ""
        for sel in ["h1.entry-title", ".entry-title", "main h1", "h1"]:
            el = node.select_one(sel)
            if el:
                title = self.clean_text(el.get_text(" ", strip=True))
                break

        text = self.clean_text(node.get_text("\n", strip=True))
        stop_markers = [
            "### Nos événements",
            "Nos événements",
            "Notre actualité",
            "Suivre sur Instagram",
            "Nos partenaires",
            "Copyright ©"
        ]
        for marker in stop_markers:
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx].strip()

        # remove repeated nav/header text before real title
        if title and title in text:
            text = text[text.find(title):].strip()

        image_urls = self.visible_images(node)
        has_attachment = False
        attachment_url = ""
        attachment_type = ""
        for a in node.select("a[href]"):
            href = a.get("href", "")
            full = urljoin(url, href)
            if re.search(r"\.pdf($|[?#])", full, re.I):
                has_attachment = True
                attachment_url = full
                attachment_type = "pdf"
                break

        excerpt = text[:200] + "..." if len(text) > 200 else text

        return {
            "source_id": self.source_id,
            "source_mode": "association",
            "source_name": self.config.get("source_name", ""),
            "source_country": self.config.get("country", ""),
            "source_language": self.config.get("language", ""),
            "source_type": self.config.get("source_type", "news_blog"),
            "article_id": self.slug_from_url(url),
            "article_url": url,
            "article_type": "article",
            "title": title,
            "excerpt": excerpt,
            "body": text,
            "word_count": len(text.split()) if text else 0,
            "author": "",
            "author_type": "",
            "user_id": "",
            "category": "Nos conseils",
            "tags": [],
            "publish_date": "",
            "date_iso": "",
            "has_attachment": has_attachment,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type,
            "section_titles": [],
            "en_savoir_plus_links": [],
            "image_urls": image_urls[:10]
        }

    def run(self):
        print(f"🚀 Starting {self.source_id} scraper")
        print(f"🌐 URL: {self.base_url}")
        print(f"💾 Output: {self.articles_file}")
        print("=" * 60)

        urls = [urljoin(self.base_url + "/", x) for x in self.config.get("seed_urls", [])]
        total = len(urls)

        for idx, url in enumerate(urls, start=1):
            article_id = "home-mieux-vivre" if url.rstrip("/") == self.base_url else self.slug_from_url(url)
            if article_id in self.existing_ids:
                self.stats["skipped_existing"] += 1
                continue

            print(f"[{idx}/{total}] {url}")
            html = self.fetch(url)
            if not html:
                continue

            try:
                soup = BeautifulSoup(html, "lxml")
                if url.rstrip("/") == self.base_url:
                    row = self.extract_homepage_sections(soup, url)
                else:
                    row = self.extract_page(soup, url)

                self.write_article(row)
                self.existing_ids.add(row["article_id"])
                self.stats["articles_scraped"] += 1
                print(f"   saved_words={row['word_count']} title={row['title'][:80]}")
            except Exception as e:
                self.log_error({
                    "source_id": self.source_id,
                    "url": url,
                    "article_id": article_id,
                    "error": str(e)
                })
                print(f"   ❌ parse failed: {e}")

        print("=" * 60)
        print("✅ Done")
        print(f"articles_scraped={self.stats['articles_scraped']}")
        print(f"skipped_existing={self.stats['skipped_existing']}")
        print(f"errors={self.stats['errors']}")
        print(f"articles_file={self.articles_file}")
        print(f"errors_file={self.errors_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    Scraper(args.config).run()
