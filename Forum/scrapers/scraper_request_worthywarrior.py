#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://community.worthywarrior.com/"
}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def clean_text(value):
    if value is None:
        return ""
    value = str(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_body(value: str) -> str:
    if value is None:
        return ""
    value = value.replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip() for x in value.splitlines()]
    lines = [x for x in lines if x]
    return "\n".join(lines).strip()


def safe_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = re.sub(r"[^\d]", "", str(value))
    return int(s) if s else default


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_output_files(cfg: dict):
    output_dir = Path(cfg["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%d%m%Y_%Hh%M")
    source_id = cfg["source_id"]
    posts_file = output_dir / f"{source_id}_post_and_comment_{stamp}.jsonl"
    errors_file = output_dir / f"{source_id}_errors_{stamp}.jsonl"
    return posts_file, errors_file


def make_session(max_retries=3):
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch_json(session: requests.Session, url: str, timeout: int):
    r = session.get(url, timeout=(10, timeout))
    r.raise_for_status()
    return r.json()


def fetch_html(session: requests.Session, url: str, timeout: int):
    r = session.get(url, timeout=(10, timeout))
    r.raise_for_status()
    return r.text


def category_json_url(category_url: str, page_num: int = 0) -> str:
    """
    Discourse category latest JSON.
    Example:
    https://community.worthywarrior.com/c/living-with-endo/5/l/latest.json
    https://community.worthywarrior.com/c/living-with-endo/5/l/latest.json?page=1
    """
    category_url = category_url.rstrip("/")
    base = f"{category_url}/l/latest.json"
    if page_num <= 0:
        return base
    return f"{base}?page={page_num}"


def topic_json_url(topic_slug: str, topic_id: int) -> str:
    return f"https://community.worthywarrior.com/t/{topic_slug}/{topic_id}.json"


def extract_category_meta_from_url(category_url: str):
    parsed = urlparse(category_url)
    parts = [p for p in parsed.path.split("/") if p]
    category_slug = ""
    category_id = ""
    category_name = ""

    if len(parts) >= 3 and parts[0] == "c":
        category_slug = parts[1]
        category_id = parts[2]
        category_name = category_slug.replace("-", " ").strip().title()

    return category_slug, category_id, category_name


def extract_like_count(post_obj: dict) -> int:
    likes = 0
    for action in post_obj.get("actions_summary", []) or []:
        if action.get("id") == 2:
            likes = safe_int(action.get("count"), 0)
            break
    return likes


def extract_topic_views_from_html(session: requests.Session, topic_url: str, timeout: int):
    try:
        html = fetch_html(session, topic_url, timeout)
        soup = BeautifulSoup(html, "html.parser")
        txt = clean_text(soup.get_text(" ", strip=True))
        m = re.search(r"\b(\d+)\s+views\b", txt, re.I)
        if m:
            return safe_int(m.group(1), None)
    except Exception:
        pass
    return None


def parse_topic_posts(topic_obj: dict):
    posts = []
    seen_post_ids = set()

    post_stream = topic_obj.get("post_stream", {}) or {}
    posts_raw = post_stream.get("posts", []) or []

    opening_post_id = ""
    opening_message_id = ""
    opening_post_date = ""
    opening_post_body = ""
    thread_starter = ""
    thread_starter_id = ""

    for idx, post in enumerate(posts_raw, start=1):
        post_id = str(post.get("id", "")).strip()
        if not post_id or post_id in seen_post_ids:
            continue

        seen_post_ids.add(post_id)

        username = clean_text(post.get("username"))
        name = clean_text(post.get("name"))
        author = username if username else name
        user_id_native = str(post.get("user_id", "")).strip()
        user_id = user_id_native if user_id_native else author

        cooked_html = post.get("cooked") or ""
        body = clean_body(BeautifulSoup(cooked_html, "html.parser").get_text("\n", strip=True))
        if not body:
            continue

        created_at = clean_text(post.get("created_at"))
        updated_at = clean_text(post.get("updated_at"))
        reads = post.get("reads")
        reply_count = safe_int(post.get("reply_count"), 0)
        reply_to_post_number = post.get("reply_to_post_number")
        post_number = safe_int(post.get("post_number"), 0)
        likes_count = extract_like_count(post)

        row = {
            "type": "post" if idx == 1 else "comment",
            "is_original_post": idx == 1,
            "message_id": post_id,
            "post_id": post_id if idx == 1 else "",
            "comment_id": "" if idx == 1 else post_id,
            "native_post_id": post_id,
            "post_number": post_number,
            "reply_to_post_number": reply_to_post_number,
            "reply_count": reply_count,
            "author": author,
            "username": username,
            "display_name": name,
            "user_id": user_id,
            "native_user_id": user_id_native,
            "date": created_at,
            "date_iso": created_at,
            "updated_date": updated_at,
            "body": body,
            "likes_count": likes_count,
            "views_count": safe_int(reads, 0) if reads is not None else None
        }

        posts.append(row)

        if idx == 1:
            opening_post_id = post_id
            opening_message_id = post_id
            opening_post_date = created_at
            opening_post_body = body
            thread_starter = author
            thread_starter_id = user_id

    return {
        "posts": posts,
        "opening_post_id": opening_post_id,
        "opening_message_id": opening_message_id,
        "opening_post_date": opening_post_date,
        "opening_post_body": opening_post_body,
        "thread_starter": thread_starter,
        "thread_starter_id": thread_starter_id
    }


def scrape_category(session, cfg, category_url, posts_file: Path, errors_file: Path, seen_topic_ids: set):
    timeout = cfg["request"]["timeout_seconds"]
    sleep_seconds = cfg["request"]["sleep_seconds"]
    start_page = cfg["pagination"].get("start_page", 0)
    max_pages = cfg["pagination"].get("max_pages", 50)
    source_id = cfg["source_id"]

    category_slug, category_id, category_name = extract_category_meta_from_url(category_url)

    topic_count = 0
    post_count = 0

    for page_num in range(start_page, start_page + max_pages):
        page_url = category_json_url(category_url, page_num)

        try:
            data = fetch_json(session, page_url, timeout)
        except Exception as e:
            append_jsonl(errors_file, {
                "source_id": source_id,
                "stage": "category_page",
                "category_url": category_url,
                "page_num": page_num,
                "page_url": page_url,
                "error": str(e),
                "timestamp": iso_now()
            })

            print(f"[ERROR] category page failed page={page_num} url={page_url} error={e}")

            if page_num == start_page:
                # first page failed = category route issue or access issue
                break
            else:
                # later page failed = likely pagination end
                break

        topic_list = (data.get("topic_list") or {}).get("topics") or []
        if not topic_list:
            print(f"[INFO] empty topic list at page={page_num} url={page_url}")
            break

        page_new_topics = 0

        print(f"[INFO] Category page {page_num}: {page_url}")
        print(f"[INFO] topics_on_page={len(topic_list)}")

        for topic in topic_list:
            topic_id = str(topic.get("id", "")).strip()
            if not topic_id:
                continue

            if topic_id in seen_topic_ids:
                continue
            seen_topic_ids.add(topic_id)
            page_new_topics += 1

            slug = clean_text(topic.get("slug"))
            title = clean_text(topic.get("title"))
            topic_url = f"https://community.worthywarrior.com/t/{slug}/{topic_id}"

            replies_count = safe_int(topic.get("reply_count"), 0)
            views_count = safe_int(topic.get("views"), 0)
            like_count_topic = safe_int(topic.get("like_count"), 0)
            posts_count_visible = safe_int(topic.get("posts_count"), 0)
            created_at = clean_text(topic.get("created_at"))
            bumped_at = clean_text(topic.get("bumped_at"))
            last_posted_at = clean_text(topic.get("last_posted_at"))

            try:
                topic_json = fetch_json(session, topic_json_url(slug, int(topic_id)), timeout)
                parsed = parse_topic_posts(topic_json)

                posts = parsed["posts"]
                if not posts:
                    print(f"[WARN] topic_id={topic_id} has no parsed posts")
                    continue

                root_post_id = parsed["opening_post_id"]
                for p in posts:
                    if p["type"] == "comment":
                        p["post_id"] = root_post_id

                topic_views_fallback = extract_topic_views_from_html(session, topic_url, timeout)
                topic_views_final = views_count if views_count > 0 else topic_views_fallback

                likes_total = sum(safe_int(p.get("likes_count"), 0) for p in posts)

                record = {
                    "source_id": source_id,
                    "source_type": cfg.get("source_type", "forum"),
                    "source_mode": cfg.get("source_mode", "forum_discourse"),
                    "thread_id": topic_id,
                    "article_id": None,
                    "page_id": None,
                    "thread_url_id": topic_id,
                    "thread_title": title,
                    "thread_title_detail": title,
                    "thread_url": topic_url,
                    "listing_category": category_name,
                    "category_id": category_id,
                    "category_name": category_name,
                    "category_slug": category_slug,
                    "thread_starter": parsed["thread_starter"],
                    "thread_starter_id": parsed["thread_starter_id"],
                    "opening_post_id": parsed["opening_post_id"],
                    "opening_message_id": parsed["opening_message_id"],
                    "opening_post_date": parsed["opening_post_date"],
                    "opening_post_body": parsed["opening_post_body"],
                    "replies_count": replies_count,
                    "views_count": topic_views_final,
                    "likes_total": likes_total if likes_total > 0 else like_count_topic,
                    "posts_count": len(posts),
                    "comments_count": max(len(posts) - 1, 0),
                    "visible_posts_count": posts_count_visible,
                    "publish_date": created_at,
                    "updated_date": bumped_at,
                    "last_message_date": last_posted_at if last_posted_at else bumped_at,
                    "thread_pages_count": 1,
                    "posts": posts
                }

                append_jsonl(posts_file, record)
                topic_count += 1
                post_count += len(posts)

                print(
                    f"[INFO] saved topic_id={topic_id} "
                    f"posts={len(posts)} replies={replies_count} views={topic_views_final}"
                )

            except Exception as e:
                append_jsonl(errors_file, {
                    "source_id": source_id,
                    "stage": "topic",
                    "category_url": category_url,
                    "topic_id": topic_id,
                    "topic_url": topic_url,
                    "error": str(e),
                    "timestamp": iso_now()
                })
                print(f"[ERROR] topic_id={topic_id} error={e}")

            time.sleep(sleep_seconds)

        print(f"[INFO] new_topics_this_page={page_new_topics}")

        if page_new_topics == 0:
            break

        time.sleep(sleep_seconds)

    return topic_count, post_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config JSON")
    args = parser.parse_args()

    cfg = load_config(args.config)
    posts_file, errors_file = build_output_files(cfg)
    session = make_session(cfg["request"].get("max_retries", 3))

    all_topics = 0
    all_posts = 0
    seen_topic_ids = set()

    print(f"[INFO] Starting scraper for {cfg['source_id']}")
    print("=" * 60)

    for category_url in cfg.get("start_urls", []):
        print(f"[INFO] Category: {category_url}")
        topics, posts = scrape_category(
            session=session,
            cfg=cfg,
            category_url=category_url,
            posts_file=posts_file,
            errors_file=errors_file,
            seen_topic_ids=seen_topic_ids
        )
        all_topics += topics
        all_posts += posts

    print("=" * 60)
    print("[DONE]")
    print(f"[INFO] topics_scraped={all_topics}")
    print(f"[INFO] posts_comments_scraped={all_posts}")
    print(f"[INFO] output_file={posts_file}")
    print(f"[INFO] errors_file={errors_file}")


if __name__ == "__main__":
    main()