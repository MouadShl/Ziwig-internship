import pandas as pd
import re
from tqdm import tqdm
from transformers import pipeline
from langdetect import detect, LangDetectException

# ============================================================
# PATCH — Re-analyze all "Unanalyzed" rows using XLM-RoBERTa
# Replaces "Unanalyzed" with real labels in-place
# ============================================================

INPUT_FILE  = r"E:\S T A G E\E3\outputs\sentiment_results.csv"
OUTPUT_FILE = r"E:\S T A G E\E3\outputs\sentiment_results.csv"
REPORT_FILE = r"E:\S T A G E\E3\outputs\sentiment_report.csv"

print("Loading sentiment_results.csv...")
df = pd.read_csv(INPUT_FILE, low_memory=False)

unanalyzed_mask = df["sentiment_label"] == "Unanalyzed"
print(f"Total Unanalyzed rows to fix: {unanalyzed_mask.sum()}")

# Rows with genuinely empty body_raw → Neutral silently, no model needed
empty_mask = unanalyzed_mask & (df["body_raw"].isna() | (df["body_raw"].astype(str).str.strip() == ""))
print(f"  → Empty body_raw (will become Neutral/0.0): {empty_mask.sum()}")

df.loc[empty_mask, "sentiment_label"] = "Neutral"
df.loc[empty_mask, "sentiment_score"]  = 0.0
df.loc[empty_mask, "model_used"]       = "none-empty"

# Rows with actual text but failed → run through XLM
to_reanalyze_mask = unanalyzed_mask & ~empty_mask
print(f"  → Has text, needs reanalysis via XLM-RoBERTa: {to_reanalyze_mask.sum()}")

if to_reanalyze_mask.sum() > 0:
    print("\nLoading XLM-RoBERTa multilingual model...")
    xlm_model = pipeline(
        "sentiment-analysis",
        model="cardiffnlp/twitter-xlm-roberta-base-sentiment",
        tokenizer="cardiffnlp/twitter-xlm-roberta-base-sentiment",
        truncation=True,
        max_length=512,
    )

    def normalize_label(label):
        label = str(label).lower().strip()
        if "positive" in label: return "Positive"
        if "negative" in label: return "Negative"
        if "neutral"  in label: return "Neutral"
        if label == "label_0":  return "Negative"
        if label == "label_1":  return "Neutral"
        if label == "label_2":  return "Positive"
        return "Neutral"

    indices = df[to_reanalyze_mask].index
    new_labels = []
    new_scores = []

    print("Re-analyzing rows with XLM-RoBERTa...")
    for idx in tqdm(indices):
        text = str(df.at[idx, "body_raw"])[:1000]
        try:
            result = xlm_model(text)[0]
            new_labels.append(normalize_label(result["label"]))
            new_scores.append(round(float(result["score"]), 4))
        except Exception as e:
            # Last resort — truly cannot analyze → Neutral
            new_labels.append("Neutral")
            new_scores.append(0.0)

    df.loc[indices, "sentiment_label"] = new_labels
    df.loc[indices, "sentiment_score"]  = new_scores
    df.loc[indices, "model_used"]       = "xlm-roberta-patch"

print("\nVerifying no Unanalyzed rows remain...")
remaining = (df["sentiment_label"] == "Unanalyzed").sum()
print(f"Remaining Unanalyzed: {remaining}")

# Save patched sentiment_results
print("Saving patched sentiment_results.csv...")
df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
print(f"Saved → {OUTPUT_FILE}")

# Regenerate sentiment_report
print("\nRegenerating sentiment_report.csv...")
df["source_code"] = df["source_code"].fillna("UNKNOWN")

source_sentiment = (
    df.groupby(["source_code", "sentiment_label"], dropna=False)
    .size()
    .reset_index(name="count")
)
source_totals = (
    df.groupby("source_code", dropna=False)
    .size()
    .reset_index(name="total")
)

report = source_sentiment.merge(source_totals, on="source_code")
report["percentage"] = round((report["count"] / report["total"]) * 100, 2)
report.to_csv(REPORT_FILE, index=False, encoding="utf-8-sig")
print(f"Saved → {REPORT_FILE}")

print("\n--- Final sentiment distribution ---")
print(df["sentiment_label"].value_counts(dropna=False).to_string())

print("\n--- Top 5 most negative sources ---")
neg = report[report["sentiment_label"] == "Negative"].sort_values("percentage", ascending=False).head(5)
print(neg[["source_code", "count", "total", "percentage"]].to_string(index=False))

print("\n--- Top 5 most positive sources ---")
pos = report[report["sentiment_label"] == "Positive"].sort_values("percentage", ascending=False).head(5)
print(pos[["source_code", "count", "total", "percentage"]].to_string(index=False))

print("\nPatch complete.")