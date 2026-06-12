import os
import re
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "configs", "SRC016.json")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config(CONFIG_PATH)


def ensure_parent_dir(filepath: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    clean_query = "&".join(
        p for p in parts.query.split("&")
        if p and not p.lower().startswith("sid=")
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, ""))


def absolute_url(base_url: str, url: str) -> str:
    return normalize_url(urljoin(base_url, url))


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def text_to_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    s = value.strip().upper().replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([KM]?)$", s)
    if not m:
        digits = re.sub(r"[^\d]", "", s)
        return int(digits) if digits else None
    num = float(m.group(1))
    suffix = m.group(2)
    if suffix == "K":
        num *= 1000
    elif suffix == "M":
        num *= 1000000
    return int(num)


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TapatalkOfflineScraper:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.output_file = os.path.join(BASE_DIR, config["outputs"]["final_jsonl"])
        self.error_file = os.path.join(BASE_DIR, config["outputs"]["errors_jsonl"])
        self.todo_file = os.path.join(BASE_DIR, config["outputs"]["threads_todo_jsonl"])

        ensure_parent_dir(self.output_file)
        ensure_parent_dir(self.error_file)
        ensure_parent_dir(self.todo_file)

        self.resume_mode = bool(config.get("resume_mode", True))
        self.overwrite_output = bool(config.get("overwrite_output", False))
        self.base_url = config.get("base_url", "https://www.tapatalk.com")
        self.saved_root = Path(BASE_DIR) / config["saved_html_root"]
        self.existing_thread_ids = self.load_existing_thread_ids()

    def load_existing_thread_ids(self) -> Set[str]:
        existing: Set[str] = set()
        if not self.resume_mode:
            return existing
        if not os.path.exists(self.output_file):
            return existing

        with open(self.output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    thread_id = str(row.get("thread_id", "")).strip()
                    if thread_id:
                        existing.add(thread_id)
                except Exception:
                    continue
        return existing

    def write_jsonl(self, filepath: str, row: dict) -> None:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_error(self, payload: dict) -> None:
        payload = deepcopy(payload)
        payload["logged_at"] = now_ts()
        self.write_jsonl(self.error_file, payload)

    def read_local_html(self, file_path: Path) -> str:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def extract_thread_id_from_url(self, url: str) -> str:
        url = normalize_url(url)
        m = re.search(r"-t(\d+)", url)
        return m.group(1) if m else ""

    def extract_user_id_from_href(self, href: str) -> Optional[str]:
        if not href:
            return None
        m = re.search(r"[?&]u=(\d+)", href)
        return m.group(1) if m else None

    def find_listing_container(self, title_anchor: Tag) -> Optional[Tag]:
        node = title_anchor
        for _ in range(10):
            if not isinstance(node, Tag):
                break
            text = node.get_text(" ", strip=True)
            if "Replies:" in text and "Views:" in text:
                return node
            node = node.parent
        return title_anchor.parent if isinstance(title_anchor.parent, Tag) else None

    def extract_listing_meta(self, container: Tag) -> dict:
        text = clean_text(container.get_text("\n", strip=True))
        lines = [clean_text(x) for x in text.split("\n") if clean_text(x)]

        replies_count = None
        views_count = None
        thread_starter = ""
        thread_starter_id = ""
        listing_author = ""
        listing_author_id = ""
        last_message_author = ""
        last_message_author_id = ""
        last_message_date = ""

        m_rep = re.search(r"Replies:\s*([0-9.,KkMm]+)", text)
        if m_rep:
            replies_count = text_to_int(m_rep.group(1))

        m_views = re.search(r"Views:\s*([0-9.,KkMm]+)", text)
        if m_views:
            views_count = text_to_int(m_views.group(1))

        by_line = next((ln for ln in lines if ln.lower().startswith("by ")), "")
        if by_line:
            m = re.match(r"by\s+(.+?)\s*»\s*(.+)$", by_line, flags=re.I)
            if m:
                thread_starter = clean_text(m.group(1))
                if thread_starter:
                    thread_starter_id = thread_starter

        anchors = container.find_all("a", href=True)
        visible_people = []
        for a in anchors:
            name = clean_text(a.get_text(" ", strip=True))
            href = a.get("href", "")
            if not name:
                continue
            if "-t" in href:
                continue
            uid = self.extract_user_id_from_href(href)
            visible_people.append((name, uid or name))

        if visible_people:
            if not thread_starter:
                thread_starter = visible_people[0][0]
                thread_starter_id = visible_people[0][1]
            listing_author = thread_starter
            listing_author_id = thread_starter_id
            last_message_author = visible_people[-1][0]
            last_message_author_id = visible_people[-1][1]

        iso_dates = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?", text)
        if iso_dates:
            last_message_date = iso_dates[-1]

        return {
            "replies_count": replies_count if replies_count is not None else 0,
            "views_count": views_count,
            "thread_starter": thread_starter,
            "thread_starter_id": thread_starter_id,
            "listing_author": listing_author,
            "listing_author_id": listing_author_id,
            "last_message_author": last_message_author,
            "last_message_author_id": last_message_author_id,
            "last_message_date": last_message_date
        }

    def parse_listing_threads(self, soup: BeautifulSoup, category: dict) -> List[dict]:
        threads = []
        seen_thread_ids = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "-t" not in href:
                continue

            thread_url = absolute_url(self.base_url, href)
            thread_id = self.extract_thread_id_from_url(thread_url)
            if not thread_id or thread_id in seen_thread_ids:
                continue

            title = clean_text(a.get_text(" ", strip=True))
            if not title or title in {"Next", "Previous"}:
                continue

            container = self.find_listing_container(a)
            if container is None:
                continue

            meta = self.extract_listing_meta(container)

            seen_thread_ids.add(thread_id)
            threads.append({
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": title,
                "thread_title_detail": title,
                "thread_url": thread_url,
                "listing_category": category["listing_category"],
                "category_id": category.get("category_id"),
                "category_name": category.get("category_name"),
                "category_slug": category.get("category_slug"),
                "thread_starter": meta.get("thread_starter", ""),
                "thread_starter_id": meta.get("thread_starter_id", ""),
                "listing_author": meta.get("listing_author", ""),
                "listing_author_id": meta.get("listing_author_id", ""),
                "replies_count": meta.get("replies_count"),
                "views_count": meta.get("views_count"),
                "last_message_date": meta.get("last_message_date", ""),
                "last_message_author": meta.get("last_message_author", ""),
                "last_message_author_id": meta.get("last_message_author_id", "")
            })

        return threads

    def collect_post_blocks(self, soup: BeautifulSoup) -> List[Tag]:
        blocks: List[Tag] = []
        for a in soup.find_all("a", href=True):
            txt = clean_text(a.get_text(" ", strip=True))
            if not re.fullmatch(r"#\d+", txt):
                continue

            block = self.find_post_container(a)
            if block is not None:
                blocks.append(block)

        unique = []
        seen_ids = set()
        for b in blocks:
            key = id(b)
            if key not in seen_ids:
                seen_ids.add(key)
                unique.append(b)
        return unique

    def find_post_container(self, anchor_tag: Tag) -> Optional[Tag]:
        node = anchor_tag
        for _ in range(10):
            if not isinstance(node, Tag):
                break
            text = node.get_text("\n", strip=True)
            if re.search(r"#\d+", text) and re.search(r"\d{4}-\d{2}-\d{2}T", text):
                return node
            node = node.parent
        return anchor_tag.parent if isinstance(anchor_tag.parent, Tag) else None

    def extract_post_body(self, block: Tag, author: str, date_text: str, date_iso: str) -> str:
        text = clean_text(block.get_text("\n", strip=True))
        lines = [clean_text(x) for x in text.split("\n") if clean_text(x)]
        body_lines: List[str] = []

        skip_values = set(filter(None, [author, date_text, date_iso, "Read more posts"]))

        for ln in lines:
            if re.fullmatch(r"#\d+", ln):
                continue
            if ln in skip_values:
                continue
            if re.fullmatch(r"[\d,]+(?:\s+\d+)?", ln):
                continue
            if re.search(r"^\d{4}-\d{2}-\d{2}T", ln):
                continue
            if re.search(r"^[A-Z][a-z]{2}\s+\d{2},\s+\d{4}$", ln):
                continue
            if re.fullmatch(r"\d+\s+posts?", ln, flags=re.I):
                continue
            if re.search(r"^(Share|Back to top|Display mode|Font Size|DONE)$", ln, flags=re.I):
                continue
            body_lines.append(ln)

        body = "\n".join(body_lines)
        body = re.sub(r"\n?(Read more posts.*)$", "", body, flags=re.I | re.S)
        return clean_text(body)

    def extract_post_from_block(self, block: Tag, thread_id: str, thread_url: str, post_number_default: int) -> dict:
        text = clean_text(block.get_text("\n", strip=True))
        lines = [clean_text(x) for x in text.split("\n") if clean_text(x)]

        anchor_tag = None
        post_number = post_number_default
        for a in block.find_all("a", href=True):
            t = clean_text(a.get_text(" ", strip=True))
            if re.fullmatch(r"#\d+", t):
                anchor_tag = a
                post_number = int(t.replace("#", ""))
                break

        anchor_id = anchor_tag.get_text(strip=True) if anchor_tag else f"#{post_number}"

        date_iso = ""
        m_iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?)", text)
        if m_iso:
            date_iso = m_iso.group(1)

        date_text = ""
        if m_iso:
            prefix = text[:m_iso.start()]
            m_date = re.search(r"([A-Z][a-z]{2}\s+\d{2},\s+\d{4})\s*$", prefix)
            if m_date:
                date_text = m_date.group(1)

        author = ""
        native_user_id = ""
        user_id = ""

        author_candidates: List[Tuple[str, str]] = []
        for a in block.find_all("a", href=True):
            name = clean_text(a.get_text(" ", strip=True))
            href = a.get("href", "")
            if not name:
                continue
            if re.fullmatch(r"#\d+", name):
                continue
            uid = self.extract_user_id_from_href(href)
            if uid:
                author_candidates.append((name, uid))

        if author_candidates:
            author, native_user_id = author_candidates[0]
            user_id = native_user_id or author

        if not author and lines:
            for ln in lines[:6]:
                if re.fullmatch(r"#\d+", ln):
                    continue
                if re.search(r"\d{4}-\d{2}-\d{2}T", ln):
                    continue
                if re.search(r"^[A-Z][a-z]{2}\s+\d{2},\s+\d{4}$", ln):
                    continue
                if re.fullmatch(r"[\d,]+", ln):
                    author = ln
                    user_id = ln
                    break

        body = self.extract_post_body(block, author, date_text, date_iso)

        likes_count = 0
        m_likes = re.search(r"Likes?:\s*(\d+)", text, flags=re.I)
        if m_likes:
            likes_count = int(m_likes.group(1))

        native_post_id = ""
        if anchor_tag and anchor_tag.get("href"):
            m = re.search(r"#p?(\d+)", anchor_tag.get("href", ""))
            if m:
                native_post_id = m.group(1)

        message_id = native_post_id or f"{thread_id}_{post_number}"
        post_url = f"{normalize_url(thread_url)}#{anchor_id.lstrip('#')}"

        return {
            "author": author,
            "user_id": user_id or author,
            "native_user_id": native_user_id or "",
            "date": date_text,
            "date_iso": date_iso,
            "body": body,
            "likes_count": likes_count,
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": message_id,
            "native_post_id": native_post_id or message_id,
            "anchor_id": anchor_id,
            "post_number": post_number,
            "type": "comment",
            "is_original_post": False,
            "post_id": message_id,
            "comment_id": message_id,
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": post_url
        }

    def scrape_saved_listing_pages(self, category: dict) -> List[dict]:
        listing_dir = self.saved_root / "listing" / category["category_slug"]
        if not listing_dir.exists():
            raise FileNotFoundError(f"Missing listing folder: {listing_dir}")

        threads = []
        page_files = sorted(list(listing_dir.glob("page_*.html")) + list(listing_dir.glob("page_*.txt")))
        if not page_files:
            raise FileNotFoundError(f"No listing files found in: {listing_dir}")

        for file_path in page_files:
            html = self.read_local_html(file_path)
            soup = self.soup(html)
            page_threads = self.parse_listing_threads(soup, category)
            page_threads_count = len(page_threads)
            threads.extend(page_threads)
            print(f"[listing-file] file={file_path.name} page_threads={page_threads_count}")

        dedup = {}
        for t in threads:
            dedup[t["thread_id"]] = t

        return list(dedup.values())

    def scrape_saved_thread(self, thread_meta: dict) -> dict:
        thread_id = thread_meta["thread_id"]
        thread_dir = self.saved_root / "threads" / thread_id
        if not thread_dir.exists():
            raise FileNotFoundError(f"Missing thread folder: {thread_dir}")

        page_files = sorted(list(thread_dir.glob("page_*.html")) + list(thread_dir.glob("page_*.txt")))
        if not page_files:
            raise FileNotFoundError(f"No thread files found in: {thread_dir}")

        posts = []

        for file_path in page_files:
            html = self.read_local_html(file_path)
            soup = self.soup(html)
            post_blocks = self.collect_post_blocks(soup)

            for block in post_blocks:
                post = self.extract_post_from_block(
                    block=block,
                    thread_id=thread_id,
                    thread_url=thread_meta["thread_url"],
                    post_number_default=len(posts) + 1
                )
                posts.append(post)

            print(f"[thread-file] thread_id={thread_id} file={file_path.name} file_posts={len(post_blocks)}")

        deduped: List[dict] = []
        seen = set()
        for p in sorted(posts, key=lambda x: (x.get("post_number", 0), x.get("message_id", ""))):
            key = (p.get("message_id"), p.get("post_number"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)

        if not deduped:
            raise ValueError(f"No posts found in saved thread {thread_id}")

        opening_post = deepcopy(deduped[0])
        opening_post["type"] = "post"
        opening_post["is_original_post"] = True
        opening_post["comment_id"] = ""

        replies = []
        for p in deduped[1:]:
            item = deepcopy(p)
            item["type"] = "comment"
            item["is_original_post"] = False
            replies.append(item)

        likes_total = sum(int(x.get("likes_count", 0) or 0) for x in deduped)
        last_message = deduped[-1]

        return {
            "source_id": self.config["source_id"],
            "source_mode": self.config["source_mode"],
            "thread_id": thread_id,
            "thread_url_id": thread_meta.get("thread_url_id") or thread_id,
            "thread_title": thread_meta.get("thread_title", ""),
            "thread_title_detail": thread_meta.get("thread_title_detail", ""),
            "thread_url": thread_meta.get("thread_url", ""),
            "listing_category": thread_meta.get("listing_category"),
            "category_id": thread_meta.get("category_id"),
            "category_name": thread_meta.get("category_name"),
            "category_slug": thread_meta.get("category_slug"),
            "thread_starter": thread_meta.get("thread_starter") or opening_post.get("author", ""),
            "thread_starter_id": thread_meta.get("thread_starter_id") or opening_post.get("native_user_id") or opening_post.get("author", ""),
            "opening_post_id": opening_post.get("post_id", ""),
            "opening_message_id": opening_post.get("message_id", ""),
            "opening_post_date": opening_post.get("date_iso") or opening_post.get("date", ""),
            "opening_post_body": opening_post.get("body", ""),
            "listing_author": thread_meta.get("listing_author") or opening_post.get("author", ""),
            "listing_author_id": thread_meta.get("listing_author_id") or opening_post.get("native_user_id") or opening_post.get("author", ""),
            "replies_count": len(replies),
            "views_count": thread_meta.get("views_count"),
            "last_message_date": last_message.get("date_iso") or last_message.get("date", ""),
            "last_message_author": last_message.get("author", ""),
            "last_message_author_id": last_message.get("native_user_id") or last_message.get("author", ""),
            "last_message_id": last_message.get("message_id", ""),
            "last_page": len(page_files),
            "thread_pages_count": len(page_files),
            "posts_count": len(deduped),
            "comments_count": len(replies),
            "likes_total": likes_total,
            "post": opening_post,
            "replies": replies
        }

    def write_todo_if_missing(self, thread_meta: dict) -> None:
        self.write_jsonl(self.todo_file, {
            "source_id": self.config["source_id"],
            "thread_id": thread_meta["thread_id"],
            "thread_title": thread_meta.get("thread_title", ""),
            "thread_url": thread_meta.get("thread_url", ""),
            "category_slug": thread_meta.get("category_slug", ""),
            "status": "missing_saved_thread_html"
        })

    def run(self) -> None:
        if self.overwrite_output and not self.resume_mode:
            for fp in [self.output_file, self.error_file, self.todo_file]:
                if os.path.exists(fp):
                    os.remove(fp)

        categories = self.config.get("categories", [])
        for category in categories:
            try:
                threads = self.scrape_saved_listing_pages(category)
                print(f"[listing-summary] category={category['category_slug']} unique_threads={len(threads)}")

                new_threads = 0
                skipped_existing = 0
                missing_thread_html = 0

                for thread_meta in threads:
                    thread_id = thread_meta["thread_id"]

                    if self.resume_mode and thread_id in self.existing_thread_ids:
                        skipped_existing += 1
                        continue

                    thread_dir = self.saved_root / "threads" / thread_id
                    if not thread_dir.exists():
                        self.write_todo_if_missing(thread_meta)
                        missing_thread_html += 1
                        print(f"[thread-missing] thread_id={thread_id} title={thread_meta.get('thread_title', '')}")
                        continue

                    try:
                        row = self.scrape_saved_thread(thread_meta)
                        self.write_jsonl(self.output_file, row)
                        self.existing_thread_ids.add(thread_id)
                        new_threads += 1
                        messages_scraped = 1 + len(row.get("replies", []))
                        print(f"[thread] thread_id={thread_id} messages_comments_scraped={messages_scraped}")
                    except Exception as exc:
                        self.write_error({
                            "source_id": self.config["source_id"],
                            "category_slug": category.get("category_slug"),
                            "thread_id": thread_id,
                            "thread_url": thread_meta.get("thread_url"),
                            "error": str(exc)
                        })
                        print(f"[error] thread_id={thread_id} error={exc}")

                print(
                    f"[category-done] category={category['category_slug']} "
                    f"new_threads={new_threads} "
                    f"skipped_existing={skipped_existing} "
                    f"missing_thread_html={missing_thread_html}"
                )

            except Exception as exc:
                self.write_error({
                    "source_id": self.config["source_id"],
                    "category_slug": category.get("category_slug"),
                    "error": str(exc)
                })
                print(f"[category-error] category={category.get('category_slug')} error={exc}")


if __name__ == "__main__":
    scraper = TapatalkOfflineScraper(CONFIG)
    scraper.run()
