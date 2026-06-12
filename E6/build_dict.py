"""
build_dict.py — Generate E6_data_dictionary.xlsx from E6_schema.sql
"""

import re
import pandas as pd

SQL_FILE = r"E:\S T A G E\E6\E6_schema.sql"
OUTPUT_FILE = r"E:\S T A G E\E6\E6_data_dictionary.xlsx"

def parse_schema(sql_path):
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()

    # Find all CREATE TABLE blocks
    table_pattern = r"CREATE TABLE\s+(\w+)\s*\((.*?)\);"
    tables = re.findall(table_pattern, sql, re.DOTALL | re.IGNORECASE)

    rows = []
    for table_name, body in tables:
        # Clean up table name (remove dbo. if present)
        table_name = table_name.replace("dbo.", "")

        # Split columns, handling commas inside parentheses
        lines = []
        current = ""
        paren_depth = 0

        for char in body:
            if char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "," and paren_depth == 0:
                lines.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            lines.append(current.strip())

        for line in lines:
            line = line.strip()
            if not line or line.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "INDEX", "CREATE")):
                continue

            # Parse column definition: name type [constraints]
            match = re.match(r"(\w+)\s+(\w+(?:\(\d+(?:,\d+)?\))?)\s*(.*)", line)
            if match:
                col_name, data_type, constraints = match.groups()
                constraints = constraints.strip()

                is_nullable = "NOT NULL" not in constraints.upper()
                is_pk = "PRIMARY KEY" in constraints.upper() or "IDENTITY" in constraints.upper()
                is_fk = any(fk in constraints.upper() for fk in ["REFERENCES", "FOREIGN KEY"])
                default = re.search(r"DEFAULT\s+(\S+)", constraints, re.I)
                default_val = default.group(1) if default else None

                rows.append({
                    "Table": table_name,
                    "Column": col_name,
                    "Data Type": data_type,
                    "Nullable": "YES" if is_nullable else "NO",
                    "Primary Key": "YES" if is_pk else "NO",
                    "Foreign Key": "YES" if is_fk else "NO",
                    "Default": default_val,
                    "Description": "",  # Fill manually or from comments
                    "Constraints": constraints,
                })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = parse_schema(SQL_FILE)

    # Reorder columns
    cols = ["Table", "Column", "Data Type", "Nullable", "Primary Key", "Foreign Key", "Default", "Description", "Constraints"]
    df = df[cols]

    # Write to Excel with formatting
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Data Dictionary", index=False)

        # Auto-adjust column widths
        worksheet = writer.sheets["Data Dictionary"]
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column_letter].width = adjusted_width

    print(f"✅ Data dictionary saved to: {OUTPUT_FILE}")
    print(f"   {len(df)} columns documented across {df['Table'].nunique()} tables")