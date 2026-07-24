from pathlib import Path

from werkzeug.security import generate_password_hash

from klean_pod_checker.config import Settings
from klean_pod_checker.webapp import create_app


class DisabledWriter:
    enabled = False


class FakeMappings:
    def get_tracking_refs(self, _order_number):
        return []

    def get_order_for_tracking(self, _tracking_number):
        return None


class FakeStatusCache:
    def put(self, _result):
        return None


class FakeSearch:
    def search(self, *_args, **_kwargs):
        raise AssertionError("search should not run in auth tests")


class FakeAdminHealth:
    def snapshot(self):
        return {
            "overall": "ok",
            "checked_at": "2026-07-24T12:00:00+07:00",
            "counts": {"ok": 1, "warning": 0, "error": 0},
            "checks": [],
        }


def settings(tmp_path: Path) -> Settings:
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
        output_dir=tmp_path,
        web_secret_key="x" * 48,
        google_sheets_webhook_url="",
        google_sheets_webhook_secret="",
        cs_access_pin_hash=generate_password_hash(
            "safe-test-pin", method="pbkdf2:sha256:600000"
        ),
        web_cookie_secure=False,
        supabase_url="https://example.supabase.co",
        supabase_secret_key="test-secret",
    )


def app(tmp_path):
    result = create_app(
        settings=settings(tmp_path),
        search_service=FakeSearch(),
        sheet_writer=DisabledWriter(),
        mapping_store=FakeMappings(),
        status_cache=FakeStatusCache(),
        multiple_tracking_sync=lambda: 0,
        admin_health_service=FakeAdminHealth(),
    )
    result.config["TESTING"] = True
    return result


def test_cs_pages_redirect_to_login(tmp_path):
    client = app(tmp_path).test_client()
    for path in (
        "/",
        "/dashboard",
        "/admin",
        "/report",
        "/latest.html",
        "/download/latest.csv",
    ):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_customer_routes_do_not_exist(tmp_path):
    client = app(tmp_path).test_client()
    assert client.get("/customer.html").status_code == 404
    assert client.post("/api/customer-check", json={"order": "x"}).status_code == 404


def test_pin_hash_login_and_logout(tmp_path):
    client = app(tmp_path).test_client()
    failed = client.post("/login", data={"pin": "wrong"})
    assert failed.status_code == 200
    assert "PIN ไม่ถูกต้อง".encode() in failed.data

    logged_in = client.post(
        "/login", data={"pin": "safe-test-pin"}, follow_redirects=True
    )
    assert logged_in.status_code == 200
    assert "เช็กสถานะออเดอร์ได้ทันที".encode() in logged_in.data

    logged_out = client.post("/logout")
    assert logged_out.status_code == 302
    assert "/login" in logged_out.headers["Location"]


def test_health_is_public_but_api_is_protected(tmp_path):
    client = app(tmp_path).test_client()
    assert client.get("/health").get_json() == {"status": "ok"}
    api = client.post("/api/check", json={"order": "test"})
    assert api.status_code == 401
    assert client.get("/api/admin/health").status_code == 401


def test_admin_page_and_health_api_require_valid_session(tmp_path):
    client = app(tmp_path).test_client()
    client.post("/login", data={"pin": "safe-test-pin"})

    page = client.get("/admin")
    assert page.status_code == 200
    assert "ตรวจสุขภาพระบบ".encode() in page.data

    payload = client.get("/api/admin/health")
    assert payload.status_code == 200
    assert payload.get_json()["overall"] == "ok"
