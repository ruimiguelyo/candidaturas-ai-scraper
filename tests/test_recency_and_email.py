import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from date_utils import parse_publication_date, recency_status
from email_notifier import generate_html_email, send_daily_email
from filter_engine import JobFilterEngine
from models import JobPost


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def job(post_date):
    return JobPost(
        source="Test",
        job_id=str(post_date),
        title="Junior Software Engineer",
        company="Example",
        location="Portugal",
        job_url=f"https://jobs.example/{post_date}",
        post_date=post_date,
    )


class TestRecencyAndEmail(unittest.TestCase):
    def test_supported_absolute_and_relative_dates_are_normalized(self):
        samples = {
            "today": "2026-08-30",
            "6 days ago": "2026-08-24",
            "1 week ago": "2026-08-23",
            "24 ago": "2026-08-24",
            "2026-08-29T10:00:00+00:00": "2026-08-29",
            1788019200: "2026-08-29",
        }
        for raw, expected in samples.items():
            with self.subTest(raw=raw):
                parsed = parse_publication_date(raw, now=NOW)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.date().isoformat(), expected)

    def test_seven_day_boundary_is_inclusive_and_older_is_rejected(self):
        self.assertTrue(recency_status("1 week ago", now=NOW)[0])
        recent = JobFilterEngine.evaluate_job(job("1 week ago"), now=NOW)
        old = JobFilterEngine.evaluate_job(job("8 days ago"), now=NOW)
        self.assertTrue(recent.accepted, recent.reason_detail)
        self.assertFalse(old.accepted)
        self.assertEqual(old.reason_code, "outside_recency_window")

    def test_missing_or_unverifiable_date_is_rejected(self):
        for raw in (None, "", "Recente", "unknown"):
            with self.subTest(raw=raw):
                decision = JobFilterEngine.evaluate_job(job(raw), now=NOW)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason_code, "missing_post_date")

    def test_large_digest_is_one_compact_email_without_csv(self):
        jobs = [job("today").model_dump() for _ in range(236)]
        html = generate_html_email(jobs)
        self.assertLess(len(html.encode("utf-8")), 95_000)
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.dict(
            os.environ,
            {
                "SMTP_USER": "sender@example.com",
                "SMTP_PASS": "secret",
                "RECEIVER_EMAIL": "receiver@example.com",
            },
            clear=False,
        ), patch("email_notifier.smtplib.SMTP", return_value=smtp):
            delivered = send_daily_email(jobs=jobs)

        self.assertTrue(delivered)
        self.assertEqual(smtp.send_message.call_count, 1)
        subject = smtp.send_message.call_args.args[0]["Subject"]
        self.assertNotIn("[1/", subject)
        for call in smtp.send_message.call_args_list:
            message = call.args[0]
            self.assertFalse(any(part.get_filename() for part in message.walk()))


if __name__ == "__main__":
    unittest.main()
