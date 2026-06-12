"""
SRC004 — Mumsnet Endometriosis Scraper  (FIXED)
================================================
ROOT CAUSE OF 0 RESULTS:
  The original config called GET /api/v3/search?query=endometriosis&page=1
  Mumsnet's API requires POST /api/v3/search with a JSON body.
  Also missing: XSRF-TOKEN cookie → x-xsrf-token header.

HOW IT WORKS:
  Step 1: GET /search  →  harvest XSRF-TOKEN from cookies
  Step 2: POST /api/v3/search  →  paginate through thread listings
  Step 3: GET /api/v3/talk/threads/{id}  →  fetch full thread + replies
  Step 4: GET /api/v3/talk/threads/{id}/similar-threads  →  extra discovery
"""

import json
import time
import random
import os
import requests
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
BASE_URL       = "https://www.mumsnet.com"
SEARCH_URL     = f"{BASE_URL}/api/v3/search"
THREAD_URL     = f"{BASE_URL}/api/v3/talk/threads/{{thread_id}}"
SIMILAR_URL    = f"{BASE_URL}/api/v3/talk/threads/{{thread_id}}/similar-threads"
QUERY          = "endometriosis"
MAX_PAGES      = 100
SLEEP          = 1.5
OUTPUT_DIR     = "outputs/SRC004"
POSTS_FILE     = os.path.join(OUTPUT_DIR, "SRC004_post_and_comment_final.jsonl")
ERRORS_FILE    = os.path.join(OUTPUT_DIR, "SRC004_errors_final.jsonl")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Session setup ────────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept":          "application/json",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/search",
    "x-post-source":   "Web",
    "x-requested-with":"XMLHttpRequest",
})


def get_xsrf_token():
    """
    Step 1 — Visit the search page to get XSRF-TOKEN cookie.
    Mumsnet sets it via a Set-Cookie header on the first page load.
    """
    print("[step1] Fetching XSRF-TOKEN from search page...")
    resp = session.get(f"{BASE_URL}/search", timeout=30)
    resp.raise_for_status()

    xsrf = session.cookies.get("XSRF-TOKEN")
    if not xsrf:
        # Sometimes it's in a different cookie name
        for name, value in session.cookies.items():
            if "xsrf" in name.lower():
                xsrf = value
                break

    if not xsrf:
        raise RuntimeError("XSRF-TOKEN cookie not found — Mumsnet may have changed cookie names.")

    # Decode URL-encoded token if needed
    from urllib.parse import unquote
    xsrf = unquote(xsrf)

    session.headers["x-xsrf-token"] = xsrf
    # Generate a random socket ID (mimics browser WebSocket ID)
    socket_id = f"{random.randint(1000000000, 9999999999)}.{random.randint(1000000000, 9999999999)}"
    session.headers["x-socket-id"] = socket_id

    print(f"[step1] XSRF-TOKEN obtained. socket-id={socket_id}")
    return xsrf


def search_threads(page: int) -> list:
    """
    Step 2 — POST to /api/v3/search with JSON body.
    Returns list of thread dicts, or empty list if no more results.
    """
    body = {
        "query": QUERY,
        "type":  "op",       # "op" = original posts / threads
        "page":  page
        # "from" omitted → gets all dates
    }
    session.headers["Content-Type"] = "application/json, application/json"

    resp = session.post(SEARCH_URL, json=body, timeout=30)

    if resp.status_code == 422:
        # XSRF expired — re-fetch
        print(f"[search] 422 on page {page} — re-fetching XSRF token...")
        get_xsrf_token()
        resp = session.post(SEARCH_URL, json=body, timeout=30)

    resp.raise_for_status()
    data = resp.json()

    # Response structure: {"data": [...threads...]}
    threads = data.get("data", [])
    return threads


def fetch_thread(thread_id: str) -> dict | None:
    """
    Step 3 — GET /api/v3/talk/threads/{id} for full content + replies.
    """
    url = THREAD_URL.format(thread_id=thread_id)
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log_error(thread_id, str(e), url)
        return None


def fetch_similar(thread_id: str) -> list:
    """
    Step 4 — GET similar threads for extra discovery.
    Returns list of thread IDs.
    """
    url = SIMILAR_URL.format(thread_id=thread_id)
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return [str(t.get("id") or t.get("thread_id", "")) for t in data.get("data", [])]
    except Exception:
        pass
    return []


def build_record(search_item: dict, full_thread: dict | None) -> dict:
    """
    Combine search result + full thread data into a clean output record.
    """
    record = {
        "source_id":      "SRC004",
        "thread_id":      search_item.get("thread_id") or search_item.get("id"),
        "title":          search_item.get("title", {}).get("raw", ""),
        "url":            search_item.get("url", ""),
        "username":       search_item.get("username", ""),
        "date":           search_item.get("date", ""),
        "topic":          search_item.get("topic", {}).get("name", ""),
        "topic_url":      search_item.get("topic", {}).get("url", ""),
        "replies_count":  search_item.get("replies_count", 0),
        "body":           search_item.get("body", {}).get("raw", ""),
        "comments":       [],
        "scraped_at":     datetime.utcnow().isoformat() + "Z",
    }

    # Attach full replies if available
    if full_thread:
        posts = full_thread.get("posts") or full_thread.get("data", {}).get("posts", [])
        for post in posts:
            comment = {
                "post_id":   str(post.get("id", "")),
                "username":  post.get("username", "") or post.get("poster_uid", ""),
                "body":      post.get("message", "") or post.get("body", ""),
                "date":      post.get("created_at", "") or post.get("date", ""),
                "likes":     post.get("num_likes", 0),
            }
            record["comments"].append(comment)

    return record


def log_error(thread_id: str, error: str, url: str = ""):
    with open(ERRORS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "thread_id": thread_id,
            "url": url,
            "error": error,
            "ts": datetime.utcnow().isoformat()
        }) + "\n")


def main():
    print(f"[start] SRC004 Mumsnet scraper — query='{QUERY}'")

    # Load already-scraped thread IDs (resume support)
    scraped_ids = set()
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    scraped_ids.add(json.loads(line)["thread_id"])
                except Exception:
                    pass
    print(f"[resume] existing_thread_ids={len(scraped_ids)}")

    # Step 1 — get XSRF token
    get_xsrf_token()

    all_thread_ids = []          # from search pagination
    extra_ids      = set()       # from similar-threads discovery
    scraped_new    = 0

    # ── Step 2: paginate through search results ──────────────────────────────
    for page in range(1, MAX_PAGES + 1):
        print(f"[search] page={page}...")
        try:
            threads = search_threads(page)
        except Exception as e:
            print(f"[search] ERROR on page {page}: {e}")
            log_error("search", str(e), SEARCH_URL)
            break

        if not threads:
            print(f"[search] No more results at page {page}. Stopping.")
            break

        for t in threads:
            tid = str(t.get("thread_id") or t.get("id", ""))
            if tid:
                all_thread_ids.append((tid, t))

        print(f"[search] page={page} returned {len(threads)} threads (total so far: {len(all_thread_ids)})")
        time.sleep(SLEEP)

    print(f"[discover] total_candidate_threads={len(all_thread_ids)}")

    # ── Step 3: fetch full content for each thread ────────────────────────────
    with open(POSTS_FILE, "a", encoding="utf-8") as out:
        for tid, search_item in all_thread_ids:
            if tid in scraped_ids:
                continue

            print(f"  [thread] {tid} — {search_item.get('title',{}).get('raw','')[:60]}")

            # Fetch full thread
            full = fetch_thread(tid)
            time.sleep(SLEEP)

            # Build and write record
            record = build_record(search_item, full)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            scraped_ids.add(tid)
            scraped_new += 1

            # Step 4 — discover similar threads
            sim_ids = fetch_similar(tid)
            for sim_id in sim_ids:
                if sim_id and sim_id not in scraped_ids:
                    extra_ids.add(sim_id)
            time.sleep(SLEEP * 0.5)

        # ── Also scrape similar-threads discoveries ──────────────────────────
        if extra_ids:
            print(f"\n[similar] Found {len(extra_ids)} additional threads via similar-threads discovery")
            for tid in extra_ids:
                if tid in scraped_ids:
                    continue
                print(f"  [similar] {tid}")
                full = fetch_thread(tid)
                if full:
                    record = build_record({"thread_id": tid, "body": {}, "title": {}, "topic": {}}, full)
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    scraped_ids.add(tid)
                    scraped_new += 1
                time.sleep(SLEEP)

    print(f"\n[done] scraped_new={scraped_new} | output={POSTS_FILE}")


if __name__ == "__main__":
    main()
