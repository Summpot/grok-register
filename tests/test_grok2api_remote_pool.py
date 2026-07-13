import json
import unittest
from unittest.mock import patch

from grok_register import app


class DummyResponse:
    def __init__(self, payload=None, status_code=200, reason="", text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.reason = reason
        if text:
            self.text = text
        else:
            self.text = (
                json.dumps(self._payload, ensure_ascii=False)
                if isinstance(self._payload, (dict, list))
                else str(self._payload or "")
            )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}: {self.reason}")

    def json(self):
        return self._payload


class Grok2ApiRemotePoolTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        app._grok2api_v3_token_cache = {}

    def tearDown(self):
        app.config = self.original_config
        app._grok2api_v3_token_cache = {}

    def test_remote_pool_falls_back_to_admin_api_prefix_when_root_tokens_add_is_404(self):
        app.config.update({
            "grok2api_remote_mode": "legacy",
            "grok2api_remote_base": "https://grok.example.com",
            "grok2api_remote_app_key": "app-secret",
            "grok2api_pool_name": "ssoBasic",
        })
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://grok.example.com/tokens/add":
                return DummyResponse(status_code=404)
            return DummyResponse({"status": "success", "count": 1})

        with patch.object(app, "http_post", side_effect=fake_post):
            ok = app.add_token_to_grok2api_remote_pool("sso=abc123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in calls], [
            "https://grok.example.com/tokens/add",
            "https://grok.example.com/admin/api/tokens/add",
        ])
        self.assertEqual(calls[-1][1]["params"], {"app_key": "app-secret"})
        self.assertEqual(calls[-1][1]["json"], {
            "tokens": ["abc123"],
            "pool": "basic",
            "tags": ["auto-register"],
        })

    def test_remote_pool_does_not_duplicate_admin_api_prefix_when_base_already_points_to_admin_api(self):
        app.config.update({
            "grok2api_remote_mode": "legacy",
            "grok2api_remote_base": "https://grok.example.com/admin/api",
            "grok2api_remote_app_key": "app-secret",
            "grok2api_pool_name": "ssoSuper",
        })
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return DummyResponse({"status": "success", "count": 1})

        with patch.object(app, "http_post", side_effect=fake_post):
            ok = app.add_token_to_grok2api_remote_pool("sso=super123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in calls], [
            "https://grok.example.com/admin/api/tokens/add",
        ])
        self.assertEqual(calls[0][1]["json"]["pool"], "super")

    def test_remote_pool_full_save_fallback_tries_admin_api_tokens_path(self):
        app.config.update({
            "grok2api_remote_mode": "legacy",
            "grok2api_remote_base": "https://grok.example.com",
            "grok2api_remote_app_key": "app-secret",
            "grok2api_pool_name": "ssoBasic",
        })
        get_calls = []
        post_calls = []

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            if url.endswith("/tokens/add"):
                return DummyResponse(status_code=404)
            if url == "https://grok.example.com/admin/api/tokens":
                return DummyResponse({"status": "success"})
            return DummyResponse(status_code=404)

        def fake_get(url, **kwargs):
            get_calls.append((url, kwargs))
            if url == "https://grok.example.com/admin/api/tokens":
                return DummyResponse({"tokens": {"ssoBasic": []}})
            return DummyResponse(status_code=404)

        with patch.object(app, "http_post", side_effect=fake_post), \
                patch.object(app, "http_get", side_effect=fake_get):
            ok = app.add_token_to_grok2api_remote_pool("sso=fallback123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in get_calls], [
            "https://grok.example.com/tokens",
            "https://grok.example.com/admin/api/tokens",
        ])
        self.assertEqual(post_calls[-1][0], "https://grok.example.com/admin/api/tokens")
        self.assertEqual(post_calls[-1][1]["json"], {
            "ssoBasic": [{"token": "fallback123", "tags": ["auto-register"], "note": "a@example.com"}],
        })

    def test_v3_root_normalization(self):
        self.assertEqual(app.get_grok2api_v3_root("http://h:5003/"), "http://h:5003")
        self.assertEqual(app.get_grok2api_v3_root("http://h:5003/api/admin/v1"), "http://h:5003")
        self.assertEqual(app.get_grok2api_v3_root("http://h:5003/api/admin"), "http://h:5003")

    def test_v3_web_import_uses_admin_login_and_multipart(self):
        app.config.update({
            "grok2api_remote_mode": "v3",
            "grok2api_remote_base": "http://grok.example.com:5003",
            "grok2api_remote_username": "admin",
            "grok2api_remote_password": "secret-pass",
            "grok2api_pool_name": "ssoBasic",
            "grok2api_v3_web_tier": "auto",
        })
        login_calls = []
        import_calls = []

        def fake_http_post(url, **kwargs):
            login_calls.append((url, kwargs))
            if url.endswith("/api/admin/v1/auth/login"):
                return DummyResponse({
                    "data": {
                        "tokens": {
                            "accessToken": "jwt-access-token",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                })
            return DummyResponse(status_code=404, reason="not found")

        def fake_std_post(url, **kwargs):
            import_calls.append((url, kwargs))
            if url.endswith("/api/admin/v1/accounts/web/import"):
                return DummyResponse(
                    payload={},
                    text="event: complete\ndata: {\"created\":1,\"updated\":0,\"synced\":1,\"syncFailed\":0}\n\n",
                )
            return DummyResponse(status_code=404, reason="not found")

        with patch.object(app, "http_post", side_effect=fake_http_post), \
                patch.object(app, "std_requests") as std_mod:
            std_mod.post.side_effect = fake_std_post
            ok = app.add_token_to_grok2api_remote_pool("sso=eyJtesttoken", email="user@example.com")

        self.assertTrue(ok)
        self.assertEqual(login_calls[0][0], "http://grok.example.com:5003/api/admin/v1/auth/login")
        self.assertEqual(login_calls[0][1]["json"], {"username": "admin", "password": "secret-pass"})
        self.assertEqual(import_calls[0][0], "http://grok.example.com:5003/api/admin/v1/accounts/web/import")
        headers = import_calls[0][1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer jwt-access-token")
        files = import_calls[0][1]["files"]
        self.assertIn("file", files)
        filename, content, content_type = files["file"]
        self.assertTrue(filename.endswith(".json"))
        self.assertEqual(content_type, "application/json")
        doc = json.loads(content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else content)
        self.assertEqual(doc["provider"], "grok_web")
        self.assertEqual(doc["accounts"][0]["sso_token"], "eyJtesttoken")
        self.assertEqual(doc["accounts"][0]["name"], "user@example.com")
        self.assertEqual(doc["accounts"][0]["tier"], "basic")

    def test_auto_mode_falls_back_to_legacy_when_v3_fails(self):
        app.config.update({
            "grok2api_remote_mode": "auto",
            "grok2api_remote_base": "https://grok.example.com",
            "grok2api_remote_username": "admin",
            "grok2api_remote_password": "pw",
            "grok2api_remote_app_key": "app-secret",
            "grok2api_pool_name": "ssoBasic",
        })
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if "/api/admin/v1/auth/login" in url:
                return DummyResponse(status_code=401, reason="unauthorized")
            if url.endswith("/tokens/add"):
                return DummyResponse({"status": "ok"})
            return DummyResponse(status_code=404)

        with patch.object(app, "http_post", side_effect=fake_post):
            ok = app.add_token_to_grok2api_remote_pool("sso=legacytoken", email="b@example.com")

        self.assertTrue(ok)
        self.assertTrue(any("/api/admin/v1/auth/login" in u for u in calls))
        self.assertTrue(any(u.endswith("/tokens/add") for u in calls))

    def test_cpa_xai_auth_to_v3_build_entry(self):
        entry = app.cpa_xai_auth_to_v3_build_entry({
            "email": "a@example.com",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "sub": "user-1",
            "expired": "2026-07-13T05:21:32Z",
            "expires_in": 21600,
            "id_token": "id-1",
            "token_type": "Bearer",
        })
        self.assertEqual(entry["provider"], "grok_build")
        self.assertEqual(entry["email"], "a@example.com")
        self.assertEqual(entry["access_token"], "at-1")
        self.assertEqual(entry["refresh_token"], "rt-1")
        self.assertEqual(entry["user_id"], "user-1")
        self.assertEqual(entry["expires_at"], "2026-07-13T05:21:32Z")
        self.assertEqual(entry["id_token"], "id-1")

    def test_v3_build_import_uses_accounts_import_endpoint(self):
        app.config.update({
            "grok2api_remote_mode": "v3",
            "grok2api_remote_base": "http://grok.example.com:5003",
            "grok2api_remote_username": "admin",
            "grok2api_remote_password": "secret-pass",
            "grok2api_auto_add_build": True,
        })
        login_calls = []
        import_calls = []

        def fake_http_post(url, **kwargs):
            login_calls.append((url, kwargs))
            if url.endswith("/api/admin/v1/auth/login"):
                return DummyResponse({
                    "data": {"tokens": {"accessToken": "jwt-access-token"}}
                })
            return DummyResponse(status_code=404)

        def fake_std_post(url, **kwargs):
            import_calls.append((url, kwargs))
            if url.endswith("/api/admin/v1/accounts/import"):
                return DummyResponse(
                    payload={},
                    text="event: complete\ndata: {\"created\":1,\"updated\":0,\"synced\":1,\"syncFailed\":0}\n\n",
                )
            return DummyResponse(status_code=404)

        auth = {
            "email": "b@example.com",
            "access_token": "at-build",
            "refresh_token": "rt-build",
            "sub": "sub-b",
            "expired": "2026-07-13T05:21:32Z",
        }
        with patch.object(app, "http_post", side_effect=fake_http_post), \
                patch.object(app, "std_requests") as std_mod:
            std_mod.post.side_effect = fake_std_post
            ok = app.add_cpa_auth_to_grok2api_v3_build(auth)

        self.assertTrue(ok)
        self.assertTrue(login_calls[0][0].endswith("/api/admin/v1/auth/login"))
        self.assertEqual(import_calls[0][0], "http://grok.example.com:5003/api/admin/v1/accounts/import")
        files = import_calls[0][1]["files"]
        _name, content, _ct = files["file"]
        doc = json.loads(content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else content)
        self.assertEqual(doc["accounts"][0]["provider"], "grok_build")
        self.assertEqual(doc["accounts"][0]["access_token"], "at-build")
        self.assertEqual(doc["accounts"][0]["refresh_token"], "rt-build")


if __name__ == "__main__":
    unittest.main()
