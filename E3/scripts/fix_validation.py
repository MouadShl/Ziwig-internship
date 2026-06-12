# ============================================================
# SCRIPT DE CORRECTION — Stage 3 Sentiment Analysis
# Ziwig Morocco · Data Science · May 4–8, 2026
#
# Ce script corrige UNIQUEMENT les deux problèmes restants :
#   1. Régénérer le fichier manual_validation_sample_200.csv
#      (l'ancien contient 4 lignes "Unanalyzed")
#   2. Diagnostiquer SRC046 (99.71% Neutral suspect)
#
# NE RETOURNE PAS les 301 707 lignes — s'exécute en < 1 minute
# ============================================================

import pandas as pd

# ============================================================
# CHEMINS — modifier si nécessaire
# ============================================================

# Fichier principal déjà analysé (sentiment_results.csv)
RESULTS_FILE = r"E:\S T A G E\E3\outputs\sentiment_results.csv"

# Fichier de validation à régénérer
VALIDATION_OUTPUT = r"E:\S T A G E\E3\outputs\manual_validation_sample_200.csv"


# ============================================================
# ÉTAPE 1 — Chargement du fichier principal
# ============================================================

print("Chargement de sentiment_results.csv...")

# Lecture du fichier CSV complet avec détection automatique des types
df = pd.read_csv(RESULTS_FILE, low_memory=False)

# Affichage de la forme du fichier pour vérification
print(f"Fichier chargé : {len(df)} lignes × {len(df.columns)} colonnes")


# ============================================================
# ÉTAPE 2 — Vérification rapide avant correction
# ============================================================

# Compter les lignes encore marquées "Unanalyzed" dans le fichier principal
nb_unanalyzed = (df["sentiment_label"] == "Unanalyzed").sum()
print(f"\nLignes 'Unanalyzed' restantes dans sentiment_results : {nb_unanalyzed}")

# Compter les source_code encore marqués "UNKNOWN"
nb_unknown = (df["source_code"] == "UNKNOWN").sum()
print(f"Lignes 'UNKNOWN' dans source_code : {nb_unknown}")

# Afficher le nombre de sources uniques présentes
print(f"Sources uniques présentes : {df['source_code'].nunique()} / 46")


# ============================================================
# ÉTAPE 3 — Régénération du fichier de validation
# (100 FR + 100 EN, sans aucune ligne Unanalyzed)
# ============================================================

print("\n--- Régénération du fichier de validation ---")

# Filtrer uniquement les lignes en français détectées correctement
# et exclure toute ligne encore marquée Unanalyzed
fr_rows = df[
    (df["detected_language"] == "fr") &
    (df["sentiment_label"] != "Unanalyzed")
]

# Filtrer uniquement les lignes en anglais détectées correctement
# et exclure toute ligne encore marquée Unanalyzed
en_rows = df[
    (df["detected_language"] == "en") &
    (df["sentiment_label"] != "Unanalyzed")
]

print(f"Lignes FR disponibles pour l'échantillon : {len(fr_rows)}")
print(f"Lignes EN disponibles pour l'échantillon : {len(en_rows)}")

# Tirer 100 lignes françaises aléatoirement (random_state=42 pour reproductibilité)
fr_sample = fr_rows.sample(n=min(100, len(fr_rows)), random_state=42)

# Tirer 100 lignes anglaises aléatoirement
en_sample = en_rows.sample(n=min(100, len(en_rows)), random_state=42)

# Fusionner les deux échantillons en un seul DataFrame
validation_sample = pd.concat([fr_sample, en_sample], ignore_index=True)

# Ajouter une colonne vide "manual_label" pour que le directeur
# puisse remplir manuellement l'étiquette correcte lors de la validation
validation_sample["manual_label"] = ""

# Sauvegarder le fichier de validation corrigé
validation_sample.to_csv(VALIDATION_OUTPUT, index=False, encoding="utf-8-sig")

print(f"\nFichier de validation sauvegardé → {VALIDATION_OUTPUT}")
print(f"Total : {len(validation_sample)} lignes ({len(fr_sample)} FR + {len(en_sample)} EN)")

# Vérification finale : aucune ligne Unanalyzed ne doit rester
nb_unanalyzed_val = (validation_sample["sentiment_label"] == "Unanalyzed").sum()
print(f"Lignes 'Unanalyzed' dans le fichier de validation : {nb_unanalyzed_val}")

# Afficher la distribution des étiquettes dans l'échantillon
print("\nDistribution des étiquettes dans l'échantillon de validation :")
print(validation_sample["sentiment_label"].value_counts().to_string())


# ============================================================
# ÉTAPE 4 — Diagnostic SRC046 (99.71% Neutral suspect)
# ============================================================

print("\n--- Diagnostic SRC046 ---")

# Isoler toutes les lignes appartenant à la source SRC046
src46 = df[df["source_code"] == "SRC046"]

print(f"Total lignes SRC046 : {len(src46)}")

# Voir comment les lignes ont été traitées par les modèles
print("\nRépartition par modèle utilisé (model_used) :")
print(src46["model_used"].value_counts().to_string())

# Voir la distribution des étiquettes de sentiment
print("\nRépartition des étiquettes de sentiment :")
print(src46["sentiment_label"].value_counts().to_string())

# Voir la langue détectée pour ces lignes
print("\nLangues détectées pour SRC046 :")
print(src46["detected_language"].value_counts(dropna=False).to_string())

# Afficher 10 exemples de body_raw pour comprendre
# le type de contenu de cette source
print("\n10 exemples de body_raw pour SRC046 :")
exemples = src46["body_raw"].dropna().head(10).tolist()
for i, exemple in enumerate(exemples, 1):
    # Tronquer à 150 caractères pour lisibilité
    print(f"  [{i}] {str(exemple)[:150]}")

# Si tous les body_raw sont vides → source structurée sans texte libre
# Si body_raw contient du texte → problème de langue non détectée
nb_vides = src46["body_raw"].isna().sum() + (src46["body_raw"].astype(str).str.strip() == "").sum()
print(f"\nLignes SRC046 avec body_raw vide : {nb_vides} / {len(src46)}")
print(f"Lignes SRC046 avec body_raw rempli : {len(src46) - nb_vides} / {len(src46)}")


# ============================================================
# RÉSUMÉ FINAL
# ============================================================

print("\n" + "=" * 60)
print("RÉSUMÉ DES CORRECTIONS APPLIQUÉES")
print("=" * 60)
print(f"✅ Fichier de validation régénéré : {len(validation_sample)} lignes propres")
print(f"✅ Lignes Unanalyzed dans validation : {nb_unanalyzed_val}")
print(f"✅ Sources présentes dans results   : {df['source_code'].nunique()} / 46")
print(f"✅ Lignes UNKNOWN restantes         : {nb_unknown}")
print(f"ℹ️  SRC046 — voir diagnostic ci-dessus pour décision")
print("=" * 60)
print("\nScript terminé.")