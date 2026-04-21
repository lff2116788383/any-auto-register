from types import SimpleNamespace
from unittest.mock import MagicMock

from platforms.kiro.core import KiroRegister
from platforms.kiro.protocol_mailbox import KiroProtocolMailboxWorker


class DummyClient:
    def __init__(self):
        self.tag = "KIRO"
        self.profile_wf_id = "wf-123"
        self._step11_state = None
        self._workflow_result_handle = None
        self._signup_signin_state = None
        self.logs = []
        self.step12_called = False
        self.step12f_called = False

    def log(self, message):
        self.logs.append(str(message))

    def step1_kiro_init(self):
        return "https://example.com/redirect"

    def step2_get_wsh(self, redir):
        return True

    def step3_signin_flow(self, email):
        return {"ok": True}

    def step4_signup_flow(self, email):
        return {"ok": True}

    def step5_get_tes_token(self):
        return "tes-token"

    def step6_profile_load(self):
        return True

    def step7_send_otp(self, email):
        return {"ok": True}

    def step8_create_identity(self, otp, email, name):
        return {
            "registrationCode": "reg-code",
            "signInState": "signup-signin-state",
        }

    def step9_signup_registration(self, reg_code, sign_in_state):
        return {"ok": True}

    def step10_set_password(self, password, email, signup_registration):
        return {"redirect": {"url": "https://example.com/final-login"}}

    def step11_final_login(self, email, password_state):
        return None

    def step12_get_tokens(self):
        self.step12_called = True
        return {
            "accessToken": "access-token",
            "sessionToken": "session-token",
        }

    def step12f_device_auth(self, bearer_token):
        self.step12f_called = True
        return None


class DummySuccessfulClient(DummyClient):
    def step11_final_login(self, email, password_state):
        self._step11_state = "step11-state"
        self._workflow_result_handle = "wrh-11"
        return {"stepId": "end-of-workflow-success"}


def test_kiro_protocol_worker_skips_step12_when_step11_fails():
    worker = KiroProtocolMailboxWorker(proxy=None, tag="KIRO", log_fn=lambda msg: None)
    worker.client = DummyClient()

    result = worker.run(
        email="user@example.com",
        password="Password123!",
        name="Kiro User",
        otp_callback=lambda: "123456",
    )

    assert result == {
        "email": "user@example.com",
        "password": "Password123!",
        "name": "Kiro User",
    }
    assert worker.client.step12_called is False
    assert worker.client.step12f_called is False
    assert worker.client._signup_signin_state == "signup-signin-state"
    assert any("Step11 未完成" in log for log in worker.client.logs)


def test_kiro_protocol_worker_calls_step12_after_step11_success():
    worker = KiroProtocolMailboxWorker(proxy=None, tag="KIRO", log_fn=lambda msg: None)
    worker.client = DummySuccessfulClient()

    result = worker.run(
        email="user@example.com",
        password="Password123!",
        name="Kiro User",
        otp_callback=lambda: "123456",
    )

    assert result == {
        "email": "user@example.com",
        "password": "Password123!",
        "name": "Kiro User",
        "accessToken": "access-token",
        "sessionToken": "session-token",
    }
    assert worker.client.step12_called is True
    assert worker.client.step12f_called is True
    assert worker.client._signup_signin_state == "signup-signin-state"


def test_step2_get_wsh_accepts_direct_login_workflow_state_handle():
    reg = KiroRegister(proxy=None, tag="TEST")
    reg.log = lambda msg: None
    reg._capture_cookies = MagicMock()
    reg._setup_signin_js_cookies = MagicMock()
    reg.s = SimpleNamespace(
        get=MagicMock(side_effect=[
            SimpleNamespace(
                url="https://us-east-1.signin.aws/platform/d-9067642ac7/login?workflowStateHandle=test-wsh-123"
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"csrfToken": "csrf-123"},
            ),
        ])
    )

    ok = reg.step2_get_wsh("https://example.com/authorize")

    assert ok is True
    assert reg.wsh == "test-wsh-123"
    assert reg._login_wsh == "test-wsh-123"
    assert reg._portal_csrf_token == "csrf-123"
    reg._capture_cookies.assert_called_once()
    reg._setup_signin_js_cookies.assert_called_once()


def test_step12_prefers_step10_workflow_result_handle_with_signup_signin_state_first():
    reg = KiroRegister(proxy=None, tag="TEST")
    reg.log = lambda msg: None
    reg._portal_csrf_token = "csrf-123"
    reg._step10_workflow_result_handle = "wrh-step10"
    reg._step11_workflow_result_handle = "wrh-step11"
    reg._workflow_result_handle = "wrh-step11"
    reg._step10_state = "step10-state"
    reg._step11_state = "step11-state"
    reg._signup_signin_state = "signup-signin-state-used"

    attempts = []

    def fake_post(url, headers=None, data=None, json=None, cookies=None):
        if url.endswith("/auth/sso-token"):
            attempts.append(data)
            return SimpleNamespace(status_code=400, text='{"errorMessage":"Invalid State param"}')
        raise AssertionError(f"unexpected POST: {url}")

    reg.s = SimpleNamespace(post=fake_post)

    result = reg.step12_get_tokens()

    assert result is None
    assert len(attempts) >= 1
    assert "authCode=wrh-step10" in attempts[0]
    assert "state=signup-signin-state-used" in attempts[0]


def test_step12_falls_back_to_step11_pair_after_invalid_state():
    reg = KiroRegister(proxy=None, tag="TEST")
    reg.log = lambda msg: None
    reg._portal_csrf_token = "csrf-123"
    reg._step10_workflow_result_handle = "wrh-step10"
    reg._step11_workflow_result_handle = "wrh-step11"
    reg._workflow_result_handle = "wrh-step11"
    reg._step10_state = "step10-state"
    reg._step11_state = "step11-state"
    reg._signup_signin_state = "signup-signin-state-used"

    attempts = []

    def fake_post(url, headers=None, data=None, json=None, cookies=None):
        if url.endswith("/auth/sso-token"):
            attempts.append(data)
            if len(attempts) == 1:
                return SimpleNamespace(status_code=400, text='{"errorMessage":"Invalid State param"}')
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"token": "session-token", "redirectUrl": ""},
            )
        if url.endswith("/authentication_result"):
            return SimpleNamespace(status_code=400, text='{"message":"stop after fallback"}')
        raise AssertionError(f"unexpected POST: {url}")

    def fake_get(url, headers=None, allow_redirects=None):
        if url.endswith("/token/whoAmI"):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
        raise AssertionError(f"unexpected GET: {url}")

    reg.s = SimpleNamespace(post=fake_post, get=fake_get)

    result = reg.step12_get_tokens()

    assert result is None
    assert len(attempts) >= 2
    assert "authCode=wrh-step10" in attempts[0]
    assert "state=signup-signin-state-used" in attempts[0]
    assert "authCode=wrh-step10" in attempts[1] or "authCode=wrh-step11" in attempts[1]
