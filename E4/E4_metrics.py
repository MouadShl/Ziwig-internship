import pandas as pd
import pyodbc

# Connect to SQL Server
conn = pyodbc.connect(
    'DRIVER={SQL Server};'
    'SERVER=DESKTOP-68QU840;'
    'DATABASE=EndometriosisDW;'
    'Trusted_Connection=yes;'
)

# Metrics by source
metrics = pd.read_sql("""
    SELECT 
        s.source_name,
        s.country,
        s.source_platform,
        COUNT(*) as total_posts,
        AVG(f.sentiment_score) as avg_sentiment,
        SUM(CASE WHEN f.sentiment_label = 'Positive' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as pct_positive,
        SUM(CASE WHEN f.sentiment_label = 'Negative' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as pct_negative,
        AVG(CAST(f.likes_count AS FLOAT)) as avg_likes,
        AVG(CAST(f.post_reply_count AS FLOAT)) as avg_replies
    FROM fact_posts f
    JOIN dim_source s ON f.source_key = s.source_key
    GROUP BY s.source_name, s.country, s.source_platform
    ORDER BY total_posts DESC
""", conn)

metrics.to_csv(r'E:\S T A G E\E4\metrics_by_source.csv', index=False)
print("metrics_by_source.csv saved!")
print(metrics.to_string())

# Country scoring
country = pd.read_sql("""
    SELECT 
        s.country,
        COUNT(*) as total_posts,
        AVG(f.sentiment_score) as avg_sentiment,
        SUM(CASE WHEN f.sentiment_label = 'Positive' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as pct_positive,
        SUM(CASE WHEN f.sentiment_label = 'Negative' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as pct_negative
    FROM fact_posts f
    JOIN dim_source s ON f.source_key = s.source_key
    GROUP BY s.country
    ORDER BY total_posts DESC
""", conn)

# Scoring 0-100
country['opportunity_score'] = (
    (country['pct_positive'] / 100 * 50) +
    (country['total_posts'] / country['total_posts'].max() * 50)
).round(1)

country['ranking'] = pd.cut(
    country['opportunity_score'],
    bins=[0, 33, 66, 100],
    labels=['Watchlist', 'Medium', 'High']
)

country.to_csv(r'E:\S T A G E\E4\country_scoring.csv', index=False)
print("country_scoring.csv saved!")
print(country.to_string())

conn.close()