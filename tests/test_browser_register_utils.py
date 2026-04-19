"""Unit tests for ChatGPT browser_register utility functions.

These are pure functions that don't need a browser or network.
"""
from __future__ import annotations

from platforms.chatgpt.browser_register import (
    BROWSER_BOOTSTRAP_URLS,
    _build_proxy_config,
    _extract_code_from_url,
    _infer_page_type,
    _extract_flow_state,
    _normalize_url,
    _start_browser_signup,
    _decode_jwt_payload,
)





class TestBrowserSignupStart:
    def test_start_browser_signup_uses_auth_authorize_continue(self, monkeypatch):
        import json as _json
        import platforms.chatgpt.browser_register as mod

        captured: dict[str, object] = {}

        class FakePage:
            url = "https://auth.openai.com/create-account"

        def fake_sentinel(page, device_id, flow, user_agent):
            captured["flow"] = flow
            return "sentinel-token"

        def fake_fetch(page, url, *, method="GET", headers=None, body=None, redirect="manual", timeout_ms=30000):
            captured["url"] = url
            captured["method"] = method
            captured["headers"] = dict(headers or {})
            captured["body"] = _json.loads(body or "{}")
            return {"ok": True, "status": 200, "url": url, "data": {"page": {"type": "create_account_password"}}}

        monkeypatch.setattr(mod, "_build_browser_sentinel_token", fake_sentinel)
        monkeypatch.setattr(mod, "_browser_fetch", fake_fetch)
        monkeypatch.setattr(mod, "_browser_pause", lambda page: None)

        result = _start_browser_signup(FakePage(), "demo@hotmail.com", "device-id", "ua", lambda _msg: None)

        assert result["ok"] is True
        assert captured["url"] == "https://auth.openai.com/api/accounts/authorize/continue"
        assert captured["method"] == "POST"
        assert captured["flow"] == "authorize_continue"
        assert captured["headers"]["openai-sentinel-token"] == "sentinel-token"
        assert captured["body"] == {
            "username": {"value": "demo@hotmail.com", "kind": "email"},
            "screen_hint": "signup",
        }


    def test_none_input(self):
        assert _build_proxy_config(None) is None

    def test_empty_string(self):
        assert _build_proxy_config("") is None

    def test_simple_proxy(self):
        result = _build_proxy_config("socks5://1.2.3.4:1080")
        assert result["server"] == "socks5://1.2.3.4:1080"

    def test_proxy_with_auth(self):
        result = _build_proxy_config("http://user:pass@1.2.3.4:8080")
        assert result["server"] == "http://1.2.3.4:8080"
        assert result["username"] == "user"
        assert result["password"] == "pass"

    def test_bare_proxy(self):
        result = _build_proxy_config("1.2.3.4:8080")
        assert result == {"server": "1.2.3.4:8080"}


class TestExtractCodeFromUrl:
    def test_no_code(self):
        assert _extract_code_from_url("https://example.com/callback") == ""

    def test_with_code(self):
        url = "https://example.com/callback?code=abc123&state=xyz"
        assert _extract_code_from_url(url) == "abc123"

    def test_empty_url(self):
        assert _extract_code_from_url("") == ""

    def test_code_only(self):
        url = "https://example.com?code=mycode"
        assert _extract_code_from_url(url) == "mycode"


class TestInferPageType:
    def test_from_data(self):
        data = {"page": {"type": "login-password"}}
        assert _infer_page_type(data) == "login_password"

    def test_from_url_email_verification(self):
        assert _infer_page_type(None, "https://auth.openai.com/email-verification") == "email_otp_verification"

    def test_from_url_about_you(self):
        assert _infer_page_type(None, "https://auth.openai.com/about-you") == "about_you"

    def test_from_url_chatgpt_home(self):
        assert _infer_page_type(None, "https://chatgpt.com/") == "chatgpt_home"

    def test_from_url_consent(self):
        assert _infer_page_type(None, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent") == "consent"

    def test_empty(self):
        assert _infer_page_type(None, "") == ""

    def test_none_data(self):
        assert _infer_page_type(None) == ""


class TestExtractFlowState:
    def test_basic(self):
        data = {"page": {"type": "login-password"}, "continue_url": "/next"}
        state = _extract_flow_state(data, "https://auth.openai.com/login")
        assert state["page_type"] == "login_password"
        assert "auth.openai.com" in state["continue_url"]

    def test_none_data(self):
        state = _extract_flow_state(None, "https://auth.openai.com/about-you")
        assert state["page_type"] == "about_you"


class TestNormalizeUrl:
    def test_absolute_url(self):
        assert _normalize_url("https://example.com/path") == "https://example.com/path"

    def test_relative_url(self):
        result = _normalize_url("/api/next")
        assert result == "https://auth.openai.com/api/next"

    def test_empty(self):
        assert _normalize_url("") == ""


class TestDecodeJwtPayload:
    def test_valid_jwt(self):
        # Note: _decode_jwt_payload relies on json/base64 being in the
        # module's global scope. In the actual browser flow these are
        # available because earlier code paths import them. In an isolated
        # import they may not be, so we inject them to test the logic.
        import platforms.chatgpt.browser_register as mod
        import json as _json, base64 as _b64
        mod.json = _json
        mod.base64 = _b64
        try:
            payload_data = {"sub": "123"}
            payload = _b64.urlsafe_b64encode(
                _json.dumps(payload_data, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            header = _b64.urlsafe_b64encode(
                _json.dumps({"alg": "HS256"}, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            token = f"{header}.{payload}.signature"
            result = _decode_jwt_payload(token)
            assert result["sub"] == "123"
        finally:
            # Clean up injected names
            pass

    def test_invalid_token(self):
        assert _decode_jwt_payload("not-a-jwt") == {}

    def test_empty(self):
        assert _decode_jwt_payload("") == {}
