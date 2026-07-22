import unittest
from unittest.mock import patch

from grok_register import app
from grok_register import cf_mail_debug


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class CloudflareAdminCreateTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        self.original_cf_domain_index = app._cf_domain_index
        app._cf_domain_index = 0

    def tearDown(self):
        app.config = self.original_config
        app._cf_domain_index = self.original_cf_domain_index

    def test_default_config_keeps_cloudflare_temp_email_new_address(self):
        app.config = app.DEFAULT_CONFIG.copy()
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "anon@example.com", "jwt": "default-jwt"})

        with patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.example.com")

        self.assertEqual(address, "anon@example.com")
        self.assertEqual(jwt, "default-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.example.com/api/new_address")
        self.assertEqual(captured["json"], {})
        self.assertEqual(captured["headers"], {"Content-Type": "application/json"})

    def test_app_uses_admin_new_address_with_x_admin_auth(self):
        app.config.update({
            "cloudflare_api_key": "admin-secret",
            "cloudflare_auth_mode": "x-admin-auth",
            "cloudflare_path_accounts": "/admin/new_address",
            "defaultDomains": "vitassk.com",
            "enable_random_subdomain": False,
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "adminuser@vitassk.com", "jwt": "address-jwt"})

        with patch.object(app, "generate_username", return_value="adminuser"), \
                patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "adminuser@vitassk.com")
        self.assertEqual(jwt, "address-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/admin/new_address")
        self.assertEqual(captured["json"], {
            "name": "adminuser",
            "domain": "vitassk.com",
            "enablePrefix": True,
        })
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["x-admin-auth"], "admin-secret")

    def test_app_passes_enable_random_subdomain_on_admin_create(self):
        app.config.update({
            "cloudflare_api_key": "admin-secret",
            "cloudflare_auth_mode": "x-admin-auth",
            "cloudflare_path_accounts": "/admin/new_address",
            "defaultDomains": "vitassk.com",
            "enable_random_subdomain": True,
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({
                "address": "adminuser@x7k2p9q1.vitassk.com",
                "jwt": "sub-jwt",
            })

        with patch.object(app, "generate_username", return_value="adminuser"), \
                patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "adminuser@x7k2p9q1.vitassk.com")
        self.assertEqual(jwt, "sub-jwt")
        self.assertEqual(captured["json"], {
            "name": "adminuser",
            "domain": "vitassk.com",
            "enablePrefix": True,
            "enableRandomSubdomain": True,
        })

    def test_app_passes_enable_random_subdomain_on_anonymous_create(self):
        app.config.update({
            "cloudflare_api_key": "",
            "cloudflare_auth_mode": "none",
            "cloudflare_path_accounts": "/api/new_address",
            "defaultDomains": "vitassk.com",
            "enable_random_subdomain": True,
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({
                "address": "anon@abcd1234.vitassk.com",
                "jwt": "anon-sub-jwt",
            })

        with patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "anon@abcd1234.vitassk.com")
        self.assertEqual(jwt, "anon-sub-jwt")
        self.assertEqual(captured["json"], {
            "domain": "vitassk.com",
            "enableRandomSubdomain": True,
        })

    def test_app_keeps_anonymous_new_address_with_none_auth(self):
        app.config.update({
            "cloudflare_api_key": "",
            "cloudflare_auth_mode": "none",
            "cloudflare_path_accounts": "/api/new_address",
            "defaultDomains": "vitassk.com",
            "enable_random_subdomain": False,
        })
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "anon@vitassk.com", "jwt": "anon-jwt"})

        with patch.object(app, "http_post", side_effect=fake_post):
            address, jwt = app.cloudflare_create_temp_address("https://temp-mail.ikun.day")

        self.assertEqual(address, "anon@vitassk.com")
        self.assertEqual(jwt, "anon-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/api/new_address")
        self.assertEqual(captured["json"], {"domain": "vitassk.com"})
        self.assertEqual(captured["headers"], {"Content-Type": "application/json"})

    def test_debug_tool_can_create_address_through_admin_api(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"address": "debuguser@vitassk.com", "jwt": "debug-jwt"})

        with patch.object(cf_mail_debug.requests, "post", side_effect=fake_post):
            address, jwt = cf_mail_debug.create_address(
                "https://temp-mail.ikun.day",
                auth_mode="x-admin-auth",
                api_key="admin-secret",
                create_path="/admin/new_address",
                domain="vitassk.com",
                name="debuguser",
            )

        self.assertEqual(address, "debuguser@vitassk.com")
        self.assertEqual(jwt, "debug-jwt")
        self.assertEqual(captured["url"], "https://temp-mail.ikun.day/admin/new_address")
        self.assertEqual(captured["json"], {
            "name": "debuguser",
            "domain": "vitassk.com",
            "enablePrefix": True,
        })
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["x-admin-auth"], "admin-secret")

    def test_debug_tool_passes_enable_random_subdomain(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({
                "address": "debuguser@zz99aa11.vitassk.com",
                "jwt": "debug-sub-jwt",
            })

        with patch.object(cf_mail_debug.requests, "post", side_effect=fake_post):
            address, jwt = cf_mail_debug.create_address(
                "https://temp-mail.ikun.day",
                auth_mode="x-admin-auth",
                api_key="admin-secret",
                create_path="/admin/new_address",
                domain="vitassk.com",
                name="debuguser",
                enable_random_subdomain=True,
            )

        self.assertEqual(address, "debuguser@zz99aa11.vitassk.com")
        self.assertEqual(jwt, "debug-sub-jwt")
        self.assertEqual(captured["json"], {
            "name": "debuguser",
            "domain": "vitassk.com",
            "enablePrefix": True,
            "enableRandomSubdomain": True,
        })


if __name__ == "__main__":
    unittest.main()
