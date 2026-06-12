import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "ctg-studies.json"

OUTPUT_DIR = BASE_DIR / "outputs" / "SRC037"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_JSON = OUTPUT_DIR / "SRC037_threads_final.json"
OUTPUT_JSONL = OUTPUT_DIR / "SRC037_threads_final.jsonl"


def clean(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def safe_get(d, key, default=""):
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def build_thread(study):
    study = as_dict(study)

    proto = as_dict(safe_get(study, "protocolSection", {}))
    ident = as_dict(safe_get(proto, "identificationModule", {}))
    status = as_dict(safe_get(proto, "statusModule", {}))
    sponsor = as_dict(safe_get(proto, "sponsorCollaboratorsModule", {}))
    desc = as_dict(safe_get(proto, "descriptionModule", {}))
    cond = as_dict(safe_get(proto, "conditionsModule", {}))
    outcomes = as_dict(safe_get(proto, "outcomesModule", {}))
    elig = as_dict(safe_get(proto, "eligibilityModule", {}))
    contacts = as_dict(safe_get(proto, "contactsLocationsModule", {}))

    lead_sponsor = as_dict(safe_get(sponsor, "leadSponsor", {}))
    start_struct = as_dict(safe_get(status, "startDateStruct", {}))

    nct = clean(safe_get(ident, "nctId", ""))
    title = clean(safe_get(ident, "briefTitle", ""))
    official = clean(safe_get(ident, "officialTitle", ""))
    sponsor_name = clean(safe_get(lead_sponsor, "name", ""))
    start_date = clean(safe_get(start_struct, "date", ""))
    summary = clean(safe_get(desc, "briefSummary", ""))
    detailed = clean(safe_get(desc, "detailedDescription", ""))

    if not nct:
        return None

    body_parts = [
        f"Title: {title}",
        f"Official Title: {official}",
        f"NCT ID: {nct}",
        f"Sponsor: {sponsor_name}",
        f"Start Date: {start_date}",
    ]

    if summary:
        body_parts += ["", "=== SUMMARY ===", summary]

    if detailed:
        body_parts += ["", "=== DESCRIPTION ===", detailed]

    conditions = as_list(safe_get(cond, "conditions", []))
    if conditions:
        body_parts += ["", "=== CONDITIONS ==="]
        body_parts += [clean(x) for x in conditions if clean(x)]

    primary_outcomes = as_list(safe_get(outcomes, "primaryOutcomes", []))
    if primary_outcomes:
        body_parts += ["", "=== PRIMARY OUTCOMES ==="]
        for item in primary_outcomes:
            item = as_dict(item)
            measure = clean(safe_get(item, "measure", ""))
            if measure:
                body_parts.append(measure)

    eligibility = clean(safe_get(elig, "eligibilityCriteria", ""))
    if eligibility:
        body_parts += ["", "=== ELIGIBILITY ===", eligibility]

    locations = as_list(safe_get(contacts, "locations", []))
    location_lines = []
    for loc in locations:
        loc = as_dict(loc)
        facility = clean(safe_get(loc, "facility", ""))
        city = clean(safe_get(loc, "city", ""))
        country = clean(safe_get(loc, "country", ""))
        line = " | ".join([x for x in [facility, city, country] if x])
        if line:
            location_lines.append(line)

    if location_lines:
        body_parts += ["", "=== LOCATIONS ==="]
        body_parts += location_lines

    full_body = "\n".join(body_parts).strip()

    return {
        "thread_id": nct,
        "thread_title": title,
        "thread_title_detail": official,
        "thread_url": f"https://clinicaltrials.gov/study/{nct}",
        "thread_starter": sponsor_name,
        "thread_starter_url": "",
        "listing_author": sponsor_name,
        "listing_author_url": "",
        "opening_post_date": start_date,
        "opening_post_body": full_body,
        "comments_count": 0,
        "replies_count": 0,
        "posts": [
            {
                "post_id": f"{nct}_1",
                "post_author": sponsor_name,
                "post_date": start_date,
                "post_body": full_body,
            }
        ],
    }


def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    studies = data if isinstance(data, list) else []

    threads = []
    skipped = 0

    for study in studies:
        try:
            thread = build_thread(study)
            if thread:
                threads.append(thread)
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            print(f"Skipped item: {e}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(threads, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        for row in threads:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Saved {len(threads)} threads")
    print(f"Skipped {skipped} items")
    print(f"→ {OUTPUT_JSON}")
    print(f"→ {OUTPUT_JSONL}")


if __name__ == "__main__":
    main()