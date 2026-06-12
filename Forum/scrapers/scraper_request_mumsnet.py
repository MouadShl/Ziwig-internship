import os
import re
import json
import time
from copy import deepcopy
from datetime import datetime

import requests
from bs4 import BeautifulSoup


HEADER_RE = re.compile(
    r"^(?P<author>.+?)\s*·\s*(?P<date>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2})$"
)

NOISE_EXACT = {
    "Skip to content",
    "Original poster",
    "OP posts: See all",
    "See all",
    "Share",
    "Save",
    "Watch",
    "Watch this thread",
    "Start a new thread",
    "Flip",
    "Hide thread",
    "Hide shortcut buttons",
    "Add post",
    "Customise",
    "Getting started",
    "FAQ's",
    "Back to top",
    "Top",
    "Bottom",
    "Report",
    "Edit post",
    "PM",
    "Follow topic",
    "Start thread",
    "Active",
    "My feed",
    "I'm on",
    "I'm watching",
    "Saved",
    "Last hour",
    "Advanced search"
}

STOP_PREFIXES = (
    "New posts on this thread",
    "Back to top",
    "Get involved",
    "About us",
    "Download the Talk app",
    "© "
)

SKIP_LINE_PATTERNS = [
    re.compile(r"^\d+\s+replies$", re.I),
    re.compile(r"^page \d+ of \d+$", re.I),
]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def append_jsonl(path, item):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_existing_thread_ids(jsonl_path):
    existing = set()
    if not os.path.exists(jsonl_path):
        return existing

    with open(jsonl_path, "r", encoding="utf-8") as f:
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


def clean_text(value):
    if value is None:
        return ""
    value = str(value).replace("\r", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def html_to_text(html_value):
    if not html_value:
        return ""
    soup = BeautifulSoup(html_value, "html.parser")
    return clean_text(soup.get_text("\n", strip=True))


def iso_to_display(iso_value):
    if not iso_value:
        return ""
    try:
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_value


def uk_to_iso(date_str):
    if not date_str:
        return ""
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.isoformat()
        except Exception:
            pass
    return ""


def extract_category_slug_from_url(url):
    m = re.search(r"/talk/([^/]+)/", url or "")
    return m.group(1) if m else ""


def should_skip_line(line):
    if not line:
        return True
    if line in NOISE_EXACT:
        return True
    for prefix in STOP_PREFIXES:
        if line.startswith(prefix):
            return True
    for pattern in SKIP_LINE_PATTERNS:
        if pattern.match(line):
            return True
    return False


class MumsnetPageOneScraper:
    def __init__(self, config_path):
        self.config = load_json(config_path)

        self.source_id = self.config["source_id"]
        self.source_mode = self.config.get("source_mode", "forum_mumsnet_api_assisted_page1_only")
        self.base_url = self.config["base_url"].rstrip("/")
        self.search_query = self.config["search_query"]
        self.listing_api_url = self.config["listing_api_url"]
        self.thread_api_url_template = self.config["thread_api_url_template"]
        self.listing_post_body_template = self.config["listing_post_body"]

        request_cfg = self.config.get("request", {})
        self.timeout = int(request_cfg.get("timeout_seconds", 30))
        self.sleep_seconds = float(request_cfg.get("sleep_seconds", 1.0))
        self.headers = request_cfg.get("headers", {})

        pagination_cfg = self.config.get("pagination", {})
        self.page_only = int(pagination_cfg.get("page_only", 1))
        self.max_thread_pages = int(pagination_cfg.get("max_thread_pages", 100))

        output_cfg = self.config["output"]
        self.output_dir = output_cfg["dir"]
        self.posts_path = os.path.join(self.output_dir, output_cfg["posts_file"])
        self.errors_path = os.path.join(self.output_dir, output_cfg["errors_file"])

        ensure_dir(self.output_dir)

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.existing_thread_ids = read_existing_thread_ids(self.posts_path)

    def log_error(self, payload):
        append_jsonl(self.errors_path, payload)

    def post_json(self, url, payload):
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_json(self, url):
        headers = dict(self.headers)
        headers["Accept"] = "application/json"
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_soup(self, url):
        headers = dict(self.headers)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def fetch_page_one_records(self):
        payload = deepcopy(self.listing_post_body_template)
        payload["query"] = self.search_query
        payload["page"] = self.page_only

        response_json = self.post_json(self.listing_api_url, payload)
        data = response_json.get("data") or []

        records = []
        page_threads = 0
        new_threads = 0
        skipped_existing = 0

        for item in data:
            thread_id = str(item.get("thread_id") or item.get("id") or "").strip()
            if not thread_id:
                continue

            page_threads += 1

            record = {
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_url": clean_text(item.get("url", "")),
                "thread_title": clean_text((item.get("title") or {}).get("raw", "")),
                "listing_author": clean_text(item.get("username", "")),
                "listing_author_id": clean_text(item.get("username", "")),
                "listing_category": clean_text((item.get("topic") or {}).get("name", "")),
                "category_name": clean_text((item.get("topic") or {}).get("name", "")),
                "category_slug": extract_category_slug_from_url((item.get("topic") or {}).get("url", "")),
                "listing_date_iso": clean_text(item.get("date", "")),
                "replies_count": int(item.get("replies_count") or 0),
                "opening_post_body_from_listing": clean_text((item.get("body") or {}).get("raw", ""))
            }
            records.append(record)

            if thread_id in self.existing_thread_ids:
                skipped_existing += 1
            else:
                new_threads += 1

        print(
            f"[listing] page={self.page_only} "
            f"page_threads={page_threads} "
            f"new_threads={new_threads} "
            f"skipped_existing={skipped_existing}"
        )
        return records

    def fetch_thread_detail(self, thread_id):
        url = self.thread_api_url_template.format(thread_id=thread_id)
        payload = self.get_json(url)
        return payload.get("data") or {}

    def extract_thread_pages_count(self, soup):
        max_page = 1
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                max_page = max(max_page, int(m.group(1)))

            txt = clean_text(a.get_text(" ", strip=True))
            if txt.isdigit():
                max_page = max(max_page, int(txt))
        return max_page

    def parse_visible_posts_from_soup(self, soup, thread_title):
        raw_lines = soup.get_text("\n").splitlines()
        lines = [clean_text(x) for x in raw_lines]
        lines = [x for x in lines if x]

        start_idx = 0
        for i, line in enumerate(lines):
            if thread_title and line == thread_title:
                start_idx = i + 1
                break

        usable = lines[start_idx:] if start_idx else lines

        blocks = []
        current = None

        for line in usable:
            if any(line.startswith(prefix) for prefix in STOP_PREFIXES):
                if current:
                    blocks.append(current)
                break

            if should_skip_line(line):
                continue

            m = HEADER_RE.match(line)
            if m:
                if current:
                    blocks.append(current)
                current = {
                    "author": clean_text(m.group("author")),
                    "date": clean_text(m.group("date")),
                    "date_iso": uk_to_iso(clean_text(m.group("date"))),
                    "body_lines": []
                }
                continue

            if current:
                if should_skip_line(line):
                    continue
                current["body_lines"].append(line)

        if current:
            blocks.append(current)

        posts = []
        for block in blocks:
            body_lines = []
            for line in block["body_lines"]:
                if should_skip_line(line):
                    continue
                body_lines.append(line)

            body = clean_text("\n".join(body_lines))
            if not body:
                continue

            posts.append({
                "author": block["author"],
                "user_id": block["author"],
                "native_user_id": "",
                "date": block["date"],
                "date_iso": block["date_iso"],
                "body": body,
                "likes_count": 0,
                "dislikes_count": 0
            })

        return posts

    def scrape_thread(self, listing_record):
        thread_id = listing_record["thread_id"]
        thread_url = listing_record["thread_url"]

        if not thread_url:
            self.log_error({
                "source_id": self.source_id,
                "stage": "thread_url_missing",
                "thread_id": thread_id
            })
            return None

        detail = {}
        try:
            detail = self.fetch_thread_detail(thread_id)
        except Exception as e:
            self.log_error({
                "source_id": self.source_id,
                "stage": "thread_api_request",
                "thread_id": thread_id,
                "thread_url": thread_url,
                "error": str(e)
            })

        thread_title = clean_text(detail.get("subject") or listing_record.get("thread_title") or "")
        thread_title_detail = thread_title

        op_body = html_to_text(detail.get("body", "")) or listing_record.get("opening_post_body_from_listing", "")
        op_author = clean_text(detail.get("username", "")) or listing_record.get("listing_author", "")
        op_date_iso = clean_text(detail.get("created_at", "")) or listing_record.get("listing_date_iso", "")
        op_date = iso_to_display(op_date_iso) if op_date_iso else ""

        topic = detail.get("topic") or {}
        category_name = clean_text(topic.get("name", "")) or listing_record.get("category_name", "")
        category_slug = clean_text(topic.get("slug", "")) or listing_record.get("category_slug", "")
        category_id = topic.get("category_id") if isinstance(topic, dict) else None
        if category_id == "":
            category_id = None

        replies_count = int(detail.get("replies_count") or listing_record.get("replies_count") or 0)

        op = {
            "author": op_author,
            "user_id": op_author,
            "native_user_id": "",
            "date": op_date,
            "date_iso": op_date_iso,
            "body": op_body,
            "likes_count": 0,
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": thread_id,
            "native_post_id": thread_id,
            "anchor_id": "",
            "post_number": 1,
            "type": "post",
            "is_original_post": True,
            "post_id": thread_id,
            "comment_id": "",
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": thread_url
        }

        replies = []
        seen_keys = set()
        if op["body"]:
            seen_keys.add((op["author"], op["date"], op["body"]))

        try:
            first_soup = self.get_soup(thread_url)
        except Exception as e:
            self.log_error({
                "source_id": self.source_id,
                "stage": "thread_html_request",
                "thread_id": thread_id,
                "thread_url": thread_url,
                "page_num": 1,
                "error": str(e)
            })
            return {
                "source_id": self.source_id,
                "source_mode": self.source_mode,
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": thread_title,
                "thread_title_detail": thread_title_detail,
                "thread_url": thread_url,
                "listing_category": category_name,
                "category_id": category_id,
                "category_name": category_name,
                "category_slug": category_slug,
                "thread_starter": op_author,
                "thread_starter_id": op_author,
                "opening_post_id": thread_id,
                "opening_message_id": thread_id,
                "opening_post_date": op_date,
                "opening_post_body": op_body,
                "listing_author": listing_record.get("listing_author", "") or op_author,
                "listing_author_id": listing_record.get("listing_author_id", "") or op_author,
                "replies_count": replies_count,
                "views_count": None,
                "last_message_date": op_date,
                "last_message_author": op_author,
                "last_message_author_id": op_author,
                "last_message_id": thread_id,
                "last_page": 1,
                "thread_pages_count": 1,
                "posts_count": 1,
                "comments_count": 0,
                "likes_total": 0,
                "post": op,
                "replies": []
            }

        thread_pages_count = self.extract_thread_pages_count(first_soup)
        thread_pages_count = max(1, min(thread_pages_count, self.max_thread_pages))
        last_page = 1

        for page_num in range(1, thread_pages_count + 1):
            page_url = thread_url if page_num == 1 else f"{thread_url}?page={page_num}"

            try:
                soup = first_soup if page_num == 1 else self.get_soup(page_url)
            except Exception as e:
                self.log_error({
                    "source_id": self.source_id,
                    "stage": "thread_html_request",
                    "thread_id": thread_id,
                    "thread_url": thread_url,
                    "page_num": page_num,
                    "page_url": page_url,
                    "error": str(e)
                })
                break

            visible_posts = self.parse_visible_posts_from_soup(soup, thread_title)
            page_new = 0

            for post in visible_posts:
                dedupe_key = (post["author"], post["date"], post["body"])
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                post_number = len(replies) + 2
                reply_obj = {
                    "author": post["author"],
                    "user_id": post["user_id"],
                    "native_user_id": "",
                    "date": post["date"],
                    "date_iso": post["date_iso"],
                    "body": post["body"],
                    "likes_count": 0,
                    "dislikes_count": 0,
                    "thread_id": thread_id,
                    "message_id": "",
                    "native_post_id": "",
                    "anchor_id": "",
                    "post_number": post_number,
                    "type": "comment",
                    "is_original_post": False,
                    "post_id": "",
                    "comment_id": "",
                    "reply_to_post_number": "",
                    "reply_to_post_id": "",
                    "post_url": page_url
                }
                replies.append(reply_obj)
                page_new += 1

            print(
                f"[thread] thread_id={thread_id} "
                f"page={page_num} "
                f"messages/comments scraped={page_new}"
            )

            last_page = page_num
            time.sleep(self.sleep_seconds)

        last_item = replies[-1] if replies else op

        row = {
            "source_id": self.source_id,
            "source_mode": self.source_mode,
            "thread_id": thread_id,
            "thread_url_id": thread_id,
            "thread_title": thread_title,
            "thread_title_detail": thread_title_detail,
            "thread_url": thread_url,
            "listing_category": category_name,
            "category_id": category_id,
            "category_name": category_name,
            "category_slug": category_slug,
            "thread_starter": op_author,
            "thread_starter_id": op_author,
            "opening_post_id": thread_id,
            "opening_message_id": thread_id,
            "opening_post_date": op_date,
            "opening_post_body": op_body,
            "listing_author": listing_record.get("listing_author", "") or op_author,
            "listing_author_id": listing_record.get("listing_author_id", "") or op_author,
            "replies_count": replies_count if replies_count else len(replies),
            "views_count": None,
            "last_message_date": last_item.get("date", ""),
            "last_message_author": last_item.get("author", ""),
            "last_message_author_id": last_item.get("user_id", ""),
            "last_message_id": last_item.get("message_id", ""),
            "last_page": last_page,
            "thread_pages_count": thread_pages_count,
            "posts_count": 1,
            "comments_count": len(replies),
            "likes_total": 0,
            "post": op,
            "replies": replies
        }

        return row

    def run(self):
        listing_records = self.fetch_page_one_records()

        print(f"[resume] existing_thread_ids={len(self.existing_thread_ids)}")
        print(f"[discover] total_candidate_threads={len(listing_records)}")

        scraped_new = 0
        skipped_existing = 0

        for idx, listing_record in enumerate(listing_records, start=1):
            thread_id = listing_record["thread_id"]

            if thread_id in self.existing_thread_ids:
                skipped_existing += 1
                print(f"[skip] {idx}/{len(listing_records)} thread_id={thread_id} reason=existing_output")
                continue

            try:
                row = self.scrape_thread(listing_record)
                if row:
                    append_jsonl(self.posts_path, row)
                    self.existing_thread_ids.add(thread_id)
                    scraped_new += 1
                    print(
                        f"[saved] {idx}/{len(listing_records)} "
                        f"thread_id={thread_id} "
                        f"comments_count={row.get('comments_count', 0)}"
                    )
                else:
                    print(f"[empty] {idx}/{len(listing_records)} thread_id={thread_id}")
            except Exception as e:
                self.log_error({
                    "source_id": self.source_id,
                    "stage": "thread_run",
                    "thread_id": thread_id,
                    "thread_url": listing_record.get("thread_url", ""),
                    "error": str(e)
                })
                print(f"[error] {idx}/{len(listing_records)} thread_id={thread_id} error={e}")

            time.sleep(self.sleep_seconds)

        print(
            f"[done] scraped_new={scraped_new} "
            f"skipped_existing={skipped_existing} "
            f"output={self.posts_path}"
        )


if __name__ == "__main__":
    scraper = MumsnetPageOneScraper("configs/SRC008.json")
    scraper.run()