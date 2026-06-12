#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


should_stop = False


def signal_handler(sig, frame):
    global should_stop
    print("\n[STOP] Stopping after current thread...")
    should_stop = True


signal.signal(signal.SIGINT, signal_handler)

TOPIC_RE = re.compile(r"/t/([^/?#]+)/(?P<topic_id>\d+)(?:/\d+)?/?$")
TOPIC_ID_RE = re.compile(r"/t/[^/?#]+/(?P<topic_id>\d+)")
NEXT_TEXT_RE = re.compile(r"next\s*page", re.I)


@dataclass(frozen=True)
class PatientClient:
    site_url: str
    timeout_seconds: float = 25.0

    def __post_init__(self) -> None:
        session = requests.Session()

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        object.__setattr__(self, "_session", session)

    def warm_up(self) -> None:
        for url in [self.site_url + "/", self.site_url + "/latest"]:
            try:
                self.fetch_html(url)
                time.sleep(0.5)
            except Exception:
                pass

    def fetch_html(self, url: str, referer: str = "") -> str:
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            )
        }
        if referer:
            headers["Referer"] = referer

        response = self._session.get(url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.text

    def fetch_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        referer: str = "",
    ) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer

        response = self._session.get(
            url,
            params=params or {},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_source_mode(cfg: Dict[str, Any]) -> str:
    return cfg.get("source_mode") or cfg.get("mode") or "forum_patient_info"


def clean_text(value: str) -> str:
    value = value or ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_multiline_text(value: str) -> str:
    value = value or ""
    value = value.replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def normalize_topic_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href).split("#")[0].split("?")[0].rstrip("/")


def normalize_page_url(base_url: str, href: str) -> str:
    full = urljoin(base_url, href)
    parts = urlsplit(full)
    clean_path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, clean_path, parts.query, ""))


def extract_topic_id_from_url(url: str) -> str:
    match = TOPIC_ID_RE.search(url or "")
    return match.group("topic_id") if match else ""


def extract_listing_threads(soup: BeautifulSoup, page_url: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        full_url = normalize_topic_url(page_url, href)

        if "/t/" not in full_url:
            continue

        match = TOPIC_RE.search(full_url)
        if not match:
            continue

        topic_slug = match.group(1)
        thread_id = match.group("topic_id")
        thread_title = clean_text(a.get_text(" ", strip=True))

        if not thread_title:
            continue
        if thread_id in seen:
            continue

        seen.add(thread_id)
        items.append(
            {
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "topic_slug": topic_slug,
                "thread_title": thread_title,
                "thread_url": full_url,
            }
        )

    return items


def find_next_page(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        rel = " ".join(a.get("rel", [])) if a.get("rel") else ""

        if not href:
            continue

        if NEXT_TEXT_RE.search(text) or rel.lower() == "next":
            return normalize_page_url(current_url, href)

    return None


def collect_threads(
    client: PatientClient,
    start_url: str,
    limit_threads: int,
    max_pages: int,
    sleep_seconds: float,
) -> List[Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    visited_pages = set()
    current_url = start_url
    referer = client.site_url + "/"
    page_num = 1

    while current_url and current_url not in visited_pages and not should_stop:
        if max_pages > 0 and page_num > max_pages:
            break

        visited_pages.add(current_url)
        print(f"[INFO] Listing page {page_num}: {current_url}")

        html = client.fetch_html(current_url, referer=referer)
        soup = BeautifulSoup(html, "html.parser")
        page_threads = extract_listing_threads(soup, current_url)

        added = 0
        for item in page_threads:
            thread_id = item["thread_id"]
            if thread_id not in found:
                found[thread_id] = item
                added += 1
                if limit_threads > 0 and len(found) >= limit_threads:
                    break

        print(f"[INFO] page_threads={len(page_threads)} new_threads={added} total={len(found)}")

        if limit_threads > 0 and len(found) >= limit_threads:
            break

        next_url = find_next_page(soup, current_url)
        if not next_url:
            break

        referer = current_url
        current_url = next_url
        page_num += 1
        time.sleep(sleep_seconds)

    results = list(found.values())
    if limit_threads > 0:
        results = results[:limit_threads]
    return results


def fetch_category_map(client: PatientClient) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}

    for url in [client.site_url + "/categories.json", client.site_url + "/site.json"]:
        try:
            payload = client.fetch_json(url, referer=client.site_url + "/")
        except Exception:
            continue

        categories: List[Dict[str, Any]] = []
        if isinstance(payload.get("category_list"), dict):
            categories = payload["category_list"].get("categories") or []
        elif isinstance(payload.get("categories"), list):
            categories = payload.get("categories") or []
        elif isinstance(payload.get("site"), dict):
            categories = payload["site"].get("categories") or []

        for cat in categories:
            cat_id = cat.get("id")
            if cat_id is None:
                continue
            out[str(cat_id)] = {
                "category_id": cat_id,
                "category_name": cat.get("name") or "",
                "category_slug": cat.get("slug") or "",
            }

        if out:
            break

    return out


def topic_json_url(thread_url: str) -> str:
    return thread_url.rstrip("/") + ".json"


def html_to_text(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.select("script, style, noscript"):
        bad.decompose()

    text = soup.get_text("\n", strip=True)
    return clean_multiline_text(text)


def extract_post_body(post: Dict[str, Any]) -> str:
    raw = post.get("raw")
    if isinstance(raw, str) and raw.strip():
        return clean_multiline_text(raw)

    cooked = post.get("cooked")
    if isinstance(cooked, str) and cooked.strip():
        return html_to_text(cooked)

    return ""


def extract_likes_count(post: Dict[str, Any]) -> int:
    if isinstance(post.get("like_count"), int):
        return int(post.get("like_count"))

    for action in post.get("actions_summary") or []:
        if not isinstance(action, dict):
            continue
        if action.get("id") == 2:
            return int(action.get("count") or 0)

    return 0


def fetch_topic_page_json(
    client: PatientClient,
    thread_url: str,
    page_num: int,
) -> Dict[str, Any]:
    url = topic_json_url(thread_url)
    params: Dict[str, Any] = {"include_raw": "true"}
    if page_num > 1:
        params["page"] = page_num

    return client.fetch_json(url, params=params, referer=thread_url)


def fetch_all_posts_for_topic(
    client: PatientClient,
    thread_url: str,
    sleep_seconds: float,
) -> Dict[str, Any]:
    first_payload = fetch_topic_page_json(client, thread_url, page_num=1)
    post_stream = first_payload.get("post_stream") or {}

    seen_post_ids = set()
    all_posts: List[Dict[str, Any]] = []

    def add_posts(posts: List[Dict[str, Any]]) -> None:
        for post in posts:
            if not isinstance(post, dict):
                continue
            post_id = post.get("id")
            if post_id is None:
                continue
            post_id_str = str(post_id)
            if post_id_str in seen_post_ids:
                continue
            seen_post_ids.add(post_id_str)
            all_posts.append(post)

    add_posts(post_stream.get("posts") or [])

    total_posts = first_payload.get("posts_count")
    if not isinstance(total_posts, int) or total_posts <= 0:
        stream = post_stream.get("stream") or []
        total_posts = len(stream) if isinstance(stream, list) and stream else len(all_posts)

    total_pages = max(1, math.ceil(total_posts / 20))
    pages_fetched = 1

    for page_num in range(2, total_pages + 1):
        if should_stop:
            break

        try:
            payload = fetch_topic_page_json(client, thread_url, page_num=page_num)
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 404:
                break
            raise

        page_posts = (payload.get("post_stream") or {}).get("posts") or []
        before = len(all_posts)
        add_posts(page_posts)
        after = len(all_posts)
        pages_fetched = page_num

        print(f"[INFO]   topic_page={page_num} page_posts={len(page_posts)} new_posts={after - before}")
        time.sleep(sleep_seconds)

    all_posts.sort(key=lambda x: int(x.get("post_number", 0) or 0))

    return {
        "topic": first_payload,
        "posts": all_posts,
        "pages_fetched": pages_fetched,
    }


def build_thread_object(
    cfg: Dict[str, Any],
    thread_url: str,
    topic_payload: Dict[str, Any],
    posts: List[Dict[str, Any]],
    pages_fetched: int,
    category_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    thread_id = str(topic_payload.get("id") or extract_topic_id_from_url(thread_url))
    thread_url_id = thread_id
    thread_title = clean_text(topic_payload.get("title") or "")
    thread_title_detail = thread_title

    category_id = topic_payload.get("category_id")
    category = category_map.get(str(category_id), {})
    category_name = category.get("category_name", "")
    category_slug = category.get("category_slug", "")

    opening_post_id = ""
    opening_message_id = ""
    opening_post_date = ""
    opening_post_body = ""
    thread_starter = ""
    thread_starter_id = ""

    last_message_date = ""
    last_message_author = ""
    last_message_author_id = ""
    last_message_id = ""

    likes_total = 0
    opening_post_obj: Dict[str, Any] = {}
    replies: List[Dict[str, Any]] = []
    post_id_by_number: Dict[int, str] = {}

    if posts:
        first_post = posts[0]
        opening_post_id = str(first_post.get("id") or "")
        opening_message_id = opening_post_id
        opening_post_date = first_post.get("created_at") or ""
        opening_post_body = extract_post_body(first_post)
        thread_starter = clean_text(str(first_post.get("username") or ""))
        native_starter_id = first_post.get("user_id")
        thread_starter_id = str(native_starter_id) if native_starter_id is not None else thread_starter

    views_count = topic_payload.get("views")

    for idx, post in enumerate(posts, start=1):
        post_number = int(post.get("post_number") or idx)
        native_post_id = str(post.get("id") or "")
        post_id_by_number[post_number] = native_post_id

    for idx, post in enumerate(posts, start=1):
        post_number = int(post.get("post_number") or idx)
        native_post_id = str(post.get("id") or "")
        username = clean_text(str(post.get("username") or ""))
        native_user_id = post.get("user_id")
        user_id = str(native_user_id) if native_user_id is not None else username
        likes_count = extract_likes_count(post)
        likes_total += likes_count
        reply_to_post_number = post.get("reply_to_post_number")
        reply_to_post_id = ""
        if reply_to_post_number is not None:
            try:
                reply_to_post_id = post_id_by_number.get(int(reply_to_post_number), "")
            except Exception:
                reply_to_post_id = ""

        item = {
            "author": username,
            "user_id": user_id,
            "native_user_id": str(native_user_id) if native_user_id is not None else "",
            "date": post.get("created_at") or "",
            "date_iso": post.get("created_at") or "",
            "body": extract_post_body(post),
            "likes_count": likes_count,
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": native_post_id,
            "native_post_id": native_post_id,
            "anchor_id": native_post_id,
            "post_number": post_number,
            "type": "post" if post_number == 1 else "comment",
            "is_original_post": post_number == 1,
            "post_id": native_post_id if post_number == 1 else opening_post_id,
            "comment_id": "" if post_number == 1 else native_post_id,
            "reply_to_post_number": reply_to_post_number if reply_to_post_number is not None else "",
            "reply_to_post_id": reply_to_post_id,
            "post_url": f"{thread_url}/{post_number}",
        }

        if post_number == 1:
            opening_post_obj = dict(item)
        else:
            replies.append(dict(item))

        last_message_date = item["date"]
        last_message_author = item["author"]
        last_message_author_id = item["user_id"]
        last_message_id = item["message_id"]

    replies_count = len(replies)
    comments_count = replies_count
    posts_count = 1 + replies_count if opening_post_obj else replies_count
    thread_pages_count = max(1, pages_fetched)

    return {
        "source_id": cfg["source_id"],
        "source_mode": get_source_mode(cfg),
        "thread_id": thread_id,
        "thread_url_id": thread_url_id,
        "thread_title": thread_title,
        "thread_title_detail": thread_title_detail,
        "thread_url": thread_url,
        "listing_category": category_name,
        "category_id": category_id,
        "category_name": category_name,
        "category_slug": category_slug,
        "thread_starter": thread_starter,
        "thread_starter_id": thread_starter_id,
        "opening_post_id": opening_post_id,
        "opening_message_id": opening_message_id,
        "opening_post_date": opening_post_date,
        "opening_post_body": opening_post_body,
        "listing_author": thread_starter,
        "listing_author_id": thread_starter_id,
        "replies_count": replies_count,
        "views_count": views_count,
        "last_message_date": last_message_date,
        "last_message_author": last_message_author,
        "last_message_author_id": last_message_author_id,
        "last_message_id": last_message_id,
        "last_page": thread_pages_count,
        "thread_pages_count": thread_pages_count,
        "posts_count": posts_count,
        "comments_count": comments_count,
        "likes_total": likes_total,
        "post": opening_post_obj,
        "replies": replies,
    }


def scrape_patient_info(cfg: Dict[str, Any]) -> Dict[str, Path]:
    site_url = cfg["site_url"].rstrip("/")
    start_url = cfg["start_url"]
    limit_threads = int(cfg.get("limit_threads", 10))
    timeout_seconds = float(cfg.get("request", {}).get("timeout_seconds", 25))
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))
    max_pages = int(cfg.get("listing", {}).get("max_pages", 50))

    output_dir = Path(cfg["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    posts_file = output_dir / f"{cfg['source_id']}_post_and_comment_final.jsonl"
    errors_file = output_dir / f"{cfg['source_id']}_errors_final.jsonl"

    posts_file.write_text("", encoding="utf-8")
    errors_file.write_text("", encoding="utf-8")

    client = PatientClient(site_url=site_url, timeout_seconds=timeout_seconds)

    print(f"[INFO] Starting {cfg['source_id']}")
    print(f"[INFO] Target: {start_url}")
    print(f"[INFO] Limit threads: {limit_threads}")
    print("[INFO] Warming up session...")
    client.warm_up()

    print("[INFO] Fetching category map...")
    category_map = fetch_category_map(client)

    print("[INFO] Collecting thread URLs...")
    threads = collect_threads(
        client=client,
        start_url=start_url,
        limit_threads=limit_threads,
        max_pages=max_pages,
        sleep_seconds=sleep_seconds,
    )

    print(f"[INFO] Collected {len(threads)} thread URLs")

    total_threads = 0
    total_posts = 0
    total_replies = 0
    total_messages = 0

    for idx, item in enumerate(threads, start=1):
        if should_stop:
            break

        thread_url = item["thread_url"]
        thread_id = item["thread_id"]

        print("-" * 70)
        print(f"[INFO] Thread {idx}/{len(threads)} | thread_id={thread_id}")
        print(f"[INFO] URL: {thread_url}")

        try:
            fetched = fetch_all_posts_for_topic(
                client=client,
                thread_url=thread_url,
                sleep_seconds=sleep_seconds,
            )

            thread_obj = build_thread_object(
                cfg=cfg,
                thread_url=thread_url,
                topic_payload=fetched["topic"],
                posts=fetched["posts"],
                pages_fetched=fetched["pages_fetched"],
                category_map=category_map,
            )

            with open(posts_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(thread_obj, ensure_ascii=False) + "\n")

            post_count = 1 if thread_obj.get("post") else 0
            reply_count = len(thread_obj.get("replies") or [])
            message_count = post_count + reply_count

            total_threads += 1
            total_posts += post_count
            total_replies += reply_count
            total_messages += message_count

            print(
                f"[OK] thread_saved post={post_count} replies={reply_count} "
                f"messages={message_count} views={thread_obj.get('views_count')} "
                f"thread_id={thread_obj.get('thread_id')}"
            )

        except Exception as e:
            print(f"[ERROR] thread_id={thread_id} error={e}")
            error_obj = {
                "source_id": cfg["source_id"],
                "source_mode": get_source_mode(cfg),
                "thread_id": thread_id,
                "thread_url": thread_url,
                "error": str(e),
            }
            with open(errors_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(error_obj, ensure_ascii=False) + "\n")

        time.sleep(sleep_seconds)

    print()
    print("=" * 70)
    print("[FINAL] DONE")
    print(f"Threads scraped: {total_threads}")
    print(f"Posts scraped: {total_posts}")
    print(f"Replies scraped in this run: {total_replies}")
    print(f"Messages scraped in this run: {total_messages}")
    print(f"Posts file: {posts_file}")
    print(f"Errors file: {errors_file}")

    return {
        "posts_file": posts_file,
        "errors_file": errors_file,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    scrape_patient_info(cfg)


if __name__ == "__main__":
    main()
