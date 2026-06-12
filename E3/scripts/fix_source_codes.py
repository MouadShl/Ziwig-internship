import pandas as pd

df = pd.read_csv(r"E:\S T A G E\E3\outputs\sentiment_results.csv", low_memory=False)

# Fill source_code from source_folder where it is UNKNOWN
mask = df["source_code"] == "UNKNOWN"
df.loc[mask, "source_code"] = df.loc[mask, "source_folder"].str.extract(r"(SRC\d+)")[0]

# Verify
print(df["source_code"].value_counts().sort_index().to_string())

# Save
df.to_csv(r"E:\S T A G E\E3\outputs\sentiment_results.csv", index=False, encoding="utf-8-sig")

# Regenerate report
source_sentiment = df.groupby(["source_code","sentiment_label"], dropna=False).size().reset_index(name="count")
source_totals = df.groupby("source_code", dropna=False).size().reset_index(name="total")
report = source_sentiment.merge(source_totals, on="source_code")
report["percentage"] = round((report["count"]/report["total"])*100, 2)
report.to_csv(r"E:\S T A G E\E3\outputs\sentiment_report.csv", index=False, encoding="utf-8-sig")
print("Done — report now has", report['source_code'].nunique(), "sources")