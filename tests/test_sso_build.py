import json
import unittest
from unittest.mock import MagicMock, patch

from grok_register.sso_build import (
    DEVICE_GRANT_TYPE,
    MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS,
    SSO_BUILD_CLIENT_ID,
    SSO_BUILD_REFERRER,
    SSO_BUILD_SCOPE,
    SSOBuildError,
    SSOBuildFlow,
    access_token_has_bot_flag,
    build_grok2api_import_document,
    build_verification_uri_complete,
    normalize_sso_token,
    safe_xai_url,
    save_build_auth,
    valid_user_code,
)


class DummyResponse:
    def __init__(self, status_code=200, body=b"", headers=None, cookies=None, url=""):
        self.status_code = status_code
        self.content = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.url = url


def _make_jwt(claims: dict) -> str:
    import base64

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}.sig"


class SSOBuildHelpersTests(unittest.TestCase):
    def test_normalize_sso_token(self):
        self.assertEqual(normalize_sso_token("sso=abc; Path=/"), "abc")
        self.assertEqual(normalize_sso_token("  raw.token  "), "raw.token")

    def test_safe_xai_url(self):
        self.assertTrue(safe_xai_url("https://auth.x.ai/oauth2/device/code"))
        self.assertTrue(safe_xai_url("https://accounts.x.ai/"))
        self.assertTrue(safe_xai_url("https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"))
        self.assertTrue(safe_xai_url("http://localhost:22255/oauth2/device"))
        self.assertFalse(safe_xai_url("http://auth.x.ai/"))
        self.assertFalse(safe_xai_url("https://evil.com/"))
        self.assertFalse(safe_xai_url("https://user@auth.x.ai/"))

    def test_scopes_match_grok_build_contract(self):
        scopes = SSO_BUILD_SCOPE.split()
        self.assertEqual(
            scopes,
            [
                "openid",
                "profile",
                "email",
                "offline_access",
                "grok-cli:access",
                "api:access",
                "conversations:read",
                "conversations:write",
                "workspaces:read",
                "workspaces:write",
            ],
        )
        self.assertEqual(SSO_BUILD_REFERRER, "grok-build")
        self.assertEqual(SSO_BUILD_CLIENT_ID, "b1a00492-073a-47ea-816f-4c329264a828")
        self.assertEqual(DEVICE_GRANT_TYPE, "urn:ietf:params:oauth:grant-type:device_code")
        self.assertEqual(MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS, 600)

    def test_valid_user_code_and_complete_uri(self):
        self.assertTrue(valid_user_code("ABCD-1234"))
        self.assertFalse(valid_user_code("AB CD"))
        self.assertFalse(valid_user_code(""))
        built = build_verification_uri_complete(
            "https://accounts.x.ai/oauth2/device",
            "ABCD-1234",
            "",
        )
        self.assertIn("user_code=ABCD-1234", built)
        self.assertTrue(built.startswith("https://accounts.x.ai/oauth2/device"))

    def test_access_token_has_bot_flag(self):
        self.assertTrue(
            access_token_has_bot_flag(_make_jwt({"bot_flag_source": 1, "sub": "x"}))
        )
        self.assertTrue(
            access_token_has_bot_flag(_make_jwt({"bot_flag_source": "1"}))
        )
        self.assertFalse(
            access_token_has_bot_flag(_make_jwt({"bot_flag_source": 0, "sub": "x"}))
        )
        self.assertFalse(access_token_has_bot_flag(_make_jwt({"sub": "x"})))
        self.assertFalse(access_token_has_bot_flag(""))

    def test_build_documents(self):
        seed = {
            "name": "a@example.com",
            "email": "a@example.com",
            "user_id": "uid-1",
            "team_id": "team-1",
            "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": "idt",
            "expires_in": 3600,
            "expires_at": "2026-07-18T00:00:00Z",
            "scope": "openid",
        }
        doc = build_grok2api_import_document(seed)
        self.assertEqual(doc["accounts"][0]["provider"], "grok_build")
        self.assertEqual(doc["accounts"][0]["access_token"], "at")
        self.assertEqual(doc["accounts"][0]["refresh_token"], "rt")


class SSOBuildFlowTests(unittest.TestCase):
    def test_convert_happy_path(self):
        # Sequence of _do calls inside convert():
        # 1 GET accounts
        # 2 POST device/code
        # 3 GET verification complete
        # 4 POST verify
        # 5 POST approve
        # 6+ POST token (poll)
        device_body = json.dumps(
            {
                "device_code": "dev-1",
                "user_code": "ABCD-1234",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                "interval": 1,
                "expires_in": 600,
            }
        ).encode()
        token_body = json.dumps(
            {
                "access_token": "access-xyz",
                "refresh_token": "refresh-xyz",
                "id_token": "id-xyz",
                "expires_in": 3600,
            }
        ).encode()
        verify_page = (
            b'<form action="https://auth.x.ai/oauth2/device/verify" method="POST">'
            b'<input name="user_code" value="ABCD-1234"/>'
            b"<button>Continue</button></form>"
        )
        consent_page = (
            b'<form action="https://auth.x.ai/oauth2/device/approve" method="POST">'
            b'<input name="user_code" value="ABCD-1234"/>'
            b'<input name="action" value=""/>'
            b"<button>Deny</button><button>Allow</button></form>"
        )

        call_plan = []

        def fake_request(method, url, headers=None, data=None):
            call_plan.append((method, url, data, dict(headers or {})))
            # accounts validate
            if url.rstrip("/") == "https://accounts.x.ai":
                return DummyResponse(200, b"ok")
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            if "accounts.x.ai/oauth2/device" in url and method == "GET":
                return DummyResponse(200, verify_page)
            if url.endswith("/oauth2/device/verify"):
                return DummyResponse(
                    302,
                    b"",
                    headers={"Location": "https://auth.x.ai/oauth2/device/consent"},
                )
            if url.endswith("/oauth2/device/consent"):
                return DummyResponse(200, consent_page)
            if url.endswith("/oauth2/device/approve"):
                return DummyResponse(
                    302, b"", headers={"Location": "https://auth.x.ai/oauth2/device/done"}
                )
            if url.endswith("/oauth2/device/done"):
                return DummyResponse(200, b"done")
            if url.endswith("/oauth2/token"):
                return DummyResponse(200, token_body)
            return DummyResponse(404, b"missing")

        flow = SSOBuildFlow("sso-token-value", user_agent="test-agent", proxies={})
        with patch.object(flow, "_request", side_effect=fake_request):
            with patch("grok_register.sso_build.time.sleep", return_value=None):
                seed = flow.convert(email="user@example.com")

        self.assertEqual(seed["access_token"], "access-xyz")
        self.assertEqual(seed["refresh_token"], "refresh-xyz")
        self.assertEqual(seed["email"], "user@example.com")
        self.assertEqual(seed["provider"], "grok_build")
        self.assertIn("workspaces:read", seed["scope"])
        self.assertTrue(any(u.endswith("/oauth2/device/code") for _, u, _, _ in call_plan))
        self.assertTrue(any(u.endswith("/oauth2/token") for _, u, _, _ in call_plan))

        # Device code + token requests must send grok-build referrer/scopes/headers.
        code_calls = [c for c in call_plan if c[1].endswith("/oauth2/device/code")]
        self.assertTrue(code_calls)
        code_data = code_calls[0][2] or ""
        self.assertIn("referrer=grok-build", code_data)
        self.assertIn("workspaces%3Aread", code_data.replace("%3a", "%3A"))
        # urlencode uses %3A for ':'
        self.assertTrue(
            "workspaces%3Aread" in code_data or "workspaces:read" in code_data
        )
        code_headers = code_calls[0][3]
        self.assertEqual(code_headers.get("x-grok-client-version"), "0.2.109")
        self.assertEqual(code_headers.get("x-grok-client-surface"), "cli")
        self.assertIn("application/json", code_headers.get("Accept", ""))

        token_calls = [c for c in call_plan if c[1].endswith("/oauth2/token")]
        self.assertTrue(token_calls)
        token_data = token_calls[0][2] or ""
        self.assertIn("device_code=dev-1", token_data)
        self.assertIn(f"client_id={SSO_BUILD_CLIENT_ID}", token_data)
        self.assertIn("grant_type=", token_data)
        self.assertIn("device_code", token_data)

    def test_start_device_builds_complete_uri_when_missing(self):
        device_body = json.dumps(
            {
                "device_code": "dev-2",
                "user_code": "WXYZ-9999",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "interval": 5,
                "expires_in": 1800,
            }
        ).encode()

        def fake_request(method, url, headers=None, data=None):
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            return DummyResponse(404, b"missing")

        flow = SSOBuildFlow("sso-token-value")
        with patch.object(flow, "_request", side_effect=fake_request):
            device = flow._start_device()
        self.assertEqual(device["user_code"], "WXYZ-9999")
        self.assertIn("user_code=WXYZ-9999", device["verification_uri_complete"])

    def test_empty_sso_raises(self):
        with self.assertRaises(SSOBuildError):
            SSOBuildFlow("")

    def test_save_build_auth(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        seed = {
            "email": "x@y.z",
            "user_id": "sub",
            "access_token": "a",
            "refresh_token": "r",
            "id_token": "i",
            "expires_in": 100,
            "expires_at": "2026-07-18T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            path = save_build_auth(seed, td, email="x@y.z")
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["accounts"][0]["access_token"], "a")
            self.assertEqual(data["accounts"][0]["provider"], "grok_build")

    def test_browser_steps_continue_then_allow(self):
        """Must click Continue on user-code page before Allow on consent."""
        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        wait_returns = iter(["user_code", "consent"])

        def fake_wait(page, wanted, timeout=15.0, log_callback=None, label=""):
            try:
                return next(wait_returns)
            except StopIteration:
                return "done"

        calls = []

        def fake_continue(page, user_code, log_callback=None):
            calls.append(("continue", user_code))
            return True

        def fake_allow(page, user_code, log_callback=None):
            calls.append(("allow", user_code))
            return True

        with patch.object(flow, "_wait_browser_phase", side_effect=fake_wait):
            with patch.object(flow, "_browser_submit_continue", side_effect=fake_continue):
                with patch.object(flow, "_browser_submit_allow", side_effect=fake_allow):
                    ok = flow._browser_device_flow_steps(page, "ABCD-1234")

        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], ["continue", "allow"])
        self.assertEqual(calls[0][1], "ABCD-1234")

    def test_browser_allow_refuses_user_code_phase(self):
        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        with patch.object(flow, "_wait_browser_phase", return_value="user_code"):
            ok = flow._browser_submit_allow(page, "ABCD-1234")
        self.assertFalse(ok)

    def test_verify_http_rejects_auth_bounce(self):
        flow = SSOBuildFlow("sso-token-value")
        device = {
            "device_code": "dev",
            "user_code": "ABCD-1234",
            "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
        }

        def fake_do(method, endpoint, form, api_client=False):
            if method == "GET":
                return 200, "https://accounts.x.ai/sign-in?redirect=oauth2", b"login"
            return 200, endpoint, b""

        with patch.object(flow, "_do", side_effect=fake_do):
            with self.assertRaises(SSOBuildError) as ctx:
                flow._verify_and_approve_http(device)
        self.assertTrue(ctx.exception.unauthorized)


if __name__ == "__main__":
    unittest.main()
