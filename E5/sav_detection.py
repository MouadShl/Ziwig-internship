import pandas as pd
import os

os.makedirs("outputs", exist_ok=True)

keywords = ["livraison", "retard", "remboursement", "retour", "cassé",
            "défaut", "service client", "problème", "réclamation", "erreur"]

df = pd.read_csv("sentiment_results.csv", low_memory=False).dropna(subset=["body_clean"])
df["is_sav"] = df["body_clean"].str.lower().apply(
    lambda x: any(k in x for k in keywords)
)

sav_df = df[df["is_sav"] == True]
sav_df.to_csv("outputs/sav_irritants.csv", index=False)
print(f"Done! {len(sav_df)} SAV mentions found.")
