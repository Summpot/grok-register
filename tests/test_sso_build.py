import json
import unittest
from unittest.mock import MagicMock, patch

from grok_register.sso_build import (
    SSOBuildError,
    SSOBuildFlow,
    access_token_has_bot_flag,
    build_grok2api_import_document,
    normalize_sso_token,
    safe_xai_url,
    save_build_auth,
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
        self.assertFalse(safe_xai_url("http://auth.x.ai/"))
        self.assertFalse(safe_xai_url("https://evil.com/"))
        self.assertFalse(safe_xai_url("https://user@auth.x.ai/"))

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
                "verification_uri_complete": "https://auth.x.ai/oauth2/device/user_code?user_code=ABCD-1234",
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

        responses = [
            DummyResponse(200, b"ok", url="https://accounts.x.ai/"),
            DummyResponse(200, device_body),
            DummyResponse(200, b"verify-page"),
            DummyResponse(302, b"", headers={"Location": "https://auth.x.ai/oauth2/device/consent"}),
            DummyResponse(302, b"", headers={"Location": "https://auth.x.ai/oauth2/device/done"}),
            DummyResponse(200, token_body),
        ]
        # After redirects, _do continues until non-3xx. Simulate final OK for consent/done.
        # Our DummyResponse for 302 will be followed by the flow's next request only if
        # Location is used and another request is made. Patch _request instead.

        call_plan = []

        def fake_request(method, url, headers=None, data=None):
            call_plan.append((method, url, data))
            # accounts validate
            if url.rstrip("/") == "https://accounts.x.ai":
                return DummyResponse(200, b"ok")
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            if "user_code=ABCD-1234" in url or "device/user_code" in url:
                return DummyResponse(200, b"page")
            if url.endswith("/oauth2/device/verify"):
                return DummyResponse(
                    302, b"", headers={"Location": "https://auth.x.ai/oauth2/device/consent"}
                )
            if url.endswith("/oauth2/device/consent"):
                return DummyResponse(200, b"consent")
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
        self.assertTrue(any(u.endswith("/oauth2/device/code") for _, u, _ in call_plan))
        self.assertTrue(any(u.endswith("/oauth2/token") for _, u, _ in call_plan))

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


if __name__ == "__main__":
    unittest.main()
