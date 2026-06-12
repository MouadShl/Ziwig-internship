-- ============================================================
-- E6 - Data Warehouse Schema (SQL Server)
-- Architecture: Raw → Clean → Curated (Star Schema)
-- ============================================================

-- ============================================================
-- 0. CREATE DATABASE (run once)
-- ============================================================
-- CREATE DATABASE EndometriosisDW;
-- GO
-- USE EndometriosisDW;
-- GO

-- ============================================================
-- 1. DIMENSION TABLES
-- ============================================================

-- dim_source: one row per data source (forum, app, etc.)
IF OBJECT_ID('dim_source', 'U') IS NOT NULL DROP TABLE dim_source;
CREATE TABLE dim_source (
    source_key        INT IDENTITY(1,1) PRIMARY KEY,
    source_code       NVARCHAR(50)  NOT NULL UNIQUE,  -- e.g. SRC001
    source_name       NVARCHAR(200) NOT NULL,          -- e.g. forum_doctissimo
    source_platform   NVARCHAR(100) NOT NULL,          -- forum / app / article / etc.
    source_type       NVARCHAR(100) NULL,
    source_mode       NVARCHAR(100) NULL,
    country           NCHAR(5)      NULL,              -- ISO country code e.g. FR
    language          NCHAR(5)      NULL,              -- ISO language code e.g. fr
    source_file       NVARCHAR(300) NULL,              -- original CSV filename
    source_folder     NVARCHAR(300) NULL,
    created_at        DATETIME2     DEFAULT GETDATE()
);

-- dim_thread: one row per discussion thread
IF OBJECT_ID('dim_thread', 'U') IS NOT NULL DROP TABLE dim_thread;
CREATE TABLE dim_thread (
    thread_key                  INT IDENTITY(1,1) PRIMARY KEY,
    thread_id                   NVARCHAR(100) NOT NULL,
    source_key                  INT           NOT NULL REFERENCES dim_source(source_key),
    thread_title                NVARCHAR(MAX) NULL,
    thread_url                  NVARCHAR(MAX) NULL,
    thread_url_id               NVARCHAR(300) NULL,
    thread_title_detail         NVARCHAR(MAX) NULL,
    thread_starter              NVARCHAR(300) NULL,
    thread_starter_id           NVARCHAR(300) NULL,
    thread_last_message_datetime DATETIME2    NULL,
    thread_pages_count          INT           NULL,
    comments_count              INT           NULL,
    replies_count               INT           NULL,
    listing_replies_count       INT           NULL,
    messages_count              INT           NULL,
    last_message_date           DATETIME2     NULL,
    last_message_author         NVARCHAR(300) NULL,
    last_message_author_id      NVARCHAR(300) NULL,
    listing_category            NVARCHAR(300) NULL,
    category_id                 NVARCHAR(100) NULL,
    category_name               NVARCHAR(300) NULL,
    category_slug               NVARCHAR(300) NULL,
    scraped_at                  DATETIME2     NULL,
    local_thread_files          NVARCHAR(MAX) NULL,
    CONSTRAINT uq_thread UNIQUE (thread_id, source_key)
);

-- dim_author: one row per unique author
IF OBJECT_ID('dim_author', 'U') IS NOT NULL DROP TABLE dim_author;
CREATE TABLE dim_author (
    author_key          INT IDENTITY(1,1) PRIMARY KEY,
    author_user_id      NVARCHAR(200) NOT NULL,
    source_key          INT           NOT NULL REFERENCES dim_source(source_key),
    author_name         NVARCHAR(300) NULL,
    author_display_name NVARCHAR(300) NULL,
    author_username     NVARCHAR(300) NULL,
    CONSTRAINT uq_author UNIQUE (author_user_id, source_key)
);

-- dim_date: calendar dimension (pre-populated or generated on load)
IF OBJECT_ID('dim_date', 'U') IS NOT NULL DROP TABLE dim_date;
CREATE TABLE dim_date (
    date_key    INT  PRIMARY KEY,   -- YYYYMMDD integer
    full_date   DATE NOT NULL UNIQUE,
    year        SMALLINT NOT NULL,
    quarter     TINYINT  NOT NULL,
    month       TINYINT  NOT NULL,
    month_name  NVARCHAR(20) NOT NULL,
    week        TINYINT  NOT NULL,
    day_of_month TINYINT NOT NULL,
    day_name    NVARCHAR(20) NOT NULL,
    is_weekend  BIT NOT NULL DEFAULT 0
);

-- ============================================================
-- 2. FACT TABLE
-- ============================================================

IF OBJECT_ID('fact_posts', 'U') IS NOT NULL DROP TABLE fact_posts;
CREATE TABLE fact_posts (
    post_key              BIGINT IDENTITY(1,1) PRIMARY KEY,

    -- Foreign keys to dimensions
    source_key            INT  NOT NULL REFERENCES dim_source(source_key),
    thread_key            INT  NOT NULL REFERENCES dim_thread(thread_key),
    author_key            INT  NOT NULL REFERENCES dim_author(author_key),
    post_date_key         INT  NULL     REFERENCES dim_date(date_key),

    -- Natural keys
    message_id            NVARCHAR(200) NOT NULL,
    native_post_id        NVARCHAR(200) NULL,
    clean_row_number      BIGINT        NULL,

    -- Post metadata
    message_type          NVARCHAR(50)  NULL,   -- post / comment
    post_datetime         DATETIME2     NULL,
    is_original_post      BIT           NULL,
    thread_page_number    INT           NULL,
    post_sequence_on_page INT           NULL,
    post_date_raw         NVARCHAR(100) NULL,
    post_date_iso_raw     NVARCHAR(100) NULL,

    -- Content
    body_raw              NVARCHAR(MAX) NULL,
    body_clean            NVARCHAR(MAX) NULL,
    body_clean_length     INT           NULL,

    -- Engagement metrics
    likes_count           INT           NULL,
    dislikes_count        INT           NULL,
    likes_total           INT           NULL,
    views_count           INT           NULL,
    post_views_count      INT           NULL,
    post_reply_count      INT           NULL,
    score                 FLOAT         NULL,
    upvotes_count         INT           NULL,
    downvotes_count       INT           NULL,

    -- Review-specific fields (app stores)
    review_id             NVARCHAR(200) NULL,
    review_author         NVARCHAR(300) NULL,
    rating                FLOAT         NULL,
    review_text           NVARCHAR(MAX) NULL,
    review_datetime       DATETIME2     NULL,
    review_created_version NVARCHAR(100) NULL,
    app_version           NVARCHAR(100) NULL,
    developer_reply_text  NVARCHAR(MAX) NULL,
    developer_reply_datetime DATETIME2  NULL,
    has_developer_reply   BIT           NULL,

    -- NLP results
    detected_language     NCHAR(10)     NULL,
    sentiment_label       NVARCHAR(50)  NULL,   -- Positive / Negative / Neutral
    sentiment_score       FLOAT         NULL,
    model_used            NVARCHAR(100) NULL,   -- e.g. camembert
    is_human              BIT           NULL,
    is_honest             BIT           NULL,
    ziwig_mentioned       BIT           NULL,
    ziwig_sentiment       NVARCHAR(50)  NULL,

    -- Manual validation (from manual_validation_sample_200)
    manual_label          NVARCHAR(50)  NULL,

    -- Audit
    loaded_at             DATETIME2     DEFAULT GETDATE(),

    CONSTRAINT uq_post UNIQUE (message_id, source_key)
);

-- ============================================================
-- 3. INDEXES for query performance
-- ============================================================

CREATE NONCLUSTERED INDEX idx_fact_source       ON fact_posts (source_key);
CREATE NONCLUSTERED INDEX idx_fact_thread       ON fact_posts (thread_key);
CREATE NONCLUSTERED INDEX idx_fact_author       ON fact_posts (author_key);
CREATE NONCLUSTERED INDEX idx_fact_date         ON fact_posts (post_date_key);
CREATE NONCLUSTERED INDEX idx_fact_sentiment    ON fact_posts (sentiment_label);
CREATE NONCLUSTERED INDEX idx_fact_message_type ON fact_posts (message_type);
CREATE NONCLUSTERED INDEX idx_fact_ziwig        ON fact_posts (ziwig_mentioned);
CREATE NONCLUSTERED INDEX idx_thread_source     ON dim_thread (source_key);
CREATE NONCLUSTERED INDEX idx_author_source     ON dim_author (source_key);

-- ============================================================
-- END OF SCHEMA
-- ============================================================
