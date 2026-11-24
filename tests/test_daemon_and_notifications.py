"""Testes para execução silenciosa em daemon, locks, baseline ntfy e CLI."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from estagio_radar import cli
from estagio_radar.models import (
    CompanyProfile,
    CompanyType,
    DomainCategory,
    EnrichedJob,
    GeoZone,
    RoleTier,
    ScoreBreakdown,
)
from estagio_radar.notifiers.ntfy_notifier import build_ntfy_headers_and_body
from estagio_radar.storage import RadarStorage

NOW = datetime(2026, 11, 24, 17, 30, tzinfo=timezone.utc)


def _sample_job(fp: str = "fp-daemon-1") -> EnrichedJob:
    return EnrichedJob(
        fingerprint=fp,
        title="AI Engineering Intern",
        company="Feedzai",
        location="Lisboa, Portugal",
        job_url="https://boards.greenhouse.io/feedzai/jobs/101",
        canonical_url="https://boards.greenhouse.io/feedzai/jobs/101",
        sources=["Greenhouse (Feedzai)"],
        primary_source="Greenhouse (Feedzai)",
        source_type="ats",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.LISBOA_METRO,
        published_at=NOW,
        published_date="2026-11-24",
        freshness_label="Há 5m",
        company_profile=CompanyProfile(
            company_name="Feedzai",
            normalized_name="feedzai",
            teamlyzer_rating=4.0,
            teamlyzer_reviews=50,
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=93.0,
        score_breakdown=ScoreBreakdown(
            total=93.0,
            role_points=30,
            company_points=26,
            location_points=17,
            freshness_points=20,
        ),
        badges=["🔥 NOVO", "🏆 TIER S"],
    )


def test_mark_all_active_notified_prevents_resending(tmp_path):
    db_path = tmp_path / "radar.db"
    storage = RadarStorage(db_path)
    j1 = _sample_job("fp-1")
    j2 = _sample_job("fp-2").model_copy(update={"title": "Data Science Intern"})
    _, new_initial = storage.upsert_jobs([j1, j2], now=NOW)
    assert len(new_initial) == 2

    marked = storage.mark_all_active_notified(now=NOW)
    assert marked == 2
    assert storage.mark_all_active_notified(now=NOW) == 0

    _, new_after = storage.upsert_jobs([j1, j2], now=NOW)
    assert new_after == []


def test_single_instance_lock_and_status_detection(tmp_path):
    lock_path = tmp_path / "radar_watch.lock"
    pid_path = tmp_path / "radar_watch.pid"

    running_before, pid_before = cli._is_daemon_running(lock_path, pid_path)
    assert running_before is False
    assert pid_before is None

    handle = cli._acquire_single_instance_lock(lock_path, pid_path)
    assert handle is not None
    try:
        second = cli._acquire_single_instance_lock(lock_path, pid_path)
        assert second is None

        running_now, pid_now = cli._is_daemon_running(lock_path, pid_path)
        assert running_now is True
        assert pid_now == os.getpid()
    finally:
        cli._release_single_instance_lock(handle, pid_path)

    running_after, _ = cli._is_daemon_running(lock_path, pid_path)
    assert running_after is False


def test_configure_stdio_handles_none_and_logfile(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli._configure_stdio()
    try:
        assert sys.stdout is not None
        assert sys.stderr is not None
        print("pythonw safe devnull check")
    finally:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    log_file = tmp_path / "logs" / "radar.log"
    cli._configure_stdio(str(log_file))
    try:
        print("linha silenciosa utf8 Estágio Radar")
        sys.stdout.flush()
    finally:
        if cli._LOG_STREAM is not None and not cli._LOG_STREAM.closed:
            cli._LOG_STREAM.close()
            cli._LOG_STREAM = None

    assert log_file.exists()
    assert "Estágio Radar" in log_file.read_text(encoding="utf-8")


def test_ntfy_runner_label_optional(monkeypatch):
    job = _sample_job()

    monkeypatch.delenv("RADAR_RUNNER_LABEL", raising=False)
    _, body_plain = build_ntfy_headers_and_body(job)
    assert "Fonte: Greenhouse (Feedzai)" in body_plain
    assert "[" not in body_plain

    monkeypatch.setenv("RADAR_RUNNER_LABEL", "PC Windows 5m")
    _, body_labeled = build_ntfy_headers_and_body(job)
    assert "Fonte: Greenhouse (Feedzai) · [PC Windows 5m]" in body_labeled


def test_cli_mark_notified_command(tmp_path):
    db_path = tmp_path / "cli_radar.db"
    storage = RadarStorage(db_path)
    storage.upsert_jobs([_sample_job("fp-cli-1")], now=NOW)

    rc = cli.main(["mark-notified", "--db", str(db_path)])
    assert rc == 0

    _, new_jobs = storage.upsert_jobs([_sample_job("fp-cli-1")], now=NOW)
    assert new_jobs == []
