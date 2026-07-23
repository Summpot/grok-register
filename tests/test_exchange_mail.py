#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for Exchange catch-all temporary mailbox provider."""

import unittest
from unittest.mock import patch

from grok_register import app
from grok_register import exchange_mail as em


class DummyResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text or ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ExchangeHelpersTests(unittest.TestCase):
    def test_generate_mail_username_starts_with_letter(self):
        for _ in range(20):
            name = em.generate_mail_username(12, "tmp")
            self.assertTrue(name[0].isalpha())
            self.assertTrue(name.startswith("tmp"))
            self.assertLessEqual(len(name), 64)

    def test_message_matches_to_recipients(self):
        msg = {
            "toRecipients": [
                {"emailAddress": {"address": "tmpabc@contoso.onmicrosoft.com"}}
            ],
            "subject": "hi",
        }
        self.assertTrue(em.message_matches_address(msg, "tmpabc@contoso.onmicrosoft.com"))
        self.assertFalse(em.message_matches_address(msg, "other@contoso.onmicrosoft.com"))

    def test_message_matches_original_to_header(self):
        msg = {
            "toRecipients": [
                {"emailAddress": {"address": "catchall@contoso.onmicrosoft.com"}}
            ],
            "internetMessageHeaders": [
                {
                    "name": "X-Original-To",
                    "value": "tmpxyz@contoso.onmicrosoft.com",
                }
            ],
        }
        self.assertTrue(em.message_matches_address(msg, "tmpxyz@contoso.onmicrosoft.com"))


class ExchangeMailClientTests(unittest.TestCase):
    def setUp(self):
        em.clear_token_cache()
        self.calls = []

        def http_get(url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            return self._dispatch("GET", url, kwargs)

        def http_post(url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return self._dispatch("POST", url, kwargs)

        self.http_get = http_get
        self.http_post = http_post
        self.handlers = {}

    def _dispatch(self, method, url, kwargs):
        for key, handler in self.handlers.items():
            m, fragment = key
            if m == method and fragment in url:
                return handler(url, kwargs)
        return DummyResponse(
            {"error": {"message": f"unhandled {method} {url}"}}, status_code=500
        )

    def _client(self, **overrides):
        cfg = {
            "exchange_tenant_id": "tenant-1",
            "exchange_client_id": "client-1",
            "exchange_client_secret": "secret-1",
            "exchange_mailbox": "catchall@contoso.onmicrosoft.com",
            "exchange_domains": "contoso.onmicrosoft.com",
            "exchange_username_prefix": "tmp",
            "exchange_username_length": 10,
            "exchange_list_top": 25,
        }
        cfg.update(overrides)
        return em.ExchangeMailClient.from_config(
            cfg,
            http_get=self.http_get,
            http_post=self.http_post,
            sleep_fn=lambda _s, _c=None: None,
        )

    def test_from_config_falls_back_to_default_domains(self):
        client = em.ExchangeMailClient.from_config(
            {
                "exchange_tenant_id": "t",
                "exchange_client_id": "c",
                "exchange_client_secret": "s",
                "exchange_mailbox": "mb@x.com",
                "exchange_domains": "",
                "defaultDomains": "a.onmicrosoft.com,b.onmicrosoft.com",
            }
        )
        self.assertEqual(client.domains, ["a.onmicrosoft.com", "b.onmicrosoft.com"])
        self.assertEqual(client.mailbox, "mb@x.com")

    def test_validate_requires_mailbox(self):
        client = em.ExchangeMailClient("t", "c", "s", mailbox="", domains=["x.com"])
        with self.assertRaises(em.ExchangeMailError) as ctx:
            client.validate()
        self.assertIn("exchange_mailbox", str(ctx.exception))

    def test_create_temp_address_no_graph_user_create(self):
        client = self._client()
        with patch.object(em, "generate_mail_username", return_value="tmpabc123"):
            address, token = client.create_temp_address()
        self.assertEqual(address, "tmpabc123@contoso.onmicrosoft.com")
        self.assertEqual(token, "catchall@contoso.onmicrosoft.com")
        # Allocation is local-only — no Graph calls.
        self.assertEqual(self.calls, [])

    def test_get_oai_code_filters_by_recipient(self):
        self.handlers[("POST", "oauth2/v2.0/token")] = lambda u, k: DummyResponse(
            {"access_token": "tok-abc", "expires_in": 3600}
        )
        self.handlers[("GET", "/messages")] = lambda u, k: DummyResponse(
            {
                "value": [
                    {
                        "id": "noise-1",
                        "subject": "other",
                        "bodyPreview": "AAA-BBB",
                        "body": {"contentType": "text", "content": "AAA-BBB"},
                        "toRecipients": [
                            {"emailAddress": {"address": "someoneelse@contoso.onmicrosoft.com"}}
                        ],
                    },
                    {
                        "id": "hit-1",
                        "subject": "XYZ-123 xAI",
                        "bodyPreview": "code XYZ-123",
                        "body": {
                            "contentType": "text",
                            "content": "verification code XYZ-123",
                        },
                        "toRecipients": [
                            {"emailAddress": {"address": "tmp@contoso.onmicrosoft.com"}}
                        ],
                    },
                ]
            }
        )

        client = self._client()
        code = client.get_oai_code(
            "catchall@contoso.onmicrosoft.com",
            "tmp@contoso.onmicrosoft.com",
            timeout=30,
            poll_interval=0,
            extract_fn=lambda text, subject: app.extract_verification_code(text, subject),
        )
        self.assertEqual(code.upper(), "XYZ-123")
        get_urls = [c[1] for c in self.calls if c[0] == "GET"]
        self.assertTrue(any("catchall%40contoso.onmicrosoft.com" in u or "catchall@contoso" in u for u in get_urls))


class AppExchangeProviderTests(unittest.TestCase):
    def setUp(self):
        self.original = app.config.copy()
        em.clear_token_cache()

    def tearDown(self):
        app.config = self.original
        em.clear_token_cache()

    def test_get_email_provider_normalizes_case(self):
        app.config["email_provider"] = "Exchange"
        self.assertEqual(app.get_email_provider(), "exchange")

    def test_get_email_and_token_routes_to_exchange(self):
        app.config.update(
            {
                "email_provider": "exchange",
                "exchange_tenant_id": "t",
                "exchange_client_id": "c",
                "exchange_client_secret": "s",
                "exchange_mailbox": "catchall@contoso.onmicrosoft.com",
                "exchange_domains": "contoso.onmicrosoft.com",
            }
        )

        class FakeClient:
            def create_temp_address(self, **kwargs):
                return "tmp9@contoso.onmicrosoft.com", "catchall@contoso.onmicrosoft.com"

        with patch.object(app, "get_exchange_client", return_value=FakeClient()):
            address, token = app.get_email_and_token()
        self.assertEqual(address, "tmp9@contoso.onmicrosoft.com")
        self.assertEqual(token, "catchall@contoso.onmicrosoft.com")

    def test_get_oai_code_routes_to_exchange(self):
        app.config["email_provider"] = "exchange"
        with patch.object(app, "exchange_get_oai_code", return_value="ZZZ-111") as mocked:
            code = app.get_oai_code("mb@x.com", "a@b.onmicrosoft.com", timeout=5, poll_interval=1)
        self.assertEqual(code, "ZZZ-111")
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args[0][0], "mb@x.com")

    def test_cloudflare_still_default_in_example_path(self):
        app.config["email_provider"] = "cloudflare"
        with patch.object(app, "get_cloudflare_api_base", return_value="https://mail.example"), \
                patch.object(app, "cloudflare_create_temp_address", return_value=("a@b.com", "jwt")) as mocked:
            address, token = app.get_email_and_token()
        self.assertEqual((address, token), ("a@b.com", "jwt"))
        mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
