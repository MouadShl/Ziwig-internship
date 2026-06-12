import pandas as pd
import os

os.makedirs("outputs", exist_ok=True)

# Load topics output
df = pd.read_csv("outputs/topics_output.csv")

# Remove noise topic (-1)
df = df[df["topic"] != -1]

# Define Axe1 = Medical/Symptomes, Axe2 = SAV/Experience
axe1_keywords = [
    "douleur", "traitement", "symptome", "médecin", "chirurgie",
    "coelioscopie", "hormone", "enantone", "irm", "diagnostic",
    "grossesse", "ovaire", "kyste", "endométriose", "menopause"
]

axe2_keywords = [
    "retour", "problème", "retard", "erreur", "remboursement",
    "réclamation", "livraison", "cassé", "défaut", "service"
]

def assign_axe(text):
    text = str(text).lower()
    score1 = sum(k in text for k in axe1_keywords)
    score2 = sum(k in text for k in axe2_keywords)
    if score1 == 0 and score2 == 0:
        return "Autre"
    return "Axe1 - Medical & Symptomes" if score1 >= score2 else "Axe2 - SAV & Experience"

print("Assigning axes to topics...")
df["axe"] = df["body_clean"].apply(assign_axe)

# Summary by axe
summary = df.groupby("axe").agg(
    nb_messages=("body_clean", "count"),
    nb_topics=("topic", "nunique")
).reset_index()

print("\n=== Clustering Results ===")
print(summary.to_string(index=False))

# Save
df.to_csv("outputs/topics_clustered_axes.csv", index=False)
summary.to_csv("outputs/axes_summary.csv", index=False)

print("\nDone! Files saved:")
print("  - outputs/topics_clustered_axes.csv")
print("  - outputs/axes_summary.csv")
