import csv
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from exporter import (
    PUBLIC_FIELDS,
    REJECTION_AUDIT_FIELDS,
    REJECTION_AUDIT_FILENAME,
    export_public_artifacts,
    export_rejection_audit,
)
from models import JobPost


def rejection(**overrides):
    row = {
        "source": "Example",
        "title": "Junior AI Engineer",
        "company": "Example Co",
        "location": "Portugal",
        "job_url": "https://jobs.example/1",
        "rejection_stage": "pre_filter",
        "reason_code": "missing_entry_level_marker",
        "reason_detail": "No explicit entry-level marker in the title.",
    }
    row.update(overrides)
    return row


def job(**overrides):
    values = {
        "source": "Example",
        "job_id": "1",
        "title": "Junior AI Engineer",
        "company": "Example Co",
        "location": "Portugal",
        "job_url": "https://jobs.example/1",
    }
    values.update(overrides)
    return JobPost(**values)


class TestRejectionAudit(unittest.TestCase):
    def test_empty_audit_has_stable_header_and_accepts_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = export_rejection_audit([], Path(directory))
            destination = Path(directory) / REJECTION_AUDIT_FILENAME

            self.assertEqual(rows, [])
            with destination.open(encoding="utf-8", newline="") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(tuple(reader.fieldnames or ()), REJECTION_AUDIT_FIELDS)
                self.assertEqual(list(reader), [])

    def test_export_whitelists_public_fields_for_dicts_and_structured_records(self):
        @dataclass
        class StructuredRejection:
            source: str = "Structured"
            title: str = "AI Engineer"
            company: str = "Private Co"
            location: str = "Remote"
            job_url: str = "https://jobs.example/2"
            rejection_stage: str = "title_filter"
            reason_code: str = "missing_entry_level_marker"
            reason_detail: str = "Review manually."
            description_snippet: str = "private description"
            human_outreach: dict | None = None

        private_fields = {
            "description_snippet": "secret job description",
            "human_outreach": {"name": "Private Person"},
            "candidate_email": "private@example.com",
        }

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "audit.csv"
            rows = export_rejection_audit(
                [rejection(**private_fields), StructuredRejection(human_outreach={"name": "Hidden"})],
                destination,
            )

            self.assertEqual([tuple(row) for row in rows], [REJECTION_AUDIT_FIELDS] * 2)
            contents = destination.read_text(encoding="utf-8")
            self.assertNotIn("secret job description", contents)
            self.assertNotIn("Private Person", contents)
            self.assertNotIn("private@example.com", contents)
            self.assertNotIn("private description", contents)
            self.assertNotIn("Hidden", contents)

    def test_formula_prefixes_are_neutralized_in_every_column(self):
        formula_values = ("=1+1", "+2", "-3", "@cmd", "\tcmd", "\rcmd", "\ncmd", "=HYPERLINK")
        dangerous = dict(zip(REJECTION_AUDIT_FIELDS, formula_values))

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "audit.csv"
            export_rejection_audit([dangerous], destination)

            with destination.open(encoding="utf-8", newline="") as csv_file:
                row = next(csv.DictReader(csv_file))
            for field, original in dangerous.items():
                self.assertEqual(row[field], f"'{original}")

    def test_failed_atomic_replace_preserves_previous_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "audit.csv"
            original = "previous valid audit\n"
            destination.write_text(original, encoding="utf-8")

            with patch("exporter.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    export_rejection_audit([rejection()], destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(directory).glob("audit.csv.*.tmp")), [])

    def test_bundle_with_zero_rows_writes_all_three_valid_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            csv_path = output / "jobs.csv"
            json_path = output / "jobs.json"

            job_rows, rejection_rows = export_public_artifacts(
                [],
                [],
                csv_path,
                json_path,
                output,
            )

            self.assertEqual(job_rows, [])
            self.assertEqual(rejection_rows, [])
            with csv_path.open(encoding="utf-8", newline="") as csv_file:
                self.assertEqual(tuple(csv.DictReader(csv_file).fieldnames or ()), PUBLIC_FIELDS)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), [])
            with (output / REJECTION_AUDIT_FILENAME).open(encoding="utf-8", newline="") as csv_file:
                self.assertEqual(
                    tuple(csv.DictReader(csv_file).fieldnames or ()),
                    REJECTION_AUDIT_FIELDS,
                )

    def test_bundle_whitelists_private_fields_from_both_datasets(self):
        private_job = job(
            description_snippet="private description",
            human_outreach={"name": "Private Person"},
        )
        private_rejection = rejection(
            description_snippet="private rejected description",
            human_outreach={"name": "Hidden Contact"},
            candidate_email="private@example.com",
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            export_public_artifacts(
                [private_job],
                [private_rejection],
                output / "jobs.csv",
                output / "jobs.json",
                output / "rejections.csv",
            )
            published = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())

            self.assertNotIn("private description", published)
            self.assertNotIn("Private Person", published)
            self.assertNotIn("private rejected description", published)
            self.assertNotIn("Hidden Contact", published)
            self.assertNotIn("private@example.com", published)

    def test_bundle_write_failure_occurs_before_any_destination_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            destinations = (output / "jobs.csv", output / "jobs.json", output / "rejections.csv")
            originals = ("old csv\n", "old json\n", "old audit\n")
            for destination, original in zip(destinations, originals):
                destination.write_text(original, encoding="utf-8")

            with patch("exporter.json.dump", side_effect=OSError("json write failed")):
                with patch("exporter.os.replace", wraps=os.replace) as replace_mock:
                    with self.assertRaisesRegex(OSError, "json write failed"):
                        export_public_artifacts(
                            [job()],
                            [rejection()],
                            *destinations,
                        )

            self.assertEqual(replace_mock.call_count, 0)
            self.assertEqual(
                tuple(destination.read_text(encoding="utf-8") for destination in destinations),
                originals,
            )
            self.assertEqual(list(output.glob("*.tmp")), [])

    def test_bundle_rolls_back_every_completed_replace_after_intermediate_failure(self):
        for failing_replace in (2, 3):
            with self.subTest(failing_replace=failing_replace):
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory)
                    destinations = (
                        output / "jobs.csv",
                        output / "jobs.json",
                        output / "rejections.csv",
                    )
                    originals = ("old csv\n", "old json\n", "old audit\n")
                    for destination, original in zip(destinations, originals):
                        destination.write_text(original, encoding="utf-8")

                    real_replace = os.replace
                    replace_count = 0

                    def fail_once(source, destination):
                        nonlocal replace_count
                        replace_count += 1
                        if replace_count == failing_replace:
                            raise OSError(f"replace {failing_replace} failed")
                        return real_replace(source, destination)

                    with patch("exporter.os.replace", side_effect=fail_once):
                        with self.assertRaisesRegex(OSError, f"replace {failing_replace} failed"):
                            export_public_artifacts(
                                [job(title="New title")],
                                [rejection(title="Rejected title")],
                                *destinations,
                            )

                    self.assertEqual(
                        tuple(destination.read_text(encoding="utf-8") for destination in destinations),
                        originals,
                    )
                    self.assertEqual(list(output.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
