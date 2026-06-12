#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

try:
    from google_play_scraper import Sort, reviews
except Exception:
    Sort = None
    reviews = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_pretty_json_one_per_line(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("[\n")
        for i, row in enumerate(rows):
            payload = json.dumps(row, ensure_ascii=False)
            suffix = "," if i < len(rows) - 1 else ""
            f.write(f"  {payload}{suffix}\n")
        f.write("]\n")


def get_app_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    app_id = qs.get("id", [None])[0]
    if not app_id:
        raise ValueError("Could not find Google Play app id in URL. Expected ?id=<package_name>")
    return app_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch as many Google Play comments as possible and save JSON.")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    return parser.parse_args()


def normalize_review(row: Dict[str, Any], source_id: str, app_id: str, lang: str, country: str, sort_name: str) -> Dict[str, Any]:
    at = row.get("at")
    replied_at = row.get("repliedAt")
    return {
        "source_id": source_id,
        "app_id": app_id,
        "review_id": row.get("reviewId"),
        "user": row.get("userName"),
        "rating": row.get("score"),
        "comment": row.get("content"),
        "likes": row.get("thumbsUpCount", 0),
        "date": at.strftime("%Y-%m-%d %H:%M:%S") if at else None,
        "review_date_iso": at.date().isoformat() if at else None,
        "review_created_version": row.get("reviewCreatedVersion"),
        "app_version": row.get("appVersion"),
        "developer_reply_text": row.get("replyContent"),
        "developer_reply_date": replied_at.strftime("%Y-%m-%d %H:%M:%S") if replied_at else None,
        "fetched_lang": lang,
        "fetched_country": country,
        "fetched_sort": sort_name,
        "scraped_at": now_iso(),
    }


def dedupe_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
    review_id = item.get("review_id")
    if review_id:
        return ("review_id", review_id)
    return (
        "fallback",
        (item.get("user") or "").strip(),
        (item.get("comment") or "").strip(),
        item.get("date"),
        item.get("rating"),
    )


def fetch_batch(app_id: str, lang: str, country: str, sort_name: str, count: int, continuation_token: Any):
    if reviews is None or Sort is None:
        raise RuntimeError("google-play-scraper is not installed. Run: python -m pip install google-play-scraper")
    sort_value = Sort.NEWEST if sort_name == "newest" else Sort.MOST_RELEVANT
    return reviews(
        app_id,
        lang=lang,
        country=country,
        sort=sort_value,
        count=count,
        continuation_token=continuation_token,
    )


def fetch_all_possible_reviews(app_id: str, source_id: str, locales: List[Dict[str, str]], sorts: List[str], max_total: int, sleep_seconds: float) -> List[Dict[str, Any]]:
    seen = set()
    collected: List[Dict[str, Any]] = []

    for locale in locales:
        lang = locale["lang"]
        country = locale["country"]
        for sort_name in sorts:
            continuation_token = None
            empty_rounds = 0
            while True:
                batch, continuation_token = fetch_batch(
                    app_id=app_id,
                    lang=lang,
                    country=country,
                    sort_name=sort_name,
                    count=200,
                    continuation_token=continuation_token,
                )
                if not batch:
                    empty_rounds += 1
                    if empty_rounds >= 1:
                        break
                else:
                    empty_rounds = 0

                new_in_this_batch = 0
                for row in batch:
                    content = row.get("content")
                    if not content or not str(content).strip():
                        continue
                    item = normalize_review(
                        row=row,
                        source_id=source_id,
                        app_id=app_id,
                        lang=lang,
                        country=country,
                        sort_name=sort_name,
                    )
                    key = dedupe_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(item)
                    new_in_this_batch += 1
                    if max_total > 0 and len(collected) >= max_total:
                        return collected

                if continuation_token is None:
                    break
                if new_in_this_batch == 0:
                    break
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    collected.sort(key=lambda x: (x.get("date") or "", x.get("review_id") or ""), reverse=True)
    return collected


def main() -> int:
    args = parse_args()
    config = load_json(Path(args.config))

    source_id = config.get("source_id", "SRC001")
    url = config.get("url")
    if not url:
        raise ValueError("Config must include 'url'.")
    app_id = config.get("app_id") or get_app_id_from_url(url)
    locales = config.get(
        "locales",
        [
            {"lang": "en", "country": "us"},
            {"lang": "en", "country": "gb"},
            {"lang": "de", "country": "de"},
        ],
    )
    sorts = config.get("sorts", ["newest", "most_relevant"])
    max_total = int(config.get("max_reviews", 0))
    sleep_seconds = float(config.get("sleep_seconds", 0.25))
    output_dir = Path(config.get("output_dir", f"outputs/{source_id}"))
    output_file = config.get("output_file", f"{source_id}_comments.json")

    comments = fetch_all_possible_reviews(
        app_id=app_id,
        source_id=source_id,
        locales=locales,
        sorts=sorts,
        max_total=max_total,
        sleep_seconds=sleep_seconds,
    )

    if not comments:
        raise RuntimeError("No text comments were returned. This app may expose ratings without review text, or Google Play may be limiting the public endpoint.")

    output_path = output_dir / output_file
    write_pretty_json_one_per_line(output_path, comments)

    print(f"[OK] Saved {len(comments)} unique text comments to {output_path}")
    print(f"[INFO] App ID: {app_id}")
    print(f"[INFO] Locales tried: {', '.join(f'{x['lang']}-{x['country']}' for x in locales)}")
    print(f"[INFO] Sorts tried: {', '.join(sorts)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
