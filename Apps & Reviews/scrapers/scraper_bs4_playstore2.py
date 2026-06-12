#!/usr/bin/env python3
import json
import os
from datetime import datetime
from google_play_scraper import reviews_all, Sort

def now():
    return datetime.utcnow().isoformat()

def fetch_comments(app_id, lang="en", country="us"):
    data = reviews_all(
        app_id,
        lang=lang,
        country=country,
        sort=Sort.NEWEST
    )
    results = []
    for r in data:
        results.append({
            "user_name": r.get("userName"),
            "score": r.get("score"),
            "text": r.get("content"),
            "like_count": r.get("thumbsUpCount"),
            "date": str(r.get("at")),
            "scraped_at": now()
        })
    return results

def save_comments_json(comments, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("[\n")
        for i, c in enumerate(comments):
            line = json.dumps(c, ensure_ascii=False)
            if i < len(comments) - 1:
                f.write(f"  {line},\n")
            else:
                f.write(f"  {line}\n")
        f.write("]\n")

def main():
    app_id = "com.clue.android"
    output = "outputs/SRC002/SRC002_comments.json"
    comments = fetch_comments(app_id)
    if not comments:
        print("No comments fetched")
        return
    save_comments_json(comments, output)
    print(f"Saved {len(comments)} comments to {output}")

if __name__ == "__main__":
    main()
