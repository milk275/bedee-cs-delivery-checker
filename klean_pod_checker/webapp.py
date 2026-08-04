from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from .admin_health import AdminHealthService
from .config import Settings
from .interexpress import InterexpressClient, InterexpressError
from .kex import KEX_TRACKING_RE, KexClient, KexError
from .models import JobResult
from .multiple_tracking_sync import import_multiple_tracking_sheet
from .proof_tokens import local_kex_proof_parts, read_sheet_proof_token
from .shopee import SHOPEE_ORDER_RE, extract_shopee_tracking
from .sheets import KLEAN_ORDER_RE, normalize_auto_search_input, normalize_tracking_input
from .sheets_sync import GoogleSheetsWriter
from .skyfrog import SkyfrogClient, SkyfrogError
from .supabase_mapping import SupabaseMappingError, SupabaseMappingStore
from .supabase_status_cache import SupabaseStatusCache, SupabaseStatusCacheError


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, seconds: int) -> None:
        self.limit = limit
        self.seconds = seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


class LiveSearchService:
    """Reuse carrier clients safely inside each web process."""

    def __init__(self, settings: Settings, status_cache: Any) -> None:
        self.settings = settings
        self.status_cache = status_cache
        self._client: SkyfrogClient | None = None
        self._kex_client = KexClient(
            settings.kex_proof_pin,
            settings.kex_proof_dir,
            timeout=settings.request_timeout_seconds,
        )
        self._interexpress_client = InterexpressClient(
            settings.interexpress_username,
            settings.interexpress_password,
            timeout=settings.request_timeout_seconds,
        )
        self._lock = threading.Lock()

    def _new_client(self) -> SkyfrogClient:
        client = SkyfrogClient(
            self.settings.skyfrog_customer_code,
            self.settings.skyfrog_username,
            self.settings.skyfrog_password,
            timeout=self.settings.request_timeout_seconds,
            request_delay=self.settings.request_delay_seconds,
        )
        client.login()
        return client

    def _discard_client(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None

    def _cache(self, result: JobResult) -> None:
        try:
            self.status_cache.put(result)
        except SupabaseStatusCacheError:
            # A cache outage must never block a live CS carrier search.
            return

    def search(self, order_number: str, carrier: str = "skyfrog") -> JobResult:
        with self._lock:
            if carrier == "kex":
                result = self._kex_client.search_order(order_number)
                self._cache(result)
                return result
            if carrier == "interexpress":
                result = self._interexpress_client.search_order(order_number)
                self._cache(result)
                return result
            last_error: Exception | None = None
            for _ in range(2):
                try:
                    if self._client is None:
                        self._client = self._new_client()
                    result = self._client.search_order(order_number)
                    self._cache(result)
                    return result
                except (SkyfrogError, requests.RequestException) as exc:
                    last_error = exc
                    self._discard_client()
            assert last_error is not None
            raise last_error


def _client_ip() -> str:
    forwarded = request.headers.get("CF-Connecting-IP", "").strip()
    return forwarded or (request.remote_addr or "unknown")


def _is_authenticated() -> bool:
    return session.get("cs_authenticated") is True


def _safe_next_url(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _login_required(api: bool = False) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            if not _is_authenticated():
                if api:
                    return jsonify(error="กรุณาเข้าสู่ระบบใหม่"), 401
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _report_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": 0,
        "delivered": 0,
        "pending": 0,
        "missing": 0,
        "updated": "ยังไม่มีรายงาน",
    }
    if not path.exists():
        return summary
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                summary["total"] += 1
                if row.get("delivered", "").casefold() == "true":
                    summary["delivered"] += 1
                elif row.get("found", "").casefold() != "true":
                    summary["missing"] += 1
                else:
                    summary["pending"] += 1
        modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        summary["updated"] = modified.strftime("%d/%m/%Y %H:%M น.")
    except (OSError, csv.Error):
        summary["updated"] = "อ่านรายงานไม่ได้"
    return summary


def _public_result(result: JobResult, carrier: str = "") -> dict[str, Any]:
    payload = asdict(result)
    payload.pop("raw", None)
    payload.pop("customer", None)
    if carrier == "skyfrog":
        # Skyfrog places two customer-signature captures before the actual
        # delivery photos. They are sensitive and must not appear in live CS
        # search results.
        payload["proof_urls"] = list(payload.get("proof_urls") or [])[2:]
        payload["proof_urls_filtered"] = True
    return payload


def _search_with_carrier_priority(
    service: Any, order_number: str
) -> tuple[JobResult, str]:
    """Try every carrier in the CS automatic-search priority order."""
    last_result: JobResult | None = None
    last_carrier = "interexpress"
    last_error: Exception | None = None
    for carrier in ("skyfrog", "kex", "interexpress"):
        try:
            result = service.search(order_number, carrier)
        except (SkyfrogError, KexError, InterexpressError, requests.RequestException) as error:
            # A temporary carrier outage must not prevent the next carrier
            # from being checked for an ambiguous tracking number.
            last_error = error
            continue
        if result.found:
            return result, carrier
        last_result = result
        last_carrier = carrier
    if last_result is not None:
        return last_result, last_carrier
    assert last_error is not None
    raise last_error


def _tracking_group_for_input(
    mapping_store: Any, raw_value: str
) -> tuple[str, list[tuple[str, str]]] | None:
    """Resolve an order or tracking number to its imported Shopee group."""
    candidate = re.sub(r"\s+", "", raw_value or "").lstrip("#").upper()
    if not candidate:
        return None
    if SHOPEE_ORDER_RE.fullmatch(candidate):
        references = mapping_store.get_tracking_refs(candidate)
        return (candidate, references) if references else None
    tracking_match = KEX_TRACKING_RE.search(candidate)
    if not tracking_match:
        return None
    tracking_number = tracking_match.group(0).upper()
    order_number = mapping_store.get_order_for_tracking(tracking_number)
    if not order_number:
        return None
    references = mapping_store.get_tracking_refs(order_number)
    return (order_number, references) if references else None

def _search_group_tracking(
    service: Any, tracking_number: str, carrier: str
) -> tuple[JobResult, str]:
    """Check an imported ANB reference without querying Skyfrog unnecessarily."""
    if carrier == "skyfrog":
        carriers = ("skyfrog",)
    elif carrier == "interexpress":
        carriers = ("interexpress",)
    else:
        # The grouped-tracking tab does not include the carrier.  Try KEX first,
        # then InterExpress; a KEX value that is no longer present can therefore
        # still be resolved as InterExpress.
        carriers = ("kex", "interexpress")

    last_result: JobResult | None = None
    last_carrier = carriers[-1]
    last_error: Exception | None = None
    for selected_carrier in carriers:
        try:
            result = service.search(tracking_number, selected_carrier)
        except (
            SkyfrogError,
            KexError,
            InterexpressError,
            requests.RequestException,
        ) as error:
            last_error = error
            continue
        last_result = result
        last_carrier = selected_carrier
        if result.found:
            return result, selected_carrier
    if last_result is not None:
        return last_result, last_carrier
    return (
        JobResult(
            order_number=tracking_number,
            found=False,
            checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            error="carrier_unavailable" if last_error else "tracking_unavailable",
        ),
        last_carrier,
    )


def _group_summary(order_number: str, entries: list[tuple[str, str, JobResult]]) -> dict[str, Any]:
    total = len(entries)
    delivered = sum(result.delivered for _, _, result in entries)
    errors = sum(bool(result.error) for _, _, result in entries)
    missing = sum(not result.found and not result.error for _, _, result in entries)
    return {
        "order_number": order_number,
        "total": total,
        "delivered": delivered,
        "pending": total - delivered - errors - missing,
        "missing": missing,
        "errors": errors,
        "multiple": total > 1,
    }


def _public_group_entry(
    tracking_number: str, carrier: str, result: JobResult
) -> dict[str, Any]:
    return {
        "tracking_number": tracking_number,
        "carrier": CUSTOMER_CARRIER_LABELS.get(carrier, carrier),
        "result": _public_result(result, carrier),
    }


def _sync_checked_results(
    writer: Any,
    report_path: Path,
    entries: list[tuple[str, str, JobResult]],
) -> dict[str, Any]:
    sync = {"enabled": bool(writer.enabled), "updated_rows": 0, "ok": True}
    if not writer.enabled:
        return sync
    try:
        for tracking_number, carrier, result in entries:
            rows = _report_sheet_rows(report_path, tracking_number)
            sync["updated_rows"] += writer.update_rows(rows, result, carrier=carrier)
    except Exception:
        sync["ok"] = False
    return sync


CUSTOMER_CARRIER_LABELS = {
    "skyfrog": "KLEAN&KARE",
    "kex": "KEX",
    "interexpress": "InterExpress",
}



def _report_sheet_rows(path: Path, order_number: str) -> list[int]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("order_number", "").upper() != order_number.upper():
                    continue
                values = row.get("sheet_rows", "").split(",")
                return [int(value.strip()) for value in values if value.strip().isdigit()]
    except (OSError, csv.Error):
        return []
    return []


def create_app(
    *,
    settings: Settings | None = None,
    search_service: Any | None = None,
    sheet_writer: Any | None = None,
    mapping_store: Any | None = None,
    status_cache: Any | None = None,
    multiple_tracking_sync: Callable[[], int] | None = None,
    admin_health_service: Any | None = None,
) -> Flask:
    settings = settings or Settings.from_env(require_credentials=True)
    if not settings.cs_access_pin_hash:
        raise ValueError("กรุณาตั้งค่า CS_ACCESS_PIN_HASH ในไฟล์ .env")
    if len(settings.web_secret_key) < 32:
        raise ValueError("กรุณาตั้งค่า WEB_SECRET_KEY อย่างน้อย 32 ตัวอักษรในไฟล์ .env")

    app = Flask(__name__)
    app.secret_key = settings.web_secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.web_cookie_secure,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.web_session_hours),
        MAX_CONTENT_LENGTH=8 * 1024,
    )
    statuses = status_cache or SupabaseStatusCache(
        settings.supabase_url,
        settings.supabase_secret_key,
        table=settings.supabase_status_table,
        timeout=min(settings.request_timeout_seconds, 15),
    )
    service = search_service or LiveSearchService(settings, statuses)
    writer = sheet_writer or GoogleSheetsWriter(settings)
    mappings = mapping_store or SupabaseMappingStore(
        settings.supabase_url,
        settings.supabase_secret_key,
        table=settings.supabase_mapping_table,
        timeout=min(settings.request_timeout_seconds, 15),
    )
    sync_multiple_tracking = multiple_tracking_sync or (
        lambda: import_multiple_tracking_sheet(
            settings.sheet_id,
            mappings,
            timeout=settings.request_timeout_seconds,
        )
    )
    admin_health = admin_health_service or AdminHealthService(settings)
    login_limiter = SlidingWindowLimiter(limit=8, seconds=10 * 60)
    search_limiter = SlidingWindowLimiter(limit=30, seconds=60)
    shopee_login_limiter = SlidingWindowLimiter(limit=3, seconds=10 * 60)
    shopee_verify_limiter = SlidingWindowLimiter(limit=3, seconds=10 * 60)

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://www.skyfrog.net https://skyfrog.net; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.endpoint not in {"static", "health", "sheet_proof"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET" and _is_authenticated():
            return redirect(url_for("dashboard"))
        error = ""
        next_url = _safe_next_url(request.values.get("next", "/"))
        if request.method == "POST":
            ip = _client_ip()
            if not login_limiter.allow(ip):
                return render_template(
                    "login.html",
                    error="ลอง PIN หลายครั้งเกินไป กรุณารอ 10 นาที",
                    next_url=next_url,
                ), 429
            pin = request.form.get("pin", "")
            pin_matches = check_password_hash(settings.cs_access_pin_hash, pin)
            if pin_matches:
                login_limiter.clear(ip)
                session.clear()
                session.permanent = True
                session["cs_authenticated"] = True
                return redirect(next_url)
            error = "PIN ไม่ถูกต้อง กรุณาลองอีกครั้ง"
        return render_template("login.html", error=error, next_url=next_url)

    @app.post("/logout")
    @_login_required()
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @app.get("/dashboard")
    @_login_required()
    def dashboard():
        return render_template(
            "dashboard.html",
            summary=_report_summary(settings.output_dir / "latest.csv"),
        )

    @app.get("/admin")
    @_login_required()
    def admin():
        return render_template("admin.html")

    @app.get("/api/admin/health")
    @_login_required(api=True)
    def admin_health_snapshot():
        return jsonify(admin_health.snapshot())

    @app.get("/api/health/shopee")
    @_login_required(api=True)
    def shopee_health_snapshot():
        return jsonify(admin_health.shopee_snapshot())

    @app.post("/api/shopee/login-session")
    @_login_required(api=True)
    def start_shopee_login_session():
        if not request.is_json:
            return jsonify(error="คำขอไม่ถูกต้อง"), 415
        if not shopee_login_limiter.allow(_client_ip()):
            return jsonify(error="เปิดหน้าล็อกอินถี่เกินไป กรุณารอสักครู่"), 429
        if not settings.shopee_vnc_url:
            return jsonify(error="ยังไม่ได้ตั้งค่าลิงก์ VNC ของ Shopee"), 503
        trigger = settings.shopee_login_trigger
        try:
            if not trigger.parent.is_dir():
                raise OSError("Shopee control directory is unavailable")
            trigger.write_text(
                datetime.now().astimezone().isoformat(timespec="seconds"),
                encoding="utf-8",
            )
        except OSError:
            app.logger.exception("Could not request the Shopee login window")
            return jsonify(error="เปิดหน้าล็อกอินบน Pi ไม่สำเร็จ"), 503
        return jsonify(
            ok=True,
            url=settings.shopee_vnc_url,
            expires_minutes=settings.shopee_vnc_window_minutes,
        ), 202

    @app.post("/api/shopee/verify-session")
    @_login_required(api=True)
    def verify_shopee_login_session():
        if not request.is_json:
            return jsonify(error="คำขอไม่ถูกต้อง"), 415
        if not shopee_verify_limiter.allow(_client_ip()):
            return jsonify(error="ตรวจ Shopee ถี่เกินไป กรุณารอสักครู่"), 429
        trigger = settings.shopee_verify_trigger
        try:
            if not trigger.parent.is_dir():
                raise OSError("Shopee control directory is unavailable")
            trigger.write_text(
                datetime.now().astimezone().isoformat(timespec="seconds"),
                encoding="utf-8",
            )
        except OSError:
            app.logger.exception("Could not verify the Shopee login session")
            return jsonify(error="สั่งตรวจ Shopee บน Pi ไม่สำเร็จ"), 503
        return jsonify(
            ok=True,
            message="กำลังตรวจสอบ Shopee session ใหม่",
        ), 202

    @app.post("/api/check")
    @_login_required(api=True)
    def check_order():
        if not search_limiter.allow(_client_ip()):
            return jsonify(error="ค้นหาถี่เกินไป กรุณารอสักครู่แล้วลองใหม่"), 429
        payload = request.get_json(silent=True) or {}
        raw_order = str(payload.get("order", ""))
        searched_candidate = re.sub(r"\s+", "", raw_order).lstrip("#").upper()
        searched_with_tracking = bool(KEX_TRACKING_RE.fullmatch(searched_candidate))
        requested_carrier = str(payload.get("carrier", "auto")).strip().lower()
        direct_kex_lookup = requested_carrier == "kex" and searched_with_tracking
        group = None
        if not direct_kex_lookup:
            try:
                # Refresh the grouped-tracking tab before a mapped or automatic
                # search so newly added split shipments are available immediately.
                sync_multiple_tracking()
            except (OSError, ValueError, SupabaseMappingError, requests.RequestException):
                # Keep the last successfully imported mapping available if Google
                # Sheets is temporarily unavailable; live carrier checking still
                # proceeds normally.
                app.logger.warning("Grouped tracking sheet refresh failed", exc_info=True)
            try:
                group = _tracking_group_for_input(mappings, raw_order)
            except SupabaseMappingError:
                app.logger.warning("Supabase mapping lookup failed", exc_info=True)
        display_order_number: str | None = None
        try:
            if group:
                group_order, tracking_refs = group
                entries: list[tuple[str, str, JobResult]] = []
                for tracking_number, stored_carrier in tracking_refs:
                    result, resolved_carrier = _search_group_tracking(
                        service, tracking_number, stored_carrier
                    )
                    entries.append((tracking_number, resolved_carrier, result))
                sync = _sync_checked_results(
                    writer, settings.output_dir / "latest.csv", entries
                )
                if not sync["ok"]:
                    app.logger.error("Google Sheet sync failed for grouped tracking %s", group_order)
                group_payload = _group_summary(group_order, entries)
                if group_payload["multiple"]:
                    return jsonify(
                        result=_public_result(entries[0][2], entries[0][1]),
                        results=[
                            _public_group_entry(tracking, carrier, result)
                            for tracking, carrier, result in entries
                        ],
                        group=group_payload,
                        sheet_sync=sync,
                    )
                order_number, carrier, result = entries[0]
                display_order_number = group_order
            else:
                if requested_carrier == "auto":
                    order_number = normalize_auto_search_input(raw_order)
                    result, carrier = _search_with_carrier_priority(service, order_number)
                else:
                    carrier, order_number = normalize_tracking_input(raw_order, requested_carrier)
                    result = service.search(order_number, carrier)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except (
            SkyfrogError,
            KexError,
            InterexpressError,
            requests.RequestException,
        ):
            app.logger.exception("Carrier search failed for %s", order_number)
            return jsonify(error="เชื่อมต่อระบบขนส่งไม่สำเร็จ กรุณาลองใหม่อีกครั้ง"), 424
        sync = _sync_checked_results(
            writer,
            settings.output_dir / "latest.csv",
            [(order_number, carrier, result)],
        )
        if not sync["ok"]:
            app.logger.error("Google Sheet sync failed for %s", order_number)
        result_payload = _public_result(result, carrier)
        result_payload["carrier"] = CUSTOMER_CARRIER_LABELS.get(carrier, carrier)
        if display_order_number:
            result_payload["tracking_number"] = order_number
            result_payload["mapping_order_number"] = display_order_number
            if searched_with_tracking:
                result_payload["order_number"] = display_order_number
        return jsonify(result=result_payload, sheet_sync=sync)

    @app.get("/report")
    @app.get("/latest.html")
    @_login_required()
    def report():
        path = settings.output_dir / "latest.html"
        if not path.exists():
            return "ยังไม่มีรายงาน กรุณารอรอบตรวจอัตโนมัติ", 404
        return send_file(path, mimetype="text/html", conditional=False)

    @app.get("/proof/kex/<tracking>/<filename>")
    @_login_required()
    def kex_proof(tracking: str, filename: str):
        tracking = tracking.upper()
        if not KEX_TRACKING_RE.fullmatch(tracking):
            return "ไม่พบรูปหลักฐาน", 404
        if not re.fullmatch(r"proof-\d+-[0-9a-f]{10}\.(?:jpg|png|webp)", filename):
            return "ไม่พบรูปหลักฐาน", 404
        return send_from_directory(
            settings.kex_proof_dir / tracking,
            filename,
            conditional=True,
        )

    @app.get("/sheet-proof/<token>")
    def sheet_proof(token: str):
        try:
            source = read_sheet_proof_token(token, settings.web_secret_key)
        except ValueError:
            return "ไม่พบรูปหลักฐาน", 404
        local_proof = local_kex_proof_parts(source)
        if local_proof:
            tracking, filename = local_proof
            return send_from_directory(
                settings.kex_proof_dir / tracking,
                filename,
                conditional=True,
                max_age=3600,
            )
        try:
            upstream = requests.get(
                source,
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": "Google-Sheets-Proof-Proxy/1.0"},
            )
            upstream.raise_for_status()
        except requests.RequestException:
            return "ไม่สามารถโหลดรูปหลักฐาน", 502
        content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0]
        if not content_type.startswith("image/") or len(upstream.content) > 10 * 1024 * 1024:
            return "ไม่พบรูปหลักฐาน", 404
        return send_file(
            io.BytesIO(upstream.content),
            mimetype=content_type,
            conditional=True,
            max_age=3600,
        )

    @app.get("/download/latest.csv")
    @_login_required()
    def download_report():
        path = settings.output_dir / "latest.csv"
        if not path.exists():
            return "ยังไม่มีรายงาน กรุณารอรอบตรวจอัตโนมัติ", 404
        return send_file(
            path,
            mimetype="text/csv",
            as_attachment=True,
            download_name="klean-kare-latest.csv",
            conditional=False,
        )

    return app
