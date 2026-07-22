"""Tests for allow_bot_flagged config on Device Flow / pool routing."""

import json
import unittest
from unittest.mock import MagicMock, patch

from grok_register import app


def _make_jwt(claims: dict) -> str:
    import base64

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}.sig"


def _bot_seed(email="bot@example.com"):
    return {
        "email": email,
        "user_id": "uid-bot",
        "access_token": _make_jwt({"bot_flag_source": 1, "sub": "uid-bot"}),
        "refresh_token": "rt-bot",
        "id_token": "idt-bot",
        "expires_in": 3600,
        "expires_at": "2026-07-18T00:00:00Z",
        "provider": "grok_build",
    }


class AllowBotFlaggedTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()

    def tearDown(self):
        app.config = self.original_config

    def test_convert_rejects_bot_by_default(self):
        app.config.update(
            {
                "local_build_device_flow": True,
                "allow_bot_flagged": False,
                "local_build_auth_dir": "",
                "grok2api_auto_add_remote": False,
                "grok2api_auto_add_build": False,
            }
        )
        seed = _bot_seed()
        logs = []
        page = MagicMock()
        with patch("grok_register.sso_build.convert_sso_to_build", return_value=dict(seed)):
            with patch("grok_register.sso_build.save_build_auth") as save_mock:
                with patch.object(app, "add_build_credential_to_grok2api_remote") as up_mock:
                    out = app.convert_sso_to_build_local(
                        "sso-token",
                        email="bot@example.com",
                        page=page,
                        log_callback=logs.append,
                    )
        self.assertIsNotNone(out)
        self.assertTrue(out.get("_bot_flagged"))
        save_mock.assert_not_called()
        up_mock.assert_not_called()

    def test_convert_allows_bot_when_configured(self):
        app.config.update(
            {
                "local_build_device_flow": True,
                "allow_bot_flagged": True,
                "local_build_auth_dir": "./output/build_auths",
                "grok2api_auto_add_remote": True,
                "grok2api_auto_add_build": True,
            }
        )
        seed = _bot_seed()
        logs = []
        page = MagicMock()
        with patch("grok_register.sso_build.convert_sso_to_build", return_value=dict(seed)):
            with patch("grok_register.sso_build.save_build_auth", return_value="fake.json") as save_mock:
                with patch.object(
                    app, "add_build_credential_to_grok2api_remote", return_value=True
                ) as up_mock:
                    out = app.convert_sso_to_build_local(
                        "sso-token",
                        email="bot@example.com",
                        page=page,
                        log_callback=logs.append,
                    )
        self.assertIsNotNone(out)
        self.assertTrue(out.get("_bot_flagged"))
        self.assertTrue(out.get("_remote_build_imported"))
        save_mock.assert_called_once()
        up_mock.assert_called_once()
        self.assertTrue(any("allow_bot_flagged=true" in m for m in logs))

    def test_apply_post_register_pools_rejects_bot_by_default(self):
        app.config["allow_bot_flagged"] = False
        bot_seed = dict(_bot_seed())
        bot_seed["_bot_flagged"] = True
        with patch.object(app, "convert_sso_to_build_local", return_value=bot_seed):
            with patch.object(app, "add_token_to_grok2api_pools") as web_mock:
                result = app.apply_post_register_pools("sso", email="bot@example.com")
        self.assertFalse(result["ok"])
        self.assertTrue(result["bot_flagged"])
        self.assertTrue(result["skipped_web"])
        web_mock.assert_not_called()

    def test_apply_post_register_pools_allows_bot_when_configured(self):
        app.config["allow_bot_flagged"] = True
        bot_seed = dict(_bot_seed())
        bot_seed["_bot_flagged"] = True
        bot_seed["_remote_build_imported"] = True
        with patch.object(app, "convert_sso_to_build_local", return_value=bot_seed):
            with patch.object(app, "add_token_to_grok2api_pools") as web_mock:
                result = app.apply_post_register_pools("sso", email="bot@example.com")
        self.assertTrue(result["ok"])
        self.assertTrue(result["bot_flagged"])
        self.assertTrue(result["skipped_web"])
        web_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
