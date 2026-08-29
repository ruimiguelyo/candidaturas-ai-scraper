import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from models import JobPost


# These fields are useful while building a private notification, but must never
# be committed to the public repository. Descriptions can also contain large
# third-party HTML fragments, so the public dataset keeps only decision-ready
# metadata.
PRIVATE_FIELDS = {"description_snippet", "human_outreach"}
PUBLIC_FIELDS = tuple(name for name in JobPost.model_fields if name not in PRIVATE_FIELDS)
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def public_job_record(job: JobPost) -> dict:
    """Return the stable, privacy-safe representation used in public exports."""
    return job.model_dump(include=set(PUBLIC_FIELDS))


def _csv_value(value):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        # Spreadsheet applications may execute untrusted cells as formulas.
        return f"'{value}"
    return value


def _temporary_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f"{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(raw_path)


def export_public_jobs(
    jobs: Iterable[JobPost],
    csv_path: Path | str,
    json_path: Path | str,
) -> list[dict]:
    """Write deterministic public CSV/JSON files via atomic replacements."""
    csv_destination = Path(csv_path)
    json_destination = Path(json_path)
    rows = [public_job_record(job) for job in jobs]
    csv_temporary = _temporary_path(csv_destination)
    json_temporary = _temporary_path(json_destination)

    try:
        with csv_temporary.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=PUBLIC_FIELDS, quoting=csv.QUOTE_NONNUMERIC)
            writer.writeheader()
            writer.writerows(
                {field: _csv_value(row.get(field)) for field in PUBLIC_FIELDS}
                for row in rows
            )

        with json_temporary.open("w", encoding="utf-8", newline="\n") as json_file:
            json.dump(rows, json_file, ensure_ascii=False, indent=2)
            json_file.write("\n")

        os.replace(csv_temporary, csv_destination)
        os.replace(json_temporary, json_destination)
    finally:
        for temporary in (csv_temporary, json_temporary):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    return rows
