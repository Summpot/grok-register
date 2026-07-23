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
    convert_sso_to_build,
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
    def test_browser_convert_happy_path(self):
        # grok-build path: mint + poll over API; verify/approve via page.
        # No accounts.x.ai HTTP pre-validate (that path returns WAF 403).
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

        call_plan = []

        def fake_request(method, url, headers=None, data=None):
            call_plan.append((method, url, data, dict(headers or {})))
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            if url.endswith("/oauth2/token"):
                return DummyResponse(200, token_body)
            return DummyResponse(404, b"missing")

        page = MagicMock()
        page.url = "https://auth.x.ai/oauth2/device/done"
        page.get = MagicMock()
        page.cookies = MagicMock(return_value=[])
        page.set = MagicMock()

        flow = SSOBuildFlow("sso-token-value", user_agent="test-agent", proxies={})
        with patch.object(flow, "_request", side_effect=fake_request):
            with patch.object(flow, "_browser_device_flow_steps", return_value=True):
                with patch.object(flow, "_page_is_device_done", return_value=True):
                    with patch("grok_register.sso_build.time.sleep", return_value=None):
                        seed = flow.convert_with_browser(page, email="user@example.com")

        self.assertEqual(seed["access_token"], "access-xyz")
        self.assertEqual(seed["refresh_token"], "refresh-xyz")
        self.assertEqual(seed["email"], "user@example.com")
        self.assertEqual(seed["provider"], "grok_build")
        self.assertIn("workspaces:read", seed["scope"])
        page.get.assert_called_once()
        self.assertTrue(
            page.get.call_args[0][0].startswith("https://accounts.x.ai/oauth2/device")
        )
        # Must never pre-validate via accounts.x.ai HTTP (WAF 403).
        self.assertFalse(
            any(u.rstrip("/") == "https://accounts.x.ai" for _, u, _, _ in call_plan)
        )
        self.assertTrue(any(u.endswith("/oauth2/device/code") for _, u, _, _ in call_plan))
        self.assertTrue(any(u.endswith("/oauth2/token") for _, u, _, _ in call_plan))

        code_calls = [c for c in call_plan if c[1].endswith("/oauth2/device/code")]
        self.assertTrue(code_calls)
        code_data = code_calls[0][2] or ""
        self.assertIn("referrer=grok-build", code_data)
        self.assertTrue(
            "workspaces%3Aread" in code_data or "workspaces:read" in code_data
        )
        code_headers = code_calls[0][3]
        self.assertEqual(code_headers.get("x-grok-client-version"), "0.2.110")
        self.assertEqual(code_headers.get("x-grok-client-surface"), "cli")
        self.assertIn("application/json", code_headers.get("Accept", ""))
        # Mint/poll are unauthenticated like grok-build reqwest calls.
        self.assertNotIn("Cookie", code_headers)

        token_calls = [c for c in call_plan if c[1].endswith("/oauth2/token")]
        self.assertTrue(token_calls)
        token_data = token_calls[0][2] or ""
        self.assertIn("device_code=dev-1", token_data)
        self.assertIn(f"client_id={SSO_BUILD_CLIENT_ID}", token_data)
        self.assertNotIn("Cookie", token_calls[0][3])

    def test_browser_steps_failure_does_not_http_fallback(self):
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

        def fake_request(method, url, headers=None, data=None):
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            return DummyResponse(404, b"missing")

        page = MagicMock()
        page.url = "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"
        page.get = MagicMock()
        page.cookies = MagicMock(return_value=[])
        page.set = MagicMock()

        flow = SSOBuildFlow("sso-token-value")
        with patch.object(flow, "_request", side_effect=fake_request):
            with patch.object(flow, "_page_is_device_done", return_value=False):
                with patch.object(flow, "_browser_device_flow_steps", return_value=False):
                    with patch.object(flow, "_browser_page_phase", return_value="unknown"):
                        with self.assertRaises(SSOBuildError) as ctx:
                            flow.convert_with_browser(page, email="user@example.com")
        self.assertIn("browser Device Flow steps failed", str(ctx.exception))
        # Must never have polled token after browser failure.
        # (fake_request would 404 token; exception is raised first)

    def test_browser_convert_rejects_auth_bounce_on_verify(self):
        device_body = json.dumps(
            {
                "device_code": "dev-bounce",
                "user_code": "BOUN-0001",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=BOUN-0001",
                "interval": 1,
                "expires_in": 600,
            }
        ).encode()

        def fake_request(method, url, headers=None, data=None):
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(200, device_body)
            return DummyResponse(404, b"missing")

        page = MagicMock()
        page.url = "https://accounts.x.ai/sign-in"
        page.get = MagicMock()
        page.cookies = MagicMock(return_value=[])
        page.set = MagicMock()

        flow = SSOBuildFlow("sso-token-value")
        with patch.object(flow, "_request", side_effect=fake_request):
            with self.assertRaises(SSOBuildError) as ctx:
                flow.convert_with_browser(page, email="user@example.com")
        self.assertTrue(ctx.exception.unauthorized)
        self.assertIn("bounced to sign-in", str(ctx.exception))

    def test_convert_sso_to_build_requires_page(self):
        with self.assertRaises(SSOBuildError) as ctx:
            convert_sso_to_build("sso-token", page=None, mode="http")
        self.assertIn("requires an active page", str(ctx.exception))

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

    def test_start_device_backoff_on_429_slow_down(self):
        """HTTP 429 slow_down should exponential-backoff then succeed."""
        import grok_register.sso_build as sso_mod

        device_body = json.dumps(
            {
                "device_code": "dev-3",
                "user_code": "RETRY-0001",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "interval": 5,
                "expires_in": 600,
            }
        ).encode()
        rate_body = json.dumps(
            {
                "error": "slow_down",
                "error_description": (
                    "Too many device code requests. Please wait and try again."
                ),
            }
        ).encode()
        hits = {"n": 0}

        def fake_request(method, url, headers=None, data=None):
            if url.endswith("/oauth2/device/code"):
                hits["n"] += 1
                if hits["n"] <= 2:
                    return DummyResponse(429, rate_body)
                return DummyResponse(200, device_body)
            return DummyResponse(404, b"missing")

        logs: list[str] = []
        sleeps: list[float] = []

        flow = SSOBuildFlow("sso-token-value")
        # Reset process-wide pacing so this test is isolated.
        sso_mod._device_start_next_ok_at = 0.0

        with patch.object(flow, "_request", side_effect=fake_request):
            with patch(
                "grok_register.sso_build.time.sleep",
                side_effect=lambda s: sleeps.append(float(s)),
            ):
                with patch(
                    "grok_register.sso_build.random.uniform",
                    return_value=0.0,
                ):
                    device = flow._start_device(log_callback=logs.append)

        self.assertEqual(device["user_code"], "RETRY-0001")
        self.assertEqual(hits["n"], 3)
        # attempt1 → 5s, attempt2 → 10s (base * 2^(n-1), no jitter)
        self.assertEqual(sleeps, [5.0, 10.0])
        joined = "\n".join(logs)
        self.assertIn("rate limited", joined)
        self.assertIn("backoff", joined)

    def test_start_device_exhausted_429_raises(self):
        import grok_register.sso_build as sso_mod

        rate_body = b'{"error":"slow_down","error_description":"Too many device code requests."}'

        def fake_request(method, url, headers=None, data=None):
            if url.endswith("/oauth2/device/code"):
                return DummyResponse(429, rate_body)
            return DummyResponse(404, b"missing")

        flow = SSOBuildFlow("sso-token-value")
        sso_mod._device_start_next_ok_at = 0.0
        with patch.object(flow, "_request", side_effect=fake_request):
            with patch("grok_register.sso_build.time.sleep", return_value=None):
                with patch.object(sso_mod, "DEVICE_START_MAX_ATTEMPTS", 3):
                    with self.assertRaises(SSOBuildError) as ctx:
                        flow._start_device()
        self.assertIn("429", str(ctx.exception))
        self.assertIn("exhausted", str(ctx.exception))

    def test_device_start_backoff_secs_exponential_cap(self):
        with patch("grok_register.sso_build.random.uniform", return_value=0.0):
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(1), 5.0)
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(2), 10.0)
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(3), 20.0)
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(4), 40.0)
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(5), 60.0)
            self.assertEqual(SSOBuildFlow._device_start_backoff_secs(8), 60.0)

    def test_request_uses_page_request_api(self):
        """Device/token HTTP must go through page.request (not curl)."""
        device_body = json.dumps(
            {
                "device_code": "dev-pw",
                "user_code": "PAGE-0001",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "interval": 5,
                "expires_in": 600,
            }
        ).encode()

        class FakeApiResponse:
            def __init__(self, status, body, url=""):
                self.status = status
                self._body = body
                self.url = url
                self.headers = {"content-type": "application/json"}

            def body(self):
                return self._body

            def headers_array(self):
                return [{"name": "content-type", "value": "application/json"}]

        calls = []

        class FakeApi:
            def get(self, url, **kwargs):
                calls.append(("GET", url, kwargs))
                return FakeApiResponse(200, b"ok", url)

            def post(self, url, **kwargs):
                calls.append(("POST", url, kwargs.get("data"), kwargs))
                return FakeApiResponse(200, device_body, url)

        page = MagicMock()
        page.request = FakeApi()

        flow = SSOBuildFlow("sso-token-value")
        flow._request_page = page
        import grok_register.sso_build as sso_mod

        sso_mod._device_start_next_ok_at = 0.0
        with patch("grok_register.sso_build.time.sleep", return_value=None):
            device = flow._start_device()
        self.assertEqual(device["device_code"], "dev-pw")
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/oauth2/device/code") for c in calls))
        # max_redirects=0 so _do keeps manual redirect control
        post_kwargs = next(c[3] for c in calls if c[0] == "POST")
        self.assertEqual(post_kwargs.get("max_redirects"), 0)

    def test_request_requires_page_api(self):
        flow = SSOBuildFlow("sso-token-value")
        with self.assertRaises(SSOBuildError) as ctx:
            flow._request("GET", "https://accounts.x.ai/", headers={}, data=None)
        self.assertIn("page.request", str(ctx.exception))

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

    def test_browser_click_retry_success(self):
        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        page.run_js = MagicMock(
            return_value={"ok": True, "via": "retry_click", "text": "retry"}
        )
        logs: list[str] = []
        with patch("grok_register.sso_build.time.sleep", return_value=None):
            ok = flow._browser_click_retry(page, log_callback=logs.append)
        self.assertTrue(ok)
        self.assertTrue(any("Retry" in m for m in logs))
        page.run_js.assert_called_once()

    def test_browser_allow_clicks_retry_then_reallow(self):
        """After Allow, error page with Retry → click Retry → re-Allow → done."""
        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        page.url = (
            "https://accounts.x.ai/oauth2/device/consent?user_code=ABCD-1234"
        )
        logs: list[str] = []

        wait_seq = iter(["consent", "consent"])

        def fake_wait(page, wanted, timeout=15.0, log_callback=None, label=""):
            try:
                return next(wait_seq)
            except StopIteration:
                return "done"

        allow_calls = {"n": 0}

        def fake_allow_js(page, user_code, log_callback=None):
            allow_calls["n"] += 1
            return {"ok": True, "via": "allow_click"}

        recover_calls = {"n": 0}

        def fake_recover(
            page,
            log_callback=None,
            max_clicks=3,
            wait_after=12.0,
            wanted=None,
        ):
            recover_calls["n"] += 1
            return "consent"

        # After first Allow: error once, then re-Allow marks done.
        phase_after_allow = {"n": 0}

        def fake_phase(page):
            if allow_calls["n"] == 1 and phase_after_allow["n"] == 0:
                phase_after_allow["n"] += 1
                return "error"
            return "consent"

        def is_done(page):
            return allow_calls["n"] >= 2

        with patch.object(flow, "_wait_browser_phase", side_effect=fake_wait):
            with patch.object(flow, "_browser_page_phase", side_effect=fake_phase):
                with patch.object(
                    flow, "_browser_click_allow_js", side_effect=fake_allow_js
                ):
                    with patch.object(
                        flow,
                        "_browser_recover_error_page",
                        side_effect=fake_recover,
                    ):
                        with patch.object(
                            flow, "_page_is_device_done", side_effect=is_done
                        ):
                            with patch(
                                "grok_register.sso_build.time.sleep",
                                return_value=None,
                            ):
                                ok = flow._browser_submit_allow(
                                    page,
                                    "ABCD-1234",
                                    log_callback=logs.append,
                                )

        self.assertTrue(ok)
        self.assertGreaterEqual(allow_calls["n"], 2)
        self.assertGreaterEqual(recover_calls["n"], 1)
        self.assertTrue(any("Retry" in m or "error" in m.lower() for m in logs))

    def test_device_flow_steps_retries_on_initial_error(self):
        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        wait_returns = iter(["error", "consent"])

        def fake_wait(page, wanted, timeout=15.0, log_callback=None, label=""):
            try:
                return next(wait_returns)
            except StopIteration:
                return "done"

        calls = []

        def fake_recover(
            page,
            log_callback=None,
            max_clicks=3,
            wait_after=12.0,
            wanted=None,
        ):
            calls.append("retry")
            return "consent"

        def fake_allow(page, user_code, log_callback=None):
            calls.append("allow")
            return True

        with patch.object(flow, "_wait_browser_phase", side_effect=fake_wait):
            with patch.object(
                flow, "_browser_recover_error_page", side_effect=fake_recover
            ):
                with patch.object(
                    flow, "_browser_submit_allow", side_effect=fake_allow
                ):
                    ok = flow._browser_device_flow_steps(page, "ABCD-1234")

        self.assertTrue(ok)
        self.assertEqual(calls, ["retry", "allow"])

    def test_format_browser_diag_includes_key_fields(self):
        flow = SSOBuildFlow("sso-token-value")
        text = flow._format_browser_diag(
            {
                "phase": "user_code",
                "url": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                "ready_state": "complete",
                "title": "Authorize",
                "forms": [
                    {
                        "i": 0,
                        "method": "post",
                        "action": "https://auth.x.ai/oauth2/device/verify",
                        "inputs": [{"name": "user_code", "type": "text", "disabled": False}],
                    }
                ],
                "buttons": [{"text": "Continue", "disabled": False, "visible": True}],
                "code_input": {
                    "name": "user_code",
                    "valueLen": 9,
                    "valuePreview": "AB…34",
                    "matchesExpected": True,
                    "disabled": False,
                    "readOnly": False,
                },
                "errors": ["invalid code"],
                "body_snippet": "Enter device code Continue",
            },
            label="still on user-code after Continue",
        )
        self.assertIn("still on user-code after Continue", text)
        self.assertIn("phase=user_code", text)
        self.assertIn("device/verify", text)
        self.assertIn("Continue", text)
        self.assertIn("match_expected=True", text)
        self.assertIn("invalid code", text)

    def test_continue_stuck_logs_diag(self):
        import grok_register.sso_build as sso_mod

        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        page.url = "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"
        page.run_js = MagicMock(
            return_value={
                "ok": True,
                "via": "continue_click",
                "codeValueLen": 9,
                "codeMatches": True,
                "url": page.url,
            }
        )
        logs: list[str] = []

        with patch.object(sso_mod, "DEVICE_VERIFY_MAX_ATTEMPTS", 2):
            with patch.object(flow, "_wait_browser_phase", return_value="user_code"):
                with patch.object(flow, "_page_is_device_rate_limited", return_value=False):
                    with patch.object(flow, "_page_is_device_done", return_value=False):
                        with patch.object(
                            flow, "_browser_page_phase", return_value="user_code"
                        ):
                            with patch(
                                "grok_register.sso_build.time.sleep", return_value=None
                            ):
                                with patch.object(
                                    flow,
                                    "_browser_reopen_user_code_page",
                                    return_value=None,
                                ):
                                    with patch.object(
                                        flow,
                                        "_collect_browser_diag",
                                        return_value={
                                            "phase": "user_code",
                                            "url": page.url,
                                            "ready_state": "complete",
                                            "title": "t",
                                            "forms": [],
                                            "buttons": [
                                                {
                                                    "text": "Continue",
                                                    "disabled": False,
                                                    "visible": True,
                                                }
                                            ],
                                            "code_input": {
                                                "name": "user_code",
                                                "valueLen": 9,
                                                "valuePreview": "AB…34",
                                                "matchesExpected": True,
                                                "disabled": False,
                                                "readOnly": False,
                                            },
                                            "errors": [],
                                            "body_snippet": "Enter device code",
                                        },
                                    ):
                                        ok = flow._browser_submit_continue(
                                            page,
                                            "ABCD-1234",
                                            log_callback=logs.append,
                                        )

        self.assertFalse(ok)
        joined = "\n".join(logs)
        self.assertIn("still on user-code after Continue", joined)
        self.assertIn("phase_after", joined)

    def test_continue_rate_limited_reenters_user_code(self):
        """error=rate_limited → backoff → reopen → re-type code → consent."""
        import grok_register.sso_build as sso_mod

        flow = SSOBuildFlow("sso-token-value")
        page = MagicMock()
        page.url = "https://accounts.x.ai/oauth2/device?error=rate_limited"
        logs: list[str] = []
        clicks = {"n": 0}
        reopens = {"n": 0}

        def fake_click(page, user_code, log_callback=None):
            clicks["n"] += 1
            return {
                "ok": True,
                "via": "continue_click",
                "codeValueLen": len(user_code),
                "codeMatches": True,
                "url": page.url,
            }

        def fake_wait(page, wanted, timeout=15.0, log_callback=None, label="",
                      detect_rate_limit=True):
            # First post-Continue wait is rate_limited; second reaches consent.
            if label == "after Continue":
                return "consent" if clicks["n"] >= 2 else "rate_limited"
            return "user_code"

        def fake_reopen(page, user_code, log_callback=None):
            reopens["n"] += 1
            page.url = f"https://accounts.x.ai/oauth2/device?user_code={user_code}"

        rate_flags = iter([True, True, False, False, False, False])

        def fake_rate(page):
            try:
                return next(rate_flags)
            except StopIteration:
                return False

        sso_mod._device_start_next_ok_at = 0.0
        with patch.object(flow, "_browser_click_continue_js", side_effect=fake_click):
            with patch.object(flow, "_wait_browser_phase", side_effect=fake_wait):
                with patch.object(
                    flow, "_page_is_device_rate_limited", side_effect=fake_rate
                ):
                    with patch.object(flow, "_page_is_device_done", return_value=False):
                        with patch.object(
                            flow, "_browser_page_phase", return_value="user_code"
                        ):
                            with patch.object(
                                flow,
                                "_browser_reopen_user_code_page",
                                side_effect=fake_reopen,
                            ):
                                with patch(
                                    "grok_register.sso_build.time.sleep",
                                    return_value=None,
                                ):
                                    with patch(
                                        "grok_register.sso_build.random.uniform",
                                        return_value=0.0,
                                    ):
                                        ok = flow._browser_submit_continue(
                                            page,
                                            "64E2-WFCV",
                                            log_callback=logs.append,
                                        )

        self.assertTrue(ok)
        self.assertGreaterEqual(clicks["n"], 2)
        self.assertGreaterEqual(reopens["n"], 1)
        joined = "\n".join(logs)
        self.assertIn("rate_limited", joined)
        self.assertIn("re-enter user_code", joined)
        self.assertIn("left user-code page", joined)


if __name__ == "__main__":
    unittest.main()
