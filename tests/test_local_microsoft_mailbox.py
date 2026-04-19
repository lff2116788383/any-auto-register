from __future__ import annotations

from core.base_mailbox import LocalMicrosoftMailbox, MailboxAccount



def test_local_microsoft_normalizes_legacy_graph_scope():
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
        graph_scope="https://graph.microsoft.com/Mail.Read offline_access openid profile",
    )

    assert mailbox.graph_scope == "Mail.Read offline_access openid profile"


def test_local_microsoft_uses_normalized_scope_for_token_refresh(monkeypatch):
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
        graph_scope="https://graph.microsoft.com/Mail.Read offline_access openid profile",
    )

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"access_token":"token"}'

        def json(self):
            return {"access_token": "token"}

    def fake_post(url, data=None, proxies=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data or {})
        captured["timeout"] = timeout
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    token = mailbox._refresh_graph_access_token(
        client_id="client-id",
        refresh_token="refresh-token",
        cache_key="demo@hotmail.com:client-id",
    )

    assert token == "token"
    assert captured["data"]["scope"] == "https://graph.microsoft.com/.default offline_access"


def test_local_microsoft_falls_back_to_delegated_graph_scope(monkeypatch):
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
        graph_scope="https://graph.microsoft.com/Mail.Read offline_access openid profile",
    )

    seen_scopes: list[str] = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.content = b"payload"
            self.text = str(payload)

        def json(self):
            return self._payload

    def fake_post(url, data=None, proxies=None, timeout=None):
        scope = str((data or {}).get("scope") or "")
        seen_scopes.append(scope)
        if scope == "https://graph.microsoft.com/.default offline_access":
            return FakeResponse(400, {"error": "invalid_scope", "error_description": "AADSTS70000 invalid_scope"})
        return FakeResponse(200, {"access_token": "token-fallback"})

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    token = mailbox._refresh_graph_access_token(
        client_id="client-id",
        refresh_token="refresh-token",
        cache_key="demo@hotmail.com:client-id",
    )

    assert token == "token-fallback"
    assert seen_scopes == [
        "https://graph.microsoft.com/.default offline_access",
        "Mail.Read offline_access openid profile",
    ]



def test_local_microsoft_uses_imap_oauth_scope(monkeypatch):
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )
    account = MailboxAccount(
        email="demo@hotmail.com",
        account_id="demo",
        extra={
            "provider_account": {
                "login_identifier": "demo@hotmail.com",
                "credentials": {
                    "master_email": "demo@hotmail.com",
                    "client_id": "client-id",
                    "refresh_token": "refresh-token",
                },
            }
        },
    )

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"access_token":"imap-token"}'

        def json(self):
            return {"access_token": "imap-token"}

    class FakeImap:
        def __init__(self, *args, **kwargs):
            captured["imap_init"] = {"args": args, "kwargs": kwargs}
            captured["selected_folders"] = []

        def authenticate(self, mechanism, callback):
            captured["mechanism"] = mechanism
            captured["auth_payload"] = callback(None)
            return "OK", []

        def select(self, folder):
            captured["selected_folders"].append(folder)
            try:
                folder.encode("ascii")
            except UnicodeEncodeError as exc:
                raise AssertionError(f"folder should stay ascii-safe: {folder!r}") from exc
            if folder == "INBOX":
                return "OK", [b""]
            if folder == "Junk":
                return "OK", [b""]
            if folder == '"&V4NXPpCuTvY-"':
                return "OK", [b""]
            return "NO", [b""]

        def search(self, charset, query):
            return "OK", [b""]

        def logout(self):
            captured["logout"] = True

    def fake_post(url, data=None, proxies=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data or {})
        return FakeResponse()

    import imaplib
    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)

    messages = mailbox._list_imap_messages(account)

    assert messages == []
    assert captured["data"]["scope"] == "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    assert captured["mechanism"] == "XOAUTH2"
    assert captured["auth_payload"] == b"user=demo@hotmail.com\x01auth=Bearer imap-token\x01\x01"
    assert captured["selected_folders"][:2] == ["INBOX", "Junk"]
    assert '"&V4NXPpCuTvY-"' in captured["selected_folders"]


def test_local_microsoft_sanitizes_imap_login_before_encoding(monkeypatch):
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )
    account = MailboxAccount(
        email="demo@hotmail.com",
        account_id="demo",
        extra={
            "provider_account": {
                "login_identifier": "垃圾邮件 demo@hotmail.com",
                "credentials": {
                    "master_email": "demo@hotmail.com",
                    "client_id": "client-id",
                    "refresh_token": "refresh-token",
                },
            }
        },
    )

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"access_token":"imap-token"}'

        def json(self):
            return {"access_token": "imap-token"}

    class FakeImap:
        def authenticate(self, mechanism, callback):
            captured["payload"] = callback(None)
            return "OK", []

        def select(self, folder):
            if folder == "INBOX":
                return "OK", [b""]
            return "NO", [b""]

        def search(self, charset, query):
            return "OK", [b""]

        def logout(self):
            return None

    def fake_post(url, data=None, proxies=None, timeout=None):
        return FakeResponse()

    import imaplib
    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeImap())

    mailbox._list_imap_messages(account)

    assert captured["payload"] == b"user=demo@hotmail.com\x01auth=Bearer imap-token\x01\x01"



def test_local_microsoft_marks_mailbox_dead_on_service_abuse(monkeypatch):
    mailbox = LocalMicrosoftMailbox(
        master_email="demo@hotmail.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )
    account = MailboxAccount(
        email="demo@hotmail.com",
        account_id="demo",
        extra={
            "provider_account": {
                "login_identifier": "demo@hotmail.com",
                "credentials": {
                    "master_email": "demo@hotmail.com",
                    "client_id": "client-id",
                    "refresh_token": "refresh-token",
                    "mailbox_id": 7,
                },
            }
        },
    )

    captured: list[dict[str, object]] = []

    def fake_mark_runtime(*args, **kwargs):
        captured.append(dict(kwargs))
        return None

    def fake_list_messages(_account):
        raise RuntimeError("AADSTS70000: User account is found to be in service abuse mode.")

    monkeypatch.setattr(mailbox, "_mark_runtime", fake_mark_runtime)
    monkeypatch.setattr(mailbox, "_list_messages", fake_list_messages)

    try:
        mailbox.wait_for_code(account, timeout=1)
    except RuntimeError as exc:
        assert "service abuse mode" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert captured[0]["status"] == "dead"
    assert captured[0]["sub_status"] == "service_abuse_mode"
    assert captured[0]["release_lease"] is True
    assert len(captured) == 1





