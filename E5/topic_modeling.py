import pandas as pd
from bertopic import BERTopic
import os

os.makedirs("outputs", exist_ok=True)

df = pd.read_csv("sentiment_results.csv", low_memory=False)
df = df.dropna(subset=["body_clean"])
docs = df["body_clean"].astype(str).tolist()

model = BERTopic(language="multilingual", calculate_probabilities=True, verbose=True)
topics, probs = model.fit_transform(docs)

df["topic"] = topics
df[["body_clean", "topic"]].to_csv("outputs/topics_output.csv", index=False)
print("Done! topics_output.csv saved.")
