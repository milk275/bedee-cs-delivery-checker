from __future__ import annotations

import csv
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from .config import Settings
from .interexpress import InterexpressClient
from .kex import TRACK_URL
from .skyfrog import SkyfrogClient


LOGGER = logging.getLogger(__name__)
THAILAND_TZ = timezone(timedelta(hours=7))


@dataclass(frozen=True)
class HealthCheck:
    key: str
    group: str
    name: str
    status: str
    summary: str
    detail: str
    checked_at: str
    latency_ms: int | None = None
    latest_at: str = ""


class AdminHealthService:
    """Run privacy-safe operational checks and cache them briefly."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = min(max(settings.request_timeout_seconds, 2), 12)
        self.cache_seconds = settings.admin_health_cache_seconds
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_payload: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                self._cached_payload is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cached_payload
            payload = self._run_checks()
            self._cached_at = time.monotonic()
            self._cached_payload = payload
            return payload

    def _run_checks(self) -> dict[str, Any]:
        checks: tuple[Callable[[], HealthCheck], ...] = (
            self._check_skyfrog,
            self._check_kex,
            self._check_interexpress,
            self._check_google_sheet,
            self._check_apps_script,
            self._check_supabase,
            self._check_status_report,
            self._check_shopee_sync,
        )
        results: dict[str, HealthCheck] = {}
        with ThreadPoolExecutor(max_workers=len(checks)) as executor:
            futures = {executor.submit(check): check.__name__ for check in checks}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception:
                    LOGGER.exception("Admin health check failed: %s", name)
                    key = name.removeprefix("_check_")
                    result = self._result(
                        key,
                        "ระบบ",
                        key,
                        "error",
                        "ตรวจสอบไม่สำเร็จ",
                        "เกิดข้อผิดพลาดระหว่างตรวจระบบ กรุณาลองใหม่",
                    )
                results[result.key] = result

        order = (
            "skyfrog",
            "kex",
            "interexpress",
            "google_sheet",
            "apps_script",
            "supabase",
            "status_report",
            "shopee_sync",
        )
        items = [asdict(results[key]) for key in order if key in results]
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in ("ok", "warning", "error")
        }
        overall = "error" if counts["error"] else "warning" if counts["warning"] else "ok"
        return {
            "overall": overall,
            "checked_at": _now_iso(),
            "cache_seconds": self.cache_seconds,
            "counts": counts,
            "checks": items,
        }

    def _check_skyfrog(self) -> HealthCheck:
        started = time.monotonic()
        client = SkyfrogClient(
            self.settings.skyfrog_customer_code,
            self.settings.skyfrog_username,
            self.settings.skyfrog_password,
            timeout=self.timeout,
            request_delay=0,
        )
        try:
            client.login()
        except Exception:
            return self._result(
                "skyfrog",
                "ขนส่ง",
                "KLEAN&KARE · Skyfrog",
                "error",
                "เชื่อมต่อไม่ได้",
                "เข้าสู่ระบบขนส่งไม่สำเร็จ กรุณาตรวจบัญชีหรือการเชื่อมต่อ",
                started=started,
            )
        finally:
            client.close()
        return self._result(
            "skyfrog",
            "ขนส่ง",
            "KLEAN&KARE · Skyfrog",
            "ok",
            "เชื่อมต่อปกติ",
            "เข้าสู่ระบบและตรวจ session สำเร็จ",
            started=started,
        )

    def _check_kex(self) -> HealthCheck:
        started = time.monotonic()
        try:
            response = requests.get(
                TRACK_URL,
                timeout=self.timeout,
                headers={"User-Agent": "BeDee-Admin-Health/1.0"},
            )
            response.raise_for_status()
        except requests.RequestException:
            return self._result(
                "kex",
                "ขนส่ง",
                "KEX",
                "error",
                "เชื่อมต่อไม่ได้",
                "หน้า Tracking ของ KEX ไม่ตอบสนอง",
                started=started,
            )
        pin_ready = len(self.settings.kex_proof_pin) == 4
        return self._result(
            "kex",
            "ขนส่ง",
            "KEX",
            "ok" if pin_ready else "warning",
            "เชื่อมต่อปกติ" if pin_ready else "เชื่อมต่อได้ แต่ตั้งค่าไม่ครบ",
            (
                "หน้า Tracking ตอบสนองและตั้งค่า PIN หลักฐานครบ"
                if pin_ready
                else "หน้า Tracking ตอบสนอง แต่ยังไม่ได้ตั้งค่า PIN หลักฐานให้ครบ"
            ),
            started=started,
        )

    def _check_interexpress(self) -> HealthCheck:
        started = time.monotonic()
        client = InterexpressClient(
            self.settings.interexpress_username,
            self.settings.interexpress_password,
            timeout=self.timeout,
        )
        try:
            client.login()
        except Exception:
            return self._result(
                "interexpress",
                "ขนส่ง",
                "InterExpress",
                "error",
                "เชื่อมต่อไม่ได้",
                "เข้าสู่ระบบขนส่งไม่สำเร็จ กรุณาตรวจบัญชีหรือการเชื่อมต่อ",
                started=started,
            )
        finally:
            client.close()
        return self._result(
            "interexpress",
            "ขนส่ง",
            "InterExpress",
            "ok",
            "เชื่อมต่อปกติ",
            "เข้าสู่ระบบองค์กรสำเร็จ",
            started=started,
        )

    def _check_google_sheet(self) -> HealthCheck:
        started = time.monotonic()
        try:
            response = requests.get(
                self.settings.sheet_csv_url,
                timeout=self.timeout,
                headers={"User-Agent": "BeDee-Admin-Health/1.0"},
            )
            response.raise_for_status()
            first_line = response.text.splitlines()[0] if response.text else ""
            if not first_line or len(next(csv.reader([first_line]), [])) < 2:
                raise ValueError("invalid CSV")
        except (requests.RequestException, ValueError, csv.Error):
            return self._result(
                "google_sheet",
                "ข้อมูลและระบบอัตโนมัติ",
                "Google Sheet · อ่านข้อมูล",
                "error",
                "อ่านข้อมูลไม่ได้",
                "ลิงก์ CSV หรือสิทธิ์เข้าถึง Google Sheet มีปัญหา",
                started=started,
            )
        return self._result(
            "google_sheet",
            "ข้อมูลและระบบอัตโนมัติ",
            "Google Sheet · อ่านข้อมูล",
            "ok",
            "อ่านข้อมูลได้ปกติ",
            "ดาวน์โหลดข้อมูลต้นทางจากชีตสำเร็จ",
            started=started,
        )

    def _check_apps_script(self) -> HealthCheck:
        started = time.monotonic()
        if not (
            self.settings.google_sheets_webhook_url
            and self.settings.google_sheets_webhook_secret
        ):
            return self._result(
                "apps_script",
                "ข้อมูลและระบบอัตโนมัติ",
                "Google Apps Script · เขียนข้อมูล",
                "warning",
                "ยังไม่ได้ตั้งค่า",
                "ยังไม่มี Web App URL หรือ secret สำหรับเขียนกลับชีต",
                started=started,
            )
        try:
            response = requests.post(
                self.settings.google_sheets_webhook_url,
                json={
                    "secret": self.settings.google_sheets_webhook_secret,
                    "sheet_id": self.settings.sheet_id,
                    "sheet_gid": self.settings.sheet_gid,
                    "updates": [],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ValueError("Apps Script rejected probe")
        except (requests.RequestException, ValueError):
            return self._result(
                "apps_script",
                "ข้อมูลและระบบอัตโนมัติ",
                "Google Apps Script · เขียนข้อมูล",
                "error",
                "Web App ทำงานผิดปกติ",
                "ตรวจสิทธิ์ Apps Script หรือการเชื่อมต่อ Google Sheet ไม่สำเร็จ",
                started=started,
            )
        return self._result(
            "apps_script",
            "ข้อมูลและระบบอัตโนมัติ",
            "Google Apps Script · เขียนข้อมูล",
            "ok",
            "ทำงานปกติ",
            "ยืนยัน Web App, secret และสิทธิ์เข้าถึงชีตแล้ว โดยไม่แก้ข้อมูล",
            started=started,
        )

    def _check_supabase(self) -> HealthCheck:
        started = time.monotonic()
        if not (self.settings.supabase_url and self.settings.supabase_secret_key):
            return self._result(
                "supabase",
                "ข้อมูลและระบบอัตโนมัติ",
                "Supabase",
                "warning",
                "ยังไม่ได้ตั้งค่า",
                "ยังไม่มี URL หรือ secret สำหรับ Supabase",
                started=started,
            )
        try:
            mapping = self._supabase_latest(
                self.settings.supabase_mapping_table,
                "imported_at",
            )
            status = self._supabase_latest(
                self.settings.supabase_status_table,
                "checked_at",
            )
        except (requests.RequestException, ValueError):
            return self._result(
                "supabase",
                "ข้อมูลและระบบอัตโนมัติ",
                "Supabase",
                "error",
                "เชื่อมต่อไม่ได้",
                "อ่านตาราง Mapping หรือ Status Cache ไม่สำเร็จ",
                started=started,
            )
        mapping_at = str(mapping.get("imported_at") or "")
        status_at = str(status.get("checked_at") or "")
        detail = (
            f"Mapping ล่าสุด {_format_datetime(mapping_at)} · "
            f"Status cache ล่าสุด {_format_datetime(status_at)}"
        )
        return self._result(
            "supabase",
            "ข้อมูลและระบบอัตโนมัติ",
            "Supabase",
            "ok",
            "เชื่อมต่อปกติ",
            detail,
            started=started,
            latest_at=max(mapping_at, status_at),
        )

    def _check_status_report(self) -> HealthCheck:
        started = time.monotonic()
        csv_path = self.settings.output_dir / "latest.csv"
        html_path = self.settings.output_dir / "latest.html"
        if not csv_path.is_file() or not html_path.is_file():
            return self._result(
                "status_report",
                "ข้อมูลและระบบอัตโนมัติ",
                "รอบตรวจสถานะรายชั่วโมง",
                "error",
                "ยังไม่มีรายงาน",
                "ไม่พบไฟล์ CSV หรือหน้า HTML จากรอบอัตโนมัติ",
                started=started,
            )
        modified = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=THAILAND_TZ)
        age = datetime.now(THAILAND_TZ) - modified
        row_count = 0
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                row_count = sum(1 for _ in csv.DictReader(handle))
        except (OSError, csv.Error):
            return self._result(
                "status_report",
                "ข้อมูลและระบบอัตโนมัติ",
                "รอบตรวจสถานะรายชั่วโมง",
                "error",
                "อ่านรายงานไม่ได้",
                "ไฟล์รายงานล่าสุดมีปัญหา",
                started=started,
            )
        allowed_age = timedelta(hours=2, minutes=30) if 8 <= modified.hour <= 22 else timedelta(hours=12)
        fresh = age <= allowed_age
        return self._result(
            "status_report",
            "ข้อมูลและระบบอัตโนมัติ",
            "รอบตรวจสถานะรายชั่วโมง",
            "ok" if fresh else "error",
            "ทำงานปกติ" if fresh else "รายงานไม่อัปเดตตามรอบ",
            f"รายงานล่าสุด {_format_datetime(modified.isoformat())} · {row_count:,} รายการ",
            started=started,
            latest_at=modified.isoformat(),
        )

    def _check_shopee_sync(self) -> HealthCheck:
        started = time.monotonic()
        latest_download = self._latest_shopee_download()
        try:
            mapping = self._supabase_latest(
                self.settings.supabase_mapping_table,
                "imported_at",
            )
            imported_at = str(mapping.get("imported_at") or "")
        except (requests.RequestException, ValueError):
            imported_at = ""

        if latest_download is None:
            return self._result(
                "shopee_sync",
                "ข้อมูลและระบบอัตโนมัติ",
                "Shopee Bot → Supabase",
                "error",
                "ไม่พบรายงานจาก Shopee",
                "บอทยังไม่มีไฟล์ดาวน์โหลดล่าสุดให้ตรวจสอบ",
                started=started,
                latest_at=imported_at,
            )

        now = datetime.now(THAILAND_TZ)
        age = now - latest_download
        fresh_limit = timedelta(hours=2, minutes=30) if 8 <= now.hour <= 23 else timedelta(hours=26)
        fresh = age <= fresh_limit
        import_text = _format_datetime(imported_at) if imported_at else "ไม่พบข้อมูล"
        detail = (
            f"ดาวน์โหลด Shopee ล่าสุด {_format_datetime(latest_download.isoformat())} · "
            f"เขียน Mapping เข้า Supabase ล่าสุด {import_text}"
        )
        return self._result(
            "shopee_sync",
            "ข้อมูลและระบบอัตโนมัติ",
            "Shopee Bot → Supabase",
            "ok" if fresh and imported_at else "error",
            "ทำงานปกติ" if fresh and imported_at else "บอทไม่อัปเดตตามรอบ",
            detail,
            started=started,
            latest_at=latest_download.isoformat(),
        )

    def _latest_shopee_download(self) -> datetime | None:
        manifest = self.settings.shopee_report_manifest
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                created_at = _parse_datetime(str(payload.get("created_at") or ""))
                if created_at is not None:
                    return created_at
            except (OSError, json.JSONDecodeError):
                pass
        directory = self.settings.shopee_report_directory
        try:
            reports = [
                path
                for path in directory.glob("Order.all*.xlsx")
                if path.is_file() and path.stat().st_size > 1_000
            ]
        except OSError:
            return None
        if not reports:
            return None
        latest = max(reports, key=lambda path: path.stat().st_mtime)
        return datetime.fromtimestamp(latest.stat().st_mtime, tz=THAILAND_TZ)

    def _supabase_latest(self, table: str, timestamp_field: str) -> dict[str, Any]:
        if not (self.settings.supabase_url and self.settings.supabase_secret_key):
            raise ValueError("Supabase disabled")
        endpoint = (
            f"{self.settings.supabase_url}/rest/v1/"
            f"{quote(table, safe='')}"
        )
        headers = {
            "Accept": "application/json",
            "apikey": self.settings.supabase_secret_key,
        }
        if self.settings.supabase_secret_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.settings.supabase_secret_key}"
        response = requests.get(
            endpoint,
            params={
                "select": timestamp_field,
                "order": f"{timestamp_field}.desc",
                "limit": "1",
            },
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Invalid Supabase response")
        return payload[0] if payload and isinstance(payload[0], dict) else {}

    @staticmethod
    def _result(
        key: str,
        group: str,
        name: str,
        status: str,
        summary: str,
        detail: str,
        *,
        started: float | None = None,
        latest_at: str = "",
    ) -> HealthCheck:
        latency_ms = (
            max(0, round((time.monotonic() - started) * 1_000))
            if started is not None
            else None
        )
        return HealthCheck(
            key=key,
            group=group,
            name=name,
            status=status,
            summary=summary,
            detail=detail,
            checked_at=_now_iso(),
            latency_ms=latency_ms,
            latest_at=latest_at,
        )


def _now_iso() -> str:
    return datetime.now(THAILAND_TZ).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=THAILAND_TZ)
    return parsed.astimezone(THAILAND_TZ)


def _format_datetime(value: str) -> str:
    parsed = _parse_datetime(value)
    return parsed.strftime("%d/%m/%Y %H:%M น.") if parsed else "ไม่พบข้อมูล"
