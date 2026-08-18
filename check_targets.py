import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

with open("vagas_estritamente_junior_trainee_internship.json", "r", encoding="utf-8") as f:
    data = json.load(f)

targets = ["tensorops", "volkswagen", "euronext", "innowave", "siemens", "priberam"]

for t in targets:
    matches = [x for x in data if t in x["company"].lower() or t in x["title"].lower()]
    print(f"=== TARGET: {t.upper()} (Found {len(matches)}) ===")
    for m in matches:
        print(f"  -> Title: {m['title']} | Company: {m['company']} | Score: {m['company_score']} | URL: {m['job_url']}")
