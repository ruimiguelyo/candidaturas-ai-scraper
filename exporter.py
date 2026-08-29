import csv
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Iterable

from models import JobPost


logger = logging.getLogger("Exporter")

# These fields are useful while building a private notification, but must never
# be committed to the public repository. Descriptions can also contain large
# third-party HTML fragments, so the public dataset keeps only decision-ready
# metadata.
PRIVATE_FIELDS = {"description_snippet", "human_outreach"}
PUBLIC_FIELDS = tuple(name for name in JobPost.model_fields if name not in PRIVATE_FIELDS)
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
REJECTION_AUDIT_FILENAME = "vagas_rejeitadas.csv"
REJECTION_AUDIT_FIELDS = (
    "source",
    "title",
    "company",
    "location",
    "job_url",
    "rejection_stage",
    "reason_code",
    "reason_detail",
)


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


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_csv_file(path: Path, rows: list[dict], fieldnames: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, quoting=csv.QUOTE_NONNUMERIC)
        writer.writeheader()
        writer.writerows(
            {field: _csv_value(row.get(field)) for field in fieldnames}
            for row in rows
        )


def _write_json_file(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as json_file:
        json.dump(rows, json_file, ensure_ascii=False, indent=2)
        json_file.write("\n")


def _destination_key(destination: Path) -> str:
    return os.path.normcase(str(destination.resolve(strict=False)))


def _rollback_replacements(
    replaced_destinations: list[Path],
    backups: dict[Path, Path | None],
) -> None:
    """Best-effort restoration after a multi-file commit fails."""
    for destination in reversed(replaced_destinations):
        backup = backups[destination]
        try:
            if backup is None:
                _unlink_if_present(destination)
            else:
                os.replace(backup, destination)
        except OSError as replace_error:
            if backup is not None and backup.exists():
                try:
                    shutil.copy2(backup, destination)
                    continue
                except OSError as copy_error:
                    logger.error(
                        "Failed to restore %s after export error: %s; fallback copy failed: %s",
                        destination,
                        replace_error,
                        copy_error,
                    )
                    continue
            logger.error("Failed to remove new artifact %s during rollback: %s", destination, replace_error)


def _replace_prepared_files(prepared: list[tuple[Path, Path]]) -> None:
    """Commit prepared files together, restoring prior destinations on failure."""
    backups: dict[Path, Path | None] = {}
    backup_paths: list[Path] = []
    replaced_destinations: list[Path] = []

    try:
        # Back up every existing destination before the first replacement. A
        # failure here leaves all published files untouched.
        for _temporary, destination in prepared:
            if destination.exists():
                backup = _temporary_path(destination)
                backup_paths.append(backup)
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None

        try:
            for temporary, destination in prepared:
                os.replace(temporary, destination)
                replaced_destinations.append(destination)
        except Exception:
            _rollback_replacements(replaced_destinations, backups)
            raise
    finally:
        for backup in backup_paths:
            _unlink_if_present(backup)


def _write_artifact_set(
    artifacts: Iterable[tuple[Path, Callable[[Path], None]]],
) -> None:
    """Write every temporary artifact before committing any destination."""
    artifact_list = list(artifacts)
    destination_keys = [_destination_key(destination) for destination, _writer in artifact_list]
    if len(destination_keys) != len(set(destination_keys)):
        raise ValueError("Artifact destinations must be distinct paths.")

    prepared: list[tuple[Path, Path]] = []
    try:
        for destination, writer in artifact_list:
            temporary = _temporary_path(destination)
            prepared.append((temporary, destination))
            writer(temporary)
        _replace_prepared_files(prepared)
    finally:
        for temporary, _destination in prepared:
            _unlink_if_present(temporary)


def _rejection_audit_record(record) -> dict:
    """Reduce a structured rejection record to the public audit schema."""
    if isinstance(record, Mapping):
        payload = record
    else:
        model_dump = getattr(record, "model_dump", None)
        if callable(model_dump):
            payload = model_dump()
        elif hasattr(record, "__dict__"):
            payload = vars(record)
        else:
            raise TypeError("Rejection records must be mappings or structured objects.")

    if not isinstance(payload, Mapping):
        raise TypeError("A structured rejection record must produce a mapping.")
    return {field: payload.get(field) for field in REJECTION_AUDIT_FIELDS}


def _rejection_audit_destination(csv_path_or_output_dir: Path | str) -> Path:
    """Accept either an explicit file path or an output directory."""
    destination = Path(csv_path_or_output_dir)
    if (destination.exists() and destination.is_dir()) or not destination.suffix:
        return destination / REJECTION_AUDIT_FILENAME
    return destination


def export_rejection_audit(
    rejections: Iterable[Mapping | object],
    csv_path_or_output_dir: Path | str,
) -> list[dict]:
    """Atomically write a privacy-safe CSV explaining rejected vacancies."""
    destination = _rejection_audit_destination(csv_path_or_output_dir)
    rows = [_rejection_audit_record(record) for record in rejections]
    _write_artifact_set(
        ((destination, lambda path: _write_csv_file(path, rows, REJECTION_AUDIT_FIELDS)),)
    )

    return rows


def export_public_jobs(
    jobs: Iterable[JobPost],
    csv_path: Path | str,
    json_path: Path | str,
) -> list[dict]:
    """Write deterministic public CSV/JSON files via atomic replacements."""
    csv_destination = Path(csv_path)
    json_destination = Path(json_path)
    rows = [public_job_record(job) for job in jobs]
    _write_artifact_set(
        (
            (csv_destination, lambda path: _write_csv_file(path, rows, PUBLIC_FIELDS)),
            (json_destination, lambda path: _write_json_file(path, rows)),
        )
    )

    return rows


def export_public_artifacts(
    jobs: Iterable[JobPost],
    rejections: Iterable[Mapping | object],
    csv_path: Path | str,
    json_path: Path | str,
    rejections_csv_path_or_output_dir: Path | str,
) -> tuple[list[dict], list[dict]]:
    """Publish accepted jobs and their rejection audit as one coherent set."""
    csv_destination = Path(csv_path)
    json_destination = Path(json_path)
    rejections_destination = _rejection_audit_destination(rejections_csv_path_or_output_dir)
    job_rows = [public_job_record(job) for job in jobs]
    rejection_rows = [_rejection_audit_record(record) for record in rejections]

    _write_artifact_set(
        (
            (csv_destination, lambda path: _write_csv_file(path, job_rows, PUBLIC_FIELDS)),
            (json_destination, lambda path: _write_json_file(path, job_rows)),
            (
                rejections_destination,
                lambda path: _write_csv_file(path, rejection_rows, REJECTION_AUDIT_FIELDS),
            ),
        )
    )
    return job_rows, rejection_rows
