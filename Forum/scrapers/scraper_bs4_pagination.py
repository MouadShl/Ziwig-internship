#!/usr/bin/env python3
"""
Generic Requests + BeautifulSoup scraper for listing -> detail pages.

Use this for:
- forums
- blog listings
- directory listings
- category pages
- paginated sources

Per source, only change the JSON config.
"""

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def clean_text(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def parse_int(value):
    if value is None:
        return None
    s = re.sub(r"[^\d]", "", str(value))
    return int(s) if s.isdigit() else None


def read_value(node, spec, base_url=None):
    if not spec:
        return None

    if isinstance(spec, str):
        el = node.select_one(spec)
        return clean_text(el.get_text(" ", strip=True)) if el else None

    selector = spec.get("selector")
    attr = spec.get("attr")
    value_type = spec.get("type", "text")

    el = node.select_one(selector) if selector else node
    if not el:
        return None

    if attr:
        raw = el.get(attr)
        if raw and base_url:
            raw = urljoin(base_url, raw)
    else:
        raw = el.get_text(" ", strip=True)

    raw = clean_text(raw)

    if value_type == "int":
        return parse_int(raw)

    return raw


def extract_fields(node, fields_cfg, base_url=None):
    data = {}
    for field_name, spec in fields_cfg.items():
        data[field_name] = read_value(node, spec, base_url=base_url)
    return data


def crawl_pages(session, start_url, page_cfg, timeout, sleep_seconds):
    visited = set()
    current_url = start_url
    max_pages = int(page_cfg.get("max_pages", 1))

    for _ in range(max_pages):
        if not current_url or current_url in visited:
            break
        visited.add(current_url)

        r = session.get(current_url, timeout=timeout)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "lxml")
        yield current_url, soup

        next_page_spec = page_cfg.get("next_page")
        if not next_page_spec:
            break

        next_url = read_value(soup, next_page_spec, base_url=current_url)
        if not next_url or next_url == current_url:
            break

        current_url = next_url
        time.sleep(sleep_seconds)


def scrape_detail_url(session, detail_url, cfg):
    timeout = cfg.get("request", {}).get("timeout_seconds", 30)
    sleep_seconds = cfg.get("request", {}).get("sleep_seconds", 1.0)
    detail_cfg = cfg.get("detail", {})

    pages = list(crawl_pages(session, detail_url, detail_cfg, timeout, sleep_seconds))

    item = {
        "source_id": cfg["source_id"],
        "source_type": cfg.get("source_type", "list_detail"),
        "detail_url": detail_url,
    }

    page_fields_cfg = detail_cfg.get("page_fields", {})
    for page_url, soup in pages:
        fields = extract_fields(soup, page_fields_cfg, base_url=page_url)
        for k, v in fields.items():
            if item.get(k) is None and v is not None:
                item[k] = v

    posts_cfg = detail_cfg.get("posts", {})
    block_selector = posts_cfg.get("block_selector")
    post_fields_cfg = posts_cfg.get("fields", {})

    all_posts = []
    if block_selector:
        for page_index, (page_url, soup) in enumerate(pages, start=1):
            blocks = soup.select(block_selector)
            for idx, block in enumerate(blocks, start=1):
                post = extract_fields(block, post_fields_cfg, base_url=page_url)
                post["detail_page_number"] = page_index
                post["post_sequence_on_page"] = idx
                all_posts.append(post)

        for global_idx, post in enumerate(all_posts, start=1):
            post["type"] = "post" if global_idx == 1 else "comment"
            post["is_original_post"] = (global_idx == 1)

    item["posts"] = all_posts
    item["posts_count"] = len(all_posts)
    item["comments_count"] = max(len(all_posts) - 1, 0)

    return item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    cfg = load_config(args.config)

    output_dir = Path(cfg["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    posts_file = output_dir / cfg["output"].get("posts_jsonl", "posts.jsonl")
    errors_file = output_dir / cfg["output"].get("errors_jsonl", "errors.jsonl")

    session = requests.Session()
    session.headers.update(cfg.get("request", {}).get("headers", {"User-Agent": "Mozilla/5.0"}))

    timeout = cfg.get("request", {}).get("timeout_seconds", 30)
    sleep_seconds = cfg.get("request", {}).get("sleep_seconds", 1.0)

    listing_cfg = cfg.get("listing", {})
    item_block_selector = listing_cfg["item_block_selector"]
    detail_link_spec = listing_cfg["detail_link"]
    listing_fields_cfg = listing_cfg.get("fields", {})

    seen_detail_urls = set()

    for seed_url in cfg.get("start_urls", []):
        try:
            for listing_url, soup in crawl_pages(session, seed_url, listing_cfg, timeout, sleep_seconds):
                item_blocks = soup.select(item_block_selector)

                for block in item_blocks:
                    detail_url = read_value(block, detail_link_spec, base_url=listing_url)
                    if not detail_url or detail_url in seen_detail_urls:
                        continue

                    seen_detail_urls.add(detail_url)

                    try:
                        listing_meta = extract_fields(block, listing_fields_cfg, base_url=listing_url)
                        detail_item = scrape_detail_url(session, detail_url, cfg)

                        item = {
                            "source_id": cfg["source_id"],
                            "source_type": cfg.get("source_type", "list_detail"),
                            **listing_meta,
                            **detail_item,
                        }

                        append_jsonl(posts_file, item)

                    except Exception as e:
                        append_jsonl(errors_file, {
                            "source_id": cfg["source_id"],
                            "detail_url": detail_url,
                            "error": str(e),
                        })

                    time.sleep(sleep_seconds)

        except Exception as e:
            append_jsonl(errors_file, {
                "source_id": cfg["source_id"],
                "listing_url": seed_url,
                "error": str(e),
            })

    print(f"Done. Output written to: {posts_file}")


if __name__ == "__main__":
    main()