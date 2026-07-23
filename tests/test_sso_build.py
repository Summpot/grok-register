import hashlib
import json
import threading
import time
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

from grok_register.sso_build import (
    SSO_BUILD_CLIENT_ID,
    SSO_BUILD_REFERRER,
    SSO_BUILD_SCOPE,
    OAuthRegisterSession,
    SSOBuildError,
    SSOBuildFlow,
    _LoopbackCallbackServer,
    access_token_has_bot_flag,
    build_authorize_url,
    build_grok2api_import_document,
    convert_sso_to_build,
    generate_pkce,
    normalize_sso_token,
    parse_callback_query,
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
        self.assertTrue(safe_xai_url("https://auth.x.ai/oauth2/authorize"))
        self.assertTrue(safe_xai_url("https://auth.x.ai/oauth2/token"))
        self.assertTrue(safe_xai_url("http://127.0.0.1:56121/callback"))
        self.assertFalse(safe_xai_url("https://evil.com/"))

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

    def test_generate_pkce_s256(self):
        verifier, challenge = generate_pkce()
        import base64

        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        self.assertEqual(challenge, expected)

    def test_build_authorize_url_contract(self):
        url = build_authorize_url(
            "https://auth.x.ai/oauth2/authorize",
            client_id=SSO_BUILD_CLIENT_ID,
            redirect_uri="http://127.0.0.1:12345/callback",
            scope=SSO_BUILD_SCOPE,
            code_challenge="chal",
            state="st",
            nonce="nn",
            referrer=SSO_BUILD_REFERRER,
        )
        self.assertIn("response_type=code", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("referrer=grok-build", url)
        self.assertIn("127.0.0.1", url)

    def test_parse_callback_query(self):
        code, state, err = parse_callback_query("code=abc&state=xyz")
        self.assertEqual(code, "abc")
        self.assertEqual(state, "xyz")
        self.assertEqual(err, "")

    def test_access_token_has_bot_flag(self):
        self.assertTrue(
            access_token_has_bot_flag(_make_jwt({"bot_flag_source": 1, "sub": "x"}))
        )
        self.assertFalse(access_token_has_bot_flag(_make_jwt({"sub": "x"})))

    def test_build_documents(self):
        seed = {
            "name": "a@example.com",
            "email": "a@example.com",
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "expires_at": "2026-07-18T00:00:00Z",
        }
        doc = build_grok2api_import_document(seed)
        self.assertEqual(doc["accounts"][0]["provider"], "grok_build")
        self.assertEqual(doc["accounts"][0]["access_token"], "at")


class LoopbackServerTests(unittest.TestCase):
    def test_loopback_receives_code(self):
        server = _LoopbackCallbackServer(port=0)
        server.start()
        try:
            url = f"{server.redirect_uri}?code=auth-code-1&state=st-1"

            def hit():
                time.sleep(0.05)
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)

            t = threading.Thread(target=hit, daemon=True)
            t.start()
            payload = server.wait(timeout=5)
            t.join(timeout=5)
            self.assertEqual(payload["code"], "auth-code-1")
            self.assertEqual(payload["state"], "st-1")
        finally:
            server.close()

    def test_loopback_cors_preflight(self):
        server = _LoopbackCallbackServer(port=0)
        server.start()
        try:
            req = urllib.request.Request(
                server.redirect_uri,
                method="OPTIONS",
                headers={
                    "Origin": "https://accounts.x.ai",
                    "Access-Control-Request-Method": "GET",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertIn(resp.status, (200, 204))
                self.assertEqual(
                    resp.headers.get("Access-Control-Allow-Origin"),
                    "https://accounts.x.ai",
                )
        finally:
            server.close()


class OAuthRegisterSessionTests(unittest.TestCase):
    def test_begin_returns_authorize_url_as_signup_entry(self):
        """Primary path: generate OAuth link first (registration entry)."""
        session = OAuthRegisterSession(callback_timeout=5.0)
        try:
            # begin without page → stdlib discovery fallback
            url = session.begin(page=None)
            self.assertTrue(session.started)
            self.assertIn("https://auth.x.ai/oauth2/authorize", url)
            self.assertIn("response_type=code", url)
            self.assertIn("code_challenge_method=S256", url)
            self.assertIn("referrer=grok-build", url)
            self.assertIn("127.0.0.1", url)
            self.assertIn("/callback", session.redirect_uri)
            self.assertEqual(url, session.authorize_url)
        finally:
            session.close()

    def test_finish_after_register_exchanges_code(self):
        """After signup inside authorize session: consent/code → tokens."""
        token_body = json.dumps(
            {
                "access_token": "access-xyz",
                "refresh_token": "refresh-xyz",
                "id_token": "id-xyz",
                "expires_in": 3600,
            }
        ).encode()

        session = OAuthRegisterSession(callback_timeout=8.0)
        call_plan = []

        def fake_request(method, url, headers=None, data=None):
            call_plan.append((method, url, data, dict(headers or {})))
            if "/oauth2/token" in url:
                return DummyResponse(200, token_body)
            return DummyResponse(404, b"missing")

        page = MagicMock()
        page.url = "https://accounts.x.ai/sign-up"
        page.run_js = MagicMock(return_value="unknown")

        try:
            auth_url = session.begin(page=None)
            self.assertIn("authorize", auth_url)

            def fake_await(page, log_callback=None):
                return {"code": "auth-code-xyz", "state": session._state, "error": ""}

            with patch.object(session, "_request", side_effect=fake_request):
                with patch.object(session, "_await_code", side_effect=fake_await):
                    seed = session.finish(
                        page, email="user@example.com", already_registered=True
                    )

            self.assertEqual(seed["access_token"], "access-xyz")
            self.assertEqual(seed["refresh_token"], "refresh-xyz")
            self.assertEqual(seed["email"], "user@example.com")
            self.assertEqual(seed["provider"], "grok_build")
            token_calls = [c for c in call_plan if "/oauth2/token" in c[1]]
            self.assertTrue(token_calls)
            token_data = token_calls[0][2] or ""
            self.assertIn("grant_type=authorization_code", token_data)
            self.assertIn("code=auth-code-xyz", token_data)
            self.assertIn("code_verifier=", token_data)
            self.assertNotIn("Cookie", token_calls[0][3])
        finally:
            session.close()

    def test_finish_requires_begin(self):
        session = OAuthRegisterSession()
        page = MagicMock()
        with self.assertRaises(SSOBuildError) as ctx:
            session.finish(page)
        self.assertIn("not started", str(ctx.exception))

    def test_state_mismatch_rejected(self):
        session = OAuthRegisterSession()
        with self.assertRaises(SSOBuildError) as ctx:
            session._validate_state(
                {"code": "c", "state": "wrong", "error": ""},
                "expected",
            )
        self.assertIn("state mismatch", str(ctx.exception))

    def test_code_from_page_url(self):
        session = OAuthRegisterSession()
        page = MagicMock()
        page.url = "http://127.0.0.1:55555/callback?code=from-redirect&state=s1"
        payload = session._code_from_page_url(page)
        self.assertEqual(payload["code"], "from-redirect")

    def test_sso_build_flow_alias(self):
        self.assertIs(SSOBuildFlow, OAuthRegisterSession)

    def test_convert_sso_to_build_requires_page(self):
        with self.assertRaises(SSOBuildError) as ctx:
            convert_sso_to_build("sso-token", page=None, mode="http")
        self.assertIn("requires an active page", str(ctx.exception))

    def test_convert_sso_legacy_seeds_cookies_and_finishes(self):
        page = MagicMock()
        page.url = "https://auth.x.ai/oauth2/authorize"
        page.get = MagicMock()
        page.set = MagicMock()

        token_body = json.dumps(
            {
                "access_token": "at-legacy",
                "refresh_token": "rt-legacy",
                "id_token": "id",
                "expires_in": 100,
            }
        ).encode()

        def fake_request(method, url, headers=None, data=None):
            if "/oauth2/token" in url:
                return DummyResponse(200, token_body)
            if "openid-configuration" in url:
                return DummyResponse(
                    200,
                    json.dumps(
                        {
                            "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
                            "token_endpoint": "https://auth.x.ai/oauth2/token",
                        }
                    ).encode(),
                )
            return DummyResponse(404, b"")

        with patch.object(OAuthRegisterSession, "_request", side_effect=fake_request):
            with patch.object(
                OAuthRegisterSession,
                "_await_code",
                return_value={"code": "c1", "state": "will-be-overwritten", "error": ""},
            ) as await_mock:
                # Make await return matching state from the live session.
                def await_side(page, log_callback=None):
                    sess = await_mock.sessoin if False else None
                    # Grab state from the session that called us via bound method — re-patch.
                    return {"code": "c1", "state": "", "error": ""}

                await_mock.side_effect = await_side
                seed = convert_sso_to_build(
                    "sso-token-value",
                    email="legacy@example.com",
                    page=page,
                )
        self.assertEqual(seed["access_token"], "at-legacy")
        self.assertEqual(seed["email"], "legacy@example.com")
        page.get.assert_called_once()
        self.assertIn("authorize", page.get.call_args[0][0])

    def test_save_build_auth(self):
        import tempfile

        seed = {
            "email": "x@y.z",
            "access_token": "a",
            "refresh_token": "r",
            "expires_in": 100,
            "expires_at": "2026-07-18T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td:
            path = save_build_auth(seed, td, email="x@y.z")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["accounts"][0]["access_token"], "a")

    def test_await_code_clicks_allow_then_loopback(self):
        session = OAuthRegisterSession(callback_timeout=5.0)
        page = MagicMock()
        page.url = "https://accounts.x.ai/oauth2/consent"
        try:
            session.begin(page=None)
            expected_state = session._state

            def deliver():
                time.sleep(0.25)
                url = f"{session.redirect_uri}?code=after-allow&state={expected_state}"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    assert resp.status == 200

            t = threading.Thread(target=deliver, daemon=True)
            t.start()
            with patch.object(session, "_browser_click_allow", return_value=True) as allow:
                with patch.object(session, "_browser_page_phase", return_value="consent"):
                    with patch("grok_register.sso_build.time.sleep", return_value=None):
                        payload = session._await_code(page)
            t.join(timeout=5)
            self.assertEqual(payload["code"], "after-allow")
            allow.assert_called()
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
