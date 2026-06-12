#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STOP = False


def handle_sigint(sig, frame):
    global STOP
    print("\nStopping...")
    STOP = True


signal.signal(signal.SIGINT, handle_sigint)


# =========================================================
# HELPERS
# =========================================================

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise RuntimeError(f"Config file not found: {cfg_path}")

    if cfg_path.suffix.lower() == ".json":
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise RuntimeError("JSON config must be an object/dict")
        return cfg

    spec = importlib.util.spec_from_file_location("user_cfg_module", str(cfg_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load config module: {cfg_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if hasattr(module, "CONFIG"):
        cfg = module.CONFIG
        if not isinstance(cfg, dict):
            raise RuntimeError("CONFIG must be a dict")
        return cfg

    raise RuntimeError("Config file must define CONFIG = {...} or be a JSON file")


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def safe_int(value: Any, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        s = re.sub(r"[^\d-]", "", str(value))
        if s in ("", "-"):
            return default
        try:
            return int(s)
        except Exception:
            return default


def clean_text(value: str) -> str:
    value = value or ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def html_to_visible_text(html_value: str, fallback_text: str = "") -> str:
    if html_value:
        soup = BeautifulSoup(html_value, "html.parser")
        txt = soup.get_text("\n", strip=True)
        lines = [clean_text(x) for x in txt.splitlines()]
        lines = [x for x in lines if x]
        if lines:
            return "\n".join(lines).strip()
    return clean_text(fallback_text or "")


def utc_to_iso(value: Any) -> str:
    try:
        if value is None:
            return ""
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def build_output_files(cfg: dict) -> Tuple[Path, Path]:
    out_dir = Path(cfg["output"]["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    posts_file = out_dir / cfg["output"]["posts_file"]
    errors_file = out_dir / cfg["output"]["errors_file"]
    return posts_file, errors_file


def load_existing_thread_ids(posts_file: Path) -> Set[str]:
    seen = set()
    if not posts_file.exists():
        return seen

    with open(posts_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                thread_id = obj.get("thread_id", "")
                if thread_id:
                    seen.add(thread_id)
            except Exception:
                continue
    return seen


def normalize_user(author: str, author_fullname: Optional[str]) -> Tuple[str, str]:
    native_user_id = author_fullname or ""
    user_id = native_user_id if native_user_id else author
    return user_id, native_user_id


# =========================================================
# REQUESTS
# =========================================================

def make_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    })

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class RedditClient:
    def __init__(
        self,
        session: requests.Session,
        timeout_seconds: float,
        max_attempts: int,
        default_backoff_seconds: float,
        max_backoff_seconds: float,
    ):
        self.session = session
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.default_backoff_seconds = default_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    def _compute_wait(self, response: Optional[requests.Response], attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), 1.0)
                except Exception:
                    pass

            ratelimit_reset = response.headers.get("x-ratelimit-reset")
            if ratelimit_reset:
                try:
                    return max(float(ratelimit_reset), 1.0)
                except Exception:
                    pass

        return min(self.default_backoff_seconds * (2 ** max(attempt - 1, 0)), self.max_backoff_seconds)

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        last_error = None

        for attempt in range(1, self.max_attempts + 1):
            response = None
            try:
                p = dict(params or {})
                p["_"] = int(time.time() * 1000)
                response = self.session.get(url, params=p, timeout=self.timeout_seconds)

                if response.status_code == 429:
                    wait_s = self._compute_wait(response, attempt)
                    print(f"  429 on {response.url} -> sleep {wait_s:.1f}s")
                    time.sleep(wait_s)
                    continue

                response.raise_for_status()
                return response.json()

            except requests.RequestException as e:
                last_error = e
                if attempt >= self.max_attempts:
                    break
                wait_s = self._compute_wait(response, attempt)
                time.sleep(wait_s)

        raise last_error if last_error else RuntimeError(f"Request failed for {url}")

    def fetch_listing_page(self, subreddit: str, sort: str, limit: int, after: Optional[str] = None, time_filter: str = "") -> dict:
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
        params = {"raw_json": 1, "limit": limit}
        if after:
            params["after"] = after
        if time_filter:
            params["t"] = time_filter
        return self.get_json(url, params=params)

    def fetch_post_with_comments(self, permalink: str, comment_sort: str, comment_limit: int, comment_depth: int) -> list:
        url = f"https://www.reddit.com{permalink}.json"
        params = {
            "raw_json": 1,
            "sort": comment_sort,
            "limit": comment_limit,
            "depth": comment_depth,
        }
        return self.get_json(url, params=params)

    def fetch_more_children(self, link_id: str, children_ids: List[str], sort: str) -> dict:
        url = "https://www.reddit.com/api/morechildren.json"
        params = {
            "raw_json": 1,
            "api_type": "json",
            "link_id": link_id,
            "children": ",".join(children_ids),
            "sort": sort,
        }
        return self.get_json(url, params=params)


# =========================================================
# PARSING
# =========================================================

def iter_listing_posts(listing_payload: dict):
    data = listing_payload.get("data") or {}
    children = data.get("children") or []
    for child in children:
        if isinstance(child, dict) and child.get("kind") == "t3":
            post = child.get("data") or {}
            if post:
                yield post


def build_opening_post_item(post_data: dict) -> dict:
    post_id = post_data.get("id") or ""
    message_id = post_data.get("name") or (f"t3_{post_id}" if post_id else "")
    author = post_data.get("author") or ""
    user_id, native_user_id = normalize_user(author, post_data.get("author_fullname"))

    body = html_to_visible_text(
        post_data.get("selftext_html") or "",
        post_data.get("selftext") or "",
    )

    return {
        "type": "post",
        "is_original_post": True,
        "thread_id": post_id,
        "post_id": post_id,
        "comment_id": "",
        "message_id": message_id,
        "native_post_id": post_id,
        "native_comment_id": "",
        "parent_message_id": "",
        "parent_comment_id": "",
        "author": author,
        "user_id": user_id,
        "native_user_id": native_user_id,
        "date": utc_to_iso(post_data.get("created_utc")),
        "date_iso": utc_to_iso(post_data.get("created_utc")),
        "body": body,
        "score": safe_int(post_data.get("score"), None),
        "upvotes_count": safe_int(post_data.get("ups"), None),
        "downvotes_count": safe_int(post_data.get("downs"), None),
        "views_count": safe_int(post_data.get("view_count"), None),
        "reply_count": 0,
        "permalink": f"https://www.reddit.com{post_data.get('permalink', '')}",
        "link_id": message_id,
        "depth": 0,
        "edited": post_data.get("edited"),
        "distinguished": post_data.get("distinguished"),
        "is_submitter": True,
    }


def build_comment_item(data: dict, thread_id: str, opening_post_id: str) -> Optional[dict]:
    expected_link_id = f"t3_{thread_id}"
    link_id = data.get("link_id") or ""
    if link_id != expected_link_id:
        return None

    comment_id = data.get("id") or ""
    message_id = data.get("name") or (f"t1_{comment_id}" if comment_id else "")
    if not comment_id or not message_id:
        return None

    author = data.get("author") or ""
    user_id, native_user_id = normalize_user(author, data.get("author_fullname"))

    body = html_to_visible_text(
        data.get("body_html") or "",
        data.get("body") or "",
    )

    parent_id = data.get("parent_id") or ""
    if parent_id.startswith("t1_"):
        parent_message_id = parent_id
        parent_comment_id = parent_id.replace("t1_", "", 1)
    elif parent_id == expected_link_id:
        parent_message_id = ""
        parent_comment_id = ""
    else:
        return None

    return {
        "type": "comment",
        "is_original_post": False,
        "thread_id": thread_id,
        "post_id": opening_post_id,
        "comment_id": comment_id,
        "message_id": message_id,
        "native_post_id": opening_post_id,
        "native_comment_id": comment_id,
        "parent_message_id": parent_message_id,
        "parent_comment_id": parent_comment_id,
        "author": author,
        "user_id": user_id,
        "native_user_id": native_user_id,
        "date": utc_to_iso(data.get("created_utc")),
        "date_iso": utc_to_iso(data.get("created_utc")),
        "body": body,
        "score": safe_int(data.get("score"), None),
        "upvotes_count": safe_int(data.get("ups"), None),
        "downvotes_count": safe_int(data.get("downs"), None),
        "views_count": None,
        "reply_count": 0,
        "permalink": f"https://www.reddit.com{data.get('permalink', '')}",
        "link_id": link_id,
        "depth": safe_int(data.get("depth"), 0),
        "edited": data.get("edited"),
        "distinguished": data.get("distinguished"),
        "is_submitter": data.get("is_submitter"),
    }


def walk_comment_nodes(
    nodes: List[dict],
    thread_id: str,
    opening_post_id: str,
    seen_message_ids: Set[str],
    comments_by_message: Dict[str, dict],
    ordered_message_ids: List[str],
    pending_more_ids: List[str],
) -> None:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue

        kind = node.get("kind")
        data = node.get("data") or {}

        if kind == "t1":
            item = build_comment_item(data, thread_id, opening_post_id)
            if item is not None:
                message_id = item["message_id"]
                if message_id not in seen_message_ids:
                    seen_message_ids.add(message_id)
                    comments_by_message[message_id] = item
                    ordered_message_ids.append(message_id)

            replies = data.get("replies")
            if isinstance(replies, dict):
                reply_children = ((replies.get("data") or {}).get("children") or [])
                walk_comment_nodes(
                    nodes=reply_children,
                    thread_id=thread_id,
                    opening_post_id=opening_post_id,
                    seen_message_ids=seen_message_ids,
                    comments_by_message=comments_by_message,
                    ordered_message_ids=ordered_message_ids,
                    pending_more_ids=pending_more_ids,
                )

        elif kind == "more":
            children_ids = data.get("children") or []
            for child_id in children_ids:
                if child_id:
                    pending_more_ids.append(child_id)


def extract_more_things(payload: dict) -> List[dict]:
    return ((((payload.get("json") or {}).get("data") or {}).get("things")) or [])


def attach_reply_counts(opening_post: dict, ordered_comments: List[dict]) -> None:
    direct_counts: Dict[str, int] = {}
    top_level_count = 0

    for comment in ordered_comments:
        parent_message_id = comment.get("parent_message_id") or ""
        if parent_message_id:
            direct_counts[parent_message_id] = direct_counts.get(parent_message_id, 0) + 1
        else:
            top_level_count += 1

    opening_post["reply_count"] = top_level_count

    for comment in ordered_comments:
        comment["reply_count"] = direct_counts.get(comment["message_id"], 0)


def build_thread_output(source_id: str, cfg: dict, thread_post: dict, posts: List[dict], verified_retry_used: bool) -> dict:
    opening_post = posts[0]
    last_message = posts[-1]

    return {
        "source_id": source_id,
        "source_type": cfg["source_type"],
        "source_mode": cfg["mode"],
        "thread_id": thread_post.get("id") or "",
        "thread_url_id": thread_post.get("id") or "",
        "thread_title": clean_text(thread_post.get("title") or ""),
        "thread_title_detail": clean_text(thread_post.get("title") or ""),
        "thread_url": f"https://www.reddit.com{thread_post.get('permalink', '')}",
        "listing_category": cfg["subreddit"],
        "thread_pages_count": 1,
        "thread_starter": opening_post["author"],
        "thread_starter_id": opening_post["user_id"],
        "thread_starter_native_user_id": opening_post["native_user_id"],
        "opening_post_id": opening_post["post_id"],
        "opening_message_id": opening_post["message_id"],
        "opening_post_date": opening_post["date"],
        "opening_post_body": opening_post["body"],
        "replies_count": safe_int(thread_post.get("num_comments"), None),
        "comments_count_extracted": max(len(posts) - 1, 0),
        "views_count": safe_int(thread_post.get("view_count"), None),
        "score": safe_int(thread_post.get("score"), None),
        "upvotes_count": safe_int(thread_post.get("ups"), None),
        "downvotes_count": safe_int(thread_post.get("downs"), None),
        "upvote_ratio": thread_post.get("upvote_ratio"),
        "last_message_date": last_message.get("date_iso") or "",
        "last_message_author": last_message.get("author") or "",
        "last_message_author_id": last_message.get("user_id") or "",
        "last_message_id": last_message.get("message_id") or "",
        "posts_count": len(posts),
        "comments_count": max(len(posts) - 1, 0),
        "counts_source": "thread_json",
        "verified_retry_used": verified_retry_used,
        "posts": posts,
    }


def parse_thread_payload(
    client: RedditClient,
    permalink: str,
    comment_sort: str,
    comment_limit: int,
    comment_depth: int,
    expand_more: bool,
    max_more_batches: int,
    more_children_batch_size: int,
    more_children_sleep_seconds: float,
    verification_retry_seconds: float,
) -> Tuple[dict, List[dict], bool]:
    verified_retry_used = False

    for pass_no in range(2):
        payload = client.fetch_post_with_comments(
            permalink=permalink,
            comment_sort=comment_sort,
            comment_limit=comment_limit,
            comment_depth=comment_depth,
        )

        if not isinstance(payload, list) or len(payload) < 1:
            raise ValueError("Unexpected thread JSON payload")

        post_children = ((payload[0].get("data") or {}).get("children") or [])
        if not post_children:
            raise ValueError("Opening post missing from thread JSON")

        thread_post = (post_children[0].get("data") or {})
        opening_post = build_opening_post_item(thread_post)

        seen_message_ids: Set[str] = set()
        if opening_post["message_id"]:
            seen_message_ids.add(opening_post["message_id"])

        comments_by_message: Dict[str, dict] = {}
        ordered_message_ids: List[str] = []
        pending_more_ids: List[str] = []

        if len(payload) > 1:
            root_nodes = ((payload[1].get("data") or {}).get("children") or [])
            walk_comment_nodes(
                nodes=root_nodes,
                thread_id=opening_post["thread_id"],
                opening_post_id=opening_post["post_id"],
                seen_message_ids=seen_message_ids,
                comments_by_message=comments_by_message,
                ordered_message_ids=ordered_message_ids,
                pending_more_ids=pending_more_ids,
            )

        more_batches = 0
        while expand_more and pending_more_ids and more_batches < max_more_batches and not STOP:
            current_ids = pending_more_ids[:more_children_batch_size]
            pending_more_ids = pending_more_ids[more_children_batch_size:]

            more_payload = client.fetch_more_children(
                link_id=opening_post["message_id"],
                children_ids=current_ids,
                sort=comment_sort,
            )
            things = extract_more_things(more_payload)

            walk_comment_nodes(
                nodes=things,
                thread_id=opening_post["thread_id"],
                opening_post_id=opening_post["post_id"],
                seen_message_ids=seen_message_ids,
                comments_by_message=comments_by_message,
                ordered_message_ids=ordered_message_ids,
                pending_more_ids=pending_more_ids,
            )

            more_batches += 1
            time.sleep(more_children_sleep_seconds)

        ordered_comments = [comments_by_message[mid] for mid in ordered_message_ids if mid in comments_by_message]
        attach_reply_counts(opening_post, ordered_comments)
        posts = [opening_post] + ordered_comments

        api_num_comments = safe_int(thread_post.get("num_comments"), 0) or 0
        extracted_comments = len(ordered_comments)

        if pass_no == 0 and api_num_comments > 0 and extracted_comments == 0:
            verified_retry_used = True
            time.sleep(verification_retry_seconds)
            continue

        return thread_post, posts, verified_retry_used

    raise RuntimeError("Thread verification retry failed")


# =========================================================
# SCRAPE
# =========================================================

def scrape(cfg: dict) -> None:
    source_id = cfg["source_id"]
    subreddit = cfg["subreddit"]
    posts_file, errors_file = build_output_files(cfg)

    req_cfg = cfg["request"]
    comments_cfg = cfg["comments"]

    session = make_session(req_cfg["user_agent"])
    client = RedditClient(
        session=session,
        timeout_seconds=float(req_cfg.get("timeout_seconds", 30)),
        max_attempts=int(req_cfg.get("max_attempts", 8)),
        default_backoff_seconds=float(req_cfg.get("default_backoff_seconds", 5.0)),
        max_backoff_seconds=float(req_cfg.get("max_backoff_seconds", 120.0)),
    )

    comment_sort = comments_cfg.get("sort", "old")
    comment_limit = int(comments_cfg.get("limit", 500))
    comment_depth = int(comments_cfg.get("depth", 12))
    expand_more = bool(comments_cfg.get("expand_more", True))
    max_more_batches = int(comments_cfg.get("max_more_batches", 100))
    more_children_batch_size = int(comments_cfg.get("more_children_batch_size", 100))
    more_children_sleep_seconds = float(comments_cfg.get("more_children_sleep_seconds", 0.8))
    verification_retry_seconds = float(comments_cfg.get("verification_retry_seconds", 2.0))

    listing_sleep_seconds = float(req_cfg.get("listing_sleep_seconds", 3.0))
    thread_sleep_seconds = float(req_cfg.get("thread_sleep_seconds", 2.5))

    resume = bool(cfg.get("output", {}).get("resume", True))
    seen_thread_ids: Set[str] = load_existing_thread_ids(posts_file) if resume else set()
    total_threads = len(seen_thread_ids)
    total_messages = 0

    print(f"Starting scraper for {source_id}")
    print(f"Subreddit: r/{subreddit}")
    print(f"Start URL: {cfg.get('start_url', '')}")
    print(f"Resume mode: {resume} ({len(seen_thread_ids)} existing threads)")
    print("=" * 60)

    for job in cfg["listing_jobs"]:
        if STOP:
            break

        sort = job.get("sort", "new")
        time_filter = job.get("time_filter", "")
        page_size = int(job.get("page_size", 100))
        max_pages = int(job.get("max_pages", 1))

        print(f"Listing: sort={sort} time_filter={time_filter or '-'} pages=1->{max_pages}")

        after = None
        for page_no in range(1, max_pages + 1):
            if STOP:
                break

            try:
                listing = client.fetch_listing_page(
                    subreddit=subreddit,
                    sort=sort,
                    limit=page_size,
                    after=after,
                    time_filter=time_filter,
                )
            except Exception as e:
                append_jsonl(errors_file, {
                    "source_id": source_id,
                    "stage": "listing",
                    "sort": sort,
                    "time_filter": time_filter,
                    "page_no": page_no,
                    "after": after,
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                print(f"Listing error on page {page_no}: {e}")
                break

            listing_posts = list(iter_listing_posts(listing))
            if not listing_posts:
                print(f"Page {page_no}: 0 posts")
                break

            print(f"Page {page_no}: {len(listing_posts)} posts")

            for listing_post in listing_posts:
                if STOP:
                    break

                thread_id = listing_post.get("id") or ""
                permalink = listing_post.get("permalink") or ""
                thread_url = f"https://www.reddit.com{permalink}"

                if not thread_id or not permalink or thread_id in seen_thread_ids:
                    continue

                try:
                    thread_post, posts, verified_retry_used = parse_thread_payload(
                        client=client,
                        permalink=permalink,
                        comment_sort=comment_sort,
                        comment_limit=comment_limit,
                        comment_depth=comment_depth,
                        expand_more=expand_more,
                        max_more_batches=max_more_batches,
                        more_children_batch_size=more_children_batch_size,
                        more_children_sleep_seconds=more_children_sleep_seconds,
                        verification_retry_seconds=verification_retry_seconds,
                    )

                    item = build_thread_output(source_id, cfg, thread_post, posts, verified_retry_used)
                    append_jsonl(posts_file, item)

                    seen_thread_ids.add(thread_id)
                    total_threads += 1
                    total_messages += len(posts)
                    print(f"  OK {total_threads}: {item['thread_title'][:90]}")

                except Exception as e:
                    append_jsonl(errors_file, {
                        "source_id": source_id,
                        "stage": "thread",
                        "sort": sort,
                        "time_filter": time_filter,
                        "thread_id": thread_id,
                        "thread_url": thread_url,
                        "error": str(e),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    print(f"  ERROR {thread_url} -> {e}")

                time.sleep(thread_sleep_seconds)

            after = ((listing.get("data") or {}).get("after"))
            if not after:
                break

            time.sleep(listing_sleep_seconds)

    print("=" * 60)
    print("DONE")
    print(f"Threads scraped: {total_threads}")
    print(f"Messages scraped in this run: {total_messages}")
    print(f"Output: {posts_file}")
    print(f"Errors: {errors_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="SRC006.json", help="Path to JSON or Python config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    scrape(cfg)


if __name__ == "__main__":
    main()
