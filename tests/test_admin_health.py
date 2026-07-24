import json
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from klean_pod_checker.admin_health import (
    THAILAND_TZ,
    AdminHealthService,
    _format_datetime,
)
from klean_pod_checker.config import Settings


def settings(tmp_path: Path) -> Settings:
    reports = tmp_path / "shopee"
    return Settings(
        sheet_id="test-sheet",
        sheet_gid="1",
        sheet_tracking_column="G",
        skyfrog_customer_code="test-code",
        skyfrog_username="test-user",
        skyfrog_password="test-password",
        request_timeout_seconds=1,
        concurrency=1,
        request_delay_seconds=0,
        output_dir=tmp_path / "outputs",
        web_secret_key="x" * 48,
        google_sheets_webhook_url="",
        google_sheets_webhook_secret="",
        cs_access_pin_hash=generate_password_hash("test"),
        web_cookie_secure=False,
        supabase_url="https://example.supabase.co",
        supabase_secret_key="test-secret",
        shopee_report_directory=reports,
        shopee_report_manifest=reports / "latest-report-manifest.json",
    )


def test_latest_shopee_download_reads_manifest_timestamp(tmp_path):
    configured = settings(tmp_path)
    configured.shopee_report_directory.mkdir(parents=True)
    expected = datetime.now().astimezone() - timedelta(minutes=20)
    configured.shopee_report_manifest.write_text(
        json.dumps({"created_at": expected.isoformat()}),
        encoding="utf-8",
    )

    actual = AdminHealthService(configured)._latest_shopee_download()

    assert actual is not None
    assert abs((actual - expected).total_seconds()) < 1


def test_latest_shopee_download_falls_back_to_xlsx_mtime(tmp_path):
    configured = settings(tmp_path)
    configured.shopee_report_directory.mkdir(parents=True)
    report = configured.shopee_report_directory / "Order.all.test.xlsx"
    report.write_bytes(b"x" * 2_000)

    actual = AdminHealthService(configured)._latest_shopee_download()

    assert actual is not None
    assert abs((datetime.now().astimezone() - actual).total_seconds()) < 5


def test_datetime_formatter_does_not_expose_raw_invalid_values():
    assert _format_datetime("not-a-date") == "ไม่พบข้อมูล"


def test_datetime_formatter_always_uses_thailand_time():
    assert _format_datetime("2026-07-24T08:15:00+00:00") == "24/07/2026 15:15 น."
    assert datetime.now(THAILAND_TZ).utcoffset() == timedelta(hours=7)
