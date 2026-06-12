import os
import re
import pandas as pd
from tqdm import tqdm
from langdetect import detect, LangDetectException
from transformers import pipeline

# ============================================================
# Stage 3 — Sentiment Analysis
# Internship: Ziwig Morocco · Data Science · May 4–8, 2026
#
# Models:
#   FR  → cmarkea/distilcamembert-base-sentiment
#   EN  → cardiffnlp/twitter-roberta-base-sentiment-latest
#   ALL OTHER LANGUAGES → cardiffnlp/twitter-xlm-roberta-base-sentiment
#
# Language detection:
#   1. Uses the existing "language" column from cleaning
#   2. If missing/null → runs langdetect on body_raw
#   3. If langdetect fails → marked "unknown" and routed to XLM model
#
# Outputs:
#   sentiment_results.csv
#   manual_validation_sample_200.csv
#   sentiment_report.csv
# ============================================================


# ============================================================
# Paths
# ============================================================

CLEANING_DIR = r"E:\S T A G E\Cleaning"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

FINAL_OUTPUT         = os.path.join(OUTPUT_DIR, "sentiment_results.csv")
REPORT_OUTPUT        = os.path.join(OUTPUT_DIR, "sentiment_report.csv")
VALIDATION_OUTPUT    = os.path.join(OUTPUT_DIR, "manual_validation_sample_200.csv")


# ============================================================
# Load cleaned source files
# ============================================================

all_dataframes = []

for source_folder in os.listdir(CLEANING_DIR):
    source_path = os.path.join(CLEANING_DIR, source_folder)

    if not os.path.isdir(source_path):
        continue

    after_path = os.path.join(source_path, "after")

    if not os.path.exists(after_path):
        print(f"Skipping {source_folder}: no after/ folder")
        continue

    expected_file = os.path.join(after_path, f"{source_folder}_after.csv")

    if os.path.exists(expected_file):
        print(f"Loading: {expected_file}")
        try:
            df = pd.read_csv(expected_file, encoding="utf-8")
        except UnicodeDecodeError:
            df = pd.read_csv(expected_file, encoding="latin1")

        df["source_file"]   = os.path.basename(expected_file)
        df["source_folder"] = source_folder
        all_dataframes.append(df)
    else:
        print(f"Warning: file not found for {source_folder} — checked: {expected_file}")

if not all_dataframes:
    raise FileNotFoundError(
        "No cleaned CSV files were found. Check CLEANING_DIR and folder structure."
    )

data = pd.concat(all_dataframes, ignore_index=True)

print("=" * 60)
print(f"Total files loaded : {len(all_dataframes)}")
print(f"Total rows loaded  : {len(data)}")
print("=" * 60)


# ============================================================
# Required columns safety check
# ============================================================

required_columns = [
    "message_type",
    "body_raw",
    "language",
    "source_code",
    "country",
    "author_name",
]

for col in required_columns:
    if col not in data.columns:
        print(f"Warning: '{col}' not found — creating empty column")
        data[col] = None

# Fill null source_code so it is never dropped from groupby
data["source_code"] = data["source_code"].fillna("UNKNOWN")


# ============================================================
# Load sentiment models
# ============================================================

print("\nLoading English sentiment model (Cardiff RoBERTa)...")
english_model = pipeline(
    "sentiment-analysis",
    model="cardiffnlp/twitter-roberta-base-sentiment-latest",
    tokenizer="cardiffnlp/twitter-roberta-base-sentiment-latest",
    truncation=True,
    max_length=512,
)

print("Loading French sentiment model (DistilCamemBERT)...")
french_model = pipeline(
    "sentiment-analysis",
    model="cmarkea/distilcamembert-base-sentiment",
    tokenizer="cmarkea/distilcamembert-base-sentiment",
    truncation=True,
    max_length=512,
)

print("Loading multilingual sentiment model (XLM-RoBERTa — all other languages)...")
multilingual_model = pipeline(
    "sentiment-analysis",
    model="cardiffnlp/twitter-xlm-roberta-base-sentiment",
    tokenizer="cardiffnlp/twitter-xlm-roberta-base-sentiment",
    truncation=True,
    max_length=512,
)

print("All models loaded.\n")


# ============================================================
# Language normalization map
# Raw values from the cleaning stage → normalized ISO code
# Add any new codes you find in your data here
# ============================================================

LANG_MAP = {
    # French
    "fr": "fr", "fra": "fr", "french": "fr", "français": "fr", "francais": "fr",
    # English
    "en": "en", "eng": "en", "english": "en",
    # German
    "de": "de", "deu": "de", "german": "de", "deutsch": "de",
    # Arabic
    "ar": "ar", "ara": "ar", "arabic": "ar", "arabe": "ar",
    # Spanish
    "es": "es", "spa": "es", "spanish": "es", "español": "es", "espagnol": "es",
    # Italian
    "it": "it", "ita": "it", "italian": "it", "italiano": "it",
    # Dutch
    "nl": "nl", "nld": "nl", "dutch": "nl", "nederlands": "nl",
    # Portuguese
    "pt": "pt", "por": "pt", "portuguese": "pt", "português": "pt",
    # Polish
    "pl": "pl", "pol": "pl", "polish": "pl",
    # Turkish
    "tr": "tr", "tur": "tr", "turkish": "tr",
    # Russian
    "ru": "ru", "rus": "ru", "russian": "ru",
    # Romanian
    "ro": "ro", "ron": "ro", "romanian": "ro",
    # Swedish
    "sv": "sv", "swe": "sv", "swedish": "sv",
    # Norwegian
    "no": "no", "nor": "no", "norwegian": "no",
    # Danish
    "da": "da", "dan": "da", "danish": "da",
    # Finnish
    "fi": "fi", "fin": "fi", "finnish": "fi",
    # Greek
    "el": "el", "ell": "el", "greek": "el",
    # Czech
    "cs": "cs", "ces": "cs", "czech": "cs",
    # Hungarian
    "hu": "hu", "hun": "hu", "hungarian": "hu",
}


# ============================================================
# Helper functions
# ============================================================

def clean_text(text):
    if pd.isna(text):
        return ""
    return str(text).strip()


def normalize_lang(raw_lang):
    """Normalize any raw language value to a clean ISO code."""
    if pd.isna(raw_lang):
        return None
    cleaned = str(raw_lang).strip().lower()
    return LANG_MAP.get(cleaned, cleaned)  # Return as-is if not in map


def detect_language(text, existing_language=None):
    """
    Priority:
    1. Use the existing language column from cleaning (most reliable)
    2. Run langdetect on body_raw if existing is null/empty
    3. Return "unknown" if detection fails
    """
    # Step 1: Try existing language column
    normalized = normalize_lang(existing_language)
    if normalized:
        return normalized

    # Step 2: Run langdetect on the raw text
    text = clean_text(text)
    if text == "":
        return "unknown"

    try:
        detected = detect(text)
        return normalize_lang(detected) or detected
    except LangDetectException:
        return "unknown"


def normalize_label(label):
    """
    Converts all model-specific label formats into:
    Positive / Negative / Neutral
    """
    label = str(label).lower().strip()

    # Direct string matches (XLM-RoBERTa and Cardiff both use these)
    if "positive" in label:
        return "Positive"
    if "negative" in label:
        return "Negative"
    if "neutral" in label:
        return "Neutral"

    # Cardiff RoBERTa EN: label_0 / label_1 / label_2
    if label == "label_0":
        return "Negative"
    if label == "label_1":
        return "Neutral"
    if label == "label_2":
        return "Positive"

    # CamemBERT FR: 1 star → 5 stars
    if "1 star" in label or "2 stars" in label:
        return "Negative"
    if "3 stars" in label:
        return "Neutral"
    if "4 stars" in label or "5 stars" in label:
        return "Positive"

    return "Neutral"


def run_sentiment(text, lang):
    """
    Routes text to the correct model based on detected language.

    FR  → French CamemBERT model
    EN  → English RoBERTa model
    ALL OTHERS (de, ar, es, it, nl, pt, pl, tr, ru, unknown, etc.)
         → Multilingual XLM-RoBERTa model

    Returns: (label, score, model_used)
    """
    text = clean_text(text)

    if text == "":
        return "Unanalyzed", 0.0, "none"

    text_for_model = text[:1000]

    try:
        if lang == "fr":
            result = french_model(text_for_model)[0]
            return normalize_label(result["label"]), round(float(result["score"]), 4), "camembert"

        elif lang == "en":
            result = english_model(text_for_model)[0]
            return normalize_label(result["label"]), round(float(result["score"]), 4), "roberta-en"

        else:
            # All other languages + unknown → XLM multilingual model
            result = multilingual_model(text_for_model)[0]
            return normalize_label(result["label"]), round(float(result["score"]), 4), "xlm-roberta"

    except Exception as e:
        print(f"Sentiment error (lang={lang}): {e}")
        return "Unanalyzed", 0.0, "error"


def detect_ziwig(text):
    text = clean_text(text)
    return bool(re.search(r"\bziwig\b", text, flags=re.IGNORECASE))


def extract_ziwig_sentence(text):
    text = clean_text(text)
    if text == "":
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if re.search(r"\bziwig\b", sentence, flags=re.IGNORECASE):
            return sentence
    return ""


# Placeholder patterns: content that looks like text but isn't real human content
PLACEHOLDER_PATTERNS = [
    r"^\[removed\]$",
    r"^\[deleted\]$",
    r"^\[mod removed\]$",
    r"^\[modéré\]$",
    r"^voir plus$",
    r"^\d+\s*like[s]?$",
    r"^\d+\s*réponse[s]?$",
    r"^répondre$",
    r"^reply$",
]

SPAM_PATTERNS = [
    r"http[s]?://",
    r"click here",
    r"buy now",
    r"limited offer",
    r"promo code",
    r"earn money",
    r"visit our website",
    r"subscribe now",
    r"free trial",
    r"cliquez ici",
    r"offre limitée",
]

def is_human_like(text):
    """
    Returns True when the message reads as authentic human content.
    Filters: placeholders, spam, scraped UI artifacts, very short text.
    """
    text = clean_text(text)
    if text == "":
        return False

    lower = text.lower().strip()

    # Reject known placeholder patterns (Reddit [removed], forum artifacts)
    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, lower):
            return False

    # Reject spam patterns
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, lower):
            return False

    # Reject very short content (less than 5 words)
    if len(text.split()) < 5:
        return False

    return True


# Honest expression: personal experience markers in EN and FR
HONEST_PATTERNS_EN = [
    r"\bi feel\b", r"\bi felt\b", r"\bi have been\b", r"\bmy experience\b",
    r"\bin my case\b", r"\bfor me\b", r"\bi was diagnosed\b", r"\bi suffer\b",
    r"\bi've had\b", r"\bi've been\b", r"\bmy doctor\b", r"\bmy symptoms\b",
    r"\bmy pain\b", r"\bmy body\b", r"\bi went through\b", r"\bi struggled\b",
]

HONEST_PATTERNS_FR = [
    r"\bje ressens\b", r"\bje souffre\b", r"\bmon cas\b", r"\bpour moi\b",
    r"\bj'ai été\b", r"\bchez moi\b", r"\bmon médecin\b", r"\bmes douleurs\b",
    r"\bj'ai souffert\b", r"\bj'ai vécu\b", r"\bmon expérience\b",
    r"\bma douleur\b", r"\bje vis avec\b",
]

def is_honest_expression(text):
    """
    Returns True when the message contains a genuine personal experience or opinion.
    Requires at least 10 words AND at least 1 personal marker match.
    """
    text = clean_text(text)
    if len(text.split()) < 10:
        return False

    lower = text.lower()

    en_match = any(re.search(p, lower) for p in HONEST_PATTERNS_EN)
    fr_match = any(re.search(p, lower) for p in HONEST_PATTERNS_FR)

    return en_match or fr_match


# ============================================================
# Sentiment analysis loop
# ============================================================

sentiment_labels  = []
sentiment_scores  = []
models_used       = []
detected_langs    = []
human_flags       = []
honest_flags      = []
ziwig_flags       = []
ziwig_sentiments  = []

print("Running sentiment analysis on all rows...")
print("(FR → CamemBERT | EN → RoBERTa | Other → XLM-RoBERTa)\n")

for _, row in tqdm(data.iterrows(), total=len(data)):
    text = clean_text(row.get("body_raw", ""))
    lang = detect_language(text, row.get("language", None))

    label, score, model_used = run_sentiment(text, lang)

    sentiment_labels.append(label)
    sentiment_scores.append(score)
    models_used.append(model_used)
    detected_langs.append(lang)
    human_flags.append(is_human_like(text))
    honest_flags.append(is_honest_expression(text))

    ziwig_mentioned = detect_ziwig(text)
    ziwig_flags.append(ziwig_mentioned)

    if ziwig_mentioned:
        ziwig_sentence = extract_ziwig_sentence(text)
        ziwig_label, _, _ = run_sentiment(ziwig_sentence, lang)
        ziwig_sentiments.append(ziwig_label)
    else:
        ziwig_sentiments.append(None)

# Write results back to dataframe
data["detected_language"] = detected_langs   # normalized ISO code
data["sentiment_label"]   = sentiment_labels  # Positive / Negative / Neutral / Unanalyzed
data["sentiment_score"]   = sentiment_scores  # 0.0–1.0
data["model_used"]        = models_used       # camembert / roberta-en / xlm-roberta / none / error
data["is_human"]          = human_flags
data["is_honest"]         = honest_flags
data["ziwig_mentioned"]   = ziwig_flags
data["ziwig_sentiment"]   = ziwig_sentiments

# Drop permanently empty columns before saving
COLS_TO_DROP = ["attachment_url", "scrape_error", "website_links"]
data = data.drop(columns=[c for c in COLS_TO_DROP if c in data.columns])


# ============================================================
# Save full sentiment results
# ============================================================

data.to_csv(FINAL_OUTPUT, index=False, encoding="utf-8-sig")
print(f"\nSaved full results → {FINAL_OUTPUT}")
print(f"Total rows: {len(data)} | Columns: {len(data.columns)}")


# ============================================================
# Language distribution report (new — shows all languages)
# ============================================================

print("\n--- Language distribution in dataset ---")
lang_dist = data["detected_language"].value_counts(dropna=False)
print(lang_dist.to_string())


# ============================================================
# Manual validation sample (100 FR + 100 EN, stratified)
# ============================================================

fr_rows = data[data["detected_language"] == "fr"]
en_rows = data[data["detected_language"] == "en"]

fr_sample = fr_rows.sample(n=min(100, len(fr_rows)), random_state=42)
en_sample = en_rows.sample(n=min(100, len(en_rows)), random_state=42)

validation_sample = pd.concat([fr_sample, en_sample], ignore_index=True)

# Add empty column for human review
validation_sample["manual_label"] = ""

validation_sample.to_csv(VALIDATION_OUTPUT, index=False, encoding="utf-8-sig")
print(f"\nSaved validation sample → {VALIDATION_OUTPUT}")
print(f"({len(fr_sample)} FR + {len(en_sample)} EN rows)")


# ============================================================
# Sentiment report — per source, per language, all languages shown
# ============================================================

# --- Sentiment by source ---
source_sentiment = (
    data.groupby(["source_code", "sentiment_label"], dropna=False)
    .size()
    .reset_index(name="count")
)

source_totals = (
    data.groupby("source_code", dropna=False)
    .size()
    .reset_index(name="total")
)

report = source_sentiment.merge(source_totals, on="source_code")
report["percentage"] = round((report["count"] / report["total"]) * 100, 2)

# Flag sources that are 100% Unanalyzed (warning signal)
unanalyzed_sources = (
    report[report["sentiment_label"] == "Unanalyzed"]
    .query("percentage == 100.0")["source_code"]
    .tolist()
)
if unanalyzed_sources:
    print(f"\nWARNING: These sources are 100% Unanalyzed (language issue): {unanalyzed_sources}")

report.to_csv(REPORT_OUTPUT, index=False, encoding="utf-8-sig")
print(f"\nSaved sentiment report → {REPORT_OUTPUT}")

# --- Language coverage per source (new) ---
lang_coverage = (
    data.groupby(["source_code", "detected_language"], dropna=False)
    .size()
    .reset_index(name="count")
    .sort_values(["source_code", "count"], ascending=[True, False])
)
print("\n--- Language coverage per source ---")
print(lang_coverage.to_string(index=False))

# --- Console summary ---
print("\n--- Sentiment distribution (all sources combined) ---")
print(data["sentiment_label"].value_counts(dropna=False).to_string())

print("\n--- Model routing summary ---")
print(data["model_used"].value_counts(dropna=False).to_string())

print("\n--- Top 5 most negative sources ---")
neg = report[report["sentiment_label"] == "Negative"].sort_values("percentage", ascending=False).head(5)
print(neg[["source_code", "count", "total", "percentage"]].to_string(index=False))

print("\n--- Top 5 most positive sources ---")
pos = report[report["sentiment_label"] == "Positive"].sort_values("percentage", ascending=False).head(5)
print(pos[["source_code", "count", "total", "percentage"]].to_string(index=False))

print("\n--- Ziwig sentiment breakdown ---")
print(data[data["ziwig_mentioned"] == True]["ziwig_sentiment"].value_counts(dropna=False).to_string())

print("\nStage 3 complete.")