import pandas as pd

manual = pd.read_csv(r"E:\S T A G E\E3\outputs\manual_validation_sample_200.csv")
print(manual.columns.tolist())
print(manual.head(3))