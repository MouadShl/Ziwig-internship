"""
E6_etl.py  —  Data Warehouse Load for EndometriosisDW
SQL Server : DESKTOP-68QU840
Database   : EndometriosisDW
Auth       : Windows (trusted connection)

FIXES APPLIED
  1. thread_key & author_key are now optional (NULL allowed)
  2. Deduplicate merged DataFrame before building fact rows
  3. Auto-detect ziwig mentions from body_clean
  4. Pre-flight schema check
  5. post_date_key clamped to dim_date range (2000–2030)
  6. post_date_key sent as clean Python int
  7. OUTER JOIN with SYNTHETIC IDs to keep ALL NLP rows
  8. FALLBACK SOURCE: NLP rows without source_code get "nlp_unknown" assigned
  9. MANUAL VALIDATION FIX: Proper merge tracking + column preservation
"""

import glob
import os
import sys
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from tqdm import tqdm

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

SERVER      = r"DESKTOP-68QU840"
DATABASE    = "EndometriosisDW"
CSV_ROOT    = r"E:\S T A G E\Cleaning"
NLP_FILE    = r"E:\S T A G E\E3\outputs\sentiment_results.csv"
MANUAL_FILE = r"E:\S T A G E\E3\outputs\manual_validation_sample_200.csv"

CONN_STR = (
    f"mssql+pyodbc://{SERVER}/{DATABASE}"
    "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
)

# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def safe_str(val, maxlen=None):
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return None
    return s[:maxlen] if maxlen else s


def safe_dt(val):
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    try:
        ts = pd.to_datetime(str(val), utc=True)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.tz_convert(None)
    except Exception:
        return None


def safe_int(val):
    try:
        v = str(val).strip()
        if v.lower() in ("nan", "none", ""):
            return None
        return int(float(v))
    except Exception:
        return None


def safe_float(val):
    try:
        v = str(val).strip()
        if v.lower() in ("nan", "none", ""):
            return None
        return float(v)
    except Exception:
        return None


def safe_bit(val):
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "oui"):
        return 1
    if s in ("0", "false", "no", "non"):
        return 0
    return None


def date_key(dt):
    if dt is None:
        return None
    try:
        return int(pd.Timestamp(dt).strftime("%Y%m%d"))
    except Exception:
        return None


def normalise_merge_key(s):
    if s is None:
        return None
    s = str(s).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


# ══════════════════════════════════════════════════════════════════
# ENGINE  +  PRE-FLIGHT SCHEMA CHECK
# ══════════════════════════════════════════════════════════════════

def get_engine():
    engine = create_engine(CONN_STR, fast_executemany=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("✅ Connected to SQL Server")
    return engine


def preflight_check(engine):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT COLUMN_NAME, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'fact_posts'
              AND COLUMN_NAME IN ('thread_key', 'author_key')
        """)).fetchall()

    nullable = {row[0]: row[1] for row in result}
    bad = [col for col, val in nullable.items() if val == "NO"]

    if bad:
        print("\n" + "=" * 60)
        print("❌ SCHEMA BLOCKING ERROR")
        print("=" * 60)
        print(f"These columns are still NOT NULL: {', '.join(bad)}")
        print("\nRun this in SSMS and then re-run the script:\n")
        print("    USE EndometriosisDW;")
        print("    ALTER TABLE dbo.fact_posts ALTER COLUMN thread_key INT NULL;")
        print("    ALTER TABLE dbo.fact_posts ALTER COLUMN author_key INT NULL;")
        print("=" * 60 + "\n")
        sys.exit(1)

    print("✅ Schema pre-flight passed (thread_key & author_key are nullable)")


# ══════════════════════════════════════════════════════════════════
# CLEAR TABLES
# ══════════════════════════════════════════════════════════════════

def clear_tables(engine):
    order = ["fact_posts", "dim_thread", "dim_author", "dim_source", "dim_date"]
    with engine.begin() as conn:
        for t in order:
            conn.execute(text(f"DELETE FROM dbo.{t}"))
    print("🗑️  All tables cleared")


# ══════════════════════════════════════════════════════════════════
# DIM_DATE
# ══════════════════════════════════════════════════════════════════

def populate_dim_date(engine):
    print("\n📅 Populating dim_date (2000–2030)...")
    rows = []
    d = date(2000, 1, 1)
    end = date(2030, 12, 31)
    while d <= end:
        rows.append({
            "date_key":     int(d.strftime("%Y%m%d")),
            "full_date":    d,
            "year":         d.year,
            "quarter":      (d.month - 1) // 3 + 1,
            "month":        d.month,
            "month_name":   d.strftime("%B"),
            "week":         int(d.strftime("%V")),
            "day_of_month": d.day,
            "day_name":     d.strftime("%A"),
            "is_weekend":   1 if d.isoweekday() >= 6 else 0,
        })
        d += timedelta(days=1)
    pd.DataFrame(rows).to_sql(
        "dim_date", engine, if_exists="append", index=False, chunksize=50
    )
    print(f"   ✅ {len(rows):,} date rows inserted")


# ══════════════════════════════════════════════════════════════════
# LOAD DATA  —  FIX 7 + 9: SYNTHETIC IDs + OUTER JOIN + MANUAL VALIDATION
# ══════════════════════════════════════════════════════════════════

def load_data():
    # ── CSVs ────────────────────────────────────────────────────
    print("\n📂 Loading CSVs...")
    pattern = os.path.join(CSV_ROOT, "**", "*.csv")
    files = glob.glob(pattern, recursive=True)
    files = [
        f for f in files
        if "sentiment_results" not in f and "manual_validation" not in f
    ]
    print(f"   Found {len(files)} CSV files")

    dfs = []
    for f in tqdm(files, desc="   Reading CSVs"):
        try:
            dfs.append(pd.read_csv(f, low_memory=False, dtype=str))
        except Exception as e:
            print(f"   ⚠️  Could not read {f}: {e}")

    df_csv = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
    print(f"   ✅ {len(df_csv):,} rows from CSVs")

    # ── NLP results ─────────────────────────────────────────────
    print("\n🤖 Loading NLP results...")
    nlp = pd.read_csv(NLP_FILE, low_memory=False, dtype=str)
    print(f"   ✅ {len(nlp):,} NLP rows loaded")

    # ── Manual validation ────────────────────────────────────────
    print("\n✍️  Loading manual validation...")
    manual = pd.read_csv(MANUAL_FILE, low_memory=False, dtype=str)
    print(f"   ✅ {len(manual):,} manual validation rows loaded")

    # ── Determine merge column ───────────────────────────────────
    merge_col = next(
        (c for c in ["message_id", "post_id", "id"] if c in df_csv.columns and c in nlp.columns),
        None,
    )

    if merge_col:
        # Normalise keys on both sides
        df_csv[merge_col] = df_csv[merge_col].apply(normalise_merge_key)
        nlp[merge_col] = nlp[merge_col].apply(normalise_merge_key)

        # ── FIX 7a: Generate synthetic IDs for NLP rows with null keys ──
        nlp_null_mask = nlp[merge_col].isna()
        nlp_null_count = nlp_null_mask.sum()
        if nlp_null_count:
            nlp.loc[nlp_null_mask, merge_col] = (
                "NLP_" + nlp.loc[nlp_null_mask].index.astype(str)
            )
            print(f"   🏷️  Generated synthetic IDs for {nlp_null_count:,} NLP rows with null {merge_col}")

        # ── FIX 7b: deduplicate BEFORE merge ──
        nlp_clean = nlp.drop_duplicates(subset=[merge_col], keep="first").copy()
        csv_clean = df_csv.dropna(subset=[merge_col]).drop_duplicates(subset=[merge_col], keep="first").copy()

        print(f"   📊 NLP side: {len(nlp):,} total → {len(nlp_clean):,} unique (dropped {len(nlp) - len(nlp_clean):,} duplicates)")
        print(f"   📊 CSV side:  {len(df_csv):,} total → {len(csv_clean):,} unique (dropped {len(df_csv) - len(csv_clean):,} null+dup)")

        # ── FIX 7c: OUTER JOIN on cleaned data ──
        nlp_cols = [merge_col] + [c for c in nlp.columns if c not in df_csv.columns]
        df = csv_clean.merge(nlp_clean[nlp_cols], on=merge_col, how="outer", indicator=True)

        # Report merge results
        both_mask = df["_merge"] == "both"
        left_only = df["_merge"] == "left_only"
        right_only = df["_merge"] == "right_only"
        print(f"   🔗 Merge results: {both_mask.sum():,} matched | {left_only.sum():,} CSV-only | {right_only.sum():,} NLP-only")
        print(f"   ✅ Total rows after merge: {len(df):,}")

        # Drop the indicator column
        df = df.drop(columns=["_merge"])

        # ── FIX 9: Merge manual validation with indicator + suffix handling ──
        if merge_col in manual.columns:
            manual[merge_col] = manual[merge_col].apply(normalise_merge_key)
            manual_clean = manual.dropna(subset=[merge_col]).drop_duplicates(subset=[merge_col], keep="first")
            
            # Only bring manual_label column (or any cols not already in df)
            man_cols = [merge_col] + [c for c in manual_clean.columns if c not in df.columns]
            
            # Merge with indicator to track matches
            df = df.merge(manual_clean[man_cols], on=merge_col, how="left", indicator="_manual_merge")
            
            manual_matched = (df["_manual_merge"] == "both").sum()
            print(f"   ✍️  Manual validation matched: {manual_matched:,} rows")
            
            # Drop indicator
            df = df.drop(columns=["_manual_merge"])
            
            # If manual_label_x and manual_label_y exist from previous merges, consolidate
            if "manual_label_x" in df.columns and "manual_label_y" in df.columns:
                df["manual_label"] = df["manual_label_y"].fillna(df["manual_label_x"])
                df = df.drop(columns=["manual_label_x", "manual_label_y"])
            elif "manual_label_y" in df.columns:
                df["manual_label"] = df["manual_label_y"]
                df = df.drop(columns=["manual_label_y"])

    elif len(df_csv) == len(nlp):
        extra = [c for c in nlp.columns if c not in df_csv.columns]
        df = pd.concat(
            [df_csv.reset_index(drop=True), nlp[extra].reset_index(drop=True)], axis=1
        )

    else:
        df = df_csv.copy()
        print("   ⚠️  No common merge key found — using CSV data only")

    # ── Detect ziwig mentions ──
    text_col = next((c for c in ["body_clean", "body_raw", "review_text", "text"] if c in df.columns), None)
    if text_col:
        df["ziwig_mentioned"] = df[text_col].str.lower().str.contains("ziwig", na=False).astype(int)
        ziwig_count = df["ziwig_mentioned"].sum()
        print(f"   🔍 ziwig_mentioned computed from '{text_col}' — {ziwig_count:,} mentions found")
    else:
        print("   ⚠️  No text column found for ziwig detection")

    # ── Audit: manual_label coverage ──
    if "manual_label" in df.columns:
        manual_count = df["manual_label"].notna().sum()
        print(f"   ✍️  manual_label present in {manual_count:,} rows ({manual_count/len(df)*100:.1f}%)")

    print(f"\n   ✅ Final merged dataset: {len(df):,} rows")
    return df


# ══════════════════════════════════════════════════════════════════
# DIM_SOURCE  —  FIX 8: Ensure "nlp_unknown" fallback source exists
# ══════════════════════════════════════════════════════════════════

def populate_dim_source(engine, df):
    print("\n📦 Populating dim_source...")

    col_map = {
        "source_code":     "source_code",
        "source_name":     ["source_name", "source"],
        "source_platform": ["source_platform", "platform"],
        "source_type":     ["source_type", "type"],
        "source_mode":     ["source_mode", "mode"],
        "country":         ["country"],
        "language":        ["language", "lang"],
        "source_file":     ["source_file"],
        "source_folder":   ["source_folder"],
    }

    def pick(row, candidates):
        if isinstance(candidates, str):
            candidates = [candidates]
        for c in candidates:
            v = safe_str(row.get(c))
            if v:
                return v
        return None

    if "source_code" not in df.columns:
        print("   ⚠️  No source_code column — skipping dim_source")
        return {}

    seen_codes = set(df["source_code"].dropna().astype(str).str.strip().unique())
    valid_codes = [c for c in seen_codes if c.lower() not in ("nan", "none", "")]
    print(f"   Found {len(valid_codes)} unique valid sources")

    src_map = {}
    src_rows = (
        df[df["source_code"].isin(valid_codes)]
        .groupby("source_code")
        .first()
        .reset_index()
    )

    with engine.begin() as conn:
        for _, r in src_rows.iterrows():
            code = safe_str(r.get("source_code"), 50)
            if not code:
                continue
            name = pick(r, col_map["source_name"]) or code
            platform = pick(r, col_map["source_platform"]) or "unknown"
            row_data = {
                "source_code":     code,
                "source_name":     name[:200],
                "source_platform": platform[:100],
                "source_type":     safe_str(pick(r, col_map["source_type"]), 100),
                "source_mode":     safe_str(pick(r, col_map["source_mode"]), 100),
                "country":         safe_str(pick(r, col_map["country"]), 5),
                "language":        safe_str(pick(r, col_map["language"]), 5),
                "source_file":     safe_str(pick(r, col_map["source_file"]), 300),
                "source_folder":   safe_str(pick(r, col_map["source_folder"]), 300),
            }
            conn.execute(
                text("""
                    INSERT INTO dbo.dim_source
                        (source_code, source_name, source_platform, source_type,
                         source_mode, country, language, source_file, source_folder)
                    VALUES
                        (:source_code, :source_name, :source_platform, :source_type,
                         :source_mode, :country, :language, :source_file, :source_folder)
                """),
                row_data,
            )
            result = conn.execute(
                text("SELECT source_key FROM dbo.dim_source WHERE source_code = :c"),
                {"c": code},
            ).fetchone()
            if result:
                src_map[code] = result[0]

        # ── FIX 8: Insert fallback "nlp_unknown" source ──
        if "nlp_unknown" not in src_map:
            conn.execute(
                text("""
                    INSERT INTO dbo.dim_source
                        (source_code, source_name, source_platform, source_type,
                         source_mode, country, language, source_file, source_folder)
                    VALUES
                        ('nlp_unknown', 'NLP Unknown Source', 'unknown', 'nlp_fallback',
                         'auto', NULL, NULL, NULL, NULL)
                """),
            )
            result = conn.execute(
                text("SELECT source_key FROM dbo.dim_source WHERE source_code = 'nlp_unknown'")
            ).fetchone()
            if result:
                src_map["nlp_unknown"] = result[0]
                print(f"   ✅ Added fallback source 'nlp_unknown' (key={result[0]})")

    print(f"   ✅ {len(src_map)} sources inserted")
    return src_map


# ══════════════════════════════════════════════════════════════════
# DIM_THREAD
# ══════════════════════════════════════════════════════════════════

def populate_dim_thread(engine, df, src_map):
    print("\n🧵 Populating dim_thread...")

    if "thread_id" not in df.columns:
        print("   ⚠️  No thread_id column — skipping dim_thread")
        return {}

    wanted = [
        "thread_id", "source_code",
        "thread_title", "thread_url", "thread_url_id",
        "thread_title_detail", "thread_starter", "thread_starter_id",
        "thread_last_message_datetime", "thread_pages_count",
        "comments_count", "replies_count", "listing_replies_count",
        "messages_count", "last_message_date", "last_message_author",
        "last_message_author_id", "listing_category", "category_id",
        "category_name", "category_slug", "scraped_at", "local_thread_files",
    ]
    avail = [c for c in wanted if c in df.columns]
    threads = df[avail].drop_duplicates(subset=["thread_id"]).copy()

    rows = []
    for _, r in threads.iterrows():
        sc = safe_str(r.get("source_code"), 50)
        if sc not in src_map:
            continue
        rows.append({
            "thread_id":                   safe_str(r.get("thread_id"), 100),
            "source_key":                  src_map[sc],
            "thread_title":                safe_str(r.get("thread_title")),
            "thread_url":                  safe_str(r.get("thread_url")),
            "thread_url_id":               safe_str(r.get("thread_url_id"), 300),
            "thread_title_detail":         safe_str(r.get("thread_title_detail")),
            "thread_starter":              safe_str(r.get("thread_starter"), 300),
            "thread_starter_id":           safe_str(r.get("thread_starter_id"), 300),
            "thread_last_message_datetime": safe_dt(r.get("thread_last_message_datetime")),
            "thread_pages_count":          safe_int(r.get("thread_pages_count")),
            "comments_count":              safe_int(r.get("comments_count")),
            "replies_count":               safe_int(r.get("replies_count")),
            "listing_replies_count":       safe_int(r.get("listing_replies_count")),
            "messages_count":              safe_int(r.get("messages_count")),
            "last_message_date":           safe_dt(r.get("last_message_date")),
            "last_message_author":         safe_str(r.get("last_message_author"), 300),
            "last_message_author_id":      safe_str(r.get("last_message_author_id"), 300),
            "listing_category":            safe_str(r.get("listing_category"), 300),
            "category_id":                 safe_str(r.get("category_id"), 100),
            "category_name":               safe_str(r.get("category_name"), 300),
            "category_slug":               safe_str(r.get("category_slug"), 300),
            "scraped_at":                  safe_dt(r.get("scraped_at")),
            "local_thread_files":          safe_str(r.get("local_thread_files")),
        })

    if rows:
        pd.DataFrame(rows).to_sql(
            "dim_thread", engine, if_exists="append", index=False, chunksize=50
        )

    thread_map = {}
    with engine.connect() as conn:
        for row in conn.execute(
            text("SELECT thread_key, thread_id FROM dbo.dim_thread")
        ).fetchall():
            thread_map[row[1]] = row[0]

    print(f"   ✅ {len(thread_map):,} threads inserted")
    return thread_map


# ══════════════════════════════════════════════════════════════════
# DIM_AUTHOR
# ══════════════════════════════════════════════════════════════════

def populate_dim_author(engine, df, src_map):
    print("\n👤 Populating dim_author...")

    if "author_user_id" not in df.columns:
        print("   ⚠️  No author_user_id column — skipping dim_author")
        return {}

    wanted = ["author_user_id", "source_code",
              "author_name", "author_display_name", "author_username"]
    avail = [c for c in wanted if c in df.columns]

    df["_auth_lower"] = df["author_user_id"].astype(str).str.lower().str.strip()
    authors = (
        df[avail + ["_auth_lower"]]
        .drop_duplicates(subset=["_auth_lower", "source_code"])
        .copy()
    )

    rows = []
    for _, r in authors.iterrows():
        sc = safe_str(r.get("source_code"), 50)
        if sc not in src_map:
            continue
        uid_original = safe_str(r.get("author_user_id"), 200)
        if not uid_original:
            continue
        uid_lower = uid_original.lower().strip()

        rows.append({
            "author_user_id":      uid_lower,
            "source_key":          src_map[sc],
            "author_name":         uid_original,
            "author_display_name": safe_str(r.get("author_display_name"), 300),
            "author_username":     safe_str(r.get("author_username"), 300),
        })

    if rows:
        pd.DataFrame(rows).to_sql(
            "dim_author", engine, if_exists="append", index=False, chunksize=50
        )

    author_map = {}
    with engine.connect() as conn:
        for row in conn.execute(
            text("SELECT author_key, author_user_id, source_key FROM dbo.dim_author")
        ).fetchall():
            author_map[(row[1].lower(), row[2])] = row[0]

    print(f"   ✅ {len(author_map):,} authors inserted")
    return author_map


# ══════════════════════════════════════════════════════════════════
# FACT_POSTS  —  FIX 8: Use fallback source for NLP-only rows
# ══════════════════════════════════════════════════════════════════

def populate_fact_posts(engine, df, src_map, thread_map, author_map):
    print("\n📊 Populating fact_posts...")

    date_col = next(
        (c for c in ["post_datetime", "post_date", "date", "created_at", "datetime"] if c in df.columns),
        None,
    )

    rows = []
    skipped = 0
    clamped_dates = 0
    fallback_used = 0

    for _, r in tqdm(df.iterrows(), total=len(df), desc="   Building fact rows"):

        # ── FIX 8: Try actual source_code first, fallback to "nlp_unknown" ──
        sc = safe_str(r.get("source_code"), 50)
        source_key = src_map.get(sc)
        if source_key is None:
            source_key = src_map.get("nlp_unknown")
            if source_key is not None:
                fallback_used += 1
        if source_key is None:
            skipped += 1
            continue

        tid = safe_str(r.get("thread_id"), 100)
        thread_key = thread_map.get(tid)

        uid = safe_str(r.get("author_user_id"), 200)
        uid_lower = uid.lower().strip() if uid else None
        author_key = author_map.get((uid_lower, source_key)) if uid_lower else None

        # ── dates ────────────────────────────────────────────────
        post_dt = safe_dt(r.get(date_col)) if date_col else None
        dk = date_key(post_dt)

        # Clamp to dim_date range
        if dk is not None and (dk < 20000101 or dk > 20301231):
            dk = None
            clamped_dates += 1

        msg_id = safe_str(r.get("message_id") or r.get("post_id") or r.get("id"), 200)
        if not msg_id:
            skipped += 1
            continue

        rows.append({
            "source_key":               source_key,
            "thread_key":               thread_key,
            "author_key":               author_key,
            "post_date_key":            dk,
            "message_id":               msg_id,
            "native_post_id":           safe_str(r.get("native_post_id") or r.get("post_id"), 200),
            "clean_row_number":         safe_int(r.get("clean_row_number")),
            "message_type":             safe_str(r.get("message_type"), 50),
            "post_datetime":            post_dt,
            "is_original_post":         safe_bit(r.get("is_original_post")),
            "thread_page_number":       safe_int(r.get("thread_page_number")),
            "post_sequence_on_page":    safe_int(r.get("post_sequence_on_page")),
            "post_date_raw":            safe_str(r.get("post_date_raw"), 100),
            "post_date_iso_raw":        safe_str(r.get("post_date_iso_raw"), 100),
            "body_raw":                 safe_str(r.get("body_raw")),
            "body_clean":               safe_str(r.get("body_clean")),
            "body_clean_length":        safe_int(r.get("body_clean_length")),
            "likes_count":              safe_int(r.get("likes_count")),
            "dislikes_count":           safe_int(r.get("dislikes_count")),
            "likes_total":              safe_int(r.get("likes_total")),
            "views_count":              safe_int(r.get("views_count")),
            "post_views_count":         safe_int(r.get("post_views_count")),
            "post_reply_count":         safe_int(r.get("post_reply_count")),
            "score":                    safe_float(r.get("score")),
            "upvotes_count":            safe_int(r.get("upvotes_count")),
            "downvotes_count":          safe_int(r.get("downvotes_count")),
            "review_id":                safe_str(r.get("review_id"), 200),
            "review_author":            safe_str(r.get("review_author"), 300),
            "rating":                   safe_float(r.get("rating")),
            "review_text":              safe_str(r.get("review_text")),
            "review_datetime":          safe_dt(r.get("review_datetime")),
            "review_created_version":   safe_str(r.get("review_created_version"), 100),
            "app_version":              safe_str(r.get("app_version"), 100),
            "developer_reply_text":     safe_str(r.get("developer_reply_text")),
            "developer_reply_datetime": safe_dt(r.get("developer_reply_datetime")),
            "has_developer_reply":      safe_bit(r.get("has_developer_reply")),
            "detected_language":        safe_str(r.get("detected_language"), 10),
            "sentiment_label":          safe_str(r.get("sentiment_label"), 50),
            "sentiment_score":          safe_float(r.get("sentiment_score")),
            "model_used":               safe_str(r.get("model_used"), 100),
            "is_human":                 safe_bit(r.get("is_human")),
            "is_honest":                safe_bit(r.get("is_honest")),
            "ziwig_mentioned":          safe_bit(r.get("ziwig_mentioned")),
            "ziwig_sentiment":          safe_str(r.get("ziwig_sentiment"), 50),
            "manual_label":             safe_str(r.get("manual_label"), 50),
        })

    if skipped:
        print(f"   ⚠️  {skipped:,} rows skipped (missing source_key or message_id)")
    if fallback_used:
        print(f"   🔄 {fallback_used:,} rows assigned to fallback source 'nlp_unknown'")
    if clamped_dates:
        print(f"   📅 {clamped_dates:,} rows had out-of-range dates set to NULL")

    if not rows:
        print("   ⚠️  No fact rows to insert")
        return

    print(f"   Inserting {len(rows):,} rows in chunks of 50…")
    batch = pd.DataFrame(rows)

    # Ensure post_date_key is Python int
    if "post_date_key" in batch.columns:
        batch["post_date_key"] = batch["post_date_key"].apply(
            lambda x: int(x) if pd.notna(x) else None
        )

    # dedup on SQL UNIQUE constraint
    dup_mask = batch.duplicated(subset=["message_id", "source_key"], keep=False)
    n_dups = dup_mask.sum()
    if n_dups:
        print(f"   ⚠️  {n_dups:,} duplicate (message_id, source_key) rows found; keeping first")
    batch = batch.drop_duplicates(subset=["message_id", "source_key"], keep="first")

    for i in tqdm(range(0, len(batch), 50), desc="   Inserting fact rows"):
        batch.iloc[i : i + 50].to_sql(
            "fact_posts", engine, if_exists="append", index=False, chunksize=50
        )

    print(f"   ✅ {len(batch):,} fact rows inserted")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    engine = get_engine()
    preflight_check(engine)
    clear_tables(engine)
    populate_dim_date(engine)

    df = load_data()

    src_map    = populate_dim_source(engine, df)
    thread_map = populate_dim_thread(engine, df, src_map)
    author_map = populate_dim_author(engine, df, src_map)

    populate_fact_posts(engine, df, src_map, thread_map, author_map)

    print("\n🎉 ETL complete!")