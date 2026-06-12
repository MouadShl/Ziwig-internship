import pandas as pd
import spacy
import os

os.makedirs("outputs", exist_ok=True)

nlp = spacy.load("fr_core_news_sm")
df = pd.read_csv("sentiment_results.csv", low_memory=False).dropna(subset=["body_clean"])

results = []
for text in df["body_clean"]:
    doc = nlp(str(text)[:1000])
    for ent in doc.ents:
        results.append({"text": ent.text, "label": ent.label_})

pd.DataFrame(results).to_csv("outputs/entities.csv", index=False)
print("Done! entities.csv saved.")
