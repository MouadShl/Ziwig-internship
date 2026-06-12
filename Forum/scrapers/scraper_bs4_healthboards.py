#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import signal
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

STOP_REQUESTED = False


def handle_sigint(sig, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print("\n[INFO] Stop requested. Finishing safely...")


signal.signal(signal.SIGINT, handle_sigint)


def now_iso():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(x):
    x = x or ""
    x = x.replace("\xa0", " ")
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def clean_multiline_text(x):
    x = x or ""
    x = x.replace("\xa0", " ")
    parts = [re.sub(r"\s+", " ", p).strip() for p in x.splitlines()]
    parts = [p for p in parts if p]
    return "\n".join(parts).strip()


def safe_int(x):
    if x is None:
        return None
    s = re.sub(r"[^\d]", "", str(x))
    return int(s) if s else None


def project_root_from_script():
    return Path(__file__).resolve().parents[1]


def resolve_path(value, project_root):
    p = Path(value)
    return p if p.is_absolute() else (project_root / p)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_existing_thread_ids(jsonl_path):
    existing = set()
    if not jsonl_path.exists():
        return existing
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                tid = row.get("thread_id")
                if tid is not None:
                    existing.add(str(tid))
            except Exception:
                pass
    return existing


def log_error(errors_file, phase, url, message, extra=None):
    row = {
        "timestamp": now_iso(),
        "phase": phase,
        "url": str(url),
        "error": str(message)
    }
    if extra:
        row.update(extra)
    append_jsonl(errors_file, row)


THREAD_TITLE_TD_RE = re.compile(r"^td_threadtitle_(\d+)$")
DATE_RE = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[AP]M\b", re.I)
PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
POST_MESSAGE_ID_RE = re.compile(r"^post_message_(\d+)$")
THREAD_URL_ID_RE = re.compile(r"/boards/endometriosis/(\d+)-")


def parse_last_post_cell(td):
    text = td.get_text("\n", strip=True)
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    last_message_date = None
    last_message_author = None
    date_part = None
    time_part = None

    for line in lines:
        if date_part is None and DATE_RE.search(line):
            date_part = DATE_RE.search(line).group(0)
        if time_part is None and TIME_RE.search(line):
            time_part = TIME_RE.search(line).group(0).upper()

    if date_part and time_part:
        last_message_date = f"{date_part} {time_part}"
    elif date_part:
        last_message_date = date_part

    for line in lines:
        if line.lower().startswith("by "):
            last_message_author = clean_text(line[3:])
            break

    if not last_message_author and lines:
        maybe = lines[-1]
        if not DATE_RE.search(maybe) and not TIME_RE.search(maybe):
            last_message_author = maybe

    return last_message_date, last_message_author


def parse_listing_threads(soup, listing_url, forum_id=None):
    threads = []

    tbody = None
    if forum_id:
        tbody = soup.find("tbody", id=f"threadbits_forum_{forum_id}")
    if tbody is None:
        tbody = soup.find("tbody", id=re.compile(r"^threadbits_forum_\d+$"))
    if tbody is None:
        return threads

    for tr in tbody.find_all("tr", recursive=False):
        title_td = tr.find("td", id=THREAD_TITLE_TD_RE)
        if title_td is None:
            continue

        m = THREAD_TITLE_TD_RE.match(title_td.get("id", ""))
        if not m:
            continue

        thread_id = m.group(1)
        title_link = title_td.find("a", id=re.compile(r"^thread_title_\d+$"), href=True)
        if title_link is None:
            continue

        thread_title = clean_text(title_link.get_text(" ", strip=True))
        if not thread_title:
            continue

        thread_url = urljoin(listing_url, title_link["href"])

        starter_div = title_td.find("div", class_="smallfont")
        thread_starter = clean_text(starter_div.get_text(" ", strip=True)) if starter_div else None
        if not thread_starter:
            thread_starter = None

        tds = tr.find_all("td", recursive=False)
        last_message_date = None
        last_message_author = None
        replies_count = None
        views_count = None

        if len(tds) >= 6:
            last_message_date, last_message_author = parse_last_post_cell(tds[3])
            replies_count = safe_int(tds[4].get_text(" ", strip=True))
            views_count = safe_int(tds[5].get_text(" ", strip=True))

        threads.append({
            "thread_id": thread_id,
            "thread_url_id": thread_id,
            "thread_title": thread_title,
            "thread_title_detail": thread_title,
            "thread_url": thread_url,
            "thread_starter": thread_starter,
            "thread_starter_id": thread_starter,
            "last_message_date": last_message_date,
            "last_message_author": last_message_author,
            "last_message_author_id": last_message_author,
            "replies_count": replies_count,
            "views_count": views_count
        })

    return threads


def extract_thread_title(soup, fallback=None):
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
        if title:
            return title

    title_tag = soup.find("title")
    if title_tag:
        title = clean_text(title_tag.get_text(" ", strip=True))
        title = re.sub(r"^Healthboards\s*-\s*Women\s*-\s*Endometriosis:\s*", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*Healthboards.*$", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*HealthBoards.*$", "", title, flags=re.I)
        if title:
            return title

    return fallback


def detect_thread_pages_count(soup):
    text = clean_text(soup.get_text(" ", strip=True))
    m = PAGE_OF_RE.search(text)
    if m:
        return max(1, int(m.group(2)))
    return 1


def extract_join_date_from_user_td(user_td):
    text = user_td.get_text("\n", strip=True)
    m = re.search(r"Join Date:\s*([A-Za-z]{3,9}\s+\d{4})", text, re.I)
    if m:
        return clean_text(m.group(1))
    return None


def extract_author_from_user_td(user_td):
    a = user_td.find("a", class_="bigusername")
    if a:
        name = clean_text(a.get_text(" ", strip=True))
        if name:
            return name, name

    for a in user_td.find_all("a", href=True):
        txt = clean_text(a.get_text(" ", strip=True))
        href = a.get("href", "")
        if "/members/" in href and txt:
            return txt, txt

    return None, None


def extract_post_date_from_container(container):
    text = clean_text(container.get_text(" ", strip=True))
    date_match = DATE_RE.search(text)
    time_match = TIME_RE.search(text)
    if date_match and time_match:
        return f"{date_match.group(0)} {time_match.group(0).upper()}"
    if date_match:
        return date_match.group(0)
    return None


def extract_body_from_message_div(message_div):
    soup = BeautifulSoup(str(message_div), "html.parser")
    root = soup.find()
    if root is None:
        return None

    bad = [
        "script", "style", "noscript",
        ".signature", ".postsignature", ".quote_container",
        ".bbcode_quote_container", ".postfoot", ".postcontrols",
        ".editedby", "fieldset", "legend"
    ]
    for selector in bad:
        for node in root.select(selector):
            node.decompose()

    for table in root.find_all("table"):
        txt = clean_text(table.get_text(" ", strip=True))
        if txt.startswith("Quote:") or "Originally Posted by" in txt:
            table.decompose()

    for div in root.find_all("div", class_="smallfont"):
        txt = clean_text(div.get_text(" ", strip=True))
        if txt == "Quote:":
            div.decompose()

    body = clean_multiline_text(root.get_text("\n", strip=True))
    return body or None


def parse_posts_from_vbulletin_thread(soup, thread_id, page_num, seen_post_ids):
    posts = []

    for message_div in soup.find_all("div", id=POST_MESSAGE_ID_RE):
        m = POST_MESSAGE_ID_RE.match(message_div.get("id", ""))
        if not m:
            continue
        native_post_id = m.group(1)
        if native_post_id in seen_post_ids:
            continue

        post_td = message_div.find_parent("td", id=re.compile(r"^td_post_\d+$"))
        if post_td is None:
            continue

        row = post_td.find_parent("tr")
        if row is None:
            continue

        tds = row.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        user_td = tds[0]

        author, user_id = extract_author_from_user_td(user_td)
        join_date = extract_join_date_from_user_td(user_td)
        post_date = extract_post_date_from_container(post_td)
        body = extract_body_from_message_div(message_div)

        if not body:
            continue

        seen_post_ids.add(native_post_id)
        posts.append({
            "message_id": native_post_id,
            "native_post_id": native_post_id,
            "post_id": native_post_id,
            "comment_id": None,
            "author": author,
            "user_id": user_id,
            "native_user_id": None,
            "join_date": join_date,
            "date": post_date,
            "date_iso": None,
            "body": body,
            "likes_count": None,
            "thread_id": thread_id,
            "thread_page_number": page_num
        })

    return posts


def detect_actual_thread_id_from_html(html):
    ids = THREAD_URL_ID_RE.findall(html or "")
    if not ids:
        return None
    counts = Counter(ids)
    return counts.most_common(1)[0][0]


def detect_page_num_from_html(soup, file_path):
    text = clean_text(soup.get_text(" ", strip=True))
    m = PAGE_OF_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    m = re.search(r"_page(\d+)\.html$", file_path.name, re.I)
    if m:
        return int(m.group(1))

    return 1


def scan_local_thread_files(thread_dir, errors_file):
    grouped = defaultdict(list)

    for file_path in sorted(thread_dir.glob("*.html")):
        try:
            html = file_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
            actual_thread_id = detect_actual_thread_id_from_html(html)
            if not actual_thread_id:
                log_error(errors_file, "thread_scan_no_id", str(file_path), "Could not detect actual thread id from HTML")
                continue

            page_num = detect_page_num_from_html(soup, file_path)
            detected_title = extract_thread_title(soup, fallback=None)
            filename_match = re.match(r"^(\d+)(?:_page\d+)?\.html$", file_path.name, re.I)
            filename_thread_id = filename_match.group(1) if filename_match else None

            grouped[actual_thread_id].append({
                "path": file_path,
                "page_num": page_num,
                "detected_title": detected_title,
                "filename_thread_id": filename_thread_id,
                "actual_thread_id": actual_thread_id
            })

            if filename_thread_id and filename_thread_id != actual_thread_id:
                log_error(
                    errors_file,
                    "thread_filename_mismatch",
                    str(file_path),
                    "Filename thread id does not match actual thread id detected from HTML",
                    {
                        "filename_thread_id": filename_thread_id,
                        "actual_thread_id": actual_thread_id,
                        "page_num": page_num,
                        "detected_title": detected_title
                    }
                )

        except Exception as e:
            log_error(errors_file, "thread_scan_error", str(file_path), str(e))

    for thread_id in grouped:
        grouped[thread_id] = sorted(grouped[thread_id], key=lambda x: (x["page_num"], x["path"].name))

    return grouped


def parse_local_thread_group(cfg, thread_meta, thread_files, errors_file):
    thread_id = str(thread_meta["thread_id"])
    all_posts = []
    seen_post_ids = set()
    thread_title_detail = thread_meta.get("thread_title")
    thread_pages_count = max(1, len(thread_files))
    local_paths = []

    page_nums = [x["page_num"] for x in thread_files]
    if 1 not in page_nums:
        log_error(
            errors_file,
            "thread_page1_missing",
            thread_meta["thread_url"],
            "Local HTML exists but page 1 is missing, so exact opening post cannot be guaranteed",
            {"thread_id": thread_id, "pages_found": page_nums}
        )
        return None

    for item in thread_files:
        file_path = item["path"]
        page_num = item["page_num"]
        local_paths.append(str(file_path))

        try:
            html = file_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            if page_num == 1:
                thread_title_detail = extract_thread_title(soup, fallback=thread_title_detail)
                detected_pages = detect_thread_pages_count(soup)
                thread_pages_count = max(thread_pages_count, detected_pages)

            page_posts = parse_posts_from_vbulletin_thread(soup, thread_id, page_num, seen_post_ids)
            all_posts.extend(page_posts)

        except Exception as e:
            log_error(errors_file, "thread_file_parse", str(file_path), str(e), {"thread_id": thread_id, "page_num": page_num})

    if not all_posts:
        return None

    all_posts = sorted(all_posts, key=lambda x: (x["thread_page_number"], int(x["native_post_id"])))

    opening_post = deepcopy(all_posts[0])
    opening_post["type"] = "post"
    opening_post["comment_id"] = None
    opening_post["is_original_post"] = True
    opening_post["order_in_thread"] = 1

    replies = []
    for order_in_thread, post in enumerate(all_posts[1:], start=2):
        item = deepcopy(post)
        item["type"] = "comment"
        item["comment_id"] = item["native_post_id"]
        item["is_original_post"] = False
        item["order_in_thread"] = order_in_thread
        replies.append(item)

    last_message = replies[-1] if replies else opening_post

    return {
        "source_id": cfg["source_id"],
        "source_type": cfg["source_type"],
        "source_mode": cfg["mode"],
        "thread_id": thread_id,
        "thread_url_id": thread_id,
        "thread_title": thread_meta.get("thread_title"),
        "thread_title_detail": thread_title_detail,
        "thread_url": thread_meta.get("thread_url"),
        "listing_category": cfg.get("board_name"),
        "category_id": cfg.get("forum_id"),
        "category_name": cfg.get("board_name"),
        "category_slug": cfg.get("board_slug"),
        "thread_starter": opening_post.get("author") or thread_meta.get("thread_starter"),
        "thread_starter_id": opening_post.get("user_id") or thread_meta.get("thread_starter_id"),
        "opening_post": opening_post,
        "replies": replies,
        "replies_count": len(replies),
        "listing_replies_count": thread_meta.get("replies_count"),
        "views_count": thread_meta.get("views_count"),
        "likes_total": None,
        "last_message_date": last_message.get("date") or thread_meta.get("last_message_date"),
        "last_message_author": last_message.get("author") or thread_meta.get("last_message_author"),
        "last_message_author_id": last_message.get("user_id") or thread_meta.get("last_message_author_id"),
        "thread_pages_count": thread_pages_count,
        "messages_count": 1 + len(replies),
        "comments_count": len(replies),
        "local_thread_files": local_paths,
        "scraped_at": now_iso()
    }


def run_scraper(cfg, config_path, start_page_override=None, end_page_override=None):
    project_root = project_root_from_script()

    listing_dir = resolve_path(cfg["listing_input_dir"], project_root)
    thread_dir = resolve_path(cfg["thread_input_dir"], project_root)
    output_dir = resolve_path(cfg["output"]["dir"], project_root)
    threads_file = resolve_path(cfg["output"]["threads_file"], project_root)
    errors_file = resolve_path(cfg["output"]["errors_file"], project_root)

    output_dir.mkdir(parents=True, exist_ok=True)
    threads_file.parent.mkdir(parents=True, exist_ok=True)
    errors_file.parent.mkdir(parents=True, exist_ok=True)

    existing_thread_ids = load_existing_thread_ids(threads_file) if cfg.get("resume", {}).get("enabled", True) else set()
    existing_before_run = len(existing_thread_ids)
    seen_in_run = set()

    config_start = int(cfg["pagination"]["start_page"])
    config_end = int(cfg["pagination"]["end_page"])
    start_page = start_page_override if start_page_override is not None else config_start
    end_page = end_page_override if end_page_override is not None else config_end

    if start_page > end_page:
        raise ValueError("start_page cannot be greater than end_page")

    all_listing_threads = []
    listing_seen_ids = set()

    print(f"[INFO] source_id={cfg['source_id']}")
    print(f"[INFO] config={config_path}")
    print(f"[INFO] listing_dir={listing_dir}")
    print(f"[INFO] thread_dir={thread_dir}")
    print(f"[INFO] output_threads={threads_file}")
    print(f"[INFO] output_errors={errors_file}")
    print(f"[INFO] resume_existing_threads={existing_before_run}")
    print(f"[INFO] requested_page_range={start_page}-{end_page}")

    for page_num in range(start_page, end_page + 1):
        page_file = listing_dir / f"page{page_num}.html"
        if not page_file.exists():
            log_error(errors_file, "listing_file_missing", str(page_file), "Listing HTML file not found", {"page": page_num})
            print(f"[LISTING] page={page_num} page_threads=0 unique_added=0 total_listing_threads={len(all_listing_threads)}")
            continue

        html = page_file.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        listing_url = cfg["start_url"] if page_num == 1 else urljoin(cfg["start_url"], f"index{page_num}.html")
        page_threads = parse_listing_threads(soup, listing_url, forum_id=str(cfg.get("forum_id")))

        page_new = 0
        for row in page_threads:
            tid = str(row["thread_id"])
            if tid in listing_seen_ids:
                continue
            listing_seen_ids.add(tid)
            row["_listing_page"] = page_num
            all_listing_threads.append(row)
            page_new += 1

        print(f"[LISTING] page={page_num} page_threads={len(page_threads)} unique_added={page_new} total_listing_threads={len(all_listing_threads)}")

    print(f"[INFO] total_listing_threads_in_range={len(all_listing_threads)}")

    local_groups = scan_local_thread_files(thread_dir, errors_file)
    print(f"[INFO] detected_local_thread_groups={len(local_groups)}")

    total_new_threads = 0
    total_skipped_existing = 0
    total_missing_local_html = 0
    parsed_in_current_run = 0
    missing_threads = []

    for idx, thread_meta in enumerate(all_listing_threads, start=1):
        if STOP_REQUESTED:
            break

        thread_id = str(thread_meta["thread_id"])
        listing_page = thread_meta.get("_listing_page")

        if thread_id in existing_thread_ids or thread_id in seen_in_run:
            total_skipped_existing += 1
            print(f"[THREAD] {idx}/{len(all_listing_threads)} page={listing_page} thread_id={thread_id} skipped_existing=1")
            continue

        thread_files = local_groups.get(thread_id, [])

        if not thread_files:
            total_missing_local_html += 1
            missing_threads.append({
                "thread_id": thread_id,
                "listing_page": listing_page,
                "thread_title": thread_meta.get("thread_title"),
                "thread_url": thread_meta.get("thread_url")
            })
            log_error(errors_file, "thread_file_missing", thread_meta["thread_url"], "Local thread HTML not found for this exact thread id", {"thread_id": thread_id, "listing_page": listing_page})
            print(f"[THREAD] {idx}/{len(all_listing_threads)} page={listing_page} thread_id={thread_id} local_html_missing=1")
            continue

        try:
            record = parse_local_thread_group(cfg, thread_meta, thread_files, errors_file)
            if record is None:
                print(f"[THREAD] {idx}/{len(all_listing_threads)} page={listing_page} thread_id={thread_id} exact_parse_skipped=1")
                continue

            append_jsonl(threads_file, record)
            existing_thread_ids.add(thread_id)
            seen_in_run.add(thread_id)
            total_new_threads += 1
            parsed_in_current_run += 1

            mismatch_flag = ""
            if record.get("listing_replies_count") is not None and record["listing_replies_count"] != record["replies_count"]:
                mismatch_flag = " replies_mismatch=1"

            print(
                f"[THREAD] {idx}/{len(all_listing_threads)} "
                f"page={listing_page} "
                f"thread_id={thread_id} "
                f"files={len(thread_files)} "
                f"messages={record['messages_count']} "
                f"comments={record['comments_count']} "
                f"listing_replies={record.get('listing_replies_count')} "
                f"parsed_replies={record['replies_count']}"
                f"{mismatch_flag} "
                f"total_collected={total_new_threads}"
            )

        except Exception as e:
            log_error(errors_file, "thread", thread_meta["thread_url"], str(e), {"thread_id": thread_id, "listing_page": listing_page})
            print(f"[THREAD] {idx}/{len(all_listing_threads)} page={listing_page} thread_id={thread_id} ERROR={e}")

        time.sleep(float(cfg["request"].get("sleep_seconds", 0.0)))

    print("[DONE]")
    print(f"[DONE] page_range={start_page}-{end_page}")
    print(f"[DONE] total_listing_threads_in_range={len(all_listing_threads)}")
    print(f"[DONE] parsed_in_current_run={parsed_in_current_run}")
    print(f"[DONE] skipped_existing_in_current_run={total_skipped_existing}")
    print(f"[DONE] missing_local_html_in_current_run={total_missing_local_html}")
    print(f"[DONE] threads_file={threads_file}")
    print(f"[DONE] errors_file={errors_file}")


def main():
    parser = argparse.ArgumentParser(description="HealthBoards local incremental scraper")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    parser.add_argument("--start-page", type=int, default=None, help="Override start page")
    parser.add_argument("--end-page", type=int, default=None, help="Override end page")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = load_json(config_path)
    run_scraper(cfg, config_path, start_page_override=args.start_page, end_page_override=args.end_page)


if __name__ == "__main__":
    main()
